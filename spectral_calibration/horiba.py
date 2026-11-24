"""Control ICL documentado por HORIBA; compatible con el Python 3.7 del laboratorio.

Protocolo: EzSpec-python-sdk/docs/icl_api20251029.md, API 300.
Se usa WebSocket JSON directamente para no exigir Python >=3.9 del SDK Python.
"""
from dataclasses import asdict, dataclass
from decimal import Decimal
import json
import logging
import threading
import time

from .acquisition import AcquisitionCancelled
from .instruments import CommunicationError
from .models import integer, number, utc_now


logger = logging.getLogger(__name__)

# ICL API 300, mono_getConfig. Límites/paso: documentación HORIBA iHR.
SLIT_LOCATIONS = {1: "Entrada axial", 2: "Entrada lateral", 3: "Salida axial", 4: "Salida lateral"}
SLIT_TYPES = {1: (2.0, .00225), 2: (7.0, .00625)}
# mono_getMirrorPosition / mono_moveMirror: índices de comando desde cero.
EXIT_MIRROR = 1
OUTPUT_POSITIONS = {"Axial": 0, "Lateral": 1}
# SDK oficial Monochromator.Grating: FIRST=0, SECOND=1, THIRD=2.
GRATING_POSITIONS = {1: 0, 2: 1, 3: 2}


class HoribaError(CommunicationError):
    pass


@dataclass(frozen=True)
class InstrumentStatus:
    state: str = "disconnected"
    detail: str = "Sin sesión con el instrumento."


@dataclass
class HoribaSettings:
    url: str = "ws://127.0.0.1:25010"
    index: int = 0
    timeout_s: float = 8
    move_timeout_s: float = 180
    tolerance_nm: float = .01
    minimum_nm: float = 1
    maximum_nm: float = 1500
    slit_in: int = 0
    slit_out: int = 0
    initialize_timeout_s: float = 180
    slit_timeout_s: float = 30
    output_timeout_s: float = 30
    grating_timeout_s: float = 180

    def validate(self):
        if self.url not in ("ws://127.0.0.1:25010", "ws://localhost:25010"):
            raise ValueError("Use el ICL local: ws://127.0.0.1:25010.")
        self.index = integer(self.index, "Índice HORIBA", 0, 99)
        for key in ("timeout_s", "move_timeout_s", "initialize_timeout_s", "slit_timeout_s", "output_timeout_s", "grating_timeout_s", "tolerance_nm", "minimum_nm", "maximum_nm"):
            setattr(self, key, number(getattr(self, key), key, 0, True))
        if self.maximum_nm <= self.minimum_nm:
            raise ValueError("El límite superior debe superar al inferior.")
        for key in ("slit_in", "slit_out"):
            setattr(self, key, integer(getattr(self, key), key, 0, 4))
        if bool(self.slit_in) != bool(self.slit_out) or (self.slit_in and self.slit_in == self.slit_out):
            raise ValueError("Rendijas: detección de ambas (0/0), o dos ubicaciones distintas entre 1 y 4.")
        if self.slit_in not in (0, 1, 2) or self.slit_out not in (0, 3, 4):
            raise ValueError("Rendija de entrada: 1 o 2; salida: 3 o 4; 0/0 permite detectar al consultar rendijas.")
        return self


def wavelength_plan(start, end, step, maximum_points=10000):
    """Incluye el extremo sólo si pertenece a la grilla; nunca lo sobrepasa."""
    start, end = number(start, "Inicio [nm]", 0, True), number(end, "Fin [nm]", 0, True)
    step = number(step, "Paso [nm]")
    if step == 0 or (end-start)*step < 0:
        raise ValueError("El signo del paso debe avanzar desde inicio hacia fin.")
    a, b, s = (Decimal(str(x)) for x in (start, end, step))
    count = int((b-a)/s) + 1
    if count > maximum_points:
        raise ValueError("El barrido supera {} puntos.".format(maximum_points))
    return [float(a+i*s) for i in range(count)]


