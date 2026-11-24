"""Barrido en apertura de rendijas: para cada par entrada/salida, barrido completo en λ.

Todas las configuraciones usan la misma grilla de λ. Cada punto se agrega
inmediatamente a un único CSV (con flush+fsync) y un JSON adjunto registra
parámetros, informes de rendijas y el estado final. No hay reintentos ni
reversión automática: ante error/cancelación se conservan las filas completas
y las rendijas quedan donde estén; consulte antes de volver a mover.
"""
import csv
from dataclasses import asdict
from decimal import Decimal
import json
import math
import os
from pathlib import Path
import re
import threading
from datetime import datetime

from .acquisition import AcquisitionCancelled, acquire
from .horiba import HoribaError
from .models import number, uid, utc_now
from .storage import atomic_text


MAX_SLIT_MM = 7.0
MAX_TOTAL_POINTS = 100000
CSV_FIELDS = ["point_index", "config_index", "slit_in_mm", "slit_out_mm",
              "slit_in_read_mm", "slit_out_read_mm", "wavelength_target_nm", "wavelength_nm",
              "mean_v", "std_v", "n", "channel", "grating", "output_port",
              "started_utc", "finished_utc", "readings_v"]


def parse_slit_list(text, label):
    """Valores separados por ';', espacios o saltos de línea. Admite coma decimal."""
    items = [item for item in re.split(r"[;\s]+", str(text).strip()) if item]
    if not items:
        raise ValueError("{}: ingrese al menos una apertura.".format(label))
    values = [number(item, label, 0, True) for item in items]
    if any(value > MAX_SLIT_MM for value in values):
        raise ValueError("{}: apertura máxima {} mm.".format(label, MAX_SLIT_MM))
    return values


def slit_plan_from_lists(entrance_text, exit_text):
    """Pares (entrada, salida) en el orden dado. Una lista de un único valor se
    repite para todas las configuraciones de la otra (p. ej. entrada fija)."""
    entrance = parse_slit_list(entrance_text, "Lista de entrada [mm]")
    exit_ = parse_slit_list(exit_text, "Lista de salida [mm]")
    if len(entrance) == 1 and len(exit_) > 1:
        entrance = entrance * len(exit_)
    elif len(exit_) == 1 and len(entrance) > 1:
        exit_ = exit_ * len(entrance)
    if len(entrance) != len(exit_):
        raise ValueError("Las listas de entrada ({}) y salida ({}) deben tener igual longitud, "
                         "o una de ellas un único valor.".format(len(entrance), len(exit_)))
    return list(zip(entrance, exit_))


def slit_plan_from_range(minimum, maximum, step):
    """Entrada = salida; incluye el máximo sólo si pertenece a la grilla."""
    minimum = number(minimum, "Apertura mínima [mm]", 0, True)
    maximum = number(maximum, "Apertura máxima [mm]", 0, True)
    step = number(step, "Paso de apertura [mm]", 0, True)
    if maximum < minimum:
        raise ValueError("La apertura máxima debe ser mayor o igual que la mínima.")
    if maximum > MAX_SLIT_MM:
        raise ValueError("Apertura máxima: {} mm.".format(MAX_SLIT_MM))
    a, b, s = (Decimal(str(x)) for x in (minimum, maximum, step))
    count = int((b-a)/s) + 1
    if count > 1000:
        raise ValueError("El barrido de aperturas supera 1000 configuraciones.")
    return [(float(a+i*s),)*2 for i in range(count)]


class SimulatedSlitInstrument:
    """Simulación coherente con el lector SIM: el FWHM triangular depende de la
    rendija más ancha, √(δ0² + (a·w)²), como en el ejemplo sintético."""
    def __init__(self, reader, delta0_nm=.08, dispersion_nm_per_mm=1.8, grating="G1"):
        self.reader, self.delta0_nm, self.dispersion = reader, delta0_nm, dispersion_nm_per_mm
        self.grating = grating
        self.original_fwhm = reader.fwhm_nm
        self.slits = {}

    def connect(self):
        return self.snapshot()

    def snapshot(self):
        return dict(wavelength_nm=self.reader.wavelength_nm, grating=self.grating, **self.slits)

    def move_slits(self, entrance_mm=None, exit_mm=None, cancel=None):
        if cancel and cancel.is_set():
            raise AcquisitionCancelled("Barrido de rendijas simulado cancelado.")
        self.slits = dict(slit_in_mm=entrance_mm, slit_out_mm=exit_mm)
        width = max(entrance_mm, exit_mm)
        self.reader.fwhm_nm = math.hypot(self.delta0_nm, self.dispersion*width)
        return dict(state="confirmed", requested_mm=dict(self.slits), actual_mm=dict(self.slits),
                    simulated_fwhm_nm=self.reader.fwhm_nm)

    def move_to(self, target, cancel=None):
        if cancel and cancel.is_set():
            raise AcquisitionCancelled("Barrido de rendijas simulado cancelado.")
        self.reader.wavelength_nm = target
        return dict(self.snapshot(), target_nm=target)

    def describe(self):
        return dict(interface="SIM", model="FWHM = sqrt(delta0^2 + (a*max(entrada, salida))^2)",
                    delta0_nm=self.delta0_nm, a_nm_per_mm=self.dispersion)

    def close(self):
        self.reader.fwhm_nm = self.original_fwhm


