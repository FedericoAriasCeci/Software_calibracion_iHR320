"""
Ajuste de barridos de rendijas del iHR320 (fotodiodo -> AUX IN del SR830).

Uso (con el entorno pump_probe_env activado, o haciendo doble clic en Ajustar_barridos.bat):
    python ajustar_barrido.py                          -> todos los barrido_rendijas_*.csv de esta carpeta
    python ajustar_barrido.py archivo.csv [otro.csv]   -> solo esos archivos
    python ajustar_barrido.py slit_sweeps              -> todos los CSV de esa carpeta
    python ajustar_barrido.py "slit_sweeps\\*_1811*.csv" -> comodines (cmd no los expande solo)
Opciones: --D 2.28  --M 1.1  --sin-graficos  --min-puntos 8  --snr-min 5

Las rutas relativas se buscan en la carpeta actual y, si no están, en la carpeta del script,
así que funciona igual desde cmd, desde VS Code (botón Run) o con doble clic en el .bat.

Para cada configuración de rendijas del CSV ajusta:
  * modelo trapecio (rect ⊗ rect) ⊗ gaussiana  -> anchos de las dos "rendijas
    efectivas" en nm, desenfoque sigma, centro, área
  * gaussiana (solo como referencia)
  * FWHM sin modelo (interpolación lineal a media altura)
Genera <archivo>_ajuste.csv con el resumen y <archivo>_cfgN.png por configuración,
al lado del CSV. Las configuraciones con muy pocos puntos o sin pico (solo ruido) no se
ajustan: quedan en el resumen con su estado y el motivo en la columna "aviso".
"""
import argparse
import glob
import os
import sys
import warnings

import numpy as np
import pandas as pd
from scipy.optimize import OptimizeWarning, curve_fit
from scipy.special import erf

SERIE = "#2a78d6"
TINTA = "#0b0b0b"
TINTA2 = "#52514e"
SUPERFICIE = "#fcfcfb"

CARPETA_SCRIPT = os.path.dirname(os.path.abspath(__file__))
PATRON_DEFECTO = "barrido_rendijas_*.csv"
COLUMNAS_NECESARIAS = {"config_index", "slit_in_mm", "slit_out_mm", "wavelength_nm", "mean_v", "std_v", "n"}
COLUMNAS_SALIDA = [
    "base_alas_V", "altura_V", "fwhm_interp_nm", "centro_interp_nm", "gauss_fwhm_nm", "gauss_chi2r",
    "trap_centro_nm", "trap_centro_err_nm", "trap_ancho_nm", "trap_ancho_err_nm", "trap_angosto_nm",
    "trap_angosto_err_nm", "trap_sigma_nm", "trap_sigma_err_nm", "trap_fwhm_nm", "trap_meseta_nm",
    "trap_base_nm", "trap_area_Vnm", "trap_chi2r", "config", "rendija_ent_mm", "rendija_sal_mm",
    "esperado_fwhm_nm", "ancho_equiv_mm", "angosto_equiv_mm", "ruido_muestra_mV", "n_muestras",
    "n_puntos", "snr", "estado", "aviso",
]
COLUMNAS_PANTALLA = [
    "config", "rendija_ent_mm", "rendija_sal_mm", "estado", "trap_centro_nm", "fwhm_interp_nm", "trap_fwhm_nm",
    "esperado_fwhm_nm", "trap_ancho_nm", "trap_angosto_nm", "trap_sigma_nm", "trap_meseta_nm",
    "altura_V", "base_alas_V", "trap_chi2r", "gauss_chi2r", "snr",
]


# ----------------------------------------------------------------------------- modelos

def trapecio_gauss(x, c, a, b, s):
    """rect(a) ⊗ rect(b) ⊗ gauss(s), con altura 1 en la meseta (s -> 0)."""
    a, b, s = abs(a), abs(b), max(abs(s), 1e-6)
    P, Q = (a + b) / 2, abs(a - b) / 2

    def rampa(t):  # rampa t·H(t) convolucionada con la gaussiana
        return t * 0.5 * (1 + erf(t / (np.sqrt(2) * s))) + s / np.sqrt(2 * np.pi) * np.exp(-t**2 / (2 * s**2))

    u = x - c
    return (rampa(u + P) - rampa(u + Q) - rampa(u - Q) + rampa(u - P)) / max(min(a, b), 1e-9)


