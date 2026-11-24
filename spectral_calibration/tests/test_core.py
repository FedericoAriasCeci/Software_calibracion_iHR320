"""Pruebas sin hardware. Ejecutar desde la raíz con unittest discover."""
import copy
import csv
from dataclasses import asdict
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import numpy as np
from spectral_calibration.acquisition import acquire, AcquisitionCancelled, Worker
from spectral_calibration.analysis import analyze, fit_slits, profile_arrays, slit_series
from spectral_calibration.instruments import ConnectionSettings, CommunicationError, SimulatedReader, SR830Reader
from spectral_calibration.models import AcquisitionSettings, Batch, Experiment, Scan, number
from spectral_calibration.storage import ExportError, SessionStore
from spectral_calibration.plotting import draw_profiles, draw_slits, export_plots
from matplotlib.figure import Figure


def batch(value, std=0):
    return Batch([value-std,value,value+std],["2026-11-02T00:00:00+00:00"]*3,[0,.1,.2],
                 asdict(AcquisitionSettings(n=3,interval_s=.1,wait_s=0)),"start","finish")


def triangular(width=.4, noise=0, reverse=False):
    reader = SimulatedReader(fwhm_nm=width,noise_v=noise)
    reader.connect()
    scan = Scan(Experiment(),reader.describe())
    scan.set_background(batch(.03))
    xx = np.linspace(532-width*1.5,532+width*1.5,61)
    for x in xx[::-1] if reverse else xx:
        reader.wavelength_nm = float(x)
        scan.add_point(float(x),acquire(reader,AcquisitionSettings(n=20,interval_s=0,wait_s=0)))
    return scan


class ModelsTests(unittest.TestCase):
    def test_statistics_are_volts_and_sample_std(self):
        b = batch(2,.1)
        self.assertAlmostEqual(b.mean_v,2)
        self.assertAlmostEqual(b.std_v,.1)

    def test_validation(self):
        for value in ("nan","inf","texto"):
            with self.assertRaises(ValueError):
                number(value,"test")
        self.assertEqual(number("0,1","test"),.1)
        for settings in (AcquisitionSettings(n=1),AcquisitionSettings(n=2.5),AcquisitionSettings(channel=5),AcquisitionSettings(interval_s=-1)):
            with self.assertRaises(ValueError):
                settings.validate()
        with self.assertRaises(ValueError):
            Experiment(slit_in_mm=0).validate()

    def test_incomplete_and_nonfinite_batch(self):
        with self.assertRaises(ValueError):
            Batch([1],["t"],[0],{"n":2},"t","t")
        with self.assertRaises(ValueError):
            batch(float("nan"))

    def test_repeat_and_delete_preserve_history(self):
        scan = Scan(Experiment(),{})
        scan.add_point(532,batch(1))
        scan.add_point(532,batch(2),True)
        self.assertEqual(len(scan.points),2)
        self.assertEqual(len(scan.active_points),1)
        self.assertEqual(scan.active_points[0].replaces_id,scan.points[0].id)
        scan.delete_last()
        self.assertEqual(len(scan.points),2)
        self.assertEqual(len(scan.active_points),0)

    def test_completed_scan_and_invalid_repeat(self):
        scan = Scan(Experiment(),{})
        with self.assertRaises(ValueError):
            scan.add_point(532,batch(1),True)
        scan.add_point(532,batch(1))
        with self.assertRaises(ValueError):
            scan.add_point(533,batch(1),True)
        scan.completed = True
        with self.assertRaises(ValueError):
            scan.add_point(532,batch(1))


class FakeResource:
    def __init__(self):
        self.commands,self.closed,self.fail = [],False,False
        self.identity = "Stanford_Research_Systems,SR830,123,1.0"

    def write(self,command):
        self.commands.append(command)

    def query(self,command):
        self.commands.append(command)
        if self.fail:
            raise TimeoutError("timeout simulado")
        return self.identity if command=="*IDN?" else "0.0123"

    def close(self):
        self.closed = True


class FakeManager:
    def __init__(self,resource):
        self.resource,self.closed = resource,False

    def open_resource(self,name,**kwargs):
        self.name,self.kwargs = name,kwargs
        return self.resource

    def close(self):
        self.closed = True


