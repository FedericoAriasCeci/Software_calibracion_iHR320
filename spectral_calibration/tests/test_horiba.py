"""Contrato ICL y barridos: sin motores ni licencias simuladas como reales."""
import json
import threading
import time
import tempfile
from pathlib import Path
import unittest
from unittest import mock

from spectral_calibration.acquisition import AcquisitionCancelled
from spectral_calibration.horiba import HoribaError, HoribaSettings, ICLMonochromator, wavelength_plan
from spectral_calibration.repair_icl import repair


class FakeICL:
    def __init__(self):
        self.commands = []
        self.position, self.grating = 532., 0
        self.busy, self.initialized = False, True
        self.error, self.offset, self.closed = None, 0, False
        self.pending = None
        self.unrelated = False
        self.initializing = False
        self.init_busy_polls = 1
        self.init_succeeds = True
        self.slits = {1: .1, 4: .1}
        self.ports = [dict(locationId=1, slitType=2), dict(locationId=4, slitType=2)]
        self.slit_pending = None
        self.slit_busy_polls = 1
        self.ignore_slit_move = self.cross_slit_move = False
        self.mirrors = {0: 0, 1: 1}
        self.mirror_config = [dict(locationId=2)]
        self.output_pending = None
        self.output_busy_polls = 1
        self.ignore_output_move = False
        self.gratings = [dict(positionIndex=i, grooveDensity=d, blaze=0) for i, d in enumerate((1200, 600, 300))]
        self.grating_pending = None
        self.grating_busy_polls = 1
        self.ignore_grating_move = False
        self.after_turret_nm = None

    def settimeout(self, timeout):
        self.timeout = timeout

    def send(self, payload):
        self.pending = json.loads(payload)
        self.commands.append(self.pending)

    def recv(self):
        if self.unrelated:
            self.unrelated = False
            return json.dumps(dict(id=-1, command="event"))
        cmd = self.pending["command"]
        if cmd == "mono_init":
            self.initializing, self.busy, self.initialized = True, True, False
        if cmd == "mono_isBusy" and self.initializing:
            self.busy = self.init_busy_polls > 0
            self.init_busy_polls -= 1
            if not self.busy:
                self.initializing = False
                self.initialized = self.init_succeeds
        if cmd == "mono_moveSlitMM" and not self.error:
            params = self.pending["parameters"]
            self.slit_pending = params["locationId"], params["position"], self.slit_busy_polls
        if cmd == "mono_isBusy" and self.slit_pending:
            location, target, remaining = self.slit_pending
            self.busy = remaining > 0
            if self.busy:
                self.slit_pending = location, target, remaining-1
            else:
                if not self.ignore_slit_move:
                    self.slits[location] = target
                if self.cross_slit_move:
                    for other in self.slits:
                        if other != location:
                            self.slits[other] += .1
                self.slit_pending = None
        if cmd == "mono_moveMirror" and not self.error:
            params = self.pending["parameters"]
            self.output_pending = params["locationId"], params["position"], self.output_busy_polls
        if cmd == "mono_isBusy" and self.output_pending:
            location, target, remaining = self.output_pending
            self.busy = remaining > 0
            if self.busy:
                self.output_pending = location, target, remaining-1
            else:
                if not self.ignore_output_move:
                    self.mirrors[location] = target
                self.output_pending = None
        if cmd == "mono_moveGrating" and not self.error:
            self.grating_pending = self.pending["parameters"]["position"], self.grating_busy_polls
        if cmd == "mono_isBusy" and self.grating_pending:
            target, remaining = self.grating_pending
            self.busy = remaining > 0
            if self.busy:
                self.grating_pending = target, remaining-1
            else:
                if not self.ignore_grating_move:
                    self.grating = target
                    if self.after_turret_nm is not None:
                        self.position = self.after_turret_nm
                self.grating_pending = None
        results = {
            "icl_info": {"nodeApiVersion": 300, "nodeVersion": "test"},
            "mono_discover": {"count": 1},
            "mono_list": {"devices": [{"index": 0, "deviceType": "iHR320", "serialNumber": "TEST"}]},
            "mono_isBusy": {"busy": self.busy},
            "mono_isInitialized": {"initialized": self.initialized},
            "mono_getPosition": {"wavelength": self.position+self.offset},
            "mono_getGratingPosition": {"position": self.grating},
            "mono_getConfig": {"configuration": {"model": "iHR320", "ports": self.ports, "mirrors": self.mirror_config, "gratings": self.gratings}},
            "mono_getSlitPositionInMM": {"position": self.slits.get(self.pending["parameters"].get("locationId"))},
            "mono_getMirrorPosition": {"position": self.mirrors.get(self.pending["parameters"].get("locationId"))},
        }.get(cmd, {})
        if cmd == "mono_moveToPosition":
            self.position = self.pending["parameters"]["wavelength"]
        return json.dumps(dict(id=self.pending["id"], command=cmd, results=results,
                               errors=[self.error] if self.error else []))

    def close(self):
        self.closed = True