class ICLMonochromator:
    def __init__(self, settings=None, socket_factory=None):
        self.settings = (settings or HoribaSettings()).validate()
        self.socket_factory = socket_factory
        self.socket = None
        self.sequence = 0
        self.opened = False
        self.info, self.device = {}, {}
        self.status = InstrumentStatus()
        self.last_snapshot = self.last_initialization = None
        self.slit_ports, self.slit_positions = {}, {}
        self.last_slit_move = None
        self.output_position = None
        self.output_monitoring = False
        self.last_output_move = None
        self.gratings = {}
        self.grating_position = self.grating_wavelength_nm = None
        self.grating_monitoring = False
        self.grating_change_pending = False
        self.last_grating_move = None
        self._initialization_lock = threading.Lock()

    def _ready(self, detail):
        self.status = InstrumentStatus("error", "Cambio de grating sin completar: aplique grating y λ antes de adquirir.") if self.grating_change_pending else InstrumentStatus("ready", detail)

    def _connect_socket(self):
        if self.socket is None:
            factory = self.socket_factory
            if factory is None:
                try:
                    from websocket import create_connection
                except ImportError as exc:
                    raise HoribaError("Falta websocket-client. Use el entorno local .venv de calibración.") from exc
                factory = create_connection
            try:
                self.socket = factory(self.settings.url, timeout=self.settings.timeout_s)
            except Exception as exc:
                raise HoribaError("No se pudo conectar a ICL. Abra icl.exe y revise su licencia: " + str(exc)) from exc

    def request(self, command, parameters=None, timeout=None):
        self.sequence += 1
        request_id = self.sequence
        deadline = time.monotonic() + (self.settings.timeout_s if timeout is None else timeout)
        try:
            self._connect_socket()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("ICL no respondió dentro del timeout.")
            self.socket.settimeout(remaining)
            self.socket.send(json.dumps(dict(id=request_id, command=command, parameters=parameters or {})))
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("ICL no respondió dentro del timeout.")
                self.socket.settimeout(remaining)
                payload = self.socket.recv()
                if time.monotonic() > deadline:
                    raise TimeoutError("ICL respondió después del timeout.")
                if isinstance(payload, bytes):
                    continue  # Eventos binarios ajenos a la respuesta solicitada.
                response = json.loads(payload)
                if response.get("id") != request_id:
                    continue
                if response.get("command") != command:
                    raise ValueError("Respuesta ICL con comando inesperado.")
                errors = response.get("errors") or []
                if errors:
                    detail = "; ".join(str(e) for e in errors)
                    if any(";-1;" in str(e) for e in errors):
                        detail += " · ICL no tiene un parser para este módulo. Revise Modules/Mono=true y Model=SDK en icl_settings.ini."
                    raise HoribaError(command + ": " + detail)
                return response.get("results") or {}
        except Exception as exc:
            self.drop()
            self.status = InstrumentStatus("error", "{}: {}".format(command, exc))
            logger.warning("ICL comando=%s id=%s índice=%s: %s", command, request_id, self.settings.index, exc)
            if isinstance(exc, HoribaError):
                raise
            raise HoribaError("Falló {}: {}. Reconecte; no se reintenta la orden.".format(command, exc)) from exc

    def diagnose(self):
        """No abre ni inicializa motores; enumera dispositivos USB."""
        self.info = self.request("icl_info")
        discovered = self.request("mono_discover")
        devices = self.request("mono_list").get("devices", [])
        return dict(icl=self.info, discovery=discovered, devices=devices)

    def _open(self):
        if self.opened and self.socket is not None:
            return
        report = self.diagnose()
        matches = [d for d in report["devices"] if d.get("index") == self.settings.index]
        if len(matches) != 1:
            self.drop()
            raise HoribaError("No se encontró el monocromador con índice {}. Detectados: {}".format(self.settings.index, report["devices"]))
        self.device = matches[0]
        self.command("mono_open")
        self.opened = True

    def connect(self):
        """Abre y verifica; nunca inicia homing implícitamente."""
        try:
            self._open()
            return self.snapshot()
        except Exception as exc:
            self.close()
            self.status = InstrumentStatus("error", str(exc))
            raise

    def command(self, command, deadline=None, **parameters):
        timeout = self.settings.timeout_s
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HoribaError("Timeout de inicialización. El homing puede continuar; consulte el estado antes de reintentar.")
            timeout = min(timeout, remaining)
        return self.request(command, dict(index=self.settings.index, **parameters), timeout=timeout)

    def _flag(self, command, key, deadline=None):
        value = self.command(command, deadline=deadline).get(key)
        if type(value) is not bool:
            self.status = InstrumentStatus("error", "Respuesta inválida de {}: {}".format(command, key))
            raise HoribaError(self.status.detail)
        return value

    def require_ready(self, deadline=None):
        if self._flag("mono_isBusy", "busy", deadline):
            self.status = InstrumentStatus("error", "Instrumento ocupado; disponibilidad sin confirmar.")
            raise HoribaError("HORIBA ocupado; espere antes de iniciar el barrido.")
        if not self._flag("mono_isInitialized", "initialized", deadline):
            self.status = InstrumentStatus("error", "Instrumento sin inicializar.")
            raise HoribaError("HORIBA sin inicializar. Pulse Inicializar espectrómetro en 1 · Equipos.")

    def snapshot(self, deadline=None):
        self.require_ready(deadline)
        result = dict(wavelength_nm=number(self.command("mono_getPosition", deadline=deadline)["wavelength"], "λ leída", 0, True),
                      grating="G{}".format(integer(self.command("mono_getGratingPosition", deadline=deadline)["position"], "Grating", 0, 2)+1))
        self.grating_position = int(result["grating"][1:])
        self.grating_wavelength_nm = result["wavelength_nm"]
        if self.grating_monitoring:
            if self.grating_position not in self.gratings:
                self.grating_position = self.grating_wavelength_nm = None
                raise HoribaError("La torreta informó un grating ausente en su configuración.")
            result["grating_info"] = dict(self.gratings[self.grating_position])
            result["grating_change_pending"] = self.grating_change_pending
        for key, location in (("slit_in_mm", self.settings.slit_in), ("slit_out_mm", self.settings.slit_out)):
            if location:
                result[key] = number(self.command("mono_getSlitPositionInMM", deadline=deadline, locationId=location)["position"], key, 0)
                maximum = self.slit_ports.get(key, {}).get("maximum_mm", 7)
                if result[key] > maximum + 1e-6:
                    raise HoribaError("Rendija en puerto {}: lectura fuera del rango 0–{} mm.".format(location, maximum))
        self.slit_positions = {key: result[key] for key in ("slit_in_mm", "slit_out_mm") if key in result}
        if self.output_monitoring:
            result["output_port"] = self._read_output_position(deadline)
        self.last_snapshot = result
        self._ready("ICL confirmó inicialización y lectura: λ={} nm, {} ({}).".format(
            result["wavelength_nm"], result["grating"], self.device.get("serialNumber", "serie no informada")))
        return result

    def initialize(self, cancel=None):
        """ICL API 300: homing explícito, sin forzar ni repetir órdenes inciertas.

        La apertura tiene el timeout de consulta; el presupuesto independiente
        de inicialización cubre comprobaciones, homing y lectura final.
        Cancelar abandona la espera, no detiene los motores.
        """
        if not self._initialization_lock.acquire(blocking=False):
            raise HoribaError("Ya hay una operación de inicialización o accesorios en curso.")
        cancel = cancel or threading.Event()
        sent = False
        self.status = InstrumentStatus("initializing", "Abriendo comunicación y consultando el instrumento…")
        try:
            if cancel.is_set():
                raise AcquisitionCancelled("Inicialización cancelada antes de abrir el equipo.")
            self._open()
            if self.info.get("nodeApiVersion") != 300:
                raise HoribaError("Inicialización documentada para ICL API 300; versión recibida: {}.".format(
                    self.info.get("nodeApiVersion")))
            deadline = time.monotonic() + self.settings.initialize_timeout_s
            if self._flag("mono_isBusy", "busy", deadline):
                raise HoribaError("HORIBA ocupado. No se envió mono_init; espere y consulte el estado.")
            already = self._flag("mono_isInitialized", "initialized", deadline)
            if cancel.is_set():
                raise AcquisitionCancelled("Inicialización cancelada antes de enviar homing.")
            if not already:
                self.status = InstrumentStatus("initializing", "Homing en curso; esperando confirmación de ICL…")
                # El envío puede haber llegado aunque se pierda la respuesta.
                sent = True
                self.command("mono_init", deadline=deadline, force=False)
                while self._flag("mono_isBusy", "busy", deadline):
                    if cancel.wait(min(.1, max(0, deadline-time.monotonic()))):
                        raise AcquisitionCancelled("Espera de inicialización cancelada. El homing puede continuar.")
            if cancel.is_set():
                raise AcquisitionCancelled("Espera de inicialización cancelada. El homing puede continuar.")
            snapshot = self.snapshot(deadline)
            self.last_initialization = dict(utc=utc_now(), already_initialized=already,
                initialization_sent=sent, snapshot=snapshot, device=dict(self.device))
            logger.info("Inicialización ICL índice=%s serie=%s enviada=%s lectura=%s",
                        self.settings.index, self.device.get("serialNumber"), sent, snapshot)
            return self.last_initialization
        except Exception as exc:
            # Sin reenvío ni órdenes de motor/cierre tras un resultado incierto.
            self.drop()
            detail = str(exc)
            if sent and "homing puede continuar" not in detail:
                detail += " El homing puede continuar; consulte el estado antes de reintentar."
            self.status = InstrumentStatus("error", detail)
            logger.warning("Inicialización ICL índice=%s serie=%s enviada=%s: %s",
                           self.settings.index, self.device.get("serialNumber"), sent, detail)
            if isinstance(exc, (HoribaError, AcquisitionCancelled)):
                raise type(exc)(detail) from exc
            raise HoribaError("No se confirmó la inicialización: " + detail) from exc
        finally:
            self._initialization_lock.release()

    def _load_slit_ports(self):
        response = self.command("mono_getConfig").get("configuration")
        config = json.loads(response) if isinstance(response, str) else response
        if not isinstance(config, dict) or not isinstance(config.get("ports"), list):
            raise HoribaError("ICL no informó una configuración válida de rendijas.")
        installed = {}
        for item in config["ports"]:
            location = integer(item.get("locationId"), "Puerto de rendija", 1, 4)
            if location in installed:
                raise HoribaError("ICL informó un puerto de rendija duplicado.")
            installed[location] = item.get("slitType")
        selected = {}
        for key, chosen, allowed in (("slit_in_mm", self.settings.slit_in, (1, 2)),
                                     ("slit_out_mm", self.settings.slit_out, (3, 4))):
            name = "Entrada" if key == "slit_in_mm" else "Salida"
            candidates = [location for location in installed if location in allowed]
            if not chosen:
                if len(candidates) != 1:
                    raise HoribaError("{}: puertos detectados {}. Seleccione el puerto de entrada/salida en 1 · Equipos.".format(name, candidates))
                chosen = candidates[0]
            if chosen not in candidates:
                raise HoribaError("{}: el puerto {} no está instalado para esta función.".format(name, chosen))
            slit_type = installed[chosen]
            if type(slit_type) is not int or slit_type not in SLIT_TYPES:
                raise HoribaError("Puerto {}: tipo de rendija desconocido {}; movimiento bloqueado.".format(chosen, slit_type))
            maximum, step = SLIT_TYPES[slit_type]
            selected[key] = dict(locationId=chosen, label=SLIT_LOCATIONS[chosen], slitType=slit_type,
                                 minimum_mm=0., maximum_mm=maximum, step_mm=step)
        self.slit_ports = selected
        self.settings.slit_in = selected["slit_in_mm"]["locationId"]
        self.settings.slit_out = selected["slit_out_mm"]["locationId"]

    def _slit_command(self, command, deadline=None, **parameters):
        timeout = self.settings.timeout_s
        if deadline is not None:
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise HoribaError("Timeout de ajuste de rendijas; posición sin confirmar.")
            timeout = min(timeout, remaining)
        return self.request(command, dict(index=self.settings.index, **parameters), timeout=timeout)

    def _read_slit_positions(self, deadline=None):
        actual = {}
        for key, port in self.slit_ports.items():
            value = number(self._slit_command("mono_getSlitPositionInMM", deadline,
                           locationId=port["locationId"])["position"], "Apertura leída [mm]", 0)
            if value > port["maximum_mm"] + 1e-6:
                raise HoribaError("Puerto {}: apertura leída fuera de rango: {} mm.".format(port["locationId"], value))
            actual[key] = value
        self.slit_positions = actual
        return dict(actual)

    def read_slits(self):
        """Detecta puertos y lee ambas aperturas sin inicializar ni mover."""
        try:
            self._open()
            self.require_ready()
            if self.info.get("nodeApiVersion") != 300:
                raise HoribaError("Control de rendijas documentado para ICL API 300.")
            self._load_slit_ports()
            actual = self._read_slit_positions()
            self._ready("Rendijas leídas: entrada={} mm, salida={} mm.".format(
                actual["slit_in_mm"], actual["slit_out_mm"]))
            return dict(utc=utc_now(), ports=dict(self.slit_ports), actual_mm=actual)
        except Exception as exc:
            self.drop()
            self.status = InstrumentStatus("error", str(exc))
            logger.warning("Lectura de rendijas índice=%s: %s", self.settings.index, exc)
            raise HoribaError("No se confirmaron las rendijas: " + str(exc)) from exc

    def move_slits(self, entrance_mm=None, exit_mm=None, cancel=None):
        """Aplica una o ambas aperturas secuencialmente, con lectura posterior.

        No se reenvía un movimiento incierto ni se intenta rollback automático.
        La lectura de ICL es la confirmación disponible; no implica un encoder.
        """
        requested = {key: number(value, "Apertura solicitada [mm]", 0)
                     for key, value in (("slit_in_mm", entrance_mm), ("slit_out_mm", exit_mm)) if value is not None}
        if not requested or any(value > 7 for value in requested.values()):
            raise ValueError("Indique al menos una apertura entre 0 y 7 mm.")
        if not self._initialization_lock.acquire(blocking=False):
            raise HoribaError("Otra operación de inicialización o accesorios está en curso.")
        cancel = cancel or threading.Event()
        report = dict(utc=utc_now(), state="checking", requested_mm=requested, confirmed_moves=[])
        self.last_slit_move = report
        sent = False
        try:
            if cancel.is_set():
                raise AcquisitionCancelled("Ajuste de rendijas cancelado antes del movimiento.")
            initial = self.read_slits()
            report.update(ports=initial["ports"], before_mm=initial["actual_mm"])
            # Valida ambos destinos antes de enviar el primero.
            for key, value in requested.items():
                if value > self.slit_ports[key]["maximum_mm"]:
                    raise ValueError("{}: rango de la rendija instalada: 0–{} mm.".format(self.slit_ports[key]["label"], self.slit_ports[key]["maximum_mm"]))
            expected = dict(initial["actual_mm"])
            deadline = time.monotonic() + self.settings.slit_timeout_s
            report["state"] = "moving"
            for key, target in requested.items():
                if cancel.is_set():
                    raise AcquisitionCancelled("Espera de ajuste de rendijas cancelada.")
                if abs(expected[key]-target) > 1e-9:
                    sent = True
                    self._slit_command("mono_moveSlitMM", deadline,
                                       locationId=self.slit_ports[key]["locationId"], position=target)
                    while True:
                        busy = self._slit_command("mono_isBusy", deadline).get("busy")
                        if type(busy) is not bool:
                            raise HoribaError("Respuesta inválida de mono_isBusy durante ajuste de rendijas.")
                        if not busy:
                            break
                        if cancel.wait(min(.1, max(0, deadline-time.monotonic()))):
                            raise AcquisitionCancelled("Espera de ajuste de rendijas cancelada.")
                if cancel.is_set():
                    raise AcquisitionCancelled("Espera de ajuste de rendijas cancelada.")
                initialized = self._slit_command("mono_isInitialized", deadline).get("initialized")
                if initialized is not True:
                    raise HoribaError("El instrumento no confirmó inicialización después del ajuste.")
                actual = self._read_slit_positions(deadline)
                report["actual_mm"] = actual
                # Un paso documentado admite la cuantización del controlador.
                tolerance = self.slit_ports[key]["step_mm"] + 1e-6
                if abs(actual[key]-target) > tolerance:
                    raise HoribaError("{}: solicitado {} mm, leído {} mm (tolerancia {:.6g} mm).".format(self.slit_ports[key]["label"], target, actual[key], tolerance))
                for other in actual:
                    if other != key and abs(actual[other]-expected[other]) > 1e-6:
                        raise HoribaError("La otra rendija cambió inesperadamente: {} de {} a {} mm.".format(self.slit_ports[other]["label"], expected[other], actual[other]))
                expected = actual
                report["confirmed_moves"].append(key)
            report.update(state="confirmed", finished_utc=utc_now())
            self._ready("Rendijas confirmadas por ICL: entrada={} mm, salida={} mm.".format(
                expected["slit_in_mm"], expected["slit_out_mm"]))
            logger.info("Rendijas índice=%s solicitud=%s lectura=%s", self.settings.index, requested, expected)
            return report
        except Exception as exc:
            report.update(state="error", error=str(exc))
            if sent:
                detail = str(exc) + " El movimiento enviado puede continuar; consulte las rendijas antes de reintentar."
                self.drop()
            else:
                detail = str(exc)
            self.status = InstrumentStatus("error", detail)
            logger.warning("Ajuste de rendijas índice=%s solicitud=%s confirmados=%s última_lectura=%s: %s",
                           self.settings.index, requested, report["confirmed_moves"], report.get("actual_mm"), detail)
            if isinstance(exc, (AcquisitionCancelled, ValueError)):
                raise type(exc)(detail) from exc
            raise HoribaError(detail) from exc
        finally:
            self._initialization_lock.release()

    def _output_command(self, command, deadline=None, **parameters):
        timeout = self.settings.timeout_s
        if deadline is not None:
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise HoribaError("Timeout del selector de salida; posición sin confirmar.")
            timeout = min(timeout, remaining)
        return self.request(command, dict(index=self.settings.index, **parameters), timeout=timeout)

    def _read_output_position(self, deadline=None):
        response = self._output_command("mono_getMirrorPosition", deadline, locationId=EXIT_MIRROR)
        code = response.get("position")
        if type(code) is not int or code not in OUTPUT_POSITIONS.values():
            self.output_position = None
            self.status = InstrumentStatus("error", "Posición desconocida del espejo de salida: {!r}.".format(code))
            raise HoribaError(self.status.detail)
        self.output_position = next(name for name, value in OUTPUT_POSITIONS.items() if value == code)
        return self.output_position

    def read_output(self):
        """Consulta exclusivamente el espejo de salida; no homing ni movimientos.

        El endpoint documenta EXIT=1, distinto de la numeración observada en
        configuration.mirrors y de los locationId de rendijas.
        """
        try:
            self._open()
            self.require_ready()
            if self.info.get("nodeApiVersion") != 300:
                raise HoribaError("Selector de salida documentado para ICL API 300.")
            position = self._read_output_position()
            self.output_monitoring = True
            self._ready("Salida óptica leída por ICL: " + position + ".")
            return dict(utc=utc_now(), locationId=EXIT_MIRROR, position=position,
                        position_code=OUTPUT_POSITIONS[position])
        except Exception as exc:
            self.drop()
            self.status = InstrumentStatus("error", str(exc))
            logger.warning("Lectura de salida índice=%s espejo=%s: %s", self.settings.index, EXIT_MIRROR, exc)
            raise HoribaError("No se confirmó la salida óptica: " + str(exc)) from exc

    def move_output(self, position, cancel=None):
        """Cambia Axial/Lateral y confirma por lectura, sin repetir órdenes inciertas."""
        if position not in OUTPUT_POSITIONS:
            raise ValueError("Seleccione la salida Axial o Lateral.")
        if not self._initialization_lock.acquire(blocking=False):
            raise HoribaError("Otra operación de inicialización o accesorios está en curso.")
        cancel = cancel or threading.Event()
        report = dict(utc=utc_now(), state="checking", requested=position,
                      locationId=EXIT_MIRROR, movement_sent=False)
        self.last_output_move = report
        try:
            if cancel.is_set():
                raise AcquisitionCancelled("Cambio de salida cancelado antes del movimiento.")
            report["before"] = self.read_output()["position"]
            deadline = time.monotonic() + self.settings.output_timeout_s
            if cancel.is_set():
                raise AcquisitionCancelled("Cambio de salida cancelado antes del movimiento.")
            if report["before"] != position:
                report.update(state="moving", movement_sent=True)
                self.output_position = None
                self._output_command("mono_moveMirror", deadline, locationId=EXIT_MIRROR,
                                     position=OUTPUT_POSITIONS[position])
            while True:
                if cancel.is_set():
                    raise AcquisitionCancelled("Espera del selector de salida cancelada.")
                busy = self._output_command("mono_isBusy", deadline).get("busy")
                if type(busy) is not bool:
                    raise HoribaError("Respuesta inválida de disponibilidad del selector de salida.")
                if not busy:
                    break
                if cancel.wait(min(.1, max(0, deadline-time.monotonic()))):
                    raise AcquisitionCancelled("Espera del selector de salida cancelada.")
            if self._output_command("mono_isInitialized", deadline).get("initialized") is not True:
                raise HoribaError("El equipo no confirmó inicialización después del cambio de salida.")
            if cancel.is_set():
                raise AcquisitionCancelled("Espera del selector de salida cancelada.")
            report["actual"] = self._read_output_position(deadline)
            if report["actual"] != position:
                raise HoribaError("Salida solicitada: {}; leída: {}. Cambio sin confirmar.".format(position, report["actual"]))
            report.update(state="confirmed", finished_utc=utc_now())
            self._ready("Salida óptica confirmada por ICL: " + position + ".")
            logger.info("Salida índice=%s espejo=%s solicitud=%s lectura=%s enviada=%s",
                        self.settings.index, EXIT_MIRROR, position, report["actual"], report["movement_sent"])
            return report
        except Exception as exc:
            report.update(state="error", error=str(exc))
            detail = str(exc)
            if report["movement_sent"]:
                detail += " El movimiento enviado puede continuar; consulte la salida antes de reintentar."
            self.drop()
            self.status = InstrumentStatus("error", detail)
            logger.warning("Salida índice=%s espejo=%s solicitud=%s enviada=%s lectura=%s: %s",
                           self.settings.index, EXIT_MIRROR, position, report["movement_sent"], report.get("actual"), detail)
            if isinstance(exc, AcquisitionCancelled):
                raise AcquisitionCancelled(detail) from exc
            raise HoribaError(detail) from exc
        finally:
            self._initialization_lock.release()

    def _grating_command(self, command, deadline=None, **parameters):
        timeout = self.settings.timeout_s
        if deadline is not None:
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise HoribaError("Timeout de torreta; grating y λ sin confirmar.")
            timeout = min(timeout, remaining)
        return self.request(command, dict(index=self.settings.index, **parameters), timeout=timeout)

    def read_gratings(self):
        """Consulta catálogo, selección y λ; no homing ni movimientos."""
        try:
            self._open()
            self.require_ready()
            if self.info.get("nodeApiVersion") != 300:
                raise HoribaError("Torreta documentada para ICL API 300.")
            config = self.command("mono_getConfig").get("configuration")
            config = json.loads(config) if isinstance(config, str) else config
            if not isinstance(config, dict) or not isinstance(config.get("gratings"), list) or not config["gratings"]:
                raise HoribaError("ICL no informó un catálogo válido de gratings instalados.")
            gratings = {}
            for item in config["gratings"]:
                code = item.get("positionIndex")
                if type(code) is not int or code not in GRATING_POSITIONS.values() or code+1 in gratings:
                    raise HoribaError("Configuración de torreta con posición inválida o duplicada.")
                density = item.get("grooveDensity")
                if density is not None:
                    density = number(density, "Densidad informada", 0)
                blaze = item.get("blaze")
                if blaze is not None:
                    blaze = number(blaze, "Blaze informado", 0)
                # Se conserva blaze sin asignarle unidades no documentadas.
                gratings[code+1] = dict(number=code+1, positionIndex=code, grooves_per_mm=density,
                                        blaze_reported=blaze, description=str(item.get("description") or item.get("name") or ""))
            position = self.command("mono_getGratingPosition").get("position")
            if type(position) is not int or position+1 not in gratings:
                raise HoribaError("Posición de torreta desconocida o no configurada: {!r}.".format(position))
            wavelength = number(self.command("mono_getPosition")["wavelength"], "λ central leída [nm]", 0)
            self.gratings = gratings
            self.grating_position, self.grating_wavelength_nm = position+1, wavelength
            self.grating_monitoring = True
            if not self.settings.minimum_nm <= wavelength <= self.settings.maximum_nm:
                self.grating_change_pending = True
            self.status = InstrumentStatus("error" if self.grating_change_pending else "ready",
                "Aplique grating y λ para completar la configuración espectral." if self.grating_change_pending
                else "Grating {} leído; λ={} nm.".format(position+1, wavelength))
            return dict(utc=utc_now(), gratings=[dict(gratings[n]) for n in sorted(gratings)],
                        number=position+1, wavelength_nm=wavelength, grating_change_pending=self.grating_change_pending)
        except Exception as exc:
            self.drop()
            self.status = InstrumentStatus("error", str(exc))
            logger.warning("Lectura de torreta índice=%s: %s", self.settings.index, exc)
            raise HoribaError("No se confirmó la torreta: " + str(exc)) from exc

    def move_grating(self, position, wavelength_nm, cancel=None):
        """Selecciona red y ajusta explícitamente λ con la lógica de movimiento existente.

        Captura la λ informada tras girar, sin asumir compensación automática.
        Un resultado incierto bloquea adquisición hasta completar grating y λ.
        """
        position = integer(position, "Grating solicitado", 1, 3)
        target = number(wavelength_nm, "λ final [nm]", 0, True)
        if not self.settings.minimum_nm <= target <= self.settings.maximum_nm:
            raise ValueError("λ final fuera de los límites configurados para HORIBA.")
        if not self._initialization_lock.acquire(blocking=False):
            raise HoribaError("Otra operación de inicialización o accesorios está en curso.")
        cancel = cancel or threading.Event()
        report = dict(utc=utc_now(), state="checking", requested=position, target_nm=target,
                      turret_movement_sent=False, wavelength_movement_sent=False)
        self.last_grating_move = report
        try:
            if cancel.is_set():
                raise AcquisitionCancelled("Cambio de grating cancelado antes del movimiento.")
            report["before"] = self.read_gratings()
            if position not in self.gratings:
                raise ValueError("Grating {} no está configurado como instalado.".format(position))
            deadline = time.monotonic() + self.settings.grating_timeout_s
            if cancel.is_set():
                raise AcquisitionCancelled("Cambio de grating cancelado antes del movimiento.")
            if self.grating_position != position:
                report.update(state="moving_turret", turret_movement_sent=True)
                self.grating_change_pending = True
                self.grating_position = self.grating_wavelength_nm = None
                self._grating_command("mono_moveGrating", deadline, position=GRATING_POSITIONS[position])
            while True:
                if cancel.is_set():
                    raise AcquisitionCancelled("Espera de torreta cancelada.")
                busy = self._grating_command("mono_isBusy", deadline).get("busy")
                if type(busy) is not bool:
                    raise HoribaError("Disponibilidad de torreta inválida.")
                if not busy:
                    break
                if cancel.wait(min(.1, max(0, deadline-time.monotonic()))):
                    raise AcquisitionCancelled("Espera de torreta cancelada.")
            if self._grating_command("mono_isInitialized", deadline).get("initialized") is not True:
                raise HoribaError("La torreta no confirmó inicialización después del cambio.")
            code = self._grating_command("mono_getGratingPosition", deadline).get("position")
            if type(code) is not int or code != GRATING_POSITIONS[position]:
                raise HoribaError("Grating solicitado {}, posición leída {!r}; cambio sin confirmar.".format(position, code))
            reported_nm = number(self._grating_command("mono_getPosition", deadline)["wavelength"], "λ tras girar [nm]", 0)
            report["after_turret_wavelength_nm"] = reported_nm
            if cancel.is_set():
                raise AcquisitionCancelled("Cambio cancelado antes de ajustar λ.")
            if report["turret_movement_sent"] or self.grating_change_pending or abs(reported_nm-target) > self.settings.tolerance_nm:
                report.update(state="moving_wavelength", wavelength_movement_sent=True)
                self.grating_change_pending = True
                # No usa mono_setPosition: mueve físicamente, no redefine el origen.
                actual = self._move_to(target, cancel)
            else:
                actual = self.snapshot()
            if actual["grating"] != "G{}".format(position):
                raise HoribaError("El grating cambió durante el ajuste de λ.")
            if abs(actual["wavelength_nm"]-target) > self.settings.tolerance_nm:
                raise HoribaError("La λ final leída no coincide con la solicitada.")
            self.grating_change_pending = False
            self.last_snapshot = dict(actual, grating_change_pending=False)
            report.update(state="confirmed", actual=self.last_snapshot, finished_utc=utc_now())
            self.status = InstrumentStatus("ready", "Grating {} y λ={} nm confirmados por ICL.".format(position, actual["wavelength_nm"]))
            logger.info("Torreta índice=%s solicitud=%s λ=%s lectura=%s", self.settings.index, position, target, self.last_snapshot)
            return report
        except Exception as exc:
            report.update(state="error", error=str(exc))
            sent = report["turret_movement_sent"] or report["wavelength_movement_sent"]
            if sent:
                self.grating_change_pending = True
            detail = str(exc)
            if sent:
                detail += " El movimiento puede continuar; consulte y complete grating y λ antes de adquirir."
            self.drop()
            self.status = InstrumentStatus("error", detail)
            logger.warning("Torreta índice=%s solicitud=%s λ=%s enviada=%s lectura_tras_torreta=%s: %s",
                           self.settings.index, position, target, sent, report.get("after_turret_wavelength_nm"), detail)
            if isinstance(exc, (AcquisitionCancelled, ValueError)):
                raise type(exc)(detail) from exc
            raise HoribaError(detail) from exc
        finally:
            self._initialization_lock.release()

    def describe(self):
        return dict(interface="ICL", settings=asdict(self.settings), icl=self.info, device=self.device,
                    initialization=self.last_initialization, slit_ports=dict(self.slit_ports),
                    last_slit_move=self.last_slit_move, output_port=self.output_position,
                    last_output_move=self.last_output_move, gratings=[dict(self.gratings[n]) for n in sorted(self.gratings)],
                    last_grating_move=self.last_grating_move, grating_change_pending=self.grating_change_pending)

    def move_to(self, target, cancel=None):
        if self.grating_change_pending:
            raise HoribaError("Cambio espectral pendiente: aplique grating y λ antes de adquirir.")
        return self._move_to(target, cancel)

    def _move_to(self, target, cancel=None):
        target = number(target, "Destino [nm]", 0, True)
        if not self.settings.minimum_nm <= target <= self.settings.maximum_nm:
            raise ValueError("Destino fuera de los límites configurados para HORIBA.")
        cancel = cancel or threading.Event()
        self.require_ready()
        if cancel.is_set():
            raise AcquisitionCancelled("Barrido cancelado antes del movimiento.")
        deadline = time.monotonic() + self.settings.move_timeout_s
        # moveToPosition mueve el motor. setPosition redefine la calibración y NO se utiliza.
        self.request("mono_moveToPosition", dict(index=self.settings.index, wavelength=target),
                     timeout=self.settings.move_timeout_s)
        while True:
            if cancel.is_set():
                raise AcquisitionCancelled("Barrido cancelado. El movimiento ya enviado puede continuar; espere a que HORIBA quede libre.")
            if time.monotonic() >= deadline:
                self.drop()
                raise HoribaError("Timeout de movimiento; no se adquirió el punto. Reconecte y compruebe HORIBA.")
            if not self.command("mono_isBusy")["busy"]:
                break
            cancel.wait(.1)
        actual = self.snapshot()
        if abs(actual["wavelength_nm"]-target) > self.settings.tolerance_nm:
            raise HoribaError("HORIBA informó {} nm para un destino de {} nm; excede la tolerancia.".format(actual["wavelength_nm"], target))
        return dict(actual, target_nm=target)

    def drop(self):
        if self.socket is not None:
            try:
                self.socket.close()
            except Exception:
                pass
        self.socket, self.opened = None, False
        self.slit_positions = {}
        self.output_position = None
        self.grating_position = self.grating_wavelength_nm = None
        self.status = InstrumentStatus()

    def close(self):
        if self.opened and self.socket is not None:
            try:
                self.command("mono_close")
            except Exception:
                pass
        self.drop()


class SimulatedMonochromator:
    def __init__(self, reader, experiment):
        self.reader, self.experiment = reader, experiment

    def connect(self):
        return self.snapshot()

    def snapshot(self):
        return dict(wavelength_nm=self.reader.wavelength_nm, grating=self.experiment.grating,
                    slit_in_mm=self.experiment.slit_in_mm, slit_out_mm=self.experiment.slit_out_mm)

    def describe(self):
        return dict(interface="SIM", device="Monocromador simulado")

    def move_to(self, target, cancel=None):
        if cancel and cancel.is_set():
            raise AcquisitionCancelled("Barrido simulado cancelado.")
        self.reader.wavelength_nm = target
        return dict(self.snapshot(), target_nm=target)

    def close(self):
        pass