class InstrumentTests(unittest.TestCase):
    def reader(self,interface):
        r = FakeResource()
        m = FakeManager(r)
        reader = SR830Reader(ConnectionSettings(interface=interface,com_port="COM12"),lambda:m)
        return reader,r,m

    def test_rs232_protocol_and_only_outx_written(self):
        from pyvisa import constants
        reader,r,m = self.reader("RS232")
        reader.connect()
        self.assertEqual(m.name,"ASRL12::INSTR")
        self.assertEqual(r.read_termination,"\r")
        self.assertEqual(r.write_termination,"\r")
        self.assertEqual(r.data_bits,8)
        self.assertEqual(r.flow_control,constants.VI_ASRL_FLOW_NONE)
        self.assertAlmostEqual(reader.read_voltage(),.0123)
        self.assertEqual(r.commands,["OUTX 0","*IDN?","OAUX? 1"])
        reader.close()
        self.assertTrue(r.closed and m.closed)

    def test_gpib_protocol(self):
        reader,r,m = self.reader("GPIB")
        reader.connect()
        self.assertEqual(r.read_termination,"\n")
        self.assertEqual(r.write_termination,"\n")
        self.assertEqual(r.commands,["OUTX 1","*IDN?"])
        self.assertEqual(m.kwargs["open_timeout"],3000)
        reader.close()

    def test_timeout_disconnects_without_retries(self):
        reader,r,m = self.reader("GPIB")
        reader.connect()
        r.fail = True
        with self.assertRaises(CommunicationError):
            reader.read_voltage()
        self.assertTrue(r.closed and m.closed)
        self.assertIsNone(reader.resource)
        self.assertEqual(r.commands.count("OAUX? 1"),1)

    def test_wrong_identity_closes_resource(self):
        reader,r,m = self.reader("GPIB")
        r.identity = "OTRO,SR850,123"
        with self.assertRaises(CommunicationError):
            reader.connect()
        self.assertTrue(r.closed and m.closed)

    def test_open_failure_closes_manager(self):
        reader,r,m = self.reader("GPIB")
        m.open_resource = mock.Mock(side_effect=OSError("desconectado"))
        with self.assertRaises(CommunicationError):
            reader.connect()
        self.assertTrue(m.closed)

    def test_connection_validation(self):
        with self.assertRaises(ValueError):
            ConnectionSettings(interface="GPIB",resource="ASRL1::INSTR").resource_name()
        with self.assertRaises(ValueError):
            ConnectionSettings(baud_rate=1234).validate()
        with self.assertRaises(ValueError):
            ConnectionSettings(interface="RS232",com_port="bad").resource_name()

    def test_simulator_seed_width_and_background(self):
        a,b = SimulatedReader(noise_v=0),SimulatedReader(noise_v=0)
        a.connect(); b.connect()
        self.assertEqual(a.read_voltage(),b.read_voltage())
        a.wavelength_nm = 532.2
        self.assertAlmostEqual(a.read_voltage()-.03,.5)
        a.blocked = True
        self.assertEqual(a.read_voltage(),.03)


class AcquisitionTests(unittest.TestCase):
    def test_cancel_before_start(self):
        event = threading.Event(); event.set()
        reader = mock.Mock()
        with self.assertRaises(AcquisitionCancelled):
            acquire(reader,AcquisitionSettings(wait_s=1),event)
        reader.read_voltage.assert_not_called()

    def test_error_discards_partial_batch(self):
        reader = mock.Mock()
        reader.read_voltage.side_effect = [1,CommunicationError("cable")]
        with self.assertRaises(CommunicationError):
            acquire(reader,AcquisitionSettings(n=3,interval_s=0,wait_s=0))

    def test_worker_remains_async_and_serializes_jobs(self):
        worker = Worker()
        gate = threading.Event()
        start = time.monotonic()
        worker.submit("slow",lambda:gate.wait(1))
        self.assertLess(time.monotonic()-start,.5)
        with self.assertRaises(RuntimeError):
            worker.submit("second",lambda:None)
        gate.set(); worker.thread.join(2)
        self.assertEqual(worker.events.get_nowait(),("done","slow",True))

    def test_progress_samples_and_actual_times(self):
        reader = SimulatedReader(); reader.connect()
        progress = []
        b = acquire(reader,AcquisitionSettings(n=3,interval_s=.001,wait_s=.001),progress=lambda *v:progress.append(v))
        self.assertEqual(len(progress),3)
        self.assertEqual(b.settings["channel"],1)
        self.assertTrue(all(b.elapsed_s[i]<b.elapsed_s[i+1] for i in range(2)))


