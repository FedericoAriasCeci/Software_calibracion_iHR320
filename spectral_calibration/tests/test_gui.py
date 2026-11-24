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

from spectral_calibration import slit_sweep
from spectral_calibration.gui import CalibrationApp, FOLLOW_CURRENT, SWEEP_MODES
from spectral_calibration.instruments import SimulatedReader, CommunicationError
from spectral_calibration.horiba import ICLMonochromator
from spectral_calibration.tests.test_horiba import FakeICL


def rows(path):
    with Path(path).open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


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
                         mock.patch("spectral_calibration.gui.messagebox.showwarning"),
                         mock.patch("spectral_calibration.gui.messagebox.showinfo")]
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

    def sweep_files(self):
        return sorted((Path(self.temp.name) / "slit_sweeps").glob("*.csv"))

    def sim_sweep_fields(self):
        for key, value in {"n": 3, "sweep_start": 531.5, "sweep_end": 532.5, "sweep_step": .1,
                           "sweep_slit_min": .1, "sweep_slit_max": .3, "sweep_slit_step": .1}.items():
            self.app.vars[key].set(value)

    def sim_sweep(self):
        self.sim_sweep_fields()
        self.app.start_slit_sweep(); self.drain()

    def icl_fixture(self):
        # Doble explícito del SR830: nunca se abre VISA ni un equipo real.
        reader = self.reader
        class ReaderDouble:
            def describe(self):
                return {"interface": "TEST", "identity": "SR830_DOUBLE"}
            def test(self):
                return "SR830_DOUBLE"
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
        # Una sola apertura igual a la del doble: equivale al antiguo barrido automático en λ.
        for key, value in {"sweep_start": 531.9, "sweep_end": 532.1, "sweep_step": .1,
                           "sweep_slit_min": .1, "sweep_slit_max": .1, "sweep_slit_step": .1}.items():
            self.app.vars[key].set(value)
        return fake, mock.patch("spectral_calibration.gui.ICLMonochromator", ControllerDouble)

    def commands(self, fake, name):
        return sum(c["command"] == name for c in fake.commands)

    # ------------------------------------------------------- HORIBA: estado
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
        self.assertEqual(self.commands(fake, "mono_init"), 1)
        self.assertEqual(self.dialogs, [])
        # Inicializar no crea un barrido ni archivos.
        self.assertIsNone(self.app.sweep)
        self.assertEqual(self.sweep_files(), [])

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
        self.assertEqual(self.commands(fake, "mono_init"), 1)

    def test_status_heartbeat_detects_loss_without_sending_homing(self):
        fake, patch = self.icl_fixture()
        with patch:
            self.app.check_horiba(); self.drain()
            fake.recv = mock.Mock(side_effect=OSError("USB desconectado"))
            self.app.horiba_check_at = 0
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
        self.assertEqual(self.commands(fake, "mono_init"), 1)

    def test_initialization_session_is_reused_for_state_read_and_sweep(self):
        fake, patch = self.icl_fixture()
        fake.initialized = False
        with patch:
            self.app.initialize_horiba(); self.drain()
            controller = self.app.horiba_controller
            self.app.read_horiba_mount(); self.drain()
            self.app.start_slit_sweep(); self.drain()
        self.assertIs(self.app.horiba_controller, controller)
        self.assertEqual(self.commands(fake, "mono_open"), 1)
        self.assertEqual(self.commands(fake, "mono_init"), 1)
        self.assertEqual(self.app.sweep["state"], "completed")
        info = json.loads(self.app.sweep["json"].read_text(encoding="utf-8"))
        self.assertTrue(info["horiba"]["configuration"]["initialization"]["initialization_sent"])
        self.assertEqual(self.dialogs, [])

    def test_state_read_updates_summary_without_moving(self):
        fake, patch = self.icl_fixture()
        fake.slits = {1: .2, 4: .3}
        with patch:
            self.app.read_horiba_mount(); self.drain()
        summary = self.app.summary_labels["horiba"].cget("text")
        for text in ("Listo", "G1", "532.0000 nm", "Lateral", "0.2/0.3 mm"):
            self.assertIn(text, summary)
        self.assertIn("Estado leído", self.app.status.cget("text"))
        for command in ("mono_init", "mono_moveToPosition", "mono_moveSlitMM", "mono_moveMirror", "mono_moveGrating"):
            self.assertNotIn(command, [c["command"] for c in fake.commands])

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

    def test_real_initialization_requires_icl_mode(self):
        with mock.patch("spectral_calibration.gui.ICLMonochromator") as controller:
            with self.assertRaisesRegex(ValueError, "ICL"):
                self.app.initialize_horiba()
            with self.assertRaisesRegex(ValueError, "ICL"):
                self.app.read_horiba_mount()
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

    # ------------------------------------------------------------- rendijas
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

    def test_slit_controls_move_independently(self):
        fake, patch = self.icl_fixture()
        with patch:
            self.app.read_horiba_slits(); self.drain()
            self.app.vars["target_slit_in_mm"].set(.2)
            self.app.vars["target_slit_out_mm"].set(.3)
            self.app.apply_horiba_slits("entrance"); self.drain()
            self.assertEqual(fake.slits, {1: .2, 4: .1})
            self.app.apply_horiba_slits("exit"); self.drain()
            self.assertEqual(fake.slits, {1: .2, 4: .3})
        self.assertIn("0.2/0.3 mm", self.app.summary_labels["horiba"].cget("text"))
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
        self.assertEqual(self.commands(fake, "mono_moveSlitMM"), 1)
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

    # --------------------------------------------------------------- salida
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
        self.assertEqual(self.commands(fake, "mono_moveMirror"), 1)
        self.assertEqual(self.dialogs, [])

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

    # ------------------------------------------------------------- gratings
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

    def test_grating_selection_moves_turret_and_wavelength_and_shows_readback(self):
        fake, patch = self.icl_fixture()
        with patch:
            self.app.read_horiba_gratings(); self.drain()
            self.app.vars["grating_target"].set("Grating 2")
            self.app.vars["grating_target_nm"].set(540)
            self.app.refresh_controls()
            self.assertEqual(str(self.app.grating_apply_button.cget("state")), "normal")
            self.app.grating_apply_button.invoke(); self.drain()
        self.assertEqual(fake.grating, 1)
        self.assertEqual(fake.position, 540)
        self.assertIn("Grating 2 leído", self.app.grating_actual_label.cget("text"))
        self.assertIn("G2", self.app.summary_labels["horiba"].cget("text"))
        self.assertIn("540.0000 nm", self.app.summary_labels["horiba"].cget("text"))
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

    def test_grating_pending_blocks_sweep_before_any_motion_until_recovered(self):
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
            self.app.vars["sweep_slit_min"].set(.2)
            self.app.vars["sweep_slit_max"].set(.2)
            self.app.start_slit_sweep(); self.drain()
            # No se crean archivos ni se mueven rendijas o λ con la configuración espectral incierta.
            self.assertIsNone(self.app.sweep)
            self.assertEqual(self.sweep_files(), [])
            self.assertNotIn("mono_moveSlitMM", [c["command"] for c in fake.commands])
            self.assertIn("pendiente", self.dialogs[-1][1])
            self.app.read_horiba_gratings(); self.drain()
            self.app.apply_horiba_grating(); self.drain()
            self.assertFalse(self.app.horiba_controller.grating_change_pending)
            self.assertEqual(self.commands(fake, "mono_moveGrating"), 1)
            self.assertEqual(self.commands(fake, "mono_moveToPosition"), 2)
            self.app.start_slit_sweep(); self.drain()
        data = rows(self.app.sweep["csv"])
        self.assertEqual(len(data), 3)
        self.assertEqual({r["grating"] for r in data}, {"G2"})
        info = json.loads(self.app.sweep["json"].read_text(encoding="utf-8"))
        self.assertEqual(info["horiba"]["initial_state"]["grating_info"]["grooves_per_mm"], 600)
        self.assertFalse(info["horiba"]["initial_state"]["grating_change_pending"])

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
        self.assertEqual(self.commands(fake, "mono_moveGrating"), 1)

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

    # ---------------------------------------------------------------- SR830
    def test_sr830_test_reads_aux_and_disconnect_updates_summary(self):
        self.app.test_connection(); self.drain()
        self.assertIn("V", self.app.voltage_label.cget("text"))
        self.assertIn("AUX =", self.app.status.cget("text"))
        self.app.disconnect(); self.drain()
        self.assertFalse(self.app.connected)
        self.assertIn("desconectado", self.app.summary_labels["sr830"].cget("text"))
        self.assertIn("Desconectado", self.app.connection_label.cget("text"))
        with self.assertRaisesRegex(ValueError, "conecte"):
            self.app.test_connection()

    def test_sr830_communication_error_during_sweep_marks_disconnected(self):
        self.sim_sweep_fields()
        self.reader.read_voltage = mock.Mock(side_effect=CommunicationError("desconexión simulada"))
        self.app.start_slit_sweep(); self.drain()
        self.assertFalse(self.app.connected)
        self.assertEqual(self.app.sweep["state"], "error")
        self.assertEqual(rows(self.app.sweep["csv"]), [])
        self.assertTrue(self.dialogs)

    # -------------------------------------------------------------- barrido
    def test_sweep_requires_compatible_reader_and_limits_without_creating_files(self):
        self.app.connected = False
        with self.assertRaisesRegex(ValueError, "conecte"):
            self.app.start_slit_sweep()
        self.app.connected = True
        self.app.vars["horiba_mode"].set("ICL")
        with self.assertRaisesRegex(ValueError, "SR830 real"):
            self.app.start_slit_sweep()
        fake, patch = self.icl_fixture()
        self.app.vars["sweep_end"].set(1600)
        self.app.vars["sweep_step"].set(1)
        with patch:
            with self.assertRaisesRegex(ValueError, "límites"):
                self.app.start_slit_sweep()
        self.app.vars["horiba_mode"].set("SIM")
        with self.assertRaisesRegex(ValueError, "lector SIM"):
            self.app.start_slit_sweep()
        self.assertFalse((Path(self.temp.name) / "slit_sweeps").exists())
        self.assertEqual(fake.commands, [])
        self.assertIsNone(self.app.sweep)

    def test_mode_selection_enables_only_its_fields(self):
        self.assertTrue(all(str(e.cget("state")) == "normal" for e in self.app.range_entries))
        self.assertTrue(all(str(e.cget("state")) == "disabled" for e in self.app.list_entries))
        self.app.vars["sweep_mode"].set(SWEEP_MODES[1])
        self.assertTrue(all(str(e.cget("state")) == "disabled" for e in self.app.range_entries))
        self.assertTrue(all(str(e.cget("state")) == "normal" for e in self.app.list_entries))

    def test_sweep_lists_preview(self):
        self.sim_sweep_fields()
        self.app.vars["sweep_mode"].set(SWEEP_MODES[1])
        self.app.vars["sweep_list_in"].set("0.1;0.2")
        self.app.vars["sweep_list_out"].set("0.3")
        self.app.preview_slit_sweep()
        self.assertIn("2 configuraciones × 11 λ = 22 puntos", self.app.sweep_label.cget("text"))
        self.assertIn("0.1/0.3, 0.2/0.3", self.app.sweep_label.cget("text"))
        self.assertEqual(self.sweep_files(), [])

    def test_sim_sweep_creates_one_csv_and_shows_only_current_configuration(self):
        self.sim_sweep_fields()
        self.app.preview_slit_sweep()
        self.assertIn("3 configuraciones × 11 λ = 33 puntos", self.app.sweep_label.cget("text"))
        self.app.start_slit_sweep(); self.drain()
        files = self.sweep_files()
        self.assertEqual(len(files), 1)
        data = rows(files[0])
        self.assertEqual(len(data), 33)
        self.assertEqual(sorted({r["slit_in_mm"] for r in data}), ["0.1", "0.2", "0.3"])
        self.assertTrue(all(r["slit_in_mm"] == r["slit_out_mm"] for r in data))
        self.assertIn("completo", self.app.sweep_label.cget("text"))
        self.assertEqual(self.reader.fwhm_nm, .4)
        self.assertEqual(self.dialogs, [])
        self.assertIn("completo · 33 puntos · sin guardar", self.app.summary_labels["sweep"].cget("text"))
        # Por defecto sólo se dibuja la configuración en curso: sin curvas previas superpuestas.
        ax = self.app.profile_ax
        self.app.errors.set(False)
        self.app.redraw()
        self.assertEqual(len(ax.get_lines()), 1)
        self.assertEqual(len(ax.get_lines()[0].get_xdata()), 11)
        self.assertEqual([t.get_text() for t in ax.get_legend().get_texts()], ["Entrada 0.3 mm / salida 0.3 mm"])
        self.assertEqual(ax.get_title(), "Configuración 3/3")
        self.assertEqual(len(self.app.table.get_children()), 11)
        # Superposición opcional: las demás configuraciones en gris, sin leyenda propia.
        self.app.overlay.set(True)
        self.app.redraw()
        colors = [l.get_color() for l in ax.get_lines()]
        self.assertEqual(colors[:2], ["0.7", "0.7"])
        self.assertNotEqual(colors[2], "0.7")
        self.assertEqual(len(ax.get_legend().get_texts()), 1)
        # Se puede volver a consultar una configuración anterior.
        self.app.overlay.set(False)
        values = self.app.view_selector.cget("values")
        self.assertEqual(values[0], FOLLOW_CURRENT)
        self.assertEqual(len(values), 4)
        self.app.view_choice.set(values[1])
        self.app.redraw(reset=True)
        self.assertEqual([t.get_text() for t in ax.get_legend().get_texts()], ["Entrada 0.1 mm / salida 0.1 mm"])
        self.assertEqual(ax.get_title(), "Configuración 1/3")

    def test_each_new_configuration_starts_a_clean_plot(self):
        self.sim_sweep_fields()
        self.app.vars["sweep_slit_max"].set(.2)
        self.app.errors.set(False)
        seen = []
        original = self.app.add_sweep_point
        def record(value):
            original(value)
            ax = self.app.profile_ax
            seen.append((value["config_index"], len(ax.get_lines()), len(ax.get_lines()[0].get_xdata()), ax.get_title()))
        self.app.add_sweep_point = record
        self.app.start_slit_sweep(); self.drain()
        self.assertEqual(len(seen), 22)
        self.assertTrue(all(lines == 1 for _, lines, _, _ in seen))
        first_of_second = next(item for item in seen if item[0] == 1)
        self.assertEqual(first_of_second[2], 1)
        self.assertEqual(first_of_second[3], "Configuración 2/2 · en curso")

    def test_new_sweep_does_not_draw_over_previous_sweep(self):
        self.sim_sweep()
        first = self.app.sweep
        self.app.start_slit_sweep()
        self.assertIsNot(self.app.sweep, first)
        self.assertEqual(self.app.profile_ax.get_lines(), [])
        self.assertEqual(self.app.profile_ax.get_title(), "Sin datos de barrido")
        self.assertEqual(tuple(self.app.view_selector.cget("values")), (FOLLOW_CURRENT,))
        self.assertEqual(self.app.table.get_children(), ())
        self.drain()
        self.assertEqual(len(self.sweep_files()), 2)
        self.assertEqual(self.app.sweep["written"], 33)

    def test_zoom_is_kept_during_redraw_and_reset_by_autoscale(self):
        self.sim_sweep()
        ax = self.app.profile_ax
        ax.set_xlim(531.9, 532.1)
        self.app.redraw()
        self.assertEqual(tuple(ax.get_xlim()), (531.9, 532.1))
        self.app.redraw(reset=True)
        self.assertNotEqual(tuple(ax.get_xlim()), (531.9, 532.1))
        self.assertTrue(ax.get_autoscalex_on())

    def test_sim_sweep_metadata_includes_notes_plan_horiba_and_sr830(self):
        self.app.notes.insert("1.0", "Láser 532 nm · ND1")
        self.sim_sweep()
        info = json.loads(self.app.sweep["json"].read_text(encoding="utf-8"))
        self.assertEqual(info["notes"], "Láser 532 nm · ND1")
        self.assertEqual(info["plan"]["slit_mode"], SWEEP_MODES[0])
        self.assertEqual(info["plan"]["slit_range_mm"], dict(minimum=.1, maximum=.3, step=.1))
        self.assertEqual(info["plan"]["points"], 33)
        self.assertEqual(info["horiba"]["control"], "SIM")
        self.assertEqual(info["horiba"]["initial_state"]["grating"], "G1")
        self.assertEqual(info["horiba"]["final_state"]["slit_in_mm"], .3)
        self.assertEqual(info["sr830"]["acquisition"], dict(n=3, interval_s=0., wait_s=0., channel=1))
        self.assertEqual(info["sr830"]["connection"]["interface"], "SIM")
        self.assertEqual(info["sr830"]["query"], "OAUX? 1")
        self.assertEqual(info["state"], "completed")
        # Sin la casilla de fondo: se registra explícitamente que no se midió.
        self.assertIsNone(info["background"])
        self.assertEqual(str(self.app.subtract_check.cget("state")), "disabled")
        self.assertIn("Sin fondo", self.app.background_label.cget("text"))

    # ----------------------------------------------------------------- fondo
    def test_sim_background_is_measured_blocked_saved_and_subtracted_only_in_view(self):
        self.app.measure_background.set(True)
        with mock.patch("spectral_calibration.gui.messagebox.askokcancel", return_value=True) as ask:
            self.sim_sweep()
        self.assertEqual([c[0][0] for c in ask.call_args_list], ["Medir fondo", "Desbloquear láser"])
        self.assertEqual(self.dialogs, [])
        self.assertFalse(self.reader.blocked)
        info = json.loads(self.app.sweep["json"].read_text(encoding="utf-8"))
        background = info["background"]
        self.assertEqual((background["n"], len(background["values_v"]), len(background["times_utc"])), (3, 3, 3))
        self.assertAlmostEqual(background["mean_v"], .03, delta=.005)
        self.assertEqual(background["channel"], 1)
        self.assertEqual(background["horiba_state"]["grating"], "G1")
        self.assertIn("bloqueado", background["condition"])
        # El CSV conserva tensiones crudas: el fondo no se resta en los datos.
        data = rows(self.app.sweep["csv"])
        raw_peak = max(float(r["mean_v"]) for r in data if r["config_index"] == "2")
        self.assertGreater(raw_peak, 1.)
        ax = self.app.profile_ax
        self.app.errors.set(False)
        self.app.redraw()
        self.assertAlmostEqual(max(ax.get_lines()[0].get_ydata()), raw_peak-background["mean_v"], places=9)
        self.assertEqual(ax.get_ylabel(), "Tensión AUX − fondo [V]")
        last = self.app.table.item(self.app.table.get_children()[-1], "values")
        self.assertAlmostEqual(float(last[4]), float(last[2])-background["mean_v"], places=6)
        self.assertEqual(str(self.app.subtract_check.cget("state")), "normal")
        self.assertIn("Fondo del barrido mostrado", self.app.background_label.cget("text"))
        self.assertIn("Fondo:", self.app.sweep_label.cget("text"))
        self.app.subtract.set(False)
        self.app.redraw(reset=True)
        self.assertAlmostEqual(max(ax.get_lines()[0].get_ydata()), raw_peak, places=9)
        self.assertEqual(ax.get_ylabel(), "Tensión AUX [V]")

    def test_background_block_prompt_cancel_starts_nothing(self):
        self.sim_sweep_fields()
        self.app.measure_background.set(True)
        with mock.patch("spectral_calibration.gui.messagebox.askokcancel", return_value=False):
            self.app.start_slit_sweep()
        self.assertFalse(self.app.awaiting)
        self.assertIsNone(self.app.sweep)
        self.assertFalse((Path(self.temp.name) / "slit_sweeps").exists())

    def test_background_unblock_cancel_stops_before_any_motion_or_file(self):
        fake, patch = self.icl_fixture()
        self.app.measure_background.set(True)
        self.app.vars["sweep_slit_min"].set(.2)
        self.app.vars["sweep_slit_max"].set(.2)
        reads = []
        original = self.app.reader.read_voltage
        def read(channel):
            reads.append(channel)
            return original(channel)
        self.app.reader.read_voltage = read
        with patch, mock.patch("spectral_calibration.gui.messagebox.askokcancel", side_effect=[True, False]):
            self.app.start_slit_sweep(); self.drain()
        self.assertEqual(len(reads), 4)
        self.assertIsNone(self.app.sweep)
        self.assertEqual(self.sweep_files(), [])
        self.assertIn("no comenzó", self.app.sweep_label.cget("text"))
        self.assertEqual(self.dialogs, [])
        for command in ("mono_moveToPosition", "mono_moveSlitMM", "mono_init"):
            self.assertNotIn(command, [c["command"] for c in fake.commands])

    def test_icl_background_is_measured_before_any_motion_and_verified_by_readback(self):
        fake, patch = self.icl_fixture()
        self.app.measure_background.set(True)
        self.app.vars["sweep_slit_min"].set(.2)
        self.app.vars["sweep_slit_max"].set(.2)
        moved = []
        original = self.app.reader.read_voltage
        def read(channel):
            moved.append(any(c["command"] in ("mono_moveToPosition", "mono_moveSlitMM") for c in fake.commands))
            return original(channel)
        self.app.reader.read_voltage = read
        with patch:
            self.app.start_slit_sweep(); self.drain()
        self.assertEqual(self.dialogs, [])
        self.assertEqual(moved[:4], [False]*4)
        self.assertTrue(all(moved[4:]))
        self.assertEqual(len(rows(self.app.sweep["csv"])), 3)
        background = json.loads(self.app.sweep["json"].read_text(encoding="utf-8"))["background"]
        for state in ("horiba_state", "horiba_state_after"):
            self.assertEqual((background[state]["slit_in_mm"], background[state]["output_port"]), (.1, "Lateral"))
        self.assertEqual(background["n"], 4)

    def test_icl_change_during_background_aborts_without_files(self):
        fake, patch = self.icl_fixture()
        self.app.measure_background.set(True)
        original = self.app.reader.read_voltage
        def read(channel):
            fake.mirrors[1] = 0
            return original(channel)
        self.app.reader.read_voltage = read
        with patch:
            self.app.start_slit_sweep(); self.drain()
        self.assertIsNone(self.app.sweep)
        self.assertEqual(self.sweep_files(), [])
        self.assertIn("medida de fondo", self.dialogs[0][1])
        self.assertNotIn("mono_moveToPosition", [c["command"] for c in fake.commands])
        self.assertNotIn("mono_moveMirror", [c["command"] for c in fake.commands])

    def test_icl_single_aperture_sweep_records_full_metadata_and_readback(self):
        fake, patch = self.icl_fixture()
        fake.offset = .001
        self.app.notes.insert("1.0", "Barrido simple en λ")
        with patch:
            self.app.start_slit_sweep(); self.drain()
        self.assertEqual(self.dialogs, [])
        self.assertEqual(self.app.sweep["state"], "completed")
        data = rows(self.app.sweep["csv"])
        self.assertEqual(len(data), 3)
        for row in data:
            self.assertAlmostEqual(float(row["wavelength_nm"])-float(row["wavelength_target_nm"]), .001)
            self.assertEqual((row["grating"], row["output_port"]), ("G1", "Lateral"))
            self.assertEqual((row["slit_in_read_mm"], row["slit_out_read_mm"]), ("0.1", "0.1"))
        commands = [c["command"] for c in fake.commands]
        self.assertEqual(commands.count("mono_moveToPosition"), 3)
        for command in ("mono_moveSlitMM", "mono_init", "mono_setPosition", "mono_moveMirror", "mono_moveGrating"):
            self.assertNotIn(command, commands)
        info = json.loads(self.app.sweep["json"].read_text(encoding="utf-8"))
        self.assertEqual(info["notes"], "Barrido simple en λ")
        horiba = info["horiba"]
        self.assertEqual(horiba["control"], "ICL")
        self.assertEqual(horiba["configuration"]["device"]["serialNumber"], "TEST")
        self.assertEqual(horiba["configuration"]["settings"]["move_timeout_s"], 180)
        self.assertEqual(horiba["configuration"]["settings"]["tolerance_nm"], .01)
        self.assertEqual(horiba["configuration"]["slit_ports"]["slit_in_mm"]["locationId"], 1)
        self.assertEqual(len(horiba["configuration"]["gratings"]), 3)
        initial = horiba["initial_state"]
        self.assertEqual((initial["grating"], initial["output_port"], initial["slit_in_mm"], initial["slit_out_mm"]),
                         ("G1", "Lateral", .1, .1))
        self.assertAlmostEqual(horiba["final_state"]["wavelength_nm"], 532.101)
        self.assertEqual(info["sr830"]["connection"]["identity"], "SR830_DOUBLE")
        self.assertEqual(info["sr830"]["acquisition"]["n"], 4)
        # Puertos detectados: la siguiente operación reutiliza la sesión ICL.
        self.assertEqual((self.app.vars["horiba_slit_in"].get(), self.app.vars["horiba_slit_out"].get()), ("1", "4"))

    def test_icl_sweep_moves_slits_before_wavelengths_per_configuration(self):
        fake, patch = self.icl_fixture()
        self.app.vars["sweep_mode"].set(SWEEP_MODES[1])
        self.app.vars["sweep_list_in"].set("0.2; 0.4")
        self.app.vars["sweep_list_out"].set("0.3; 0.4")
        with patch:
            self.app.start_slit_sweep(); self.drain()
        self.assertEqual(self.dialogs, [])
        slit_moves = [c["parameters"]["position"] for c in fake.commands if c["command"] == "mono_moveSlitMM"]
        self.assertEqual(slit_moves, [.2, .3, .4, .4])
        data = rows(self.app.sweep["csv"])
        self.assertEqual([(r["slit_in_read_mm"], r["slit_out_read_mm"]) for r in data],
                         [("0.2", "0.3")]*3 + [("0.4", "0.4")]*3)
        info = json.loads(self.app.sweep["json"].read_text(encoding="utf-8"))
        self.assertEqual(info["plan"]["slit_lists_mm"], dict(entrance="0.2; 0.4", exit="0.3; 0.4"))
        self.assertEqual(info["horiba"]["final_state"]["slit_in_mm"], .4)

    def test_icl_output_change_during_acquisition_stops_sweep(self):
        fake, patch = self.icl_fixture()
        original = self.app.reader.read_voltage
        def read(channel):
            value = original(channel)
            fake.mirrors[1] = 0
            return value
        self.app.reader.read_voltage = read
        with patch:
            self.app.start_slit_sweep(); self.drain()
        self.assertEqual(rows(self.app.sweep["csv"]), [])
        self.assertEqual(self.app.sweep["state"], "error")
        self.assertIn("output_port cambió", self.dialogs[0][1])
        self.assertNotIn("mono_moveMirror", [c["command"] for c in fake.commands])
        info = json.loads(self.app.sweep["json"].read_text(encoding="utf-8"))
        self.assertEqual(info["state"], "error")
        self.assertIn("final_state", info["horiba"])

    def test_icl_grating_external_change_stops_sweep(self):
        fake, patch = self.icl_fixture()
        original = self.app.reader.read_voltage
        def read(channel):
            value = original(channel)
            fake.grating = 1
            return value
        self.app.reader.read_voltage = read
        with patch:
            self.app.start_slit_sweep(); self.drain()
        self.assertEqual(rows(self.app.sweep["csv"]), [])
        self.assertIn("grating cambió", self.dialogs[0][1])
        self.assertNotIn("mono_moveGrating", [c["command"] for c in fake.commands])

    def test_failed_start_without_files_restores_previous_sweep_view(self):
        self.sim_sweep()
        previous = self.app.sweep
        fake, patch = self.icl_fixture()
        fake.initialized = False
        with patch:
            self.app.start_slit_sweep(); self.drain()
        self.assertIs(self.app.sweep, previous)
        self.assertEqual(len(self.sweep_files()), 1)
        self.assertIn("no comenzó", self.app.sweep_label.cget("text"))
        self.assertIn("inicializar", self.dialogs[-1][1])
        self.assertEqual(len(self.app.view_selector.cget("values")), 4)
        for command in ("mono_init", "mono_moveToPosition", "mono_moveSlitMM"):
            self.assertNotIn(command, [c["command"] for c in fake.commands])

    def test_csv_write_failure_stops_sweep_and_keeps_written_rows(self):
        self.sim_sweep_fields()
        original = slit_sweep._append_row
        calls = []
        def fail_second(path, row):
            calls.append(1)
            if len(calls) == 2:
                raise OSError("disco lleno")
            return original(path, row)
        with mock.patch("spectral_calibration.slit_sweep._append_row", side_effect=fail_second):
            self.app.start_slit_sweep(); self.drain()
        self.assertEqual(len(rows(self.app.sweep["csv"])), 1)
        self.assertEqual(self.app.sweep["state"], "error")
        self.assertEqual(self.app.sweep["written"], 1)
        self.assertIn("disco lleno", self.dialogs[0][1])
        self.assertEqual(str(self.app.save_button.cget("state")), "normal")

    def test_sweep_cancel_keeps_rows_and_allows_saving(self):
        self.sim_sweep_fields()
        self.app.vars["n"].set(20)
        self.app.vars["interval_s"].set(.01)
        self.app.start_slit_sweep()
        end = time.monotonic()+10
        path = None
        while time.monotonic() < end:
            self.root.update()
            files = self.sweep_files()
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
        self.assertIn("cancelado", self.app.sweep_label.cget("text"))
        self.assertEqual(self.app.sweep["state"], "cancelled")
        self.assertEqual(str(self.app.save_button.cget("state")), "normal")
        self.assertEqual(self.dialogs, [])

    def test_cancel_during_wait_keeps_gui_responsive(self):
        self.sim_sweep_fields()
        self.app.vars["wait_s"].set(10)
        self.app.start_slit_sweep()
        seen = []
        self.root.after(10, lambda: seen.append(self.app.awaiting))
        self.root.after(40, self.app.worker.cancel.set)
        self.drain()
        self.assertEqual(seen, [True])
        self.assertEqual(rows(self.app.sweep["csv"]), [])
        self.assertEqual(self.app.sweep["state"], "cancelled")

    # -------------------------------------------------------------- guardado
    def test_save_button_exports_csv_and_metadata_with_current_notes(self):
        self.assertEqual(str(self.app.save_button.cget("state")), "disabled")
        with self.assertRaisesRegex(ValueError, "No hay datos"):
            self.app.save_sweep_data()
        self.app.notes.insert("1.0", "notas iniciales")
        self.sim_sweep()
        self.assertEqual(str(self.app.save_button.cget("state")), "normal")
        self.app.notes.delete("1.0", "end")
        self.app.notes.insert("1.0", "notas corregidas tras el barrido")
        target = Path(self.temp.name) / "exportado" / "mi_barrido.csv"
        target.parent.mkdir()
        with mock.patch("spectral_calibration.gui.filedialog.asksaveasfilename", return_value=str(target)) as dialog:
            self.app.save_sweep_data(); self.drain()
        self.assertEqual(dialog.call_args[1]["initialfile"], self.app.sweep["csv"].name)
        self.assertEqual(target.read_bytes(), self.app.sweep["csv"].read_bytes())
        exported = json.loads(target.with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(exported["notes"], "notas corregidas tras el barrido")
        self.assertEqual(exported["csv"], "mi_barrido.csv")
        self.assertEqual(exported["backup_csv"], str(self.app.sweep["csv"]))
        for key in ("saved_utc", "horiba", "sr830", "plan", "configurations", "wavelengths_nm"):
            self.assertIn(key, exported)
        backup = json.loads(self.app.sweep["json"].read_text(encoding="utf-8"))
        self.assertEqual(backup["notes"], "notas corregidas tras el barrido")
        self.assertEqual(self.app.sweep["saved"], str(target))
        self.assertIn("guardado", self.app.summary_labels["sweep"].cget("text"))
        self.assertIn(str(target), self.app.sweep_info.cget("text"))
        self.assertEqual(self.dialogs, [])

    def test_save_dialog_cancel_writes_nothing(self):
        self.sim_sweep()
        before = sorted(Path(self.temp.name).rglob("*"))
        with mock.patch("spectral_calibration.gui.filedialog.asksaveasfilename", return_value=""):
            self.app.save_sweep_data()
        self.assertFalse(self.app.awaiting)
        self.assertEqual(sorted(Path(self.temp.name).rglob("*")), before)
        self.assertIsNone(self.app.sweep["saved"])

    def test_close_warns_about_unsaved_sweep(self):
        self.sim_sweep()
        with mock.patch.object(self.app, "_destroy") as destroy:
            with mock.patch("spectral_calibration.gui.messagebox.askyesno", return_value=False) as ask:
                self.app.close()
            destroy.assert_not_called()
            self.assertIn("no se guardó", ask.call_args[0][1])
            self.app.close()
            destroy.assert_called_once_with()