def new_sweep_path(folder):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return folder / "barrido_rendijas_{}_{}.csv".format(stamp, uid()[:6])


def _append_row(path, row):
    with Path(path).open("a", encoding="utf-8", newline="") as stream:
        csv.DictWriter(stream, fieldnames=CSV_FIELDS).writerow(row)
        stream.flush()
        os.fsync(stream.fileno())


def run_slit_sweep(instrument, reader, acquisition, wavelengths, slit_pairs, csv_path,
                   cancel=None, progress=None, status=None, tolerance_nm=1e-9,
                   slit_tolerance_mm=1e-6, metadata=None, on_point=None):
    """Ejecuta en el worker. Devuelve un resumen; ante fallo escribe el estado y relanza."""
    acquisition.validate()
    cancel = cancel or threading.Event()
    wavelengths, slit_pairs = list(wavelengths), [tuple(p) for p in slit_pairs]
    total = len(wavelengths) * len(slit_pairs)
    if not wavelengths or not slit_pairs:
        raise ValueError("El barrido no contiene puntos.")
    if total > MAX_TOTAL_POINTS:
        raise ValueError("El barrido combinado supera {} puntos.".format(MAX_TOTAL_POINTS))
    csv_path = Path(csv_path)
    json_path = csv_path.with_suffix(".json")
    with csv_path.open("x", encoding="utf-8", newline="") as stream:
        csv.DictWriter(stream, fieldnames=CSV_FIELDS).writeheader()
    info = dict(kind="slit_sweep", created_utc=utc_now(), state="running", csv=csv_path.name,
                wavelengths_nm=wavelengths, slit_pairs_mm=[list(p) for p in slit_pairs],
                acquisition=asdict(acquisition), reader=reader.describe(),
                instrument=instrument.describe(), configurations=[], points_written=0,
                **(metadata or {}))

    def save_info():
        atomic_text(json_path, json.dumps(info, ensure_ascii=False, indent=2, default=str))

    save_info()
    written = 0
    try:
        for config_index, (entrance, exit_) in enumerate(slit_pairs):
            if cancel.is_set():
                raise AcquisitionCancelled("Barrido de rendijas cancelado.")
            if status:
                status("Configuración {}/{}: ajustando rendijas a {} / {} mm…".format(
                    config_index+1, len(slit_pairs), entrance, exit_))
            report = instrument.move_slits(entrance_mm=entrance, exit_mm=exit_, cancel=cancel)
            info["configurations"].append(dict(config_index=config_index, requested_mm=[entrance, exit_],
                                               slit_report=report, started_utc=utc_now()))
            save_info()
            for target in wavelengths:
                if cancel.is_set():
                    raise AcquisitionCancelled("Barrido de rendijas cancelado.")
                if status:
                    status("Rendijas {} / {} mm · λ={} nm · punto {}/{}".format(
                        entrance, exit_, target, written+1, total))
                before = instrument.move_to(target, cancel)
                batch = acquire(reader, acquisition, cancel, progress)
                after = instrument.snapshot()
                if abs(after["wavelength_nm"]-before["wavelength_nm"]) > tolerance_nm:
                    raise HoribaError("λ cambió durante la adquisición; punto descartado.")
                for key in ("slit_in_mm", "slit_out_mm", "grating", "output_port"):
                    if key in before and key in after:
                        changed = (abs(after[key]-before[key]) > slit_tolerance_mm
                                   if key.startswith("slit") else after[key] != before[key])
                        if changed:
                            raise HoribaError("{} cambió durante la adquisición; punto descartado.".format(key))
                if cancel.is_set():
                    raise AcquisitionCancelled("Punto cancelado antes de guardar.")
                values = batch.values_v
                mean = sum(values)/len(values)
                std = math.sqrt(sum((v-mean)**2 for v in values)/(len(values)-1))
                _append_row(csv_path, dict(
                    point_index=written, config_index=config_index,
                    slit_in_mm=entrance, slit_out_mm=exit_,
                    slit_in_read_mm=after.get("slit_in_mm", ""), slit_out_read_mm=after.get("slit_out_mm", ""),
                    wavelength_target_nm=target, wavelength_nm=after["wavelength_nm"],
                    mean_v=repr(mean), std_v=repr(std), n=len(values), channel=acquisition.channel,
                    grating=after.get("grating", ""), output_port=after.get("output_port", ""),
                    started_utc=batch.started_utc, finished_utc=batch.finished_utc,
                    readings_v=";".join(repr(v) for v in values)))
                written += 1
                if on_point:
                    # Sólo después de guardar: la gráfica nunca muestra un punto ausente del CSV.
                    on_point(dict(config_index=config_index, slit_in_mm=entrance, slit_out_mm=exit_,
                                  wavelength_nm=after["wavelength_nm"], mean_v=mean, std_v=std))
            info["configurations"][-1]["finished_utc"] = utc_now()
            info["points_written"] = written
            save_info()
        info.update(state="completed", finished_utc=utc_now(), points_written=written)
        return dict(csv=str(csv_path), json=str(json_path), points=written,
                    configurations=len(slit_pairs), last_slits_mm=list(slit_pairs[-1]))
    except Exception as exc:
        info.update(state="cancelled" if isinstance(exc, AcquisitionCancelled) else "error",
                    error=str(exc), finished_utc=utc_now(), points_written=written)
        raise
    finally:
        try:
            save_info()
        except Exception:
            pass