def modelo(x, base, A, c, a, b, s):
    return base + A * trapecio_gauss(x, c, a, b, s)


def gauss(x, base, A, c, w):
    return base + A * np.exp(-4 * np.log(2) * (x - c) ** 2 / w**2)


# ----------------------------------------------------------------------------- análisis

def fwhm_interp(x, y, base):
    """FWHM e x central por interpolación lineal a media altura.

    Devuelve (nan, nan) si no hay pico sobre la base o si la curva no baja de la media
    altura a ambos lados (pico cortado por el borde de la ventana)."""
    i = int(np.argmax(y))
    h = base + (y[i] - base) / 2
    if not y[i] > h:
        return np.nan, np.nan
    l = i
    while l > 0 and y[l] > h:
        l -= 1
    r = i
    while r < len(y) - 1 and y[r] > h:
        r += 1
    if y[l] > h or y[r] > h:
        return np.nan, np.nan
    xl = np.interp(h, [y[l], y[l + 1]], [x[l], x[l + 1]])
    xr = np.interp(h, [y[r], y[r - 1]], [x[r], x[r - 1]])
    return xr - xl, (xl + xr) / 2


def evaluar_calidad(x, y, min_puntos, snr_min):
    """Decide si vale la pena ajustar. Devuelve (estado, motivo, snr); estado None = ajustar."""
    if len(x) < min_puntos:
        return "omitida", f"solo {len(x)} puntos (mínimo {min_puntos}); barrido cortado o abortado", np.nan
    base0 = np.median(np.r_[y[:10], y[-10:]])
    d = np.diff(y)
    ruido = 1.4826 * np.median(np.abs(d - np.median(d))) / np.sqrt(2)  # robusto frente al pico
    snr = (y.max() - base0) / ruido if ruido > 0 else np.inf
    if snr < snr_min:
        return "sin_pico", (f"no hay un pico claro (S/R = {snr:.2f} < {snr_min:g}): solo ruido, señal inestable "
                            f"o la línea cae fuera de la ventana. Para forzar el ajuste: --snr-min 0"), snr
    return None, "", snr


def ajustar(x, y, e):
    base0 = np.median(np.r_[y[:10], y[-10:]])
    A0 = y.max() - base0
    fw0, c0 = fwhm_interp(x, y, base0)
    out = {"base_alas_V": base0, "altura_V": A0, "fwhm_interp_nm": fw0, "centro_interp_nm": c0}
    avisos = []
    avisos_borde = not np.isfinite(fw0)
    if avisos_borde:
        avisos.append("el pico no baja a media altura de ambos lados (cortado por el borde de la ventana); "
                      "base, anchos y FWHM son extrapolados")
        fw0, c0 = (x.max() - x.min()) / 10, x[int(np.argmax(y))]
    n_sobre = int(np.sum(y > base0 + A0 / 2))
    if n_sobre < 4:
        avisos.append(f"pico submuestreado: {n_sobre} puntos sobre media altura (achicar el paso)")

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", OptimizeWarning)
        warnings.simplefilter("ignore", RuntimeWarning)

        pg = None
        try:
            pg, _ = curve_fit(gauss, x, y, p0=[base0, A0, c0, fw0], sigma=e, maxfev=20000)
            out["gauss_fwhm_nm"] = abs(pg[3])
            out["gauss_chi2r"] = np.sum(((y - gauss(x, *pg)) / e) ** 2) / (len(x) - 4)
        except (RuntimeError, ValueError):
            out["gauss_fwhm_nm"] = out["gauss_chi2r"] = np.nan

        mejor = None
        for frac in (0.3, 0.5, 0.7):
            try:
                p, C = curve_fit(modelo, x, y, p0=[base0, A0, c0, fw0, fw0 * frac, 0.01], sigma=e, maxfev=50000)
            except (RuntimeError, ValueError):
                continue
            chi = np.sum(((y - modelo(x, *p)) / e) ** 2)
            if np.isfinite(chi) and (mejor is None or chi < mejor[2]):
                mejor = (p, C, chi)
    if mejor is None:
        raise RuntimeError("el ajuste trapecio no convergió con ninguna condición inicial")

    p, C, chi = mejor
    err = np.sqrt(np.abs(np.diag(C)))  # incertezas escaladas por chi2 (absolute_sigma=False)
    if not np.all(np.isfinite(err)):
        avisos.append("incertezas no estimables (algún parámetro quedó en el límite, p. ej. sigma -> 0)")
        err = np.where(np.isfinite(err), err, np.nan)
    ancho, angosto = sorted([abs(p[3]), abs(p[4])], reverse=True)
    i_ancho, i_ang = (3, 4) if abs(p[3]) >= abs(p[4]) else (4, 3)
    # FWHM del modelo evaluado en una grilla que cubre el pico entero, aunque la ventana medida no lo haga
    medio = (ancho + angosto) / 2 + 8 * abs(p[5]) + 0.05 * (x.max() - x.min())
    xf = np.linspace(p[2] - medio, p[2] + medio, 20001)
    fwm, _ = fwhm_interp(xf, modelo(xf, *p), p[0])
    alto = modelo(np.array([p[2]]), *p)[0] - p[0]
    en_bordes = modelo(np.array([x.min(), x.max()]), *p) - p[0]
    if not avisos_borde and alto > 0 and np.max(en_bordes) > 0.1 * alto:
        avisos.append("las alas del pico quedan fuera de la ventana: base, anchos y FWHM del ajuste son "
                      "extrapolados (ampliar el rango del barrido)")
    out.update({
        "trap_centro_nm": p[2], "trap_centro_err_nm": err[2],
        "trap_ancho_nm": ancho, "trap_ancho_err_nm": err[i_ancho],
        "trap_angosto_nm": angosto, "trap_angosto_err_nm": err[i_ang],
        "trap_sigma_nm": abs(p[5]), "trap_sigma_err_nm": err[5],
        "trap_fwhm_nm": fwm, "trap_meseta_nm": ancho - angosto, "trap_base_nm": ancho + angosto,
        "trap_area_Vnm": p[1] * ancho, "trap_chi2r": chi / (len(x) - 6),
    })
    return out, p, pg, avisos


