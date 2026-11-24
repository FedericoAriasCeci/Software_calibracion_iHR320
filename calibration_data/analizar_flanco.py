"""
Perfil del spot en la rendija de entrada a partir de un barrido fino de UN flanco de la meseta.

Configuración de la medición: entrada mucho más ancha que el spot (p. ej. 2 mm), salida al máximo
(7 mm) y barrido fino en λ (≈0,02 nm) sobre el flanco de subida. Mientras la imagen del spot cruza
la mordaza de la salida, la señal es la integral acumulada del perfil horizontal del spot:
    S(λ) = fondo(λ) + tope(λ) · F(λ),   F = acumulada del perfil
y la derivada dS/dλ es el perfil. Para pasar de nm a µm en la entrada: x = Δλ / (D·M).

Uso:
    python analizar_flanco.py flanco.csv [--meseta otro.csv] [--w-ent 2.0] [--D 2.30] [--M 1.125]

--meseta: CSV con puntos de la meseta (misma configuración de rendijas) para fijar mejor el tope;
           se usan los puntos hasta 4 nm más allá del final del barrido fino.
Modelos: un spot gaussiano, y núcleo + alas (dos gaussianas, pueden estar corridas entre sí).
"""
import argparse
import os
import warnings

import numpy as np
import pandas as pd
from scipy.optimize import OptimizeWarning, curve_fit
from scipy.signal import savgol_filter
from scipy.stats import norm

SERIE, SERIE2, TINTA, TINTA2, SUP = "#2a78d6", "#eb6834", "#0b0b0b", "#52514e", "#fcfcfb"


def m_gauss(x, b0, b1, A, S, xc, s):
    x0 = xc
    return b0 + b1 * (x - x0) + (A + S * (x - x0)) * norm.cdf((x - xc) / s)


def m_doble(x, b0, b1, A, S, xc, s1, s2, fh, dc):
    F = (1 - fh) * norm.cdf((x - xc) / s1) + fh * norm.cdf((x - xc - dc) / s2)
    return b0 + b1 * (x - xc) + (A + S * (x - xc)) * F


def perfil_doble(x, p):
    _, _, _, _, xc, s1, s2, fh, dc = p
    return (1 - fh) * norm.pdf(x, xc, s1) + fh * norm.pdf(x, xc + dc, s2)


def fwhm(x, y):
    i = int(np.argmax(y))
    idx = np.where(y >= y[i] / 2)[0]
    return x[idx[-1]] - x[idx[0]]


