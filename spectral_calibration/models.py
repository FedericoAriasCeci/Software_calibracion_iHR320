"""Datos de experimento, muestras crudas y validaciones compartidas."""
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import math
import uuid

import numpy as np


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def uid():
    return uuid.uuid4().hex[:12]


def number(value, label, minimum=None, strict=False):
    try:
        result = float(str(value).replace(",", "."))
    except (ValueError, TypeError):
        raise ValueError("{}: ingrese un número.".format(label))
    if not math.isfinite(result):
        raise ValueError("{}: debe ser finito.".format(label))
    if minimum is not None and (result < minimum or (strict and result == minimum)):
        raise ValueError("{}: debe ser {} {}.".format(label, ">" if strict else ">=", minimum))
    return result


def integer(value, label, minimum=0, maximum=100000):
    result = number(value, label, minimum)
    if not result.is_integer() or result > maximum:
        raise ValueError("{}: entero entre {} y {}.".format(label, minimum, maximum))
    return int(result)


@dataclass
class Experiment:
    experiment_id: str = "calibracion"
    grating: str = "G1"
    slit_in_mm: float = 0.1
    slit_out_mm: float = 0.1
    laser_nominal_nm: float = 532.0
    grooves_per_mm: str = ""
    blaze_nm: str = ""
    notes: str = ""

    def validate(self):
        if not self.experiment_id.strip():
            raise ValueError("Ingrese un identificador de experimento.")
        if self.grating not in ("G1", "G2", "G3"):
            raise ValueError("Grating: seleccione G1, G2 o G3.")
        for name in ("slit_in_mm", "slit_out_mm", "laser_nominal_nm"):
            setattr(self, name, number(getattr(self, name), name, 0, True))
        for name in ("grooves_per_mm", "blaze_nm"):
            if str(getattr(self, name)).strip():
                setattr(self, name, number(getattr(self, name), name, 0, True))
        return self


@dataclass
class AcquisitionSettings:
    n: int = 20
    interval_s: float = 0.05
    wait_s: float = 0.3
    channel: int = 1

    def validate(self):
        self.n = integer(self.n, "N", 2)
        self.channel = integer(self.channel, "Canal AUX", 1, 4)
        self.interval_s = number(self.interval_s, "Intervalo", 0)
        self.wait_s = number(self.wait_s, "Espera", 0)
        return self


@dataclass
class Batch:
    values_v: list
    times_utc: list
    elapsed_s: list
    settings: dict
    started_utc: str
    finished_utc: str

    def __post_init__(self):
        n = len(self.values_v)
        if n < 2 or len(self.times_utc) != n or len(self.elapsed_s) != n:
            raise ValueError("Lote incompleto: se necesitan al menos dos lecturas y sus tiempos.")
        if not np.all(np.isfinite(self.values_v)):
            raise ValueError("El lote contiene tensiones no finitas.")
        if self.settings.get("n") != n:
            raise ValueError("La cantidad de muestras no coincide con N.")

    @property
    def mean_v(self):
        return float(np.mean(self.values_v))

    @property
    def std_v(self):
        return float(np.std(self.values_v, ddof=1))

    @property
    def saturated(self):
        return bool(np.any(np.abs(self.values_v) >= 10.49))


@dataclass
class Point:
    wavelength_nm: float
    batch: Batch
    id: str = field(default_factory=uid)
    active: bool = True
    replaces_id: str = ""
    instrument: dict = field(default_factory=dict)


@dataclass
class Scan:
    experiment: Experiment
    connection: dict
    id: str = field(default_factory=uid)
    created_utc: str = field(default_factory=utc_now)
    completed: bool = False
    points: list = field(default_factory=list)
    backgrounds: list = field(default_factory=list)
    use_background: bool = True
    analysis: dict = field(default_factory=dict)
    horiba: dict = field(default_factory=dict)

    @property
    def active_points(self):
        return [p for p in self.points if p.active]

    @property
    def background(self):
        return Batch(**self.backgrounds[-1]["batch"]) if self.backgrounds else None

    @property
    def background_v(self):
        return self.background.mean_v if self.use_background and self.background else 0.0

    def add_point(self, wavelength_nm, batch, replace_last=False, instrument=None):
        if self.completed:
            raise ValueError("El barrido está finalizado; inicie uno nuevo.")
        wavelength_nm = number(wavelength_nm, "Longitud de onda", 0, True)
        previous = self.active_points[-1] if self.active_points and replace_last else None
        if replace_last and previous is None:
            raise ValueError("No hay un punto para repetir.")
        if previous is not None and abs(previous.wavelength_nm - wavelength_nm) > 1e-9:
            raise ValueError("Repetir requiere la misma longitud de onda que el último punto.")
        if previous:
            previous.active = False
        point = Point(wavelength_nm, batch, replaces_id=previous.id if previous else "", instrument=instrument or {})
        self.points.append(point)
        self.analysis = {}
        return point

    def delete_last(self):
        if self.completed:
            raise ValueError("El barrido está finalizado.")
        if not self.active_points:
            raise ValueError("No hay puntos para eliminar.")
        self.active_points[-1].active = False
        self.analysis = {}

    def set_background(self, batch):
        if self.completed:
            raise ValueError("El barrido está finalizado.")
        self.backgrounds.append({"id": uid(), "batch": asdict(batch)})
        self.analysis = {}

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        data = dict(data)
        data["experiment"] = Experiment(**data["experiment"]).validate()
        data["points"] = [Point(**dict(p, batch=Batch(**p["batch"]))) for p in data["points"]]
        return cls(**data)
