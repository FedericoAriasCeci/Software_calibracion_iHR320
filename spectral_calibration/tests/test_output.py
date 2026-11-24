"""Selector de salida ICL: pruebas simuladas, sin mover un instrumento físico."""
import json
import threading
import time
import unittest
from unittest import mock

from spectral_calibration.acquisition import AcquisitionCancelled
from spectral_calibration.horiba import HoribaError, ICLMonochromator
from spectral_calibration.tests.test_horiba import FakeICL


class OutputTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeICL()
        self.mono = ICLMonochromator(socket_factory=lambda *a, **kw: self.fake)

    def tearDown(self):
        self.mono.close()

    def moves(self):
        return [c for c in self.fake.commands if c["command"] == "mono_moveMirror"]

    def change_response(self, function):
        original = self.fake.recv
        def recv():
            response = json.loads(original())
            function(response)
            return json.dumps(response)
        self.fake.recv = recv

    def test_read_uses_documented_exit_index_and_no_motion(self):
        self.assertIsNone(self.mono.output_position)
        report = self.mono.read_output()
        self.assertEqual(report["position"], "Lateral")
        self.assertEqual(report["position_code"], 1)
        queries = [c for c in self.fake.commands if c["command"] == "mono_getMirrorPosition"]
        self.assertEqual(queries[0]["parameters"], dict(index=0, locationId=1))
        self.assertEqual(self.fake.mirror_config, [dict(locationId=2)])
        self.assertEqual(self.moves(), [])
        self.assertNotIn("mono_init", [c["command"] for c in self.fake.commands])

    def test_both_directions_confirm_exact_position_and_leave_other_parts_alone(self):
        before = (dict(self.fake.slits), self.fake.grating, self.fake.position, self.fake.mirrors[0])
        for target in ("Axial", "Lateral"):
            report = self.mono.move_output(target)
            self.assertTrue(report["movement_sent"])
            self.assertEqual(report["actual"], target)
            self.assertEqual(report["state"], "confirmed")
            self.assertEqual(self.mono.output_position, target)
        self.assertEqual([c["parameters"] for c in self.moves()], [
            dict(index=0, locationId=1, position=0), dict(index=0, locationId=1, position=1)])
        self.assertEqual(before, (self.fake.slits, self.fake.grating, self.fake.position, self.fake.mirrors[0]))

    def test_already_selected_is_queried_without_repeating_motion(self):
        report = self.mono.move_output("Lateral")
        self.assertFalse(report["movement_sent"])
        self.assertEqual(report["before"], "Lateral")
        self.assertEqual(report["actual"], "Lateral")
        self.assertEqual(self.moves(), [])

    def test_invalid_requests_do_not_connect(self):
        for value in ("", "Frontal", "axial", 0, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.mono.move_output(value)
        self.assertEqual(self.fake.commands, [])

    def test_busy_or_uninitialized_prevent_movement(self):
        for attr in ("busy", "initialized"):
            with self.subTest(attr=attr):
                setattr(self.fake, attr, attr == "busy")
                with self.assertRaises(HoribaError):
                    self.mono.move_output("Axial")
                setattr(self.fake, attr, attr == "initialized")
        self.assertEqual(self.moves(), [])
        self.assertNotIn("mono_init", [c["command"] for c in self.fake.commands])

    def test_unknown_and_malformed_positions_never_default_to_axial(self):
        for value in (2, -1, None, "0", True, 1.0):
            with self.subTest(value=value):
                self.mono.drop()
                self.fake = FakeICL()
                def corrupt(response):
                    if response["command"] == "mono_getMirrorPosition":
                        response["results"]["position"] = value
                self.change_response(corrupt)
                with self.assertRaisesRegex(HoribaError, "desconocida"):
                    self.mono.move_output("Axial")
                self.assertIsNone(self.mono.output_position)
                self.assertEqual(self.moves(), [])

    def test_getter_unsupported_does_not_send_a_move(self):
        def reject(response):
            if response["command"] == "mono_getMirrorPosition":
                response["errors"] = ["[E];-510;unsupported"]
        self.change_response(reject)
        with self.assertRaisesRegex(HoribaError, "-510"):
            self.mono.move_output("Axial")
        self.assertEqual(self.moves(), [])

    def test_ack_without_readback_change_is_not_confirmed(self):
        self.fake.ignore_output_move = True
        with self.assertRaisesRegex(HoribaError, "Salida solicitada: Axial; leída: Lateral"):
            self.mono.move_output("Axial")
        self.assertEqual(self.mono.last_output_move["state"], "error")
        self.assertEqual(self.mono.last_output_move["actual"], "Lateral")
        self.assertIsNone(self.mono.output_position)
        self.assertEqual(len(self.moves()), 1)

    def test_rejected_move_logs_request_and_does_not_retry(self):
        def reject(response):
            if response["command"] == "mono_moveMirror":
                response["errors"] = ["[E];-510;rechazo simulado"]
        self.change_response(reject)
        with self.assertLogs("spectral_calibration.horiba", level="WARNING") as logs:
            with self.assertRaisesRegex(HoribaError, "-510"):
                self.mono.move_output("Axial")
        self.assertIn("espejo=1", " ".join(logs.output))
        self.assertEqual(len(self.moves()), 1)
        self.assertIsNone(self.mono.output_position)

    def test_timeout_discards_state_and_does_not_retry(self):
        self.fake.output_busy_polls = 1000
        self.mono.settings.output_timeout_s = .02
        with self.assertRaisesRegex(HoribaError, "Timeout del selector"):
            self.mono.move_output("Axial")
        self.assertEqual(self.mono.status.state, "error")
        self.assertIsNone(self.mono.output_position)
        self.assertEqual(len(self.moves()), 1)

    def test_lost_ack_then_read_recovery_does_not_resend(self):
        original = self.fake.recv
        def recv():
            if self.fake.pending["command"] == "mono_moveMirror":
                raise TimeoutError("acuse perdido")
            return original()
        self.fake.recv = recv
        with self.assertRaisesRegex(HoribaError, "puede continuar"):
            self.mono.move_output("Axial")
        self.fake.recv = original
        self.assertEqual(self.mono.read_output()["position"], "Lateral")
        self.assertEqual(len(self.moves()), 1)

    def test_cancellation_before_and_after_send(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(AcquisitionCancelled):
            self.mono.move_output("Axial", cancel)
        self.assertEqual(self.fake.commands, [])
        cancel.clear()
        original = self.fake.send
        def send(payload):
            original(payload)
            if self.fake.pending["command"] == "mono_moveMirror":
                cancel.set()
        self.fake.send = send
        with self.assertRaisesRegex(AcquisitionCancelled, "puede continuar"):
            self.mono.move_output("Axial", cancel)
        self.assertIsNone(self.mono.output_position)
        self.assertEqual(len(self.moves()), 1)

    def test_final_readback_is_inside_timeout(self):
        moved = []
        self.fake.output_busy_polls = 0
        def delay(response):
            if response["command"] == "mono_moveMirror":
                moved.append(True)
            if moved and response["command"] == "mono_getMirrorPosition":
                time.sleep(.04)
        self.change_response(delay)
        self.mono.settings.output_timeout_s = .02
        with self.assertRaisesRegex(HoribaError, "timeout"):
            self.mono.move_output("Axial")
        self.assertEqual(self.mono.last_output_move["state"], "error")

    def test_invalid_busy_or_lost_initialization_does_not_report_success(self):
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
                    self.mono.move_output("Axial")
                self.assertEqual(self.mono.last_output_move["state"], "error")
                self.assertIsNone(self.mono.output_position)

    def test_reopening_queries_physical_state_not_saved_target(self):
        self.mono.move_output("Axial")
        self.mono.close()
        self.assertIsNone(self.mono.output_position)
        self.fake.mirrors[1] = 1
        other = ICLMonochromator(socket_factory=lambda *a, **kw: self.fake)
        try:
            self.assertEqual(other.read_output()["position"], "Lateral")
            self.assertEqual(other.snapshot()["output_port"], "Lateral")
        finally:
            other.close()

    def test_snapshot_monitors_output_after_valid_query_and_metadata_is_explicit(self):
        self.assertNotIn("output_port", self.mono.connect())
        self.mono.read_output()
        self.assertEqual(self.mono.snapshot()["output_port"], "Lateral")
        self.fake.mirrors[1] = 0
        self.assertEqual(self.mono.snapshot()["output_port"], "Axial")
        self.assertEqual(self.mono.describe()["output_port"], "Axial")
        self.mono.drop()
        self.assertIsNone(self.mono.output_position)
        self.assertEqual(self.mono.connect()["output_port"], "Axial")

    def test_concurrent_accessory_and_init_commands_are_rejected(self):
        started, release = threading.Event(), threading.Event()
        original = self.fake.recv
        results, errors = [], []
        def recv():
            if self.fake.pending["command"] == "mono_moveMirror":
                started.set()
                if not release.wait(3):
                    raise TimeoutError("Test sin liberar")
            return original()
        def run():
            try:
                results.append(self.mono.move_output("Axial"))
            except Exception as exc:
                errors.append(exc)
        self.fake.recv = recv
        thread = threading.Thread(target=run)
        thread.start()
        try:
            self.assertTrue(started.wait(2))
            self.assertIsNone(self.mono.output_position)
            for operation in (lambda: self.mono.move_output("Lateral"), self.mono.initialize,
                              lambda: self.mono.move_slits(entrance_mm=.2)):
                with self.assertRaisesRegex(HoribaError, "en curso"):
                    operation()
        finally:
            release.set()
            thread.join(3)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 1)
        self.assertEqual(len(self.moves()), 1)

    def test_unavailable_connection_and_unsupported_api_are_reported(self):
        self.mono.socket_factory = mock.Mock(side_effect=OSError("ICL ausente"))
        with self.assertRaises(HoribaError):
            self.mono.read_output()
        self.mono.socket_factory = lambda *a, **kw: self.fake
        def corrupt(response):
            if response["command"] == "icl_info":
                response["results"]["nodeApiVersion"] = 200
        self.change_response(corrupt)
        with self.assertRaisesRegex(HoribaError, "API 300"):
            self.mono.move_output("Axial")
        self.assertEqual(self.moves(), [])
