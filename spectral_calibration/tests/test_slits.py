"""Rendijas ICL: dobles explícitos, sin comunicación ni motores reales."""
import json
import threading
import time
import unittest

from spectral_calibration.acquisition import AcquisitionCancelled
from spectral_calibration.horiba import HoribaError, HoribaSettings, ICLMonochromator
from spectral_calibration.tests.test_horiba import FakeICL


class SlitTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeICL()
        self.mono = ICLMonochromator(socket_factory=lambda *a, **kw: self.fake)

    def tearDown(self):
        self.mono.close()

    def moves(self):
        return [c for c in self.fake.commands if c["command"] == "mono_moveSlitMM"]

    def change_response(self, function):
        original = self.fake.recv
        def recv():
            response = json.loads(original())
            function(response)
            return json.dumps(response)
        self.fake.recv = recv

    def test_detects_documented_locations_limits_and_reads_mm(self):
        self.fake.slits = {1: 0., 4: 1.25}
        report = self.mono.read_slits()
        self.assertEqual((self.mono.settings.slit_in, self.mono.settings.slit_out), (1, 4))
        self.assertEqual(report["actual_mm"], dict(slit_in_mm=0., slit_out_mm=1.25))
        self.assertEqual(report["ports"]["slit_in_mm"]["maximum_mm"], 7)
        self.assertEqual(report["ports"]["slit_out_mm"]["minimum_mm"], 0)
        self.assertEqual(self.mono.snapshot()["slit_in_mm"], 0)
        self.assertEqual(self.mono.status.state, "ready")
        self.assertEqual(self.moves(), [])

    def test_entrance_and_exit_are_independent_and_use_mm_not_steps(self):
        first = self.mono.move_slits(entrance_mm=.2)
        self.assertEqual(first["actual_mm"], dict(slit_in_mm=.2, slit_out_mm=.1))
        second = self.mono.move_slits(exit_mm=.3)
        self.assertEqual(second["actual_mm"], dict(slit_in_mm=.2, slit_out_mm=.3))
        self.assertEqual([c["parameters"] for c in self.moves()], [
            dict(index=0, locationId=1, position=.2), dict(index=0, locationId=4, position=.3)])
        self.assertNotIn("mono_moveSlit", [c["command"] for c in self.fake.commands])
        self.assertEqual(second["state"], "confirmed")

    def test_both_moves_are_sequential_and_read_back_after_each(self):
        report = self.mono.move_slits(entrance_mm=.2, exit_mm=.4)
        self.assertEqual(report["confirmed_moves"], ["slit_in_mm", "slit_out_mm"])
        cmds = [c["command"] for c in self.fake.commands]
        positions = [i for i, command in enumerate(cmds) if command == "mono_moveSlitMM"]
        between = cmds[positions[0]+1:positions[1]]
        self.assertGreaterEqual(between.count("mono_isBusy"), 2)
        self.assertEqual(between.count("mono_getSlitPositionInMM"), 2)
        self.assertEqual(self.mono.slit_positions, report["actual_mm"])

    def test_minimum_maximum_and_comma_input(self):
        self.mono.move_slits(entrance_mm=0, exit_mm=7)
        self.assertEqual(self.fake.slits, {1: 0., 4: 7.})
        report = self.mono.move_slits(entrance_mm="0,125")
        self.assertEqual(report["actual_mm"]["slit_in_mm"], .125)

    def test_invalid_requests_send_no_commands(self):
        for value in (-.1, 7.001, "nan", "inf", "texto"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.mono.move_slits(entrance_mm=value)
        with self.assertRaises(ValueError):
            self.mono.move_slits()
        self.assertEqual(self.fake.commands, [])

    def test_2mm_limit_is_checked_for_both_before_first_move(self):
        self.fake.ports[1]["slitType"] = 1
        with self.assertRaisesRegex(ValueError, "0–2"):
            self.mono.move_slits(entrance_mm=.2, exit_mm=3)
        self.assertEqual(self.moves(), [])
        self.assertEqual(self.fake.slits, {1: .1, 4: .1})

    def test_unknown_missing_duplicate_and_ambiguous_ports_block_motion(self):
        for ports in ([dict(locationId=1, slitType=99), dict(locationId=4, slitType=2)],
                      [dict(locationId=1, slitType=2)],
                      [dict(locationId=1, slitType=2), dict(locationId=1, slitType=2)],
                      [dict(locationId=i, slitType=2) for i in (1, 2, 4)]):
            with self.subTest(ports=ports):
                self.mono.drop()
                self.fake.ports = ports
                with self.assertRaises(HoribaError):
                    self.mono.move_slits(entrance_mm=.2)
                self.assertEqual(self.moves(), [])

    def test_explicit_selection_uses_only_installed_entrance_and_exit(self):
        self.fake.ports = [dict(locationId=i, slitType=2) for i in (1, 2, 3, 4)]
        self.fake.slits = {i: .1 for i in (1, 2, 3, 4)}
        self.mono.settings.slit_in, self.mono.settings.slit_out = 2, 3
        self.mono.move_slits(entrance_mm=.25, exit_mm=.5)
        self.assertEqual([c["parameters"]["locationId"] for c in self.moves()], [2, 3])
        self.assertEqual((self.fake.slits[1], self.fake.slits[4]), (.1, .1))
        with self.assertRaises(ValueError):
            HoribaSettings(slit_in=4, slit_out=1).validate()

    def test_busy_and_uninitialized_prevent_movement_and_homing(self):
        for attr in ("busy", "initialized"):
            with self.subTest(attr=attr):
                setattr(self.fake, attr, attr == "busy")
                with self.assertRaises(HoribaError):
                    self.mono.move_slits(entrance_mm=.2)
                self.assertEqual(self.moves(), [])
                setattr(self.fake, attr, attr == "initialized")
        self.assertNotIn("mono_init", [c["command"] for c in self.fake.commands])

    def test_ack_without_position_change_is_not_success(self):
        self.fake.ignore_slit_move = True
        with self.assertRaisesRegex(HoribaError, "solicitado"):
            self.mono.move_slits(entrance_mm=.2)
        self.assertEqual(self.mono.last_slit_move["state"], "error")
        self.assertEqual(self.mono.slit_positions, {})
        self.assertEqual(len(self.moves()), 1)

    def test_change_to_other_slit_aborts_before_next_move(self):
        self.fake.cross_slit_move = True
        with self.assertRaisesRegex(HoribaError, "otra rendija"):
            self.mono.move_slits(entrance_mm=.2, exit_mm=.4)
        self.assertEqual(len(self.moves()), 1)

    def test_timeout_discards_readback_without_retry(self):
        self.fake.slit_busy_polls = 1000
        self.mono.settings.slit_timeout_s = .02
        with self.assertRaisesRegex(HoribaError, "Timeout de ajuste"):
            self.mono.move_slits(entrance_mm=.2)
        self.assertEqual(self.mono.status.state, "error")
        self.assertFalse(self.mono.opened)
        self.assertEqual(self.mono.slit_positions, {})
        self.assertEqual(len(self.moves()), 1)

    def test_partial_failure_records_first_move_without_rollback(self):
        def fail_exit(response):
            if response["command"] == "mono_moveSlitMM" and self.fake.pending["parameters"]["locationId"] == 4:
                response["errors"] = ["[E];-510;rechazo simulado"]
        self.change_response(fail_exit)
        with self.assertRaises(HoribaError):
            self.mono.move_slits(entrance_mm=.2, exit_mm=.4)
        self.assertEqual(self.mono.last_slit_move["confirmed_moves"], ["slit_in_mm"])
        self.assertEqual(self.mono.last_slit_move["state"], "error")
        self.assertEqual(len(self.moves()), 2)
        self.assertEqual(self.fake.slits[1], .2)

    def test_lost_ack_and_reconnect_do_not_resend_move(self):
        original = self.fake.recv
        def recv():
            if self.fake.pending["command"] == "mono_moveSlitMM":
                raise TimeoutError("acuse perdido")
            return original()
        self.fake.recv = recv
        with self.assertRaisesRegex(HoribaError, "puede continuar"):
            self.mono.move_slits(entrance_mm=.2)
        self.assertEqual(len(self.moves()), 1)
        self.fake.recv = original
        self.mono.read_slits()
        self.assertEqual(len(self.moves()), 1)
        self.assertEqual(self.mono.slit_positions, dict(slit_in_mm=.1, slit_out_mm=.1))

    def test_invalid_status_readback_and_configuration_never_succeed(self):
        for command, results in (("mono_getConfig", {"configuration": "invalido"}),
                                 ("mono_getSlitPositionInMM", {"position": "nan"})):
            with self.subTest(command=command):
                self.mono.drop()
                self.fake = FakeICL()
                def corrupt(response):
                    if response["command"] == command:
                        response["results"] = results
                self.change_response(corrupt)
                with self.assertRaises(HoribaError):
                    self.mono.move_slits(entrance_mm=.2)
                self.assertEqual(self.moves(), [])

    def test_cancellation_before_and_after_send(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(AcquisitionCancelled):
            self.mono.move_slits(entrance_mm=.2, cancel=cancel)
        self.assertEqual(self.fake.commands, [])
        cancel.clear()
        original = self.fake.send
        def send(payload):
            original(payload)
            if self.fake.pending["command"] == "mono_moveSlitMM":
                cancel.set()
        self.fake.send = send
        with self.assertRaisesRegex(AcquisitionCancelled, "puede continuar"):
            self.mono.move_slits(entrance_mm=.2, exit_mm=.4, cancel=cancel)
        self.assertEqual(len(self.moves()), 1)
        self.assertEqual(self.mono.slit_positions, {})

    def test_final_readback_timeout_is_not_confirmed(self):
        moved = []
        def delay(response):
            if response["command"] == "mono_moveSlitMM":
                moved.append(True)
            if moved and response["command"] == "mono_getSlitPositionInMM":
                time.sleep(.04)
        self.fake.slit_busy_polls = 0
        self.change_response(delay)
        self.mono.settings.slit_timeout_s = .02
        with self.assertRaisesRegex(HoribaError, "timeout"):
            self.mono.move_slits(entrance_mm=.2)
        self.assertEqual(self.mono.last_slit_move["state"], "error")

    def test_quantization_is_reported_without_replacing_requested_value(self):
        def quantize(response):
            if response["command"] == "mono_getSlitPositionInMM" and self.fake.slits[1] == .123:
                response["results"]["position"] = .125 if self.fake.pending["parameters"]["locationId"] == 1 else .1
        self.change_response(quantize)
        report = self.mono.move_slits(entrance_mm=.123)
        self.assertEqual(report["requested_mm"]["slit_in_mm"], .123)
        self.assertEqual(report["actual_mm"]["slit_in_mm"], .125)

    def test_reopen_recovers_both_actual_values_and_metadata(self):
        self.mono.move_slits(entrance_mm=.25, exit_mm=.5)
        self.mono.close()
        other = ICLMonochromator(socket_factory=lambda *a, **kw: self.fake)
        try:
            report = other.read_slits()
            self.assertEqual(report["actual_mm"], dict(slit_in_mm=.25, slit_out_mm=.5))
            self.assertEqual(self.mono.describe()["last_slit_move"]["state"], "confirmed")
        finally:
            other.close()

    def test_concurrent_moves_and_initialization_do_not_send_extra_commands(self):
        started, release = threading.Event(), threading.Event()
        original = self.fake.recv
        results, errors = [], []
        def recv():
            if self.fake.pending["command"] == "mono_moveSlitMM":
                started.set()
                if not release.wait(3):
                    raise TimeoutError("Test sin liberar")
            return original()
        def run():
            try:
                results.append(self.mono.move_slits(entrance_mm=.2))
            except Exception as exc:
                errors.append(exc)
        self.fake.recv = recv
        thread = threading.Thread(target=run)
        thread.start()
        try:
            self.assertTrue(started.wait(2))
            with self.assertRaisesRegex(HoribaError, "en curso"):
                self.mono.move_slits(exit_mm=.3)
            with self.assertRaisesRegex(HoribaError, "en curso"):
                self.mono.initialize()
        finally:
            release.set()
            thread.join(3)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 1)
        self.assertEqual(len(self.moves()), 1)

    def test_invalid_busy_or_lost_initialized_flag_blocks_second_move(self):
        for command, results in (("mono_isBusy", {"busy": "false"}),
                                 ("mono_isInitialized", {"initialized": False})):
            with self.subTest(command=command):
                self.mono.drop()
                self.fake = FakeICL()
                def corrupt(response):
                    if response["command"] == command and self.moves():
                        response["results"] = results
                self.change_response(corrupt)
                with self.assertRaises(HoribaError):
                    self.mono.move_slits(entrance_mm=.2, exit_mm=.3)
                self.assertEqual(len(self.moves()), 1)
                self.assertEqual(self.mono.last_slit_move["state"], "error")

    def test_configuration_can_be_json_string_and_oversized_readback_is_rejected(self):
        def stringify(response):
            if response["command"] == "mono_getConfig":
                response["results"]["configuration"] = json.dumps(response["results"]["configuration"])
        self.change_response(stringify)
        self.mono.read_slits()
        self.fake.slits[1] = 8
        with self.assertRaisesRegex(HoribaError, "fuera del rango"):
            self.mono.connect()
        self.assertEqual(self.mono.status.state, "error")
        self.assertEqual(self.mono.slit_positions, {})
