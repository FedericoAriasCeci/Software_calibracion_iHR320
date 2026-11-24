"""Figuras reutilizadas por Tk y por exportación sin pantalla."""
from pathlib import Path
import numpy as np
from matplotlib.figure import Figure
from matplotlib.backends.backend_agg import FigureCanvasAgg
from .analysis import profile_arrays, slit_series, fit_slits
from .models import uid


def draw_profiles(ax, scans, corrected=True, normalize=False, errors=True, raw_also=False):
    ax.clear()
    for scan in scans:
        x, raw, std = profile_arrays(scan)
        if len(x) == 0:
            continue
        y = raw-scan.background_v if corrected else raw
        scale = float(np.max(y)) if normalize else 1.0
        if scale <= 0:
            scale = 1.0
        label = "{} · entrada/salida={}/{} mm · {}".format(
            scan.experiment.grating, scan.experiment.slit_in_mm, scan.experiment.slit_out_mm, scan.id[:5])
        line, = ax.plot(x, y/scale, "o-", markersize=3, label=label)
        if errors:
            ax.errorbar(x, y/scale, yerr=std/scale, fmt="none", capsize=2, color=line.get_color(), alpha=0.5)
        if corrected and raw_also:
            ax.plot(x, raw/scale, "--", color=line.get_color(), alpha=0.5, label="Sin corregir · "+scan.id[:5])
        a = scan.analysis
        if a.get("fwhm_nm") is not None and corrected and a.get("background_v") == scan.background_v:
            ax.hlines(a["peak_v"]/2/scale, a["left_nm"], a["right_nm"], color=line.get_color(), linewidth=2)
    ax.set_xlabel(r"$\lambda_{\mathrm{HORIBA}}$ [nm]")
    ax.set_ylabel("Señal / máximo" if normalize else r"$V_{\mathrm{fotodiodo}}$ [V]")
    ax.grid(True, alpha=0.25)
    if ax.lines:
        ax.legend(fontsize=8)


def draw_slits(ax, scans, empirical=False):
    ax.clear()
    fits = []
    for (grating, laser), rows in sorted(slit_series(scans).items()):
        w = [r["slit_mm"] for r in rows]
        y = [r["fwhm_nm"] for r in rows]
        u = [r["uncertainty_nm"] or 0 for r in rows]
        label = "{} · láser {} nm".format(grating, laser)
        line = ax.errorbar(w, y, yerr=u, fmt="o", capsize=3, label=label)
        if empirical and len(set(w)) >= 3:
            try:
                params, model = fit_slits(rows)
                xx = np.linspace(0, max(w)*1.05, 200)
                ax.plot(xx, model(xx, params["delta0_nm"], params["a_nm_per_mm"]),
                        color=line[0].get_color(), label="Empírico · "+label)
                fits.append(dict(grating=grating, laser_nominal_nm=laser, **params))
            except (ImportError, ValueError, RuntimeError) as exc:
                fits.append(dict(grating=grating, error=str(exc)))
    ax.set_xlabel("Apertura común de las rendijas [mm]")
    ax.set_ylabel("FWHM [nm]")
    ax.grid(True, alpha=0.25)
    if ax.lines:
        ax.legend(fontsize=8)
    return fits


def export_plots(store):
    folder = store.path / "plots"
    folder.mkdir(exist_ok=True)
    stamp = uid()[:6]
    paths = []
    for scan in store.scans:
        if not scan.active_points:
            continue
        fig = Figure(figsize=(8, 5), tight_layout=True)
        FigureCanvasAgg(fig)
        draw_profiles(fig.add_subplot(111), [scan], raw_also=True)
        path = folder / (store.stem(scan)+"_"+stamp+".png")
        fig.savefig(str(path), dpi=150)
        paths.append(path)
    fig = Figure(figsize=(8, 5), tight_layout=True)
    FigureCanvasAgg(fig)
    draw_slits(fig.add_subplot(111), store.scans)
    path = folder / ("fwhm_vs_slit_"+stamp+".png")
    fig.savefig(str(path), dpi=150)
    paths.append(path)
    return paths
