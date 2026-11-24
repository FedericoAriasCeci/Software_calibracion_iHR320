"""Snapshot JSON atómico como fuente de verdad; CSV y gráficos regenerables."""
import copy
import csv
from datetime import datetime
import io
import json
import os
from pathlib import Path
import socket

from .analysis import analyze
from .models import Scan, uid, utc_now


class ExportError(RuntimeError):
    """El JSON está a salvo; falló una exportación derivada."""


def atomic_text(path, text):
    path = Path(path)
    temporary = path.with_name(path.name + "." + uid() + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temporary), str(path))
    finally:
        if temporary.exists():
            temporary.unlink()


def csv_text(fields, rows):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue()


class SessionStore:
    def __init__(self, path, create=False):
        self.path = Path(path).resolve()
        self._lock = None
        if create:
            self.path.mkdir(parents=True, exist_ok=False)
        if not self.path.is_dir():
            raise ValueError("La carpeta de sesión no existe.")
        self._acquire_lock()
        try:
            if create:
                self.created_utc = utc_now()
                self.scans = []
                self._write_snapshot(self.scans)
            else:
                with (self.path / "session.json").open(encoding="utf-8-sig") as stream:
                    data = json.load(stream)
                if data.get("schema_version") != 1:
                    raise ValueError("Versión de sesión no admitida.")
                self.created_utc = data["created_utc"]
                self.scans = [Scan.from_dict(s) for s in data["scans"]]
            self.export_tables()
        except Exception:
            self.close()
            raise

    @classmethod
    def create(cls, root):
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        return cls(root / (datetime.now().strftime("session_%Y%m%d_%H%M%S_") + uid()[:6]), True)

    def _acquire_lock(self):
        self._lock = (self.path / ".session.lock").open("a+b")
        try:
            self._lock.seek(0, 2)
            if self._lock.tell() == 0:
                self._lock.write(b"0")
                self._lock.flush()
            self._lock.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError) as exc:
            self._lock.close()
            self._lock = None
            raise RuntimeError("La sesión ya está abierta en otra instancia.") from exc

    def close(self):
        if self._lock:
            self._lock.close()  # Libera el bloqueo incluso después de un cierre anormal.
            self._lock = None

    def _write_snapshot(self, scans):
        data = dict(schema_version=1, created_utc=self.created_utc,
                    saved_utc=utc_now(), units={"wavelength": "nm", "slit": "mm", "voltage": "V"},
                    host=socket.gethostname(), scans=[s.to_dict() for s in scans])
        atomic_text(self.path / "session.json", json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False))

    def get(self, scan_id):
        return next(s for s in self.scans if s.id == scan_id)

    def commit(self, change):
        # La memoria sólo cambia cuando el snapshot completo quedó guardado.
        scans = copy.deepcopy(self.scans)
        result = change(scans)
        self._write_snapshot(scans)
        self.scans = scans
        try:
            self.export_tables()
        except Exception as exc:
            raise ExportError("El punto está guardado en session.json; falló la exportación CSV: " + str(exc)) from exc
        return result

    def add_scan(self, scan):
        self.commit(lambda scans: scans.append(scan))
        return scan.id

    def update_scan(self, scan_id, change):
        return self.commit(lambda scans: change(next(s for s in scans if s.id == scan_id)))

    @staticmethod
    def stem(scan):
        w = format(scan.experiment.slit_in_mm, ".6g").replace(".", "p")
        return "grating_{}_slit_{}mm_{}".format(scan.experiment.grating, w, scan.id)

    def export_tables(self):
        summaries, inventory = [], []
        for scan in self.scans:
            exp = scan.experiment
            stem = self.stem(scan)
            point_rows, raw_rows, bg_rows = [], [], []
            for point in scan.points:
                b = point.batch
                common = dict(scan_id=scan.id, point_id=point.id, active=point.active,
                              wavelength_horiba_nm=point.wavelength_nm, laser_nominal_nm=exp.laser_nominal_nm,
                              grating=exp.grating, slit_in_mm=exp.slit_in_mm, slit_out_mm=exp.slit_out_mm)
                common["wavelength_target_nm"] = point.instrument.get("target_nm", "")
                row = dict(common, mean_v=b.mean_v, std_v=b.std_v,
                           corrected_v=b.mean_v-scan.background_v, n=len(b.values_v),
                           utc=b.finished_utc, background_v=scan.background_v,
                           background_applied=scan.use_background,
                           background_id=scan.backgrounds[-1]["id"] if scan.use_background and scan.backgrounds else "",
                           aux_channel=b.settings["channel"], saturated=b.saturated,
                           replaces_id=point.replaces_id, interval_s=b.settings["interval_s"], wait_s=b.settings["wait_s"])
                point_rows.append(row)
                for i, value in enumerate(b.values_v):
                    raw_rows.append(dict(common, sample_index=i+1, voltage_v=value,
                                         utc=b.times_utc[i], elapsed_s=b.elapsed_s[i], aux_channel=b.settings["channel"]))
            for background in scan.backgrounds:
                for i, value in enumerate(background["batch"]["values_v"]):
                    b = background["batch"]
                    bg_rows.append(dict(background_id=background["id"], sample_index=i+1, voltage_v=value,
                                        utc=b["times_utc"][i], elapsed_s=b["elapsed_s"][i], aux_channel=b["settings"]["channel"]))
            if point_rows:
                atomic_text(self.path / (stem+".csv"), csv_text(list(point_rows[0]), point_rows))
                atomic_text(self.path / (stem+"_readings.csv"), csv_text(list(raw_rows[0]), raw_rows))
            if bg_rows:
                atomic_text(self.path / (stem+"_backgrounds.csv"), csv_text(list(bg_rows[0]), bg_rows))
            a = scan.analysis
            summaries.append(dict(scan_id=scan.id, experiment_id=exp.experiment_id,
                grating=exp.grating, laser_nominal_nm=exp.laser_nominal_nm,
                slit_in_mm=exp.slit_in_mm, slit_out_mm=exp.slit_out_mm, completed=scan.completed,
                points=len(scan.active_points), peak_nm=a.get("peak_nm"), peak_v=a.get("peak_v"),
                fwhm_nm=a.get("fwhm_nm"), fwhm_uncertainty_nm=a.get("fwhm_uncertainty_nm"),
                mc_std_nm=a.get("mc_std_nm"), sampling_component_nm=a.get("sampling_component_nm"),
                asymmetry=a.get("asymmetry"), warnings=" | ".join(a.get("warnings", []))))
            inventory.append(dict(id=scan.id, experiment=scan.to_dict()["experiment"], horiba=scan.horiba,
                                  connection=scan.connection, completed=scan.completed,
                                  background_applied=scan.use_background, analysis=a, csv=stem+".csv"))
        fields = list(summaries[0]) if summaries else ["scan_id", "fwhm_nm"]
        atomic_text(self.path / "analysis_summary.csv", csv_text(fields, summaries))
        atomic_text(self.path / "metadata.json", json.dumps(dict(schema_version=1,
            created_utc=self.created_utc, canonical_data="session.json", scans=inventory),
            ensure_ascii=False, indent=2, allow_nan=False))

    def reanalyze(self):
        def change(scans):
            for scan in scans:
                if scan.completed:
                    scan.analysis = analyze(scan)
        self.commit(change)