def estilo(a):
    a.set_facecolor(SUP)
    for s in ("top", "right"):
        a.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        a.spines[s].set_color(TINTA2)
    a.tick_params(colors=TINTA2, labelsize=9)
    a.grid(True, color="#e6e5e1", lw=0.6)
    a.set_axisbelow(True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--meseta", default=None)
    ap.add_argument("--w-ent", type=float, default=None, help="ancho de entrada a tomar del CSV de meseta")
    ap.add_argument("--D", type=float, default=2.30)
    ap.add_argument("--M", type=float, default=1.125)
    a = ap.parse_args()
    DM = a.D * a.M

    f = pd.read_csv(a.csv).sort_values("wavelength_nm")
    xf, yf = f.wavelength_nm.to_numpy(), f.mean_v.to_numpy()
    ef = np.maximum((f.std_v / np.sqrt(f.n)).to_numpy(), 1e-4)
    x, y, e = xf, yf, ef
    xm = ym = None
    if a.meseta:
        m = pd.read_csv(a.meseta)
        w_ent = a.w_ent if a.w_ent is not None else f.slit_in_mm.iloc[0]
        m = m[np.isclose(m.slit_in_mm, w_ent) & (m.wavelength_nm > xf.max() + 0.3) & (m.wavelength_nm < xf.max() + 4.5)]
        xm, ym = m.wavelength_nm.to_numpy(), m.mean_v.to_numpy()
        em = np.maximum((m.std_v / np.sqrt(m.n)).to_numpy(), 1e-4)
        x, y, e = np.r_[xf, xm], np.r_[yf, ym], np.r_[ef, em]
    e = np.maximum(e, 0.4 * np.median(ef))

    b0, top = np.median(yf[:10]), yf.max()
    c0 = xf[np.argmax(yf > b0 + (top - b0) / 2)]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", OptimizeWarning)
        p1, C1 = curve_fit(m_gauss, x, y, p0=[b0, 0, top - b0, 0, c0, 0.4], sigma=e, maxfev=50000)
        lo = [-np.inf, -1, 0, -1, c0 - 2, 0.02, 0.05, 0, -1]
        hi = [np.inf, 1, 10 * (top - b0), 1, c0 + 2, 2, 5, 0.95, 1]
        p2, C2 = curve_fit(m_doble, x, y, p0=[b0, 0, top - b0, 0, c0, 0.3, 0.8, 0.3, 0], sigma=e,
                           bounds=(lo, hi), maxfev=200000)
    chi1 = np.sum(((y - m_gauss(x, *p1)) / e) ** 2) / (len(x) - 6)
    chi2 = np.sum(((y - m_doble(x, *p2)) / e) ** 2) / (len(x) - 9)
    er2 = np.sqrt(np.abs(np.diag(C2)))

    xx = np.linspace(p2[4] - 5, p2[4] + 5, 40001)
    prof = perfil_doble(xx, p2)
    fw_nm = fwhm(xx, prof)
    paso = float(np.median(np.diff(xf)))
    ventana = max(5, int(round(0.12 / paso)) | 1)
    dy = savgol_filter(yf, ventana, 2, deriv=1, delta=paso)
    tope_rel = p2[3] / p2[2]

    print(f"Paso: {paso:.3f} nm  ->  {paso / DM * 1e3:.1f} µm en la entrada (D·M = {DM:.3f} nm/mm)")
    print(f"Un spot gaussiano : FWHM imagen = {2.3548 * p1[5]:.3f} nm -> spot {2.3548 * p1[5] / DM * 1e3:.0f} µm, chi2r = {chi1:.1f}")
    print(f"Núcleo + alas     : núcleo {2.3548 * p2[5]:.3f} nm, alas {2.3548 * p2[6]:.3f} nm ({p2[7] * 100:.0f} % de la potencia), "
          f"corrimiento alas-núcleo {p2[8]:+.3f} ± {er2[8]:.3f} nm, chi2r = {chi2:.1f}")
    print(f"  Perfil total: FWHM = {fw_nm:.3f} nm  ->  spot = {fw_nm / DM * 1e3:.0f} µm FWHM en la entrada")
    print(f"  Mitad del flanco en {p2[4]:.3f} nm; pendiente del tope {tope_rel * 100:+.2f} %/nm")

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, (ax, ax2) = plt.subplots(1, 2, figsize=(12.5, 4.8))
    fig.patch.set_facecolor(SUP)
    for q in (ax, ax2):
        estilo(q)
    ax.plot(xf, yf, "o", ms=3.2, color=SERIE, label="Barrido fino (0,02 nm)")
    if xm is not None and len(xm):
        ax.plot(xm, ym, "s", ms=6, color=SERIE2, mec=SUP, label="Meseta (barrido grueso)")
    xg = np.linspace(x.min(), x.max(), 2000)
    ax.plot(xg, m_doble(xg, *p2), color=TINTA, lw=2, label=f"Núcleo + alas (χ²r = {chi2:.0f})")
    ax.plot(xg, m_gauss(xg, *p1), color=TINTA2, lw=1.2, ls="--", label=f"Un spot gaussiano (χ²r = {chi1:.0f})")
    ax.set_xlabel("Longitud de onda leída (nm)", color=TINTA2)
    ax.set_ylabel("Señal AUX (V)", color=TINTA2)
    ax.set_title("Flanco de subida (entrada 2 mm, salida 7 mm)", loc="left", fontsize=11, color=TINTA)
    ax.legend(frameon=False, fontsize=8.5, labelcolor=TINTA2, loc="upper left")

    u = (xf - p2[4]) / DM * 1e3
    norm_ = p2[2]
    ax2.plot(u, dy / norm_ * DM / 1e3, "o", ms=3.2, color=SERIE, label="Derivada de los datos")
    uu = (xx - p2[4]) / DM * 1e3
    ax2.plot(uu, prof * DM / 1e3, color=TINTA, lw=2, label="Perfil del ajuste núcleo + alas")
    ax2.plot(uu, norm.pdf(xx, p1[4], p1[5]) * DM / 1e3, color=TINTA2, lw=1.2, ls="--", label="Perfil gaussiano")
    pk = (prof * DM / 1e3).max()
    ax2.annotate("", xy=(-fw_nm / DM * 500, pk / 2), xytext=(fw_nm / DM * 500, pk / 2),
                 arrowprops=dict(arrowstyle="<->", color=TINTA2, lw=1))
    ax2.text(fw_nm / DM * 500 + 25, pk / 2, f"FWHM ≈ {fw_nm / DM * 1e3:.0f} µm", va="center", fontsize=9.5, color=TINTA)
    ax2.set_xlim(-900, 900)
    ax2.set_xlabel("Posición en la rendija de entrada (µm, respecto del centro del spot)", color=TINTA2)
    ax2.set_ylabel("Intensidad relativa (1/µm)", color=TINTA2)
    ax2.set_title("Perfil horizontal del spot (derivada del flanco)", loc="left", fontsize=11, color=TINTA)
    ax2.legend(frameon=False, fontsize=8.5, labelcolor=TINTA2, loc="upper left")
    fig.tight_layout()
    base = os.path.splitext(a.csv)[0]
    fig.savefig(f"{base}_perfil.png", dpi=160, facecolor=SUP)
    pd.DataFrame([{
        "flanco_mitad_nm": p2[4], "gauss_fwhm_nm": 2.3548 * p1[5], "gauss_spot_um": 2.3548 * p1[5] / DM * 1e3,
        "gauss_chi2r": chi1, "nucleo_fwhm_nm": 2.3548 * p2[5], "alas_fwhm_nm": 2.3548 * p2[6], "fraccion_alas": p2[7],
        "corrimiento_alas_nm": p2[8], "corrimiento_alas_err_nm": er2[8], "perfil_fwhm_nm": fw_nm,
        "spot_fwhm_um": fw_nm / DM * 1e3, "doble_chi2r": chi2, "pendiente_tope_pct_por_nm": 100 * tope_rel,
        "D_nm_por_mm": a.D, "M": a.M,
    }]).to_csv(f"{base}_perfil.csv", index=False)


if __name__ == "__main__":
    main()
