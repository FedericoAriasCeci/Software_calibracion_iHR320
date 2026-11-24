"""Metadatos y exportación de barridos: sin Tk ni hardware."""
import json
from pathlib import Path
import tempfile
import unittest

from spectral_calibration.acquisition import acquire
from spectral_calibration.horiba import ICLMonochromator
from spectral_calibration.instruments import ConnectionSettings, SR830Reader, SimulatedReader
from spectral_calibration.models import AcquisitionSettings
from spectral_calibration.slit_sweep import SimulatedSlitInstrument, new_sweep_path, run_slit_sweep
from spectral_calibration.sweep_export import (background_metadata, horiba_metadata, record_final_state, save_sweep,
                                               sr830_metadata)
from spectral_calibration.tests.test_horiba import FakeICL


class SweepExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.reader = SimulatedReader(noise_v=0)
        self.reader.connect()
        self.settings = AcquisitionSettings(n=2, interval_s=0, wait_s=0, channel=2)

    def tearDown(self):
        self.temp.cleanup()

    def sweep(self, notes="inicial"):
        path = new_sweep_path(self.folder / "slit_sweeps")
        instrument = SimulatedSlitInstrument(self.reader)
        meta = dict(notes=notes, horiba=horiba_metadata("SIM", instrument, instrument.snapshot()),
                    sr830=sr830_metadata(self.reader, self.settings))
        run_slit_sweep(instrument, self.reader, self.settings, [531.9, 532.], [(.1, .1)], path, metadata=meta)
        record_final_state(path, instrument.snapshot(), instrument.describe())
        instrument.close()
        return path

    def test_metadata_is_a_copy_of_icl_configuration_without_extra_commands(self):
        fake = FakeICL()
        mono = ICLMonochromator(socket_factory=lambda *a, **kw: fake)
        mono.read_gratings()
        mono.read_slits()
        sent = len(fake.commands)
        meta = horiba_metadata("ICL", mono, mono.last_snapshot)
        self.assertEqual(len(fake.commands), sent)
        self.assertEqual(meta["configuration"]["settings"]["timeout_s"], 8)
        self.assertEqual(meta["configuration"]["device"]["serialNumber"], "TEST")
        mono.slit_ports["slit_in_mm"]["label"] = "modificado"
        self.assertNotEqual(meta["configuration"]["slit_ports"]["slit_in_mm"]["label"], "modificado")
        mono.close()

    def test_sr830_metadata_records_connection_resource_and_reading(self):
        reader = SR830Reader(ConnectionSettings(interface="GPIB", gpib_address=8, timeout_ms=2500))
        meta = sr830_metadata(reader, self.settings)
        self.assertEqual(meta["visa_resource"], "GPIB0::8::INSTR")
        self.assertEqual(meta["connection"]["timeout_ms"], 2500)
        self.assertEqual(meta["acquisition"]["channel"], 2)
        self.assertEqual(meta["query"], "OAUX? 2")
        self.assertNotIn("visa_resource", sr830_metadata(self.reader, self.settings))

    def test_background_metadata_keeps_readings_times_and_states(self):
        self.reader.blocked = True
        batch = acquire(self.reader, self.settings)
        meta = background_metadata(batch, dict(grating="G1"), dict(grating="G1"))
        self.assertEqual(meta["values_v"], batch.values_v)
        self.assertEqual(len(meta["times_utc"]), 2)
        self.assertEqual((meta["n"], meta["channel"], meta["saturated"]), (2, 2, False))
        self.assertAlmostEqual(meta["mean_v"], self.reader.background_v)
        self.assertEqual(meta["horiba_state"], dict(grating="G1"))
        json.dumps(meta)

    def test_final_state_is_appended_and_never_raises(self):
        path = self.sweep()
        info = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(info["horiba"]["final_state"]["slit_in_mm"], .1)
        self.assertEqual(info["horiba"]["initial_state"]["grating"], "G1")
        self.assertEqual(info["state"], "completed")
        self.assertFalse(record_final_state(self.folder / "inexistente.csv", {}, {}))
        broken = self.folder / "roto.csv"
        broken.with_suffix(".json").write_text("{no es json", encoding="utf-8")
        self.assertFalse(record_final_state(broken, {}, {}))

    def test_save_copies_csv_and_writes_metadata_with_current_notes(self):
        path = self.sweep()
        target = self.folder / "exportado"
        result = save_sweep(path, target, "corregidas")
        csv_target = self.folder / "exportado.csv"
        self.assertEqual(result["csv"], str(csv_target))
        self.assertEqual(csv_target.read_bytes(), path.read_bytes())
        exported = json.loads(csv_target.with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(exported["notes"], "corregidas")
        self.assertEqual(exported["sr830"]["acquisition"]["n"], 2)
        self.assertEqual(exported["horiba"]["control"], "SIM")
        self.assertEqual(exported["backup_csv"], str(path))
        backup = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(backup["notes"], "corregidas")
        self.assertNotIn("saved_utc", backup)
        self.assertEqual(list(self.folder.glob("*.tmp")), [])

    def test_save_onto_backup_only_updates_its_json(self):
        path = self.sweep()
        before = path.read_bytes()
        save_sweep(path, path, "mismas")
        self.assertEqual(path.read_bytes(), before)
        info = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(info["notes"], "mismas")
        self.assertIn("saved_utc", info)

    def test_save_without_backup_csv_fails_without_writing(self):
        with self.assertRaisesRegex(ValueError, "respaldo"):
            save_sweep(self.folder / "falta.csv", self.folder / "destino.csv", "")
        self.assertEqual(list(self.folder.iterdir()), [])