# ----------------------------------------------------------------------------- gráficos

def _estilo(axes):
    for a in axes:
        a.set_facecolor(SUPERFICIE)
        for s in ("top", "right"):
            a.spines[s].set_visible(False)
        for s in ("left", "bottom"):
            a.spines[s].set_color(TINTA2)
        a.tick_params(colors=TINTA2, labelsize=9)
        a.grid(True, color="#e6e5e1", lw=0.6)
        a.set_axisbelow(True)


def graficar(x, y, e, p, pg, titulo, ruta, res):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, (ax, axr) = plt.subplots(2, 1, figsize=(8, 6.2), sharex=True,
                                  gridspec_kw={"height_ratios": [3, 1], "hspace": 0.08})
    fig.patch.set_facecolor(SUPERFICIE)
    _estilo((ax, axr))

    xf = np.linspace(x.min(), x.max(), 4000)
    yf = modelo(xf, *p)
    ax.errorbar(x, 1e3 * y, yerr=1e3 * e, fmt="o", ms=4.5, color=SERIE, mec=SUPERFICIE, mew=0.8,
                elinewidth=1, capsize=0, label="Datos (media ± error de la media)", zorder=3)
    ax.plot(xf, 1e3 * yf, color=TINTA, lw=2, label="Ajuste: trapecio (dos rendijas) con bordes gaussianos", zorder=2)
    if pg is not None:
        ax.plot(xf, 1e3 * gauss(xf, *pg), color=TINTA2, lw=1.2, ls="--", label="Ajuste gaussiano (referencia)", zorder=1)

    # flecha a la media altura REAL de la curva ajustada (con sigma grande la meseta A no se alcanza)
    fw = res["trap_fwhm_nm"]
    c = p[2]
    if np.isfinite(fw) and c - fw / 2 >= x.min() and c + fw / 2 <= x.max():
        h = p[0] + (yf.max() - p[0]) / 2
        ax.annotate("", xy=(c - fw / 2, 1e3 * h), xytext=(c + fw / 2, 1e3 * h),
                    arrowprops=dict(arrowstyle="<->", color=TINTA2, lw=1))
        ax.text(c + fw / 2 + 0.03 * (x.max() - x.min()), 1e3 * h, f"FWHM = {fw:.3f} nm", ha="left", va="center",
                fontsize=9.5, color=TINTA)

    lo, hi = ax.get_ylim()
    ax.set_ylim(lo, hi + 0.38 * (hi - lo))  # aire arriba para leyenda y recuadro de texto
    ax.set_ylabel("Señal AUX (mV)", color=TINTA2, fontsize=10)
    ax.set_title(titulo, loc="left", fontsize=11.5, color=TINTA)
    ax.legend(frameon=False, fontsize=8.5, loc="upper right", labelcolor=TINTA2)
    txt = (f"Centro: {res['trap_centro_nm']:.3f} nm · FWHM: {fw:.3f} nm\n"
           f"Meseta: {res['trap_meseta_nm']:.3f} nm · Base: {res['trap_base_nm']:.3f} nm\n"
           f"Desenfoque σ: {res['trap_sigma_nm']:.3f} nm")
    ax.text(0.02, 0.97, txt, transform=ax.transAxes, va="top", fontsize=8.5, color=TINTA2)

    axr.axhline(0, color=TINTA2, lw=0.8)
    axr.plot(x, 1e3 * (y - modelo(x, *p)), "o", ms=3.5, color=SERIE)
    axr.set_ylabel("Residuo (mV)", color=TINTA2, fontsize=9)
    axr.set_xlabel("Longitud de onda leída (nm)", color=TINTA2, fontsize=10)
    fig.savefig(ruta, dpi=160, bbox_inches="tight", facecolor=SUPERFICIE)
    plt.close(fig)


