"""Lectores de tensión independientes; ninguna referencia al controlador HORIBA.

Manual SRS SR830 rev. 2.5: 4-24, 4-27, 5-1/2, 5-9/10.
"""
from dataclasses import asdict, dataclass
import math
import re
import numpy as np

from .models import integer, number


class CommunicationError(RuntimeError):
    pass


@dataclass
class ConnectionSettings:
    interface: str = "SIM"
    resource: str = ""
    com_port: str = "COM1"
    gpib_board: int = 0
    gpib_address: int = 8
    baud_rate: int = 9600
    parity: str = "N"
    stop_bits: int = 1
    timeout_ms: int = 3000
    visa_library: str = ""

    def validate(self):
        if self.interface not in ("SIM", "RS232", "GPIB"):
            raise ValueError("Interfaz inválida.")
        self.timeout_ms = integer(self.timeout_ms, "Timeout (ms)", 100, 60000)
        self.gpib_board = integer(self.gpib_board, "Placa GPIB", 0, 99)
        self.gpib_address = integer(self.gpib_address, "Dirección GPIB", 0, 30)
        self.baud_rate = integer(self.baud_rate, "Baud", 300, 19200)
        if self.baud_rate not in (300, 600, 1200, 2400, 4800, 9600, 19200):
            raise ValueError("Baud no admitido por el SR830.")
        self.stop_bits = integer(self.stop_bits, "Bits de parada", 1, 2)
        if self.parity not in ("N", "E", "O"):
            raise ValueError("Paridad: N, E u O.")
        return self

    def resource_name(self):
        if self.resource.strip():
            resource = self.resource.strip()
        elif self.interface == "RS232":
            match = re.fullmatch(r"COM(\d+)", self.com_port.strip().upper())
            if not match:
                raise ValueError("Puerto serial: use COM seguido de un número, o un recurso ASRL.")
            resource = "ASRL{}::INSTR".format(match.group(1))
        else:
            resource = "GPIB{}::{}::INSTR".format(self.gpib_board, self.gpib_address)
        prefix = "ASRL" if self.interface == "RS232" else "GPIB"
        if not resource.upper().startswith(prefix):
            raise ValueError("El recurso no coincide con la interfaz seleccionada.")
        return resource


def discover_resources(visa_library=""):
    resources, errors = [], []
    try:
        import pyvisa
        rm = pyvisa.ResourceManager(visa_library) if visa_library else pyvisa.ResourceManager()
        try:
            resources.extend(rm.list_resources())
        finally:
            rm.close()
    except Exception as exc:
        errors.append("VISA: " + str(exc))
    try:
        from serial.tools import list_ports
        resources.extend(p.device for p in list_ports.comports())
    except Exception as exc:
        errors.append("Puertos COM: " + str(exc))
    return sorted(set(resources)), errors


