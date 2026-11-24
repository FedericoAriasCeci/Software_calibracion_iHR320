"""Torreta y coherencia de λ con respuestas simuladas; no motores físicos."""
import json
import threading
import time
import unittest

from spectral_calibration.acquisition import AcquisitionCancelled
from spectral_calibration.horiba import HoribaError, HoribaSettings, ICLMonochromator
from spectral_calibration.tests.test_horiba import FakeICL


class GratingTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeICL()
        self.mono = ICLMonochromator(socket_factory=lambda *a, **kw: self.fake)

    def tearDown(self):
        self.mono.close()

    def commands(self, name):
        return [c for c in self.fake.commands if c["command"] == name]

    def change_response(self, function):
        original = self.fake.recv
        def recv():
            response = json.loads(original())
            function(response)
            return json.dumps(response)
        self.fake.recv = recv

    def test_catalog_is_reported_without_inventing_blaze_or_moving(self):
        report = self.mono.read_gratings()
        self.assertEqual([g["number"] for g in report["gratings"]], [1, 2, 3])
        self.assertEqual([g["grooves_per_mm"] for g in report["gratings"]], [1200., 600., 300.])
        self.assertEqual(report["gratings"][0]["blaze_reported"], 0)
        self.assertNotIn("blaze_nm", report["gratings"][0])
        self.assertEqual(report["number"], 1)
        self.assertEqual(report["wavelength_nm"], 532.)
        self.assertEqual(self.commands("mono_moveGrating"), [])
        self.assertEqual(self.commands("mono_moveToPosition"), [])

    def test_three_positions_map_to_zero_based_indices_and_confirm_wavelength(self):
        for target in (2, 3, 1):
            report = self.mono.move_grating(target, 532.)
            self.assertEqual(report["actual"]["grating"], "G{}".format(target))
            self.assertEqual(report["actual"]["wavelength_nm"], 532.)
            self.assertTrue(report["wavelength_movement_sent"])
            self.assertFalse(self.mono.grating_change_pending)
        self.assertEqual([c["parameters"]["position"] for c in self.commands("mono_moveGrating")], [1, 2, 0])
        self.assertEqual(len(self.commands("mono_moveToPosition")), 3)
        self.assertEqual(self.commands("mono_setPosition"), [])

    def test_turret_finishes_and_is_read_before_wavelength_is_commanded(self):
        self.mono.move_grating(2, 540.)
        commands = [c["command"] for c in self.fake.commands]
        first, second = commands.index("mono_moveGrating"), commands.index("mono_moveToPosition")
        between = commands[first+1:second]
        self.assertGreaterEqual(between.count("mono_isBusy"), 2)
        self.assertIn("mono_getGratingPosition", between)
        self.assertIn("mono_getPosition", between)

    def test_changed_or_reset_wavelength_is_observed_then_explicitly_set(self):
        for reset in (0., 1064.):
            with self.subTest(reset=reset):
                self.mono.drop()
                self.fake = FakeICL()
                self.fake.after_turret_nm = reset
                report = self.mono.move_grating(2, 532.)
                self.assertEqual(report["after_turret_wavelength_nm"], reset)
                self.assertEqual(report["actual"]["wavelength_nm"], 532.)

    def test_already_selected_skips_turret_and_only_moves_wavelength_if_needed(self):
        report = self.mono.move_grating(1, 532.)
        self.assertFalse(report["turret_movement_sent"])
        self.assertFalse(report["wavelength_movement_sent"])
        report = self.mono.move_grating(1, 540.)
        self.assertFalse(report["turret_movement_sent"])
        self.assertTrue(report["wavelength_movement_sent"])
        self.assertEqual(self.commands("mono_moveGrating"), [])

    def test_invalid_targets_do_not_connect(self):
        for pos, nm in ((0, 532), (4, 532), (1.5, 532), (1, "nan"), (1, 0), (1, 2000)):
            with self.subTest(pos=pos, nm=nm), self.assertRaises(ValueError):
                self.mono.move_grating(pos, nm)
        self.assertEqual(self.fake.commands, [])

    def test_uninstalled_position_is_rejected_before_moving(self):
        self.fake.gratings = [self.fake.gratings[0], self.fake.gratings[2]]
        with self.assertRaisesRegex(ValueError, "no está configurado"):
            self.mono.move_grating(2, 532)
        self.assertEqual(self.commands("mono_moveGrating"), [])
        self.assertEqual(self.commands("mono_moveToPosition"), [])

    def test_malformed_empty_or_duplicate_catalogs_block_motion(self):
        for catalog in ([], [dict(positionIndex=3)], [dict(positionIndex=True)],
                        [dict(positionIndex=0), dict(positionIndex=0)],
                        [dict(positionIndex=0, grooveDensity="nan")]):
            with self.subTest(catalog=catalog):
                self.mono.drop()
                self.fake.gratings = catalog
                with self.assertRaises(HoribaError):
                    self.mono.move_grating(2, 532)
                self.assertEqual(self.commands("mono_moveGrating"), [])

    def test_unknown_current_grating_does_not_default_to_first(self):
        self.fake.grating = 9
        with self.assertRaisesRegex(HoribaError, "desconocida"):
            self.mono.read_gratings()
        self.assertIsNone(self.mono.grating_position)

    def test_busy_or_uninitialized_do_not_move_or_initialize(self):
        for attr in ("busy", "initialized"):
            with self.subTest(attr=attr):
                setattr(self.fake, attr, attr == "busy")
                with self.assertRaises(HoribaError):
                    self.mono.move_grating(2, 532)
                setattr(self.fake, attr, attr == "initialized")
        self.assertEqual(self.commands("mono_moveGrating"), [])
        self.assertEqual(self.commands("mono_init"), [])

    def test_ack_without_turret_change_does_not_send_wavelength(self):
        self.fake.ignore_grating_move = True
        with self.assertRaisesRegex(HoribaError, "cambio sin confirmar"):
            self.mono.move_grating(2, 532)
        self.assertEqual(self.commands("mono_moveToPosition"), [])
        self.assertTrue(self.mono.grating_change_pending)
        self.assertIsNone(self.mono.grating_position)

    def test_turret_timeout_blocks_future_acquisition_without_retry(self):
        self.fake.grating_busy_polls = 1000
        self.mono.settings.grating_timeout_s = .02
        with self.assertRaisesRegex(HoribaError, "Timeout de torreta"):
            self.mono.move_grating(2, 532)
        with self.assertRaisesRegex(HoribaError, "pendiente"):
            self.mono.move_to(532)
        self.assertEqual(len(self.commands("mono_moveGrating")), 1)
        self.assertEqual(self.commands("mono_moveToPosition"), [])

    def test_failed_wavelength_is_pending_until_explicit_recovery(self):
        self.fake.offset = .1
        with self.assertRaisesRegex(HoribaError, "tolerancia"):
            self.mono.move_grating(2, 532)
        self.assertTrue(self.mono.grating_change_pending)
        self.fake.offset = 0
        self.assertTrue(self.mono.read_gratings()["grating_change_pending"])
        self.mono.read_slits()
        self.mono.read_output()
        self.assertEqual(self.mono.status.state, "error")
        with self.assertRaisesRegex(HoribaError, "pendiente"):
            self.mono.move_to(532)
        report = self.mono.move_grating(2, 532)
        self.assertFalse(report["turret_movement_sent"])
        self.assertTrue(report["wavelength_movement_sent"])
        self.assertFalse(self.mono.grating_change_pending)
        self.assertEqual(self.mono.status.state, "ready")

    def test_lost_turret_ack_is_not_retried_and_read_does_not_clear_pending(self):
        original = self.fake.recv
        def recv():
            if self.fake.pending["command"] == "mono_moveGrating":
                raise TimeoutError("acuse perdido")
            return original()
        self.fake.recv = recv
        with self.assertRaises(HoribaError):
            self.mono.move_grating(2, 532)
        self.fake.recv = original
        self.mono.read_gratings()
        self.assertEqual(len(self.commands("mono_moveGrating")), 1)
        self.assertTrue(self.mono.grating_change_pending)

    def test_rejected_command_is_logged_without_retry(self):
        def reject(response):
            if response["command"] == "mono_moveGrating":
                response["errors"] = ["[E];-510;unsupported"]
        self.change_response(reject)
        with self.assertLogs("spectral_calibration.horiba", level="WARNING") as logs:
            with self.assertRaisesRegex(HoribaError, "-510"):
                self.mono.move_grating(2, 532)
        self.assertIn("solicitud=2", " ".join(logs.output))
        self.assertEqual(len(self.commands("mono_moveGrating")), 1)

    def test_cancel_before_send_and_between_turret_and_wavelength(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(AcquisitionCancelled):
            self.mono.move_grating(2, 532, cancel)
        self.assertEqual(self.fake.commands, [])
        cancel.clear()
        original = self.fake.send
        def send(payload):
            original(payload)
            if self.fake.pending["command"] == "mono_getPosition" and self.commands("mono_moveGrating"):
                cancel.set()
        self.fake.send = send
        with self.assertRaisesRegex(AcquisitionCancelled, "antes de ajustar λ"):
            self.mono.move_grating(2, 532, cancel)
        self.assertEqual(self.commands("mono_moveToPosition"), [])
        self.assertTrue(self.mono.grating_change_pending)

    def test_cancellation_during_wavelength_keeps_spectral_state_pending(self):
        cancel = threading.Event()
        original = self.fake.send
        def send(payload):
            original(payload)
            if self.fake.pending["command"] == "mono_moveToPosition":
                cancel.set()
        self.fake.send = send
        with self.assertRaises(AcquisitionCancelled):
            self.mono.move_grating(2, 532, cancel)
        self.assertTrue(self.mono.grating_change_pending)

    def test_changed_grating_during_wavelength_cannot_be_confirmed(self):
        original = self.fake.send
        def send(payload):
            original(payload)
            if self.fake.pending["command"] == "mono_moveToPosition":
                self.fake.grating = 0
        self.fake.send = send
        with self.assertRaisesRegex(HoribaError, "cambió durante"):
            self.mono.move_grating(2, 532)
        self.assertTrue(self.mono.grating_change_pending)

    def test_zero_wavelength_can_be_read_and_repaired(self):
        self.fake.position = 0
        self.assertEqual(self.mono.read_gratings()["wavelength_nm"], 0)
        self.assertTrue(self.mono.grating_change_pending)
        report = self.mono.move_grating(1, 532)
        self.assertEqual(report["actual"]["wavelength_nm"], 532)
        self.assertFalse(self.mono.grating_change_pending)

    def test_fresh_controller_reads_actual_selected_grating_and_metadata(self):
        self.mono.move_grating(3, 540)
        self.mono.close()
        other = ICLMonochromator(socket_factory=lambda *a, **kw: self.fake)
        try:
            report = other.read_gratings()
            self.assertEqual(report["number"], 3)
            self.assertEqual(other.snapshot()["grating_info"]["grooves_per_mm"], 300)
            self.assertEqual(len(other.describe()["gratings"]), 3)
        finally:
            other.close()

    def test_concurrent_accessories_and_init_are_rejected_during_turret_move(self):
        started, release = threading.Event(), threading.Event()
        original = self.fake.recv
        errors = []
        def recv():
            if self.fake.pending["command"] == "mono_moveGrating":
                started.set()
                if not release.wait(3):
                    raise TimeoutError("Test sin liberar")
            return original()
        def run():
            try:
                self.mono.move_grating(2, 532)
            except Exception as exc:
                errors.append(exc)
        self.fake.recv = recv
        thread = threading.Thread(target=run)
        thread.start()
        try:
            self.assertTrue(started.wait(2))
            for op in (self.mono.initialize, lambda: self.mono.move_output("Axial"),
                       lambda: self.mono.move_slits(entrance_mm=.2), lambda: self.mono.move_grating(3, 532)):
                with self.assertRaisesRegex(HoribaError, "en curso"):
                    op()
        finally:
            release.set()
            thread.join(3)
        self.assertEqual(errors, [])
        self.assertEqual(len(self.commands("mono_moveGrating")), 1)

    def test_invalid_busy_and_initialization_after_send_block_wavelength(self):
        for command, results in (("mono_isBusy", {"busy": "false"}), ("mono_isInitialized", {"initialized": False})):
            with self.subTest(command=command):
                self.mono.drop()
                self.fake = FakeICL()
                def corrupt(response):
                    if response["command"] == command and self.commands("mono_moveGrating"):
                        response["results"] = results
                self.change_response(corrupt)
                with self.assertRaises(HoribaError):
                    self.mono.move_grating(2, 532)
                self.assertEqual(self.commands("mono_moveToPosition"), [])
                self.assertTrue(self.mono.grating_change_pending)

    def test_json_configuration_and_missing_optional_properties(self):
        self.fake.gratings = [dict(positionIndex=0)]
        def stringify(response):
            if response["command"] == "mono_getConfig":
                response["results"]["configuration"] = json.dumps(response["results"]["configuration"])
        self.change_response(stringify)
        report = self.mono.read_gratings()
        self.assertIsNone(report["gratings"][0]["grooves_per_mm"])
        self.assertIsNone(report["gratings"][0]["blaze_reported"])
        self.assertEqual(report["gratings"][0]["description"], "")

    def test_unsupported_api_prevents_grating_commands(self):
        def corrupt(response):
            if response["command"] == "icl_info":
                response["results"]["nodeApiVersion"] = 200
        self.change_response(corrupt)
        with self.assertRaisesRegex(HoribaError, "API 300"):
            self.mono.move_grating(2, 532)
        self.assertEqual(self.commands("mono_moveGrating"), [])

    def test_observed_g2_readback_difference_requires_explicit_tolerance_choice(self):
        # Reproduce la diferencia informada por ICL en el ensayo real; no es
        # una especificación de precisión óptica ni de resolución mecánica.
        target = 532.6005249023438
        self.fake.offset = 532.6220703125-target
        with self.assertRaisesRegex(HoribaError, "tolerancia"):
            self.mono.move_grating(2, target)
        self.assertTrue(self.mono.grating_change_pending)
        self.mono.settings.tolerance_nm = .05
        report = self.mono.move_grating(2, target)
        self.assertEqual(report["actual"]["wavelength_nm"], 532.6220703125)
        self.assertEqual(report["target_nm"], target)
        self.assertFalse(report["turret_movement_sent"])
        self.assertFalse(self.mono.grating_change_pending)
        self.assertEqual(HoribaSettings().tolerance_nm, .01)