def graficar_sin_ajuste(x, y, e, titulo, ruta, motivo):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(8, 4.2))
    fig.patch.set_facecolor(SUPERFICIE)
    _estilo((ax,))
    ax.errorbar(x, 1e3 * y, yerr=1e3 * e, fmt="o", ms=4.5, color=SERIE, mec=SUPERFICIE, mew=0.8,
                elinewidth=1, capsize=0, label="Datos (media ± error de la media)")
    ax.set_title(titulo + " · sin ajuste", loc="left", fontsize=11.5, color=TINTA)
    ax.text(0.02, 0.95, motivo, transform=ax.transAxes, va="top", fontsize=9, color=TINTA2)
    lo, hi = ax.get_ylim()
    ax.set_ylim(lo, hi + 0.25 * (hi - lo))
    ax.set_ylabel("Señal AUX (mV)", color=TINTA2, fontsize=10)
    ax.set_xlabel("Longitud de onda leída (nm)", color=TINTA2, fontsize=10)
    fig.savefig(ruta, dpi=160, bbox_inches="tight", facecolor=SUPERFICIE)
    plt.close(fig)


# ----------------------------------------------------------------------------- archivos

def resolver_entradas(entradas):
    """Convierte lo que se pasó por línea de comandos en una lista de CSV existentes."""
    if not entradas:
        print(f"No se indicó ningún archivo: proceso todos los {PATRON_DEFECTO} de\n  {CARPETA_SCRIPT}\n")
        entradas = [os.path.join(CARPETA_SCRIPT, PATRON_DEFECTO)]
    archivos = []
    for ent in entradas:
        candidatas = [ent] if os.path.isabs(ent) else [ent, os.path.join(CARPETA_SCRIPT, ent)]
        encontrados = []
        for c in candidatas:
            if os.path.isfile(c):
                encontrados = [c]
            elif os.path.isdir(c):
                encontrados = sorted(glob.glob(os.path.join(c, "*.csv")))
            else:
                encontrados = sorted(glob.glob(c))
            if encontrados:
                break
        if not encontrados:
            print(f"AVISO: no encontré '{ent}'\n  (busqué en {os.getcwd()} y en {CARPETA_SCRIPT})")
        archivos += encontrados
    vistos, salida = set(), []
    for a in archivos:
        a = os.path.abspath(a)
        if a.lower().endswith("_ajuste.csv") or a in vistos:
            continue
        vistos.add(a)
        salida.append(a)
    return salida