class SR830Reader:
    """Un único hilo utiliza esta instancia. Sólo escribe OUTX; no hace reset.

    read_voltage es el contrato que puede implementar un futuro lector OUTP? 3.
    RS232 usa 8 bits, paridad elegida, sin control de flujo: modo de tres hilos
    admitido por SRS. El usuario iguala baud/paridad en el panel del instrumento.
    """
    def __init__(self, settings, manager_factory=None):
        self.settings = settings.validate()
        self.manager_factory = manager_factory
        self.manager = None
        self.resource = None
        self.identity = ""

    def connect(self):
        self.close()
        try:
            import pyvisa
            from pyvisa import constants
            factory = self.manager_factory or pyvisa.ResourceManager
            self.manager = factory(self.settings.visa_library) if self.settings.visa_library else factory()
            self.resource = self.manager.open_resource(
                self.settings.resource_name(), open_timeout=self.settings.timeout_ms)
            r = self.resource
            r.timeout = self.settings.timeout_ms
            serial = self.settings.interface == "RS232"
            r.write_termination = "\r" if serial else "\n"
            r.read_termination = "\r" if serial else "\n"
            if serial:
                r.baud_rate = self.settings.baud_rate
                r.data_bits = 8
                r.parity = {"N": constants.Parity.none, "E": constants.Parity.even,
                            "O": constants.Parity.odd}[self.settings.parity]
                r.stop_bits = {1: constants.StopBits.one, 2: constants.StopBits.two}[self.settings.stop_bits]
                r.flow_control = constants.VI_ASRL_FLOW_NONE
            r.write("OUTX {}".format(0 if serial else 1))
            self.identity = self.test()
            return self.identity
        except Exception as exc:
            self.close()
            raise CommunicationError("No se pudo conectar al SR830: " + str(exc)) from exc

    def test(self):
        if self.resource is None:
            raise CommunicationError("SR830 desconectado.")
        try:
            identity = self.resource.query("*IDN?").strip()
            if not re.search(r"\bSR830\b", identity, re.IGNORECASE):
                raise ValueError("Identidad inesperada: " + identity)
            return identity
        except Exception as exc:
            self.close()
            raise CommunicationError("Falló *IDN?: " + str(exc)) from exc

    def read_voltage(self, channel=1):
        channel = integer(channel, "Canal AUX", 1, 4)
        if self.resource is None:
            raise CommunicationError("SR830 desconectado.")
        try:
            value = float(self.resource.query("OAUX? {}".format(channel)))
            if not math.isfinite(value):
                raise ValueError("Respuesta no finita.")
            return value
        except Exception as exc:
            # No se reintenta una query: una respuesta tardía podría desfasar muestras.
            self.close()
            raise CommunicationError("Falló la lectura AUX: " + str(exc)) from exc

    def describe(self):
        return dict(asdict(self.settings), identity=self.identity, source="OAUX", units="V")

    def close(self):
        for obj in (self.resource, self.manager):
            if obj is not None:
                try:
                    obj.close()
                except Exception:
                    pass
        self.resource = self.manager = None


class SimulatedReader:
    """Triángulo: max(1 - |lambda-centro|/FWHM, 0). FWHM exacto conocido."""
    def __init__(self, center_nm=532.0, fwhm_nm=0.4, amplitude_v=1.0,
                 background_v=0.03, noise_v=0.002, seed=830):
        self.center_nm = number(center_nm, "Centro simulado", 0, True)
        self.fwhm_nm = number(fwhm_nm, "FWHM simulado", 0, True)
        self.amplitude_v = number(amplitude_v, "Amplitud simulada", 0, True)
        self.background_v = number(background_v, "Fondo simulado")
        self.noise_v = number(noise_v, "Ruido simulado", 0)
        self.seed = integer(seed, "Semilla", 0, 2**32-1)
        self.rng = np.random.RandomState(self.seed)
        self.wavelength_nm = self.center_nm
        self.blocked = False
        self.connected = False
        self.identity = "SIMULADOR,SR830,TRIANGULAR,1.0"

    def connect(self):
        self.connected = True
        return self.identity

    def test(self):
        if not self.connected:
            raise CommunicationError("Simulador desconectado.")
        return self.identity

    def read_voltage(self, channel=1):
        self.test()
        integer(channel, "Canal AUX", 1, 4)
        profile = 0 if self.blocked else max(1 - abs(self.wavelength_nm-self.center_nm)/self.fwhm_nm, 0)
        return float(self.background_v + self.amplitude_v*profile + self.rng.normal(0, self.noise_v))

    def describe(self):
        return {"interface": "SIM", "identity": self.identity, "source": "OAUX", "units": "V",
                "center_nm": self.center_nm, "fwhm_nm": self.fwhm_nm,
                "amplitude_v": self.amplitude_v, "background_v": self.background_v,
                "noise_v": self.noise_v, "seed": self.seed}

    def close(self):
        self.connected = False

