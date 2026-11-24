"""Pruebas Tk optativas: CALIBRATION_GUI_TESTS=1. No usan hardware."""
import os
import gc
import csv
import json
from pathlib import Path
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest import mock

from spectral_calibration.gui import CalibrationApp
from spectral_calibration.instruments import SimulatedReader, CommunicationError
from spectral_calibration.horiba import ICLMonochromator
from spectral_calibration.models import Scan
from spectral_calibration.tests.test_horiba import FakeICL


@unittest.skipUnless(os.environ.get("CALIBRATION_GUI_TESTS")=="1","Active CALIBRATION_GUI_TESTS=1 para probar Tk")
class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.master = tk.Tk()
        cls.master.withdraw()

    @classmethod
    def tearDownClass(cls):
        gc.collect()
        cls.master.destroy()
        cls.master = None
        gc.collect()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = tk.Toplevel(self.master)
        self.root.withdraw()
        self.app = CalibrationApp(self.root,self.temp.name)
        self.dialogs = []
        self.patches = [mock.patch("spectral_calibration.gui.messagebox."+name,return_value=True)
                        for name in ("askyesno","askokcancel")]
        self.patches += [mock.patch("spectral_calibration.gui.messagebox.showerror",side_effect=lambda *a,**k:self.dialogs.append(a)),
                         mock.patch("spectral_calibration.gui.messagebox.showwarning")]
        for patch in self.patches:
            patch.start()
        self.reader = SimulatedReader(noise_v=.001)
        self.reader.connect()
        self.app.reader,self.app.connected=self.reader,True
        for key,value in {"n":4,"interval_s":0,"wait_s":0}.items():
            self.app.vars[key].set(value)

    def tearDown(self):
        if self.app.awaiting:
            self.app.worker.cancel.set()
            self.drain()
        if self.app.store:
            self.app.store.close()
        if self.app.worker.thread:
            self.app.worker.thread.join(timeout=2)
        self.root.update_idletasks()
        self.app._destroy()
        self.app, self.root = None, None
        # Los ciclos Tk/Matplotlib se recogen en el hilo principal, antes de
        # que el siguiente caso arranque un worker instrumental.
        for patch in reversed(self.patches):
            patch.stop()
        self.patches = []
        gc.collect()
        self.temp.cleanup()

    def drain(self,timeout=20):
        end = time.monotonic()+timeout
        while self.app.awaiting and time.monotonic()<end:
            self.root.update()
            time.sleep(.01)
        self.root.update()
        self.assertFalse(self.app.awaiting,"Operación no terminó")

    def point(self,wavelength):
        self.app.vars["wavelength_nm"].set(wavelength)
        self.app.start_acquire("point")
        self.drain()

    def test_full_scan_background_finish_export_and_recovery(self):
        self.app.start_acquire("background"); self.drain()
        for nm in [531.4,531.6,531.8,532,532.2,532.4,532.6]:
            self.point(nm)
        self.app.finish(); self.drain()
        self.assertTrue(self.app.scan.completed)
        self.assertAlmostEqual(self.app.scan.analysis["fwhm_nm"],.4,delta=.01)
        path = self.app.store.path
        self.assertTrue(list((path/"plots").glob("*.png")))
        self.assertEqual(self.dialogs,[])
        self.app.store.close()
        self.app.store,self.app.scan_id=None,None
        self.app.open_session(path)
        self.assertEqual(len(self.app.scan.active_points),7)

    def test_invalid_input_repeated_wavelength_and_replace(self):
        self.app.vars["n"].set("bad")
        with self.assertRaises(ValueError):
            self.app.start_acquire("point")
        self.app.vars["n"].set(4)
        self.point(532)
        self.point(532)
        self.assertEqual(len(self.app.scan.active_points),2)
        self.app.start_acquire("repeat"); self.drain()
        self.assertEqual(len(self.app.scan.active_points),2)
        self.assertEqual(len(self.app.scan.points),3)
        self.app.delete_last()
        self.assertEqual(len(self.app.scan.active_points),1)
        self.app.finish(); self.drain()
        self.assertIsNone(self.app.scan.analysis["fwhm_nm"])

    def test_cancel_during_wait_keeps_gui_responsive(self):
        self.app.vars["wait_s"].set(10)
        self.app.start_acquire("point")
        seen = []
        self.root.after(10,lambda:seen.append(True))
        self.root.after(40,self.app.worker.cancel.set)
        self.drain()
        self.assertTrue(seen)
        self.assertEqual(len(self.app.scan.points),0)

    def test_disconnect_and_negative_step(self):
        self.app.vars["step_nm"].set(-.1)
        self.point(532)
        self.assertAlmostEqual(float(self.app.vars["wavelength_nm"].get()),531.9)
        self.reader.read_voltage=mock.Mock(side_effect=CommunicationError("desconexión simulada"))
        self.app.start_acquire("point"); self.drain()
        self.assertFalse(self.app.connected)
        self.assertEqual(len(self.app.scan.points),1)
        self.assertTrue(self.dialogs)

    def test_channel_cannot_change_within_scan(self):
        self.point(532)
        self.app.vars["channel"].set(2)
        with self.assertRaises(ValueError):
            self.app.start_acquire("point")

    def test_failed_save_can_be_retried_without_rereading(self):
        self.app.ensure_scan()
        original = self.app.store._write_snapshot
        with mock.patch.object(self.app.store,"_write_snapshot",side_effect=OSError("disco lleno")):
            self.point(532)
        self.assertIsNotNone(self.app.unsaved)
        self.assertEqual(len(self.app.scan.points),0)
        self.app.save_pending(); self.drain()
        self.assertIsNone(self.app.unsaved)
        self.assertEqual(len(self.app.scan.points),1)

    def test_storage_does_not_block_tk_events(self):
        self.app.ensure_scan()
        original = self.app.store._write_snapshot
        def slow(scans):
            time.sleep(.15)
            return original(scans)
        seen = []
        with mock.patch.object(self.app.store,"_write_snapshot",side_effect=slow):
            self.root.after(50,lambda:seen.append(self.app.awaiting))
            self.point(532)
        self.assertEqual(seen,[True])

    def automatic(self):
        for key, value in {"auto_start": 531.4, "auto_end": 532.6, "auto_step": .2}.items():
            self.app.vars[key].set(value)
        self.app.start_automatic()

    def test_automatic_scan_saves_readback_and_analyzes(self):
        self.app.start_acquire("background"); self.drain()
        self.automatic(); self.drain()
        self.assertEqual(self.dialogs, [])
        self.assertTrue(self.app.scan.completed)
        self.assertEqual(len(self.app.scan.points), 7)
        self.assertAlmostEqual(self.app.scan.analysis["fwhm_nm"], .4, delta=.01)
        self.assertEqual(self.app.scan.horiba["interface"], "SIM")
        for point in self.app.scan.points:
            self.assertEqual(point.wavelength_nm, point.instrument["wavelength_nm"])
            self.assertIn("target_nm", point.instrument)
        restored = Scan.from_dict(self.app.scan.to_dict())
        self.assertEqual(restored.to_dict(), self.app.scan.to_dict())

    def icl_fixture(self):
        # Doble explícito del SR830: nunca se abre VISA ni un equipo real.
        reader = self.reader
        class ReaderDouble:
            def describe(self):
                return {"interface": "TEST", "identity": "SR830_DOUBLE"}
            def read_voltage(self, channel):
                return reader.read_voltage(channel)
            def close(self):
                reader.close()
        self.app.reader = ReaderDouble()
        fake = FakeICL()
        class ControllerDouble(ICLMonochromator):
            def __init__(self, settings):
                super().__init__(settings, socket_factory=lambda *a, **kw: fake)
        self.app.vars["horiba_mode"].set("ICL")
        self.app.vars["auto_start"].set(531.9)
        self.app.vars["auto_end"].set(532.1)
        self.app.vars["auto_step"].set(.1)
        return fake, mock.patch("spectral_calibration.gui.ICLMonochromator", ControllerDouble)

    def test_icl_pipeline_saves_measured_position_and_device_identity(self):
        fake, patch = self.icl_fixture()
        fake.offset = .001
        with patch:
            self.app.start_automatic(); self.drain()
        self.assertEqual(self.dialogs, [])
        self.assertTrue(self.app.scan.completed)
        self.assertEqual(self.app.scan.horiba["device"]["serialNumber"], "TEST")
        self.assertEqual(len(self.app.scan.points), 3)
        for point in self.app.scan.points:
            self.assertAlmostEqual(point.wavelength_nm-point.instrument["target_nm"], .001)
        self.assertEqual(sum(c["command"] == "mono_moveToPosition" for c in fake.commands), 3)
        self.assertTrue(fake.closed)

    def test_initialize_horiba_uses_worker_and_does_not_repeat_homing(self):
        fake, patch = self.icl_fixture()
        fake.initialized = False
        original = fake.recv
        release = threading.Event()
        def recv():
            if fake.pending["command"] == "mono_init":
                if not release.wait(3):
                    raise TimeoutError("El callback Tk no liberó la respuesta de prueba")
            return original()
        fake.recv = recv
        ticks = []
        declared_wavelength = self.app.vars["wavelength_nm"].get()
        def tick():
            ticks.append((self.app.awaiting, self.app.horiba_status_label.cget("text")))
            release.set()
        with patch:
            self.app.initialize_horiba()
            self.root.after(80, tick)
            with self.assertRaises(ValueError):
                self.app.initialize_horiba()
            self.drain()
            self.assertTrue(ticks[0][0])
            self.assertIn("Inicializando", ticks[0][1])
            self.assertIn("Listo", self.app.horiba_status_label.cget("text"))
            controller = self.app.horiba_controller
            self.app.initialize_horiba(); self.drain()
            self.assertIs(self.app.horiba_controller, controller)
        self.assertEqual(sum(c["command"] == "mono_init" for c in fake.commands), 1)
        self.assertEqual(self.dialogs, [])
        # Inicializar no modifica el montaje declarado ni crea un barrido.
        self.assertIsNone(self.app.scan)
        self.assertEqual(self.app.vars["wavelength_nm"].get(), declared_wavelength)

    def test_initialization_error_is_visible_and_can_reconnect(self):
        fake, patch = self.icl_fixture()
        fake.initialized, fake.init_succeeds = False, False
        with patch:
            self.app.initialize_horiba(); self.drain()
            self.assertIn("Error", self.app.horiba_status_label.cget("text"))
            fake.initialized = True
            self.app.check_horiba(); self.drain()
            self.assertIn("Listo", self.app.horiba_status_label.cget("text"))
        self.assertEqual(len(self.dialogs), 1)
        self.assertEqual(sum(c["command"] == "mono_init" for c in fake.commands), 1)

    def test_status_heartbeat_detects_loss_without_sending_homing(self):
        fake, patch = self.icl_fixture()
        with patch:
            self.app.check_horiba(); self.drain()
            fake.recv = mock.Mock(side_effect=OSError("USB desconectado"))
            self.app.horiba_check_at = 0
            # Espera el poll Tk programado, que debe lanzar la consulta de fondo.
            self.root.after(70, lambda: None)
            end = time.monotonic()+2
            while self.app.horiba_controller.status.state != "error" and time.monotonic()<end:
                self.root.update()
                time.sleep(.01)
            self.drain()
        self.assertIn("Error", self.app.horiba_status_label.cget("text"))
        self.assertNotIn("mono_init", [c["command"] for c in fake.commands])
        self.assertTrue(fake.closed)

    def test_cancel_initialization_keeps_tk_responsive_and_reports_uncertainty(self):
        fake, patch = self.icl_fixture()
        fake.initialized, fake.init_busy_polls = False, 1000
        with patch:
            self.app.initialize_horiba()
            self.root.after(120, self.app.cancel_acquisition)
            self.drain()
        self.assertIn("Error", self.app.horiba_status_label.cget("text"))
        self.assertIn("homing puede continuar", self.app.horiba_status_label.cget("text"))
        self.assertEqual(self.dialogs, [])
        self.assertEqual(sum(c["command"] == "mono_init" for c in fake.commands), 1)

    def test_initialization_session_is_reused_for_mount_and_acquisition(self):
        fake, patch = self.icl_fixture()
        fake.initialized = False
        with patch:
            self.app.initialize_horiba(); self.drain()
            controller = self.app.horiba_controller
            self.app.read_horiba_mount(); self.drain()
            self.app.start_automatic(); self.drain()
        self.assertIs(self.app.horiba_controller, controller)
        self.assertEqual(sum(c["command"] == "mono_open" for c in fake.commands), 1)
        self.assertEqual(sum(c["command"] == "mono_init" for c in fake.commands), 1)
        self.assertTrue(self.app.scan.completed)
        self.assertTrue(self.app.scan.horiba["initialization"]["initialization_sent"])
        self.assertEqual(self.dialogs, [])

    def test_changed_settings_close_old_session_before_reopening(self):
        fake, patch = self.icl_fixture()
        with patch:
            self.app.check_horiba(); self.drain()
            previous = self.app.horiba_controller
            self.app.vars["horiba_timeout_s"].set(7)
            self.app.check_horiba(); self.drain()
            self.assertIsNot(self.app.horiba_controller, previous)
            self.assertEqual(previous.status.state, "disconnected")
            self.app.disconnect_horiba(); self.drain()
        self.assertIn("Desconectado", self.app.horiba_status_label.cget("text"))
        commands = [c["command"] for c in fake.commands]
        self.assertEqual(commands.count("mono_open"), 2)
        self.assertEqual(commands.count("mono_close"), 2)
        self.assertNotIn("mono_init", commands)

    def test_invalid_scan_does_not_replace_or_leak_initialization_session(self):
        fake, patch = self.icl_fixture()
        with patch:
            self.app.initialize_horiba(); self.drain()
            previous = self.app.horiba_controller
            self.point(532.)
            self.app.vars["horiba_timeout_s"].set(7)
            with self.assertRaisesRegex(ValueError, "sin puntos"):
                self.app.start_automatic()
            self.assertIs(self.app.horiba_controller, previous)
            self.assertTrue(previous.opened)

    def test_real_initialization_requires_icl_mode(self):
        with mock.patch("spectral_calibration.gui.ICLMonochromator") as controller:
            with self.assertRaisesRegex(ValueError, "ICL"):
                self.app.initialize_horiba()
            controller.assert_not_called()

    def test_window_close_releases_icl_session_outside_tk_thread(self):
        fake, patch = self.icl_fixture()
        close_threads = []
        original = fake.recv
        def recv():
            if fake.pending["command"] == "mono_close":
                close_threads.append(threading.current_thread())
                time.sleep(.05)
            return original()
        fake.recv = recv
        with patch:
            self.app.check_horiba(); self.drain()
            with mock.patch.object(self.app, "_destroy") as destroy:
                self.app.close()
                self.assertTrue(self.app.awaiting)
                self.drain()
                destroy.assert_called_once_with()
        self.assertTrue(fake.closed)
        self.assertEqual(len(close_threads), 1)
        self.assertIsNot(close_threads[0], threading.current_thread())

    def test_icl_mount_mismatch_stops_before_first_move(self):
        fake, patch = self.icl_fixture()
        fake.grating = 1
        with patch:
            self.app.start_automatic(); self.drain()
        self.assertEqual(len(self.app.scan.points), 0)
        self.assertNotIn("mono_moveToPosition", [c["command"] for c in fake.commands])
        self.assertIsNone(self.app.automatic)
        self.assertTrue(self.dialogs)

    def test_slit_query_detects_ports_and_keeps_request_separate(self):
        fake, patch = self.icl_fixture()
        fake.slits = {1: 1., 4: .5}
        with patch:
            self.app.read_horiba_slits(); self.drain()
        self.assertEqual(self.app.vars["horiba_slit_in"].get(), "1")
        self.assertEqual(self.app.vars["horiba_slit_out"].get(), "4")
        self.assertIn("1 mm", self.app.slit_actual_labels["slit_in_mm"].cget("text"))
        self.assertIn("0.5 mm", self.app.slit_actual_labels["slit_out_mm"].cget("text"))
        self.assertEqual(self.app.vars["target_slit_in_mm"].get(), "0.1")
        self.assertTrue(all(str(b.cget("state")) == "normal" for b in self.app.slit_apply_buttons))
        self.assertNotIn("mono_moveSlitMM", [c["command"] for c in fake.commands])

    def test_slit_controls_move_independently_then_import_unequal_mount(self):
        fake, patch = self.icl_fixture()
        with patch:
            self.app.read_horiba_slits(); self.drain()
            self.app.vars["target_slit_in_mm"].set(.2)
            self.app.vars["target_slit_out_mm"].set(.3)
            self.app.apply_horiba_slits("entrance"); self.drain()
            self.assertEqual(fake.slits, {1: .2, 4: .1})
            self.app.apply_horiba_slits("exit"); self.drain()
            self.assertEqual(fake.slits, {1: .2, 4: .3})
            self.assertEqual(float(self.app.vars["slit_mm"].get()), .1)
            self.app.read_horiba_mount(); self.drain()
        exp = self.app.experiment()
        self.assertEqual((exp.slit_in_mm, exp.slit_out_mm), (.2, .3))
        self.assertEqual(self.dialogs, [])
        self.assertIn("Confirmado", self.app.slit_move_label.cget("text"))

    def test_slit_invalid_both_request_never_moves_either(self):
        fake, patch = self.icl_fixture()
        with patch:
            self.app.read_horiba_slits(); self.drain()
            self.app.vars["target_slit_in_mm"].set(.2)
            self.app.vars["target_slit_out_mm"].set(7.01)
            with self.assertRaisesRegex(ValueError, "7 mm"):
                self.app.apply_horiba_slits("both")
        self.assertEqual(fake.slits, {1: .1, 4: .1})
        self.assertNotIn("mono_moveSlitMM", [c["command"] for c in fake.commands])

    def test_slit_move_does_not_accept_ack_as_confirmation(self):
        fake, patch = self.icl_fixture()
        with patch:
            self.app.read_horiba_slits(); self.drain()
            fake.ignore_slit_move = True
            self.app.vars["target_slit_in_mm"].set(.5)
            self.app.apply_horiba_slits("entrance"); self.drain()
        self.assertTrue(self.dialogs)
        self.assertIn("sin lectura confirmada", self.app.slit_actual_labels["slit_in_mm"].cget("text"))
        self.assertIn("Sin confirmar", self.app.slit_move_label.cget("text"))
        self.assertEqual(self.app.vars["target_slit_in_mm"].get(), "0.5")

    def test_slits_without_initialization_are_disabled_and_do_not_move(self):
        fake, patch = self.icl_fixture()
        fake.initialized = False
        with patch:
            self.app.read_horiba_slits(); self.drain()
            self.app.apply_horiba_slits("entrance"); self.drain()
        self.assertTrue(all(str(b.cget("state")) == "disabled" for b in self.app.slit_apply_buttons))
        self.assertNotIn("mono_moveSlitMM", [c["command"] for c in fake.commands])
        self.assertNotIn("mono_init", [c["command"] for c in fake.commands])

    def test_slit_motion_is_blocked_for_existing_scan_and_background(self):
        fake, patch = self.icl_fixture()
        with patch:
            self.app.read_horiba_slits(); self.drain()
            self.app.start_acquire("background"); self.drain()
            with self.assertRaisesRegex(ValueError, "nuevo barrido"):
                self.app.apply_horiba_slits("both")
        self.assertTrue(all(str(b.cget("state")) == "disabled" for b in self.app.slit_apply_buttons))
        self.assertNotIn("mono_moveSlitMM", [c["command"] for c in fake.commands])

    def test_slit_unequal_values_survive_save_and_form_reload(self):
        fake, patch = self.icl_fixture()
        fake.slits = {1: .1, 4: .3}
        with patch:
            self.app.read_horiba_mount(); self.drain()
            self.point(532.)
        restored = Scan.from_dict(self.app.scan.to_dict())
        self.assertEqual((restored.experiment.slit_in_mm, restored.experiment.slit_out_mm), (.1, .3))
        self.app.vars["slit_out_mm"].set(2)
        self.app.load_scan_fields()
        self.assertEqual(float(self.app.vars["slit_out_mm"].get()), .3)

    def test_slit_unequal_mount_is_verified_during_automatic_acquisition(self):
        fake, patch = self.icl_fixture()
        fake.slits = {1: .1, 4: .3}
        with patch:
            self.app.read_horiba_mount(); self.drain()
            self.app.start_automatic(); self.drain()
        self.assertEqual(self.dialogs, [])
        self.assertTrue(self.app.scan.completed)
        for point in self.app.scan.points:
            self.assertEqual((point.instrument["slit_in_mm"], point.instrument["slit_out_mm"]), (.1, .3))

    def test_slit_wait_keeps_tk_responsive_and_cancel_stops_next_move(self):
        fake, patch = self.icl_fixture()
        fake.slit_busy_polls = 1000
        ticks = []
        with patch:
            self.app.read_horiba_slits(); self.drain()
            self.app.vars["target_slit_in_mm"].set(.2)
            self.app.vars["target_slit_out_mm"].set(.3)
            self.app.apply_horiba_slits("both")
            def cancel():
                ticks.append(self.app.awaiting)
                self.app.cancel_acquisition()
            self.root.after(120, cancel)
            self.drain()
        self.assertEqual(ticks, [True])
        self.assertEqual(sum(c["command"] == "mono_moveSlitMM" for c in fake.commands), 1)
        self.assertEqual(self.app.horiba_controller.status.state, "error")
        self.assertEqual(self.dialogs, [])

    def test_slit_closed_position_can_be_read_and_commanded(self):
        fake, patch = self.icl_fixture()
        with patch:
            self.app.read_horiba_slits(); self.drain()
            self.app.vars["target_slit_out_mm"].set(0)
            self.app.apply_horiba_slits("exit"); self.drain()
            self.app.check_horiba(); self.drain()
        self.assertIn("Salida leída: 0 mm", self.app.slit_actual_labels["slit_out_mm"].cget("text"))
        self.assertEqual(fake.slits[1], .1)
        self.assertEqual(self.dialogs, [])

    def test_automatic_cancel_keeps_complete_points(self):
        self.app.vars["wait_s"].set(.15)
        self.automatic()
        self.root.after(220, self.app.cancel_acquisition)
        self.drain()
        self.assertIsNone(self.app.automatic)
        self.assertLess(len(self.app.scan.points), 7)
        self.assertFalse(self.app.scan.completed)
        self.assertFalse(self.dialogs)

    def test_output_starts_unknown_and_query_does_not_change_requested_selection(self):
        fake, patch = self.icl_fixture()
        self.assertEqual(self.app.vars["output_target"].get(), "")
        self.assertIn("desconocida", self.app.output_actual_label.cget("text"))
        self.assertEqual(str(self.app.output_apply_button.cget("state")), "disabled")
        with patch:
            self.app.read_horiba_output(); self.drain()
        self.assertIn("Lateral", self.app.output_actual_label.cget("text"))
        self.assertEqual(self.app.vars["output_target"].get(), "")
        self.assertEqual(str(self.app.output_apply_button.cget("state")), "disabled")
        self.assertNotIn("mono_moveMirror", [c["command"] for c in fake.commands])

    def test_output_controls_both_directions_and_preserve_slits_and_entrance_mirror(self):
        fake, patch = self.icl_fixture()
        with patch:
            self.app.read_horiba_output(); self.drain()
            for target in ("Axial", "Lateral"):
                self.app.vars["output_target"].set(target)
                self.app.refresh_controls()
                self.assertEqual(str(self.app.output_apply_button.cget("state")), "normal")
                self.app.apply_horiba_output(); self.drain()
                self.assertEqual(self.app.output_actual_label.cget("text"), "Salida leída: "+target)
                self.assertEqual(self.app.vars["output_target"].get(), target)
        self.assertEqual(fake.slits, {1: .1, 4: .1})
        self.assertEqual(fake.mirrors[0], 0)
        self.assertEqual(self.dialogs, [])

    def test_output_unknown_reply_is_visible_and_read_recovers(self):
        fake, patch = self.icl_fixture()
        with patch:
            fake.mirrors[1] = 9
            self.app.read_horiba_output(); self.drain()
            self.assertIn("desconocida", self.app.output_actual_label.cget("text"))
            self.assertEqual(str(self.app.output_apply_button.cget("state")), "disabled")
            fake.mirrors[1] = 0
            self.app.read_horiba_output(); self.drain()
        self.assertEqual(self.app.output_actual_label.cget("text"), "Salida leída: Axial")
        self.assertEqual(len(self.dialogs), 1)

    def test_output_ack_without_change_does_not_report_success(self):
        fake, patch = self.icl_fixture()
        fake.ignore_output_move = True
        with patch:
            self.app.read_horiba_output(); self.drain()
            self.app.vars["output_target"].set("Axial")
            self.app.apply_horiba_output(); self.drain()
        self.assertIn("Sin confirmar", self.app.output_move_label.cget("text"))
        self.assertIn("desconocida", self.app.output_actual_label.cget("text"))
        self.assertEqual(self.app.vars["output_target"].get(), "Axial")
        self.assertEqual(len(self.dialogs), 1)

    def test_output_changes_are_blocked_with_existing_background(self):
        fake, patch = self.icl_fixture()
        with patch:
            self.app.read_horiba_output(); self.drain()
            self.app.vars["output_target"].set("Axial")
            self.app.start_acquire("background"); self.drain()
            with self.assertRaisesRegex(ValueError, "nuevo barrido"):
                self.app.apply_horiba_output()
        self.assertEqual(str(self.app.output_apply_button.cget("state")), "disabled")
        self.assertNotIn("mono_moveMirror", [c["command"] for c in fake.commands])

    def test_output_invalid_selection_and_simulation_never_contact_hardware(self):
        fake, patch = self.icl_fixture()
        with patch:
            with self.assertRaisesRegex(ValueError, "Seleccione Axial"):
                self.app.apply_horiba_output()
            self.app.vars["horiba_mode"].set("SIM")
            self.app.vars["output_target"].set("Axial")
            with self.assertRaisesRegex(ValueError, "ICL"):
                self.app.apply_horiba_output()
            with self.assertRaisesRegex(ValueError, "ICL"):
                self.app.read_horiba_output()
        self.assertEqual(fake.commands, [])

    def test_output_monitoring_updates_readback_without_moving_or_overwriting_request(self):
        fake, patch = self.icl_fixture()
        with patch:
            self.app.read_horiba_output(); self.drain()
            self.app.vars["output_target"].set("Lateral")
            fake.mirrors[1] = 0
            self.app.horiba_check_at = 0
            end = time.monotonic()+2
            while self.app.horiba_controller.output_position != "Axial" and time.monotonic() < end:
                self.root.update()
                time.sleep(.01)
            self.drain()
        self.assertEqual(self.app.output_actual_label.cget("text"), "Salida leída: Axial")
        self.assertEqual(self.app.vars["output_target"].get(), "Lateral")
        self.assertNotIn("mono_moveMirror", [c["command"] for c in fake.commands])

    def test_output_cancel_keeps_tk_responsive_and_does_not_reverse_mirror(self):
        fake, patch = self.icl_fixture()
        fake.output_busy_polls = 1000
        ticks = []
        with patch:
            self.app.read_horiba_output(); self.drain()
            self.app.vars["output_target"].set("Axial")
            self.app.apply_horiba_output()
            def cancel():
                ticks.append(self.app.awaiting)
                self.app.cancel_acquisition()
            self.root.after(120, cancel)
            self.drain()
        self.assertEqual(ticks, [True])
        self.assertIn("desconocida", self.app.output_actual_label.cget("text"))
        self.assertEqual(sum(c["command"] == "mono_moveMirror" for c in fake.commands), 1)
        self.assertEqual(self.dialogs, [])

    def test_output_is_saved_per_point_and_survives_scan_roundtrip(self):
        fake, patch = self.icl_fixture()
        with patch:
            self.app.start_automatic(); self.drain()
        scan = self.app.scan
        self.assertEqual(scan.horiba["initial"]["output_port"], "Lateral")
        self.assertEqual(scan.horiba["output_port"], "Lateral")
        for point in scan.points:
            self.assertEqual(point.instrument["output_port"], "Lateral")
            self.assertEqual(point.instrument["after"]["output_port"], "Lateral")
        self.assertEqual(Scan.from_dict(scan.to_dict()).to_dict(), scan.to_dict())

    def test_output_change_during_acquisition_discards_point(self):
        fake, patch = self.icl_fixture()
        original = self.app.reader.read_voltage
        def read(channel):
            value = original(channel)
            fake.mirrors[1] = 0
            return value
        self.app.reader.read_voltage = read
        with patch:
            self.app.start_automatic(); self.drain()
        self.assertEqual(len(self.app.scan.points), 0)
        self.assertIsNone(self.app.automatic)
        self.assertTrue(self.dialogs)
        self.assertIn("salida óptica cambió", self.dialogs[0][1])
        self.assertNotIn("mono_moveMirror", [c["command"] for c in fake.commands])

    def test_output_new_window_queries_current_position_without_saved_selection(self):
        fake, patch = self.icl_fixture()
        other_root = tk.Toplevel(self.master)
        other_root.withdraw()
        other = CalibrationApp(other_root, self.temp.name)
        try:
            with patch:
                self.app.read_horiba_output(); self.drain()
                self.app.horiba_controller.close()
                fake.mirrors[1] = 0
                other.vars["horiba_mode"].set("ICL")
                other.read_horiba_output()
                end = time.monotonic()+3
                while other.awaiting and time.monotonic() < end:
                    other_root.update()
                    time.sleep(.01)
                self.assertFalse(other.awaiting)
            self.assertEqual(other.output_actual_label.cget("text"), "Salida leída: Axial")
            self.assertEqual(other.vars["output_target"].get(), "")
        finally:
            if other.horiba_controller:
                other.horiba_controller.close()
            other._destroy()

    def test_output_uninitialized_disables_control_without_homing(self):
        fake, patch = self.icl_fixture()
        fake.initialized = False
        with patch:
            self.app.read_horiba_output(); self.drain()
        self.assertEqual(str(self.app.output_apply_button.cget("state")), "disabled")
        self.assertIn("desconocida", self.app.output_actual_label.cget("text"))
        self.assertNotIn("mono_moveMirror", [c["command"] for c in fake.commands])
        self.assertNotIn("mono_init", [c["command"] for c in fake.commands])

    def test_automatic_save_failure_stops_and_retry_does_not_move(self):
        self.app.ensure_scan()
        original = self.app.store._write_snapshot
        def fail_point(scans):
            if scans[0].points:
                raise OSError("disco lleno")
            return original(scans)
        with mock.patch.object(self.app.store, "_write_snapshot", side_effect=fail_point):
            self.automatic(); self.drain()
        self.assertIsNone(self.app.automatic)
        self.assertIsNotNone(self.app.unsaved)
        self.assertEqual(len(self.app.scan.points), 0)
        self.app.save_pending(); self.drain()
        self.assertEqual(len(self.app.scan.points), 1)
        self.assertEqual(self.reader.wavelength_nm, 531.4)

    def test_automatic_csv_failure_keeps_canonical_point_and_stops(self):
        self.app.ensure_scan()
        original = self.app.store.export_tables
        def fail_point():
            if self.app.scan.points:
                raise OSError("CSV ocupado")
            return original()
        with mock.patch.object(self.app.store, "export_tables", side_effect=fail_point):
            self.automatic(); self.drain()
        self.assertIsNone(self.app.automatic)
        self.assertIsNone(self.app.unsaved)
        self.assertEqual(len(self.app.scan.points), 1)
        self.assertEqual(self.reader.wavelength_nm, 531.4)

    def test_grating_catalog_shows_three_slots_and_separate_request(self):
        fake, patch = self.icl_fixture()
        with patch:
            self.app.read_horiba_gratings(); self.drain()
        self.assertIn("Grating 1 leído", self.app.grating_actual_label.cget("text"))
        self.assertEqual(self.app.grating_table.item("2", "values")[1], "600")
        self.assertIn("sin λ conocida", self.app.grating_table.item("1", "values")[2])
        self.assertEqual(self.app.vars["grating_target"].get(), "")
        self.assertEqual(float(self.app.vars["grating_target_nm"].get()), 532.)
        self.assertEqual(str(self.app.grating_apply_button.cget("state")), "disabled")
        self.assertNotIn("mono_moveGrating", [c["command"] for c in fake.commands])

    def test_grating_selection_updates_real_mount_and_clears_stale_blaze(self):
        fake, patch = self.icl_fixture()
        with patch:
            self.app.read_horiba_gratings(); self.drain()
            self.app.vars["blaze_nm"].set(500)
            self.app.vars["grooves_per_mm"].set(999)
            self.app.vars["grating_target"].set("Grating 2")
            self.app.vars["grating_target_nm"].set(540)
            self.app.refresh_controls()
            self.assertEqual(str(self.app.grating_apply_button.cget("state")), "normal")
            self.app.grating_apply_button.invoke(); self.drain()
        self.assertEqual(self.app.vars["grating"].get(), "G2")
        self.assertEqual(float(self.app.vars["wavelength_nm"].get()), 540)
        self.assertEqual(float(self.app.vars["grooves_per_mm"].get()), 600)
        self.assertEqual(self.app.vars["blaze_nm"].get(), "")
        self.assertEqual(fake.grating, 1)
        self.assertEqual(fake.position, 540)
        self.assertEqual(self.dialogs, [])

    def test_grating_not_installed_is_not_available_in_selector(self):
        fake, patch = self.icl_fixture()
        fake.gratings = [fake.gratings[0], fake.gratings[2]]
        with patch:
            self.app.read_horiba_gratings(); self.drain()
        self.assertEqual(tuple(self.app.grating_selector.cget("values")), ("Grating 1", "Grating 3"))
        self.assertEqual(self.app.grating_table.item("2", "values")[1], "no configurado")
        self.assertNotIn("mono_moveGrating", [c["command"] for c in fake.commands])

    def test_grating_same_position_and_wavelength_do_not_repeat_moves(self):
        fake, patch = self.icl_fixture()
        with patch:
            self.app.read_horiba_gratings(); self.drain()
            self.app.vars["grating_target"].set("Grating 1")
            self.app.apply_horiba_grating(); self.drain()
        self.assertNotIn("mono_moveGrating", [c["command"] for c in fake.commands])
        self.assertNotIn("mono_moveToPosition", [c["command"] for c in fake.commands])
        self.assertIn("Confirmado", self.app.grating_move_label.cget("text"))

    def test_grating_failed_wavelength_blocks_acquisition_until_recovered(self):
        fake, patch = self.icl_fixture()
        with patch:
            self.app.read_horiba_gratings(); self.drain()
            self.app.vars["grating_target"].set("Grating 2")
            self.app.vars["grating_target_nm"].set(532)
            fake.offset = .1
            self.app.apply_horiba_grating(); self.drain()
            self.assertTrue(self.app.horiba_controller.grating_change_pending)
            self.assertIn("pendiente", self.app.grating_actual_label.cget("text"))
            fake.offset = 0
            self.app.start_automatic(); self.drain()
            self.assertEqual(len(self.app.scan.points), 0)
            self.app.new_scan()
            self.app.read_horiba_gratings(); self.drain()
            self.app.apply_horiba_grating(); self.drain()
        self.assertFalse(self.app.horiba_controller.grating_change_pending)
        self.assertEqual(sum(c["command"] == "mono_moveGrating" for c in fake.commands), 1)
        self.assertEqual(sum(c["command"] == "mono_moveToPosition" for c in fake.commands), 2)
        self.assertEqual(self.app.vars["grating"].get(), "G2")

    def test_grating_cancellation_keeps_tk_responsive_and_does_not_move_wavelength(self):
        fake, patch = self.icl_fixture()
        fake.grating_busy_polls = 1000
        ticks = []
        with patch:
            self.app.read_horiba_gratings(); self.drain()
            self.app.vars["grating_target"].set("Grating 2")
            self.app.apply_horiba_grating()
            def cancel():
                ticks.append(self.app.awaiting)
                self.app.cancel_acquisition()
            self.root.after(120, cancel)
            self.drain()
        self.assertEqual(ticks, [True])
        self.assertTrue(self.app.horiba_controller.grating_change_pending)
        self.assertNotIn("mono_moveToPosition", [c["command"] for c in fake.commands])
        self.assertEqual(self.dialogs, [])

    def test_grating_changes_are_blocked_with_saved_background(self):
        fake, patch = self.icl_fixture()
        with patch:
            self.app.read_horiba_gratings(); self.drain()
            self.app.start_acquire("background"); self.drain()
            self.app.vars["grating_target"].set("Grating 2")
            with self.assertRaisesRegex(ValueError, "nuevo barrido"):
                self.app.apply_horiba_grating()
        self.assertEqual(str(self.app.grating_apply_button.cget("state")), "disabled")
        self.assertNotIn("mono_moveGrating", [c["command"] for c in fake.commands])

    def test_grating_and_properties_are_saved_for_each_automatic_point(self):
        fake, patch = self.icl_fixture()
        with patch:
            self.app.read_horiba_gratings(); self.drain()
            self.app.vars["grating_target"].set("Grating 2")
            self.app.apply_horiba_grating(); self.drain()
            self.app.start_automatic(); self.drain()
        scan = self.app.scan
        self.assertEqual(scan.experiment.grating, "G2")
        self.assertEqual(scan.horiba["initial"]["grating_info"]["grooves_per_mm"], 600)
        for point in scan.points:
            self.assertEqual(point.instrument["grating"], "G2")
            self.assertEqual(point.instrument["after"]["grating_info"]["number"], 2)
        self.assertEqual(Scan.from_dict(scan.to_dict()).to_dict(), scan.to_dict())
        stored = json.loads((self.app.store.path/"session.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["scans"][0]["points"][0]["instrument"]["grating_info"]["grooves_per_mm"], 600)
        with (self.app.store.path/(self.app.store.stem(scan)+".csv")).open(encoding="utf-8", newline="") as stream:
            self.assertTrue(all(row["grating"] == "G2" for row in csv.DictReader(stream)))
        self.assertEqual(self.dialogs, [])

    def test_grating_read_preserves_manual_blaze_for_same_slot(self):
        fake, patch = self.icl_fixture()
        self.app.vars["blaze_nm"].set(500)
        with patch:
            self.app.read_horiba_mount(); self.drain()
        self.assertEqual(self.app.vars["blaze_nm"].get(), "500")
        self.assertEqual(float(self.app.vars["grooves_per_mm"].get()), 1200)
        self.assertEqual(fake.grating, 0)

    def test_grating_pending_survives_changed_communication_settings(self):
        fake, patch = self.icl_fixture()
        with patch:
            self.app.read_horiba_gratings(); self.drain()
            self.app.vars["grating_target"].set("Grating 2")
            fake.offset = .1
            self.app.apply_horiba_grating(); self.drain()
            previous = self.app.horiba_controller
            self.assertTrue(previous.grating_change_pending)
            self.app.vars["horiba_tolerance_nm"].set(.05)
            self.app.read_horiba_gratings(); self.drain()
            current = self.app.horiba_controller
            self.assertIsNot(current, previous)
            self.assertTrue(current.grating_change_pending)
            self.assertEqual(current.last_grating_move["state"], "error")
            self.assertEqual(current.status.state, "error")
            with self.assertRaisesRegex(Exception, "pendiente"):
                current.move_to(532)
            fake.offset = 0
            self.app.apply_horiba_grating(); self.drain()
        self.assertFalse(current.grating_change_pending)
        self.assertEqual(sum(c["command"] == "mono_moveGrating" for c in fake.commands), 1)

    def test_grating_external_change_during_automatic_discards_point(self):
        fake, patch = self.icl_fixture()
        original = self.app.reader.read_voltage
        def read(channel):
            value = original(channel)
            fake.grating = 1
            return value
        self.app.reader.read_voltage = read
        with patch:
            self.app.start_automatic(); self.drain()
        self.assertEqual(len(self.app.scan.points), 0)
        self.assertIsNone(self.app.automatic)
        self.assertTrue(self.dialogs)
        self.assertNotIn("mono_moveGrating", [c["command"] for c in fake.commands])

    def test_grating_manual_icl_point_records_actual_selection_without_motion(self):
        fake, patch = self.icl_fixture()
        with patch:
            self.app.read_horiba_mount(); self.drain()
            self.point(532.)
        point = self.app.scan.points[0]
        self.assertEqual(point.instrument["grating"], "G1")
        self.assertEqual(point.instrument["grating_info"]["grooves_per_mm"], 1200)
        self.assertEqual(self.app.scan.horiba["initial"]["grating_info"]["number"], 1)
        self.assertNotIn("mono_moveToPosition", [c["command"] for c in fake.commands])
        self.assertEqual(self.dialogs, [])

    def test_grating_manual_icl_wavelength_mismatch_prevents_sampling(self):
        fake, patch = self.icl_fixture()
        fake.position = 533
        self.app.reader.read_voltage = mock.Mock()
        with patch:
            self.point(532.)
        self.app.reader.read_voltage.assert_not_called()
        self.assertEqual(len(self.app.scan.points), 0)
        self.assertIn("λ manual declarada", self.dialogs[0][1])

    def test_grating_manual_change_during_acquisition_discards_point(self):
        fake, patch = self.icl_fixture()
        original = self.app.reader.read_voltage
        def read(channel):
            value = original(channel)
            fake.grating = 1
            return value
        self.app.reader.read_voltage = read
        with patch:
            self.point(532.)
        self.assertEqual(len(self.app.scan.points), 0)
        self.assertIsNone(self.app.unsaved)
        self.assertTrue(self.dialogs)

    def test_grating_reconfigured_between_manual_points_is_not_mixed(self):
        fake, patch = self.icl_fixture()
        with patch:
            self.point(532.)
            fake.gratings[0]["grooveDensity"] = 1800
            fake.position = 532.1
            self.point(532.1)
        self.assertEqual(len(self.app.scan.points), 1)
        self.assertIn("configuración del grating cambió", self.dialogs[0][1])

    def test_grating_invalid_selection_wavelength_and_simulation_do_not_send(self):
        fake, patch = self.icl_fixture()
        with patch:
            with self.assertRaisesRegex(ValueError, "Seleccione Grating"):
                self.app.apply_horiba_grating()
            self.app.vars["grating_target"].set("Grating 2")
            self.app.vars["grating_target_nm"].set(2000)
            with self.assertRaisesRegex(ValueError, "límites"):
                self.app.apply_horiba_grating()
            self.app.vars["horiba_mode"].set("SIM")
            with self.assertRaisesRegex(ValueError, "ICL"):
                self.app.read_horiba_gratings()
        self.assertEqual(fake.commands, [])

    def test_grating_fresh_window_reads_current_slot_without_saved_target(self):
        fake, patch = self.icl_fixture()
        fake.grating = 2
        root = tk.Toplevel(self.master)
        root.withdraw()
        other = CalibrationApp(root, self.temp.name)
        try:
            with patch:
                other.vars["horiba_mode"].set("ICL")
                other.read_horiba_gratings()
                end = time.monotonic()+3
                while other.awaiting and time.monotonic() < end:
                    root.update()
                    time.sleep(.01)
                self.assertFalse(other.awaiting)
            self.assertIn("Grating 3 leído", other.grating_actual_label.cget("text"))
            self.assertEqual(other.vars["grating_target"].get(), "")
            self.assertEqual(other.grating_table.item("3", "values")[1], "300")
        finally:
            if other.horiba_controller:
                other.horiba_controller.close()
            other._destroy()

    def slit_sweep_fields(self):
        for key, value in {"n": 3, "sweep_start": 531.5, "sweep_end": 532.5, "sweep_step": .1,
                           "sweep_slit_min": .1, "sweep_slit_max": .3, "sweep_slit_step": .1}.items():
            self.app.vars[key].set(value)

    def test_slit_sweep_range_from_gui_creates_one_csv(self):
        self.slit_sweep_fields()
        self.app.preview_slit_sweep()
        self.assertIn("3 configuraciones × 11 λ = 33 puntos", self.app.sweep_label.cget("text"))
        self.app.start_slit_sweep()
        self.drain()
        files = list((Path(self.temp.name) / "slit_sweeps").glob("*.csv"))
        self.assertEqual(len(files), 1)
        with files[0].open(encoding="utf-8", newline="") as stream:
            data = list(csv.DictReader(stream))
        self.assertEqual(len(data), 33)
        self.assertEqual(sorted({r["slit_in_mm"] for r in data}), ["0.1", "0.2", "0.3"])
        self.assertTrue(all(r["slit_in_mm"] == r["slit_out_mm"] for r in data))
        self.assertIn("completo", self.app.sweep_label.cget("text"))
        self.assertEqual(self.reader.fwhm_nm, .4)
        self.assertEqual(self.dialogs, [])
        # Gráfica en vivo: una curva por configuración, sólo la última en color y con leyenda.
        self.assertIsNotNone(self.app.profile_ax.get_legend())
        self.app.errors.set(False)
        self.app.redraw()
        lines = self.app.profile_ax.get_lines()
        self.assertEqual(len(lines), 3)
        self.assertEqual([len(l.get_xdata()) for l in lines], [11, 11, 11])
        colors = [l.get_color() for l in lines]
        self.assertEqual(colors[:2], ["0.7", "0.7"])
        self.assertNotEqual(colors[2], "0.7")
        legend = self.app.profile_ax.get_legend()
        self.assertEqual([t.get_text() for t in legend.get_texts()],
                         ["Barrido actual · entrada 0.3 mm / salida 0.3 mm"])
        # Al adquirir un barrido normal vuelve la vista habitual.
        self.point(532)
        self.assertIsNone(self.app.sweep_live)

    def test_slit_sweep_lists_preview_and_blocked_with_selected_scan(self):
        self.slit_sweep_fields()
        self.app.vars["sweep_mode"].set("Listas entrada/salida")
        self.app.vars["sweep_list_in"].set("0.1;0.2")
        self.app.vars["sweep_list_out"].set("0.3")
        self.app.preview_slit_sweep()
        self.assertIn("0.1/0.3, 0.2/0.3", self.app.sweep_label.cget("text"))
        self.point(532)
        with self.assertRaises(ValueError):
            self.app.start_slit_sweep()
        self.assertFalse(list(Path(self.temp.name).glob("slit_sweeps/*.csv")))

    def test_slit_sweep_cancel_keeps_rows(self):
        self.slit_sweep_fields()
        self.app.vars["n"].set(20)
        self.app.vars["interval_s"].set(.01)
        self.app.start_slit_sweep()
        end = time.monotonic()+10
        path = None
        while time.monotonic() < end:
            self.root.update()
            files = list((Path(self.temp.name) / "slit_sweeps").glob("*.csv"))
            if files and len(files[0].read_text(encoding="utf-8").splitlines()) >= 3:
                path = files[0]
                break
            time.sleep(.01)
        self.app.cancel_acquisition()
        self.drain()
        self.assertIsNotNone(path)
        info = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(info["state"], "cancelled")
        self.assertGreaterEqual(info["points_written"], 2)
        self.assertIn("interrumpido", self.app.sweep_label.cget("text"))