class HoribaTests(unittest.TestCase):
    def setUp(self):
        self.socket = FakeICL()
        self.controller = ICLMonochromator(socket_factory=lambda *a, **kw: self.socket)

    def tearDown(self):
        self.controller.close()

    def test_plan_direction_endpoints_and_limits(self):
        self.assertEqual(wavelength_plan(531.9, 532.1, .1), [531.9, 532., 532.1])
        self.assertEqual(wavelength_plan(532.1, 531.9, -.1), [532.1, 532., 531.9])
        self.assertEqual(wavelength_plan(1, 1.25, .1), [1, 1.1, 1.2])
        for values in [(1, 2, 0), (1, 2, -.1), (2, 1, .1), (1, 2, 1e-9)]:
            with self.assertRaises(ValueError):
                wavelength_plan(*values)
        with self.assertRaises(ValueError):
            HoribaSettings(slit_in=1, slit_out=1).validate()

    def test_diagnosis_does_not_open_or_move(self):
        self.socket.unrelated = True
        report = self.controller.diagnose()
        self.assertEqual(len(report["devices"]), 1)
        self.assertEqual([x["command"] for x in self.socket.commands], ["icl_info", "mono_discover", "mono_list"])

    def test_move_reads_actual_position_and_never_redefines_calibration(self):
        self.controller.connect()
        snapshot = self.controller.move_to(532.2)
        self.assertEqual(snapshot["target_nm"], 532.2)
        self.assertEqual(snapshot["wavelength_nm"], 532.2)
        self.assertEqual(snapshot["grating"], "G1")
        commands = [x["command"] for x in self.socket.commands]
        self.assertIn("mono_moveToPosition", commands)
        self.assertNotIn("mono_setPosition", commands)
        self.assertNotIn("mono_init", commands)

    def test_uninitialized_and_busy_do_not_move(self):
        for attribute in ("initialized", "busy"):
            setattr(self.socket, attribute, attribute == "busy")
            with self.assertRaises(HoribaError):
                self.controller.move_to(532)
            self.assertNotIn("mono_moveToPosition", [x["command"] for x in self.socket.commands])
            setattr(self.socket, attribute, attribute == "initialized")

    def test_position_mismatch_and_boundaries_prevent_acquisition(self):
        self.socket.offset = .1
        with self.assertRaises(HoribaError):
            self.controller.move_to(532)
        with self.assertRaises(ValueError):
            self.controller.move_to(2000)

    def test_protocol_failure_drops_socket_without_retry(self):
        self.socket.error = "[E];-1;mono_discover"
        with self.assertRaisesRegex(HoribaError, "Mono=true"):
            self.controller.diagnose()
        self.assertTrue(self.socket.closed)
        self.assertEqual(len(self.socket.commands), 1)

    def test_query_timeout_closes_connection(self):
        self.socket.recv = mock.Mock(side_effect=TimeoutError("simulado"))
        with self.assertRaises(HoribaError):
            self.controller.diagnose()
        self.assertTrue(self.socket.closed)
        self.assertEqual(len(self.socket.commands), 1)

    def test_cancel_before_move_and_while_motor_is_busy(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(AcquisitionCancelled):
            self.controller.move_to(532, cancel)
        self.assertNotIn("mono_moveToPosition", [x["command"] for x in self.socket.commands])
        cancel.clear()
        original = self.socket.send
        def send(payload):
            original(payload)
            if self.socket.pending["command"] == "mono_moveToPosition":
                self.socket.busy = True
                cancel.set()
        self.socket.send = send
        with self.assertRaises(AcquisitionCancelled):
            self.controller.move_to(532, cancel)

    def test_motor_timeout_stops_polling(self):
        original = self.socket.send
        def send(payload):
            original(payload)
            if self.socket.pending["command"] == "mono_moveToPosition":
                self.socket.busy = True
        self.socket.send = send
        self.controller.settings.move_timeout_s = .01
        with self.assertRaisesRegex(HoribaError, "Timeout de movimiento"):
            self.controller.move_to(532)
        self.assertTrue(self.socket.closed)


class InitializationTests(unittest.TestCase):
    def setUp(self):
        self.socket = FakeICL()
        self.socket.initialized = False
        self.controller = ICLMonochromator(socket_factory=lambda *a, **kw: self.socket)

    def tearDown(self):
        self.controller.close()

    def commands(self):
        return [item["command"] for item in self.socket.commands]

    def test_documented_homing_waits_and_confirms_real_queries(self):
        self.assertEqual(self.controller.status.state, "disconnected")
        report = self.controller.initialize()
        self.assertTrue(report["initialization_sent"])
        self.assertFalse(report["already_initialized"])
        self.assertEqual(report["snapshot"]["wavelength_nm"], 532.)
        self.assertEqual(self.controller.status.state, "ready")
        init = next(c for c in self.socket.commands if c["command"] == "mono_init")
        self.assertEqual(init["parameters"], dict(index=0, force=False))
        self.assertEqual(self.commands().count("mono_init"), 1)
        self.assertIn("mono_getPosition", self.commands())
        self.assertIn("mono_getGratingPosition", self.commands())
        self.assertNotIn("mono_setPosition", self.commands())
        self.assertNotIn("mono_moveToPosition", self.commands())
        self.assertEqual(self.controller.describe()["initialization"], report)
        self.controller.close()
        self.assertEqual(self.controller.status.state, "disconnected")
        self.assertEqual(self.commands()[-1], "mono_close")

    def test_initialized_device_and_repeated_click_do_not_repeat_homing(self):
        self.socket.initialized = True
        for _ in range(2):
            report = self.controller.initialize()
            self.assertTrue(report["already_initialized"])
            self.assertFalse(report["initialization_sent"])
        self.assertNotIn("mono_init", self.commands())
        self.assertEqual(self.commands().count("mono_open"), 1)
        self.assertEqual(self.commands().count("mono_getPosition"), 2)

    def test_busy_instrument_does_not_receive_homing(self):
        self.socket.busy = True
        with self.assertRaisesRegex(HoribaError, "ocupado"):
            self.controller.initialize()
        self.assertNotIn("mono_init", self.commands())
        self.assertNotIn("mono_isInitialized", self.commands())
        self.assertEqual(self.controller.status.state, "error")

    def test_successful_open_without_initialization_is_not_ready(self):
        with self.assertRaisesRegex(HoribaError, "sin inicializar"):
            self.controller.connect()
        self.assertEqual(self.controller.status.state, "error")
        self.assertNotIn("mono_init", self.commands())

    def test_no_device_is_not_initialized(self):
        self.controller.settings.index = 1
        with self.assertRaisesRegex(HoribaError, "No se encontró"):
            self.controller.initialize()
        self.assertNotIn("mono_open", self.commands())
        self.assertNotIn("mono_init", self.commands())
        self.assertEqual(self.controller.status.state, "error")

    def test_unconfirmed_homing_fails_even_when_busy_clears(self):
        self.socket.init_succeeds = False
        with self.assertRaisesRegex(HoribaError, "sin inicializar"):
            self.controller.initialize()
        self.assertEqual(self.controller.status.state, "error")
        self.assertNotIn("mono_getPosition", self.commands())
        self.assertTrue(self.socket.closed)

    def test_timeout_never_retries_or_reports_ready(self):
        self.socket.init_busy_polls = 1000
        self.controller.settings.initialize_timeout_s = .02
        with self.assertRaisesRegex(HoribaError, "Timeout de inicialización"):
            self.controller.initialize()
        self.assertEqual(self.controller.status.state, "error")
        self.assertEqual(self.commands().count("mono_init"), 1)
        self.assertNotIn("mono_getPosition", self.commands())
        self.assertNotIn("mono_close", self.commands())
        self.assertTrue(self.socket.closed)

    def change_response(self, function):
        original = self.socket.recv
        def recv():
            response = json.loads(original())
            function(response)
            return json.dumps(response)
        self.socket.recv = recv

    def test_command_rejection_is_logged_with_context_and_not_retried(self):
        def reject(response):
            if response["command"] == "mono_init":
                response["errors"] = ["[E];-510;unsupported"]
        self.change_response(reject)
        with self.assertLogs("spectral_calibration.horiba", level="WARNING") as logs:
            with self.assertRaisesRegex(HoribaError, "-510"):
                self.controller.initialize()
        self.assertIn("índice=0", " ".join(logs.output))
        self.assertEqual(self.commands().count("mono_init"), 1)
        self.assertEqual(self.controller.status.state, "error")

    def test_missing_or_nonboolean_status_never_triggers_homing(self):
        for command, key, value in [("mono_isBusy", "busy", "false"),
                                    ("mono_isInitialized", "initialized", None)]:
            with self.subTest(command=command):
                self.socket = FakeICL()
                self.socket.initialized = False
                self.controller.drop()
                def corrupt(response):
                    if response["command"] == command:
                        response["results"][key] = value
                self.change_response(corrupt)
                with self.assertRaisesRegex(HoribaError, "Respuesta inválida"):
                    self.controller.initialize()
                self.assertNotIn("mono_init", self.commands())
                self.assertEqual(self.controller.status.state, "error")

    def test_position_readback_is_required_for_success(self):
        def corrupt(response):
            if response["command"] == "mono_getPosition":
                response["results"] = {"wavelength": "NaN"}
        self.change_response(corrupt)
        with self.assertRaises(HoribaError):
            self.controller.initialize()
        self.assertEqual(self.controller.status.state, "error")
        self.assertIsNone(self.controller.last_initialization)

    def test_deadline_includes_final_readback(self):
        self.socket.initialized = True
        self.controller.settings.initialize_timeout_s = .02
        def delay(response):
            if response["command"] == "mono_getGratingPosition":
                time.sleep(.04)
        self.change_response(delay)
        with self.assertRaisesRegex(HoribaError, "timeout"):
            self.controller.initialize()
        self.assertEqual(self.controller.status.state, "error")

    def test_lost_init_ack_is_not_resent_and_recovery_checks_busy(self):
        original = self.socket.recv
        def recv():
            if self.socket.pending["command"] == "mono_init":
                raise TimeoutError("respuesta perdida")
            return original()
        self.socket.recv = recv
        with self.assertRaisesRegex(HoribaError, "homing puede continuar"):
            self.controller.initialize()
        self.assertEqual(self.commands().count("mono_init"), 1)
        replacement = FakeICL()
        replacement.busy = True
        self.controller.socket_factory = lambda *a, **kw: replacement
        with self.assertRaisesRegex(HoribaError, "ocupado"):
            self.controller.initialize()
        self.assertNotIn("mono_init", [c["command"] for c in replacement.commands])
        replacement.busy = False
        report = self.controller.initialize()
        self.assertTrue(report["already_initialized"])
        self.assertEqual(self.controller.status.state, "ready")

    def test_connection_failure_can_recover_without_recreating_controller(self):
        self.controller.socket_factory = mock.Mock(side_effect=OSError("ICL no disponible"))
        with self.assertRaises(HoribaError):
            self.controller.initialize()
        self.assertEqual(self.controller.status.state, "error")
        self.controller.socket_factory = lambda *a, **kw: self.socket
        self.controller.initialize()
        self.assertEqual(self.controller.status.state, "ready")

    def test_cancel_before_and_after_send(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(AcquisitionCancelled):
            self.controller.initialize(cancel)
        self.assertEqual(self.commands(), [])
        cancel.clear()
        original = self.socket.send
        def send(payload):
            original(payload)
            if self.socket.pending["command"] == "mono_init":
                cancel.set()
        self.socket.send = send
        with self.assertRaisesRegex(AcquisitionCancelled, "homing puede continuar"):
            self.controller.initialize(cancel)
        self.assertEqual(self.commands().count("mono_init"), 1)
        self.assertEqual(self.controller.status.state, "error")

    def test_concurrent_initialize_does_not_duplicate_commands(self):
        started, release = threading.Event(), threading.Event()
        original = self.socket.recv
        def recv():
            if self.socket.pending["command"] == "mono_init":
                started.set()
                if not release.wait(2):
                    raise TimeoutError("test sin liberar")
            return original()
        self.socket.recv = recv
        reports = []
        thread = threading.Thread(target=lambda: reports.append(self.controller.initialize()))
        thread.start()
        try:
            self.assertTrue(started.wait(2))
            self.assertEqual(self.controller.status.state, "initializing")
            with self.assertRaisesRegex(HoribaError, "en curso"):
                self.controller.initialize()
        finally:
            release.set()
            thread.join(3)
        self.assertEqual(len(reports), 1)
        self.assertEqual(self.commands().count("mono_init"), 1)

    def test_fresh_application_queries_existing_device_without_homing(self):
        self.controller.initialize()
        self.controller.close()
        other = ICLMonochromator(socket_factory=lambda *a, **kw: self.socket)
        try:
            report = other.initialize()
            self.assertTrue(report["already_initialized"])
            self.assertEqual(self.commands().count("mono_init"), 1)
        finally:
            other.close()

    def test_unsupported_api_does_not_send_speculative_initialization(self):
        def corrupt(response):
            if response["command"] == "icl_info":
                response["results"]["nodeApiVersion"] = 200
        self.change_response(corrupt)
        with self.assertRaisesRegex(HoribaError, "API 300"):
            self.controller.initialize()
        self.assertNotIn("mono_init", self.commands())


class ConfigurationTests(unittest.TestCase):
    def test_preview_and_repair_preserve_originals(self):
        original = "[Modules]\nMono=false\nCcd=false\nLmb=true\nAmb=false\nSpectograph=true\nInstrumentXmb=true\nSampleController=true\nSAQ3=false\n[Instrument]\nModel=Duetta\n[Other]\nValue=keep\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sdk = root / "SDK"
            target = sdk / "icl_settings.ini"
            target.mkdir(parents=True)
            (target / "icl_settings.ini").write_text("original SDK", encoding="ascii")
            active = root / "icl_settings.ini"
            active.write_text(original, encoding="utf-8")
            preview = repair(sdk, active)
            self.assertFalse(preview["applied"])
            self.assertTrue(target.is_dir())
            result = repair(sdk, active, apply=True)
            self.assertTrue(target.is_file())
            self.assertIn("Mono=true", target.read_text())
            self.assertIn("Model=SDK", active.read_text())
            self.assertIn("Value=keep", active.read_text())
            self.assertEqual(Path(result["active_backup"]).read_text(), original)
            self.assertEqual((Path(result["sdk_backup"]) / "icl_settings.ini").read_text(), "original SDK")