def procesar(ruta, args):
    nombre = os.path.basename(ruta)
    df = pd.read_csv(ruta)
    faltan = COLUMNAS_NECESARIAS - set(df.columns)
    if faltan:
        print(f"== {nombre}: omitido, no es un CSV de barrido (faltan columnas: {', '.join(sorted(faltan))})\n")
        return False

    base = os.path.splitext(ruta)[0]
    grupos = df.groupby(["config_index", "slit_in_mm", "slit_out_mm"])
    print(f"== {nombre}  ({grupos.ngroups} configuraciones de rendijas)")
    filas, n_graf = [], 0
    for (ci, si, so), g in grupos:
        g = g.dropna(subset=["wavelength_nm", "mean_v"]).sort_values("wavelength_nm")
        x = g.wavelength_nm.to_numpy(float)
        y = g.mean_v.to_numpy(float)
        e = np.maximum((g.std_v.fillna(0) / np.sqrt(g.n.clip(lower=1))).to_numpy(float), 1e-5)
        titulo = f"iHR320 · entrada {si*1e3:.0f} µm / salida {so*1e3:.0f} µm"
        ruta_png = f"{base}_cfg{ci}.png"
        fila = {
            "config": ci, "rendija_ent_mm": si, "rendija_sal_mm": so,
            "esperado_fwhm_nm": args.D * max(args.M * si, so),
            "ruido_muestra_mV": 1e3 * np.median(g.std_v), "n_muestras": int(np.median(g.n)) if len(g) else 0,
            "n_puntos": len(x),
        }
        estado, motivo, snr = evaluar_calidad(x, y, args.min_puntos, args.snr_min)
        fila["snr"] = snr
        if estado is None:
            try:
                res, p, pg, avisos = ajustar(x, y, e)
            except (RuntimeError, ValueError, TypeError, np.linalg.LinAlgError) as exc:
                estado, motivo = "ajuste_fallido", str(exc)
            else:
                fila.update(res)
                fila.update({"ancho_equiv_mm": res["trap_ancho_nm"] / args.D,
                             "angosto_equiv_mm": res["trap_angosto_nm"] / args.D,
                             "estado": "ok", "aviso": "; ".join(avisos)})
                if not args.sin_graficos:
                    graficar(x, y, e, p, pg, titulo, ruta_png, res)
                    n_graf += 1
        if estado is not None:
            fila.update({"estado": estado, "aviso": motivo})
            if len(x) >= 3:
                fila.update({"base_alas_V": np.median(np.r_[y[:10], y[-10:]])})
                fila["altura_V"] = y.max() - fila["base_alas_V"]
            if not args.sin_graficos and len(x) >= 3:
                graficar_sin_ajuste(x, y, e, titulo, ruta_png, motivo)
                n_graf += 1
        filas.append(fila)

    out = pd.DataFrame(filas).reindex(columns=COLUMNAS_SALIDA)
    ruta_ajuste = f"{base}_ajuste.csv"
    out.to_csv(ruta_ajuste, index=False)

    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 50)
    print(out[COLUMNAS_PANTALLA].round(4).to_string(index=False))
    for _, f in out.iterrows():
        if isinstance(f.aviso, str) and f.aviso:
            print(f"  ! cfg{f.config} ({f.rendija_ent_mm*1e3:.0f}/{f.rendija_sal_mm*1e3:.0f} µm): {f.aviso}")
    print(f"  -> resumen: {ruta_ajuste}")
    if n_graf:
        print(f"  -> {n_graf} gráfico(s): {base}_cfg*.png")
    print()
    return True


def main():
    for flujo in (sys.stdout, sys.stderr):  # que la consola de Windows nunca corte por un caracter raro
        try:
            flujo.reconfigure(errors="replace")
        except AttributeError:
            pass

    ap = argparse.ArgumentParser(description="Ajuste de barridos de rendijas del iHR320.")
    ap.add_argument("csv", nargs="*",
                    help=f"CSV, carpetas o patrones con comodines. Sin nada: todos los {PATRON_DEFECTO} de la carpeta del script")
    ap.add_argument("--D", type=float, default=2.28, help="dispersión recíproca en nm/mm (iHR320, 1200 gr/mm, ~532 nm)")
    ap.add_argument("--M", type=float, default=1.1, help="magnificación entrada->salida")
    ap.add_argument("--sin-graficos", action="store_true", help="no generar los PNG")
    ap.add_argument("--min-puntos", type=int, default=8, help="mínimo de puntos por configuración para ajustar")
    ap.add_argument("--snr-min", type=float, default=5.0, help="relación señal/ruido mínima para considerar que hay pico")
    args = ap.parse_args()

    archivos = resolver_entradas(args.csv)
    if not archivos:
        print("No hay archivos para procesar. Ejemplo:  python ajustar_barrido.py barrido_rendijas_0.1.csv")
        return 1

    ok = 0
    for ruta in archivos:
        try:
            ok += procesar(ruta, args)
        except Exception as exc:  # un archivo roto no frena al resto
            print(f"== {os.path.basename(ruta)}: ERROR {type(exc).__name__}: {exc}\n")
    print(f"Listo: {ok} de {len(archivos)} archivo(s) procesado(s).")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