class AnalysisTests(unittest.TestCase):
    def test_known_triangular_width(self):
        a = analyze(triangular())
        self.assertAlmostEqual(a["fwhm_nm"],.4,places=9)
        self.assertAlmostEqual(a["asymmetry"],0,places=9)
        self.assertGreater(a["sampling_component_nm"],0)

    def test_noisy_width_and_reproducibility(self):
        scan = triangular(noise=.004)
        first,second = analyze(scan),analyze(Scan.from_dict(scan.to_dict()))
        self.assertEqual(first,second)
        self.assertLess(abs(first["fwhm_nm"]-.4),.015)
        self.assertGreater(first["mc_std_nm"],0)

    def test_reverse_scan(self):
        self.assertAlmostEqual(analyze(triangular(reverse=True))["fwhm_nm"],.4,places=9)

    def test_gaussian_without_fitting(self):
        scan = Scan(Experiment(),{})
        scan.set_background(batch(.1))
        for x in np.linspace(-2,2,101):
            scan.add_point(float(532+x),batch(.1+math.exp(-4*math.log(2)*(x/.8)**2)))
        self.assertAlmostEqual(analyze(scan)["fwhm_nm"],.8,places=8)

    def test_incomplete_wings_and_insufficient_points(self):
        scan = triangular()
        scan.points = [p for p in scan.points if p.wavelength_nm<=532.05]
        a = analyze(scan)
        self.assertIsNone(a["fwhm_nm"])
        self.assertTrue(any("cruces" in w for w in a["warnings"]))
        scan.points = scan.points[:2]
        self.assertIsNone(analyze(scan)["fwhm_nm"])

    def test_duplicates_are_grouped(self):
        scan = triangular()
        p = scan.active_points[5]
        scan.add_point(p.wavelength_nm,p.batch)
        a = analyze(scan)
        self.assertEqual(a["unique_points"],61)
        self.assertTrue(any("repetidas" in w for w in a["warnings"]))

    def test_background_reversible(self):
        scan = triangular()
        raw = [p.batch.mean_v for p in scan.points]
        corrected = analyze(scan)["fwhm_nm"]
        scan.use_background = False
        self.assertGreater(analyze(scan)["fwhm_nm"],corrected)
        self.assertEqual(raw,[p.batch.mean_v for p in scan.points])
        scan.use_background = True
        self.assertEqual(analyze(scan)["fwhm_nm"],corrected)

    def test_warning_saturation_background_coarse(self):
        scan = Scan(Experiment(),{})
        scan.set_background(batch(4))
        for x,y in [(531,4),(531.5,5),(532,10.5),(532.5,5),(533,4)]:
            scan.add_point(x,batch(y))
        text = " ".join(analyze(scan)["warnings"])
        for keyword in ("saturación","Fondo grande","Muestreo grueso","Pocos puntos"):
            self.assertIn(keyword,text)

    def test_multiple_peaks_and_nonpositive_profile(self):
        scan = Scan(Experiment(),{})
        scan.set_background(batch(0))
        for i,y in enumerate([0,0,1,0,0,.8,0,0,0]):
            scan.add_point(530+i*.1,batch(y))
        self.assertTrue(any("Múltiples" in w for w in analyze(scan)["warnings"]))
        scan.set_background(batch(2))
        self.assertIsNone(analyze(scan)["fwhm_nm"])

    def test_multiple_apertures_and_empirical_fit(self):
        scans = []
        for w in [.1,.2,.5]:
            s = triangular(width=math.sqrt(.08**2+(1.8*w)**2))
            s.experiment.slit_in_mm=s.experiment.slit_out_mm=w
            s.completed,s.analysis=True,analyze(s)
            scans.append(s)
        rows = slit_series(scans)[("G1",532.)]
        fit,_ = fit_slits(rows)
        self.assertEqual(len(rows),3)
        self.assertAlmostEqual(fit["delta0_nm"],.08,places=5)
        self.assertAlmostEqual(fit["a_nm_per_mm"],1.8,places=5)
        scans[0].experiment.laser_nominal_nm=633
        self.assertEqual(len(slit_series(scans)),2)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = SessionStore.create(self.temp.name)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_each_point_and_recovery_reproducibility(self):
        s = triangular(noise=.002)
        s.completed,s.analysis=True,analyze(s)
        self.store.add_scan(s)
        path = self.store.path
        expected = self.store.get(s.id).to_dict()
        self.store.close()
        self.store = SessionStore(path)
        loaded = self.store.get(s.id)
        self.assertEqual(loaded.to_dict(),expected)
        self.assertEqual(analyze(loaded),loaded.analysis)
        with (path/(self.store.stem(s)+".csv")).open() as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows),61)
        self.assertIn("slit_out_mm",rows[0])
        with (path/(self.store.stem(s)+"_readings.csv")).open() as stream:
            self.assertEqual(len(list(csv.DictReader(stream))),61*20)

    def test_hard_interruption_keeps_points_and_releases_lock(self):
        self.store.close()
        script = "from spectral_calibration.storage import SessionStore; from spectral_calibration.models import Experiment,Scan; from spectral_calibration.tests.test_core import batch; import os,sys; st=SessionStore(sys.argv[1]); s=Scan(Experiment(),{}); st.add_scan(s); st.update_scan(s.id,lambda s:s.add_point(532,batch(1))); os._exit(0)"
        subprocess.run([sys.executable,"-B","-c",script,str(self.store.path)],check=True)
        self.store = SessionStore(self.store.path)
        self.assertEqual(len(self.store.scans[0].active_points),1)
        self.assertFalse(self.store.scans[0].completed)
        self.store.update_scan(self.store.scans[0].id,lambda s:s.add_point(532.1,batch(.5)))
        self.assertEqual(len(self.store.scans[0].active_points),2)

    def test_snapshot_failure_does_not_change_memory_or_disk(self):
        with mock.patch("spectral_calibration.storage.atomic_text",side_effect=OSError("disco lleno")):
            with self.assertRaises(OSError):
                self.store.add_scan(Scan(Experiment(),{}))
        self.assertEqual(self.store.scans,[])
        self.assertEqual(json.loads((self.store.path/"session.json").read_text())["scans"],[])

    def test_csv_failure_is_recoverable_from_json(self):
        s = Scan(Experiment(),{})
        with mock.patch.object(self.store,"export_tables",side_effect=OSError("Excel bloquea CSV")):
            with self.assertRaises(ExportError):
                self.store.add_scan(s)
        self.assertEqual(self.store.get(s.id).id,s.id)
        path = self.store.path
        self.store.close()
        self.store = SessionStore(path)
        self.assertTrue((path/"metadata.json").exists())

    def test_lock_prevents_two_writers(self):
        with self.assertRaises(RuntimeError):
            SessionStore(self.store.path)

    def test_unique_sessions_and_scan_filenames(self):
        other = SessionStore.create(self.temp.name)
        self.assertNotEqual(self.store.path,other.path)
        other.close()
        a,b = Scan(Experiment(),{}),Scan(Experiment(),{})
        self.assertNotEqual(self.store.stem(a),self.store.stem(b))

    def test_plot_exports_and_series(self):
        scan = triangular()
        scan.completed,scan.analysis=True,analyze(scan)
        self.store.add_scan(scan)
        paths = export_plots(self.store)
        self.assertEqual(len(paths),2)
        self.assertTrue(all(p.stat().st_size>1000 for p in paths))
        fig = Figure()
        ax = fig.add_subplot(111)
        draw_profiles(ax,[scan],normalize=True,raw_also=True)
        self.assertIn("máximo",ax.get_ylabel())
        draw_slits(ax,[scan])
        self.assertEqual(ax.get_ylabel(),"FWHM [nm]")


if __name__=="__main__":
    unittest.main()

