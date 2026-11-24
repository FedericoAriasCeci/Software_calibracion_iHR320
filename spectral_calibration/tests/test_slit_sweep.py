"""Barrido en apertura de rendijas: planes, CSV único, SIM e ICL con dobles."""
import csv
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from spectral_calibration.acquisition import AcquisitionCancelled
from spectral_calibration.horiba import HoribaError, ICLMonochromator, wavelength_plan
from spectral_calibration.instruments import SimulatedReader
from spectral_calibration.models import AcquisitionSettings
from spectral_calibration.slit_sweep import (CSV_FIELDS, SimulatedSlitInstrument, new_sweep_path,
                                             run_slit_sweep, slit_plan_from_lists, slit_plan_from_range)
from spectral_calibration.tests.test_horiba import FakeICL


def rows(path):
    with Path(path).open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


class PlanTests(unittest.TestCase):
    def test_range_equal_entrance_and_exit_includes_reachable_maximum(self):
        self.assertEqual(slit_plan_from_range(.1, .5, .1), [(.1, .1), (.2, .2), (.3, .3), (.4, .4), (.5, .5)])
        self.assertEqual(slit_plan_from_range("0,1", .35, .1)[-1], (.3, .3))
        self.assertEqual(slit_plan_from_range(.2, .2, .1), [(.2, .2)])

    def test_range_rejects_invalid(self):
        for args in ((.5, .1, .1), (0, .5, .1), (.1, 8, .1), (.1, .5, 0), (.1, .5, -.1), ("x", 1, .1)):
            with self.assertRaises(ValueError):
                slit_plan_from_range(*args)

    def test_lists_pair_in_order_and_broadcast_single_value(self):
        self.assertEqual(slit_plan_from_lists("0.1; 0.2\n0,5", "0.3 0.2 0.1"), [(.1, .3), (.2, .2), (.5, .1)])
        self.assertEqual(slit_plan_from_lists("1", "0.1;0.2"), [(1, .1), (1, .2)])
        self.assertEqual(slit_plan_from_lists("0.1;0.2", "1"), [(.1, 1), (.2, 1)])

    def test_lists_reject_mismatch_empty_zero_and_too_wide(self):
        for a, b in (("0.1;0.2", "0.1;0.2;0.3"), ("", "0.1"), ("0", "0.1"), ("7.5", "1"), ("a", "1")):
            with self.assertRaises(ValueError):
                slit_plan_from_lists(a, b)


class SweepTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.reader = SimulatedReader(noise_v=0, background_v=0)
        self.reader.connect()
        self.settings = AcquisitionSettings(n=3, interval_s=0, wait_s=0)
        self.path = new_sweep_path(Path(self.temp.name) / "slit_sweeps")

    def tearDown(self):
        self.temp.cleanup()

    def test_simulated_sweep_writes_single_csv_with_slit_columns(self):
        wavelengths = wavelength_plan(531, 533, .1)
        pairs = slit_plan_from_lists("0.1;0.5", "0.1;0.2")
        instrument = SimulatedSlitInstrument(self.reader)
        points = []
        result = run_slit_sweep(instrument, self.reader, self.settings, wavelengths, pairs, self.path,
                                on_point=points.append)
        instrument.close()
        self.assertEqual(len(points), len(wavelengths)*2)
        self.assertEqual((points[-1]["config_index"], points[-1]["slit_in_mm"], points[-1]["slit_out_mm"]), (1, .5, .2))
        self.assertEqual(self.reader.fwhm_nm, .4)
        data = rows(self.path)
        self.assertEqual(list(data[0]), CSV_FIELDS)
        self.assertEqual(len(data), len(wavelengths)*2)
        self.assertEqual(result["points"], len(data))
        self.assertEqual({(r["slit_in_mm"], r["slit_out_mm"]) for r in data[:len(wavelengths)]}, {("0.1", "0.1")})
        self.assertEqual({(r["slit_in_mm"], r["slit_out_mm"]) for r in data[len(wavelengths):]}, {("0.5", "0.2")})
        self.assertEqual([float(r["wavelength_target_nm"]) for r in data[:len(wavelengths)]], wavelengths)
        self.assertEqual(len(data[0]["readings_v"].split(";")), 3)
        # Mayor apertura → perfil más ancho: más puntos sobre la mitad del máximo.
        def above_half(part):
            values = [float(r["mean_v"]) for r in part]
            return sum(v > max(values)/2 for v in values)
        self.assertGreater(above_half(data[len(wavelengths):]), above_half(data[:len(wavelengths)]))
        info = json.loads(self.path.with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(info["state"], "completed")
        self.assertEqual(info["points_written"], len(data))
        self.assertEqual(len(info["configurations"]), 2)

    def test_cancel_keeps_complete_rows_and_records_state(self):
        cancel = threading.Event()
        written = []
        def progress(i, total, value):
            if i == total:
                written.append(1)
                if len(written) == 4:
                    cancel.set()
        instrument = SimulatedSlitInstrument(self.reader)
        with self.assertRaises(AcquisitionCancelled):
            run_slit_sweep(instrument, self.reader, self.settings, [531, 532, 533], [(.1, .1), (.2, .2)],
                           self.path, cancel, progress)
        data = rows(self.path)
        self.assertEqual(len(data), 3)
        info = json.loads(self.path.with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(info["state"], "cancelled")
        self.assertEqual(info["points_written"], 3)

    def test_never_overwrites_existing_file(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("previo", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            run_slit_sweep(SimulatedSlitInstrument(self.reader), self.reader, self.settings, [532], [(.1, .1)], self.path)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "previo")


class ICLSweepTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.fake = FakeICL()
        self.mono = ICLMonochromator(socket_factory=lambda *a, **kw: self.fake)
        self.reader = SimulatedReader(noise_v=0)
        self.reader.connect()
        self.settings = AcquisitionSettings(n=2, interval_s=0, wait_s=0)
        self.path = new_sweep_path(self.temp.name)

    def tearDown(self):
        self.mono.close()
        self.temp.cleanup()

    def test_moves_slits_per_configuration_then_wavelengths_and_records_readback(self):
        self.mono.read_gratings()
        self.mono.read_output()
        run_slit_sweep(self.mono, self.reader, self.settings, [531.9, 532.0], [(.2, .3), (.4, .4)],
                       self.path, tolerance_nm=self.mono.settings.tolerance_nm)
        commands = [c["command"] for c in self.fake.commands]
        slit_moves = [c["parameters"] for c in self.fake.commands if c["command"] == "mono_moveSlitMM"]
        self.assertEqual(slit_moves, [dict(index=0, locationId=1, position=.2), dict(index=0, locationId=4, position=.3),
                                      dict(index=0, locationId=1, position=.4), dict(index=0, locationId=4, position=.4)])
        moves = [c["parameters"]["wavelength"] for c in self.fake.commands if c["command"] == "mono_moveToPosition"]
        self.assertEqual(moves, [531.9, 532.0, 531.9, 532.0])
        # Rendijas antes que λ de su configuración.
        self.assertLess(commands.index("mono_moveSlitMM"), commands.index("mono_moveToPosition"))
        self.assertNotIn("mono_setPosition", commands)
        self.assertNotIn("mono_init", commands)
        data = rows(self.path)
        self.assertEqual([(r["slit_in_mm"], r["slit_out_mm"], r["slit_in_read_mm"], r["slit_out_read_mm"]) for r in data],
                         [("0.2", "0.3", "0.2", "0.3")]*2 + [("0.4", "0.4", "0.4", "0.4")]*2)
        self.assertEqual({r["grating"] for r in data}, {"G1"})
        self.assertEqual({r["output_port"] for r in data}, {"Lateral"})

    def test_slit_failure_stops_without_retry_and_keeps_rows(self):
        self.fake.ignore_slit_move = True
        with self.assertRaises(HoribaError):
            run_slit_sweep(self.mono, self.reader, self.settings, [532.], [(.2, .2)], self.path)
        self.assertEqual(len([c for c in self.fake.commands if c["command"] == "mono_moveSlitMM"]), 1)
        self.assertEqual(rows(self.path), [])
        info = json.loads(self.path.with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(info["state"], "error")

