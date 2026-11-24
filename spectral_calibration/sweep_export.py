"""Metadatos y exportación de barridos λ × rendijas, sin Tk ni comandos de hardware.

`run_slit_sweep` escribe el CSV de respaldo punto a punto y su JSON. Este módulo
sólo arma los bloques de metadatos que recibe por su parámetro `metadata`
(notas, plan, HORIBA, SR830), agrega el último estado confirmado al terminar y
exporta una copia (CSV idéntico + JSON) cuando el usuario pulsa Guardar datos.
Las columnas del CSV no se modifican.
"""
import copy
from dataclasses import asdict
import json
import logging
import os
from pathlib import Path
import shutil

from .models import uid, utc_now
from .storage import atomic_text


logger = logging.getLogger(__name__)


def _dumps(data):
    # Mismo criterio que slit_sweep: valores no serializables quedan como texto.
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def horiba_metadata(control, instrument, initial_state):
    """Configuración completa del monocromador al comenzar.

    `configuration` es `describe()` (para ICL: parámetros y timeouts, versión ICL,
    equipo, inicialización, puertos de rendijas, catálogo de gratings, salida y
    últimos ajustes); `initial_state` es la lectura confirmada antes del primer
    movimiento. No envía comandos: sólo copia datos ya leídos.
    """
    return dict(control=control, initial_state=copy.deepcopy(initial_state),
                configuration=copy.deepcopy(instrument.describe()))


def sr830_metadata(reader, acquisition):
    """Conexión (interfaz, recurso, timeout, identidad) y configuración de lectura."""
    data = dict(connection=copy.deepcopy(reader.describe()), acquisition=asdict(acquisition),
                query="OAUX? {}".format(acquisition.channel), units="V")
    settings = getattr(reader, "settings", None)
    if settings is not None and hasattr(settings, "resource_name"):
        try:
            data["visa_resource"] = settings.resource_name()
        except ValueError:
            pass
    return data


def background_metadata(batch, horiba_state=None, horiba_state_after=None):
    """Lote medido con el láser bloqueado (confirmado por el usuario) y estado del HORIBA.

    Conserva lecturas y tiempos individuales; no se resta en el CSV, que guarda
    tensiones crudas.
    """
    data = asdict(batch)
    data.update(mean_v=batch.mean_v, std_v=batch.std_v, n=len(batch.values_v), saturated=batch.saturated,
                channel=batch.settings.get("channel"), condition="láser bloqueado (confirmado por el usuario)",
                horiba_state=copy.deepcopy(horiba_state), horiba_state_after=copy.deepcopy(horiba_state_after))
    return data


def record_final_state(csv_path, final_state, configuration):
    """Agrega al JSON del barrido el último estado confirmado (datos en memoria).

    Se llama desde un `finally`: nunca relanza, para no ocultar el error original.
    """
    path = Path(csv_path).with_suffix(".json")
    try:
        if not path.exists():
            return False
        info = json.loads(path.read_text(encoding="utf-8"))
        horiba = info.setdefault("horiba", {})
        horiba["final_state"] = copy.deepcopy(final_state)
        horiba["final_configuration"] = copy.deepcopy(configuration)
        atomic_text(path, _dumps(info))
        return True
    except Exception as exc:
        logger.warning("No se registró el estado final del barrido %s: %s", path, exc)
        return False


def _atomic_copy(source, target):
    temporary = target.with_name(target.name + "." + uid() + ".tmp")
    try:
        shutil.copyfile(str(source), str(temporary))
        with temporary.open("rb+") as stream:
            os.fsync(stream.fileno())
        os.replace(str(temporary), str(target))
    finally:
        if temporary.exists():
            temporary.unlink()


def save_sweep(source_csv, target_csv, notes):
    """Exporta el barrido: CSV idéntico al respaldo y JSON con todos los metadatos.

    Las notas vigentes reemplazan a las registradas al iniciar, también en el
    JSON de respaldo. Si destino y respaldo coinciden, sólo se actualiza el JSON.
    """
    source_csv, target_csv = Path(source_csv), Path(target_csv)
    if target_csv.suffix.lower() != ".csv":
        target_csv = target_csv.with_name(target_csv.name + ".csv")
    if not source_csv.exists():
        raise ValueError("No se encontró el CSV de respaldo del barrido: {}".format(source_csv))
    source_json, target_json = source_csv.with_suffix(".json"), target_csv.with_suffix(".json")
    info = json.loads(source_json.read_text(encoding="utf-8")) if source_json.exists() else {}
    info["notes"] = notes
    same = target_csv.resolve() == source_csv.resolve()
    if source_json.exists() and not same:
        atomic_text(source_json, _dumps(info))
    if not same:
        _atomic_copy(source_csv, target_csv)
    export = dict(info, saved_utc=utc_now(), csv=target_csv.name, backup_csv=str(source_csv))
    atomic_text(target_json, _dumps(export))
    return dict(csv=str(target_csv), json=str(target_json), state=info.get("state"),
                points=info.get("points_written"))
