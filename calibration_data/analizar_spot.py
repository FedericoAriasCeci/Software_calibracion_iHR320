"""
Tamaño del spot en la rendija de entrada del iHR320 (método "filo de cuchillo").

Idea: con la rendija de salida bien abierta, toda la luz que atraviesa la entrada llega al
fotodiodo. La potencia transmitida en función del ancho de la entrada, P(w), es la integral del
perfil horizontal del spot sobre la abertura. Su derivada es el perfil del spot.

Sirve para dos tipos de barrido (mismo formato de CSV que el resto de los barridos):
  * Filo de cuchillo (recomendado): λ fija en el centro de la meseta, salida al máximo y muchos
    anchos de entrada (p. ej. 0 a 1,2 mm cada 25 µm). Una sola λ (o pocas) por configuración.
  * Barrido en λ con salida ancha (como "engrada_variables_grueso"): de cada configuración se toma
    la altura de la meseta menos el fondo. Si además hay configuraciones con la entrada mucho más
    ancha que el spot, se ajustan los flancos de la meseta: su ancho es la imagen del spot entero.

Uso:
    python analizar_spot.py archivo.csv [--D 2.30] [--M 1.125] [--sin-graficos]

Modelos para P(w), con w_real = w_nominal + delta (error de cero de la rendija):
    simetrica : la rendija abre hacia los dos lados alrededor de su centro
    un_lado   : una mordaza fija y la otra móvil (x medido desde la mordaza fija)
El spot se modela como gaussiano: FWHM = 2,355·sigma. Si el ajuste no puede separar los
parámetros (pocos anchos medidos) lo avisa: en ese caso vale más el resultado de los flancos.
"""
import argparse
import os
import warnings

import numpy as np
import pandas as pd
from scipy.optimize import OptimizeWarning, curve_fit
from scipy.stats import norm

SERIE = "#2a78d6"
SERIE2 = "#eb6834"
TINTA = "#0b0b0b"
TINTA2 = "#52514e"
SUPERFICIE = "#fcfcfb"


# ----------------------------------------------------------------------------- modelos

def _wreal(w, delta):
    return np.clip(w + delta, 0, None)


def p_simetrica(w, B, A, xc, s, delta):
    wr = _wreal(w, delta)
    return B + A * (norm.cdf((wr / 2 - xc) / s) - norm.cdf((-wr / 2 - xc) / s))


def p_un_lado(w, B, A, xc, s, delta):
    wr = _wreal(w, delta)
    return B + A * (norm.cdf((wr - xc) / s) - norm.cdf(-xc / s))


MODELOS = {"simetrica": p_simetrica, "un_lado": p_un_lado}


# ----------------------------------------------------------------------------- potencia vs ancho

def potencia_por_config(df):
    """Devuelve una tabla con w, P, error y (si hay barrido en λ) el fondo restado."""
    filas = []
    for (ci, w, wo), g in df.groupby(["config_index", "slit_in_mm", "slit_out_mm"]):
        g = g.sort_values("wavelength_nm")
        y = g.mean_v.to_numpy()
        e = (g.std_v / np.sqrt(g.n)).to_numpy()
        if len(g) >= 6:  # barrido en λ: meseta menos fondo
            fondo = np.mean(np.r_[y[:1], y[-1:]])
            top = y.max()
            en_meseta = y >= fondo + 0.9 * (top - fondo)
            idx = np.where(en_meseta)[0]
            idx = idx[1:-1] if len(idx) > 4 else idx  # descarta puntos de borde
            P = y[idx].mean() - fondo
            err = np.hypot(y[idx].std(ddof=1) / np.sqrt(len(idx)), abs(y[0] - y[-1]) / 2)
            filas.append(dict(config=ci, w=w, w_sal=wo, P=P, err=err, fondo=fondo, tipo="meseta"))
        else:  # filo de cuchillo: promedio de las pocas λ medidas
            filas.append(dict(config=ci, w=w, w_sal=wo, P=y.mean(), err=np.sqrt(np.mean(e**2)) / np.sqrt(len(y)),
                              fondo=np.nan, tipo="lambda_fija"))
    return pd.DataFrame(filas).sort_values("w").reset_index(drop=True)


def ajustar_potencia(t):
    w, P = t.w.to_numpy(), t.P.to_numpy()
    e = np.maximum(t.err.to_numpy(), 1e-3 * np.nanmax(np.abs(P)))
    fondo_restado = t.tipo.iloc[0] == "meseta"
    resultados = {}
    libres = 5 - (1 if fondo_restado else 0)
    if len(w) < 2 * libres:
        aviso = (f"solo {len(w)} anchos para {libres} parámetros: el ajuste no está determinado; "
                 "hacé el barrido fino de filo de cuchillo")
    else:
        aviso = ""
    for nombre, f in MODELOS.items():
        mejor = None
        for xc0 in (0.0, 0.05, 0.15, 0.3):
            for s0 in (0.05, 0.15, 0.3):
                p0 = [0.0 if fondo_restado else P.min(), P.max() - (0 if fondo_restado else P.min()), xc0, s0, 0.0]
                if fondo_restado:
                    g = lambda w_, A, xc, s, d: f(w_, 0.0, A, xc, s, d)
                    p0_ = p0[1:]
                else:
                    g = f
                    p0_ = p0
                try:
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore", OptimizeWarning)
                        warnings.simplefilter("ignore", RuntimeWarning)
                        pp, C = curve_fit(g, w, P, p0=p0_, sigma=e, maxfev=40000)
                except (RuntimeError, ValueError):
                    continue
                chi = np.sum(((P - g(w, *pp)) / e) ** 2)
                if mejor is None or chi < mejor[2]:
                    mejor = (pp, C, chi, g)
        if mejor is None:
            continue
        pp, C, chi, g = mejor
        err = np.sqrt(np.abs(np.diag(C)))
        if fondo_restado:
            pp = np.r_[0.0, pp]
            err = np.r_[0.0, err]
        resultados[nombre] = dict(B=pp[0], A=pp[1], xc_mm=pp[2], xc_err=err[2], sigma_mm=abs(pp[3]),
                                  sigma_err=err[3], fwhm_mm=2.3548 * abs(pp[3]), fwhm_err=2.3548 * err[3],
                                  delta_mm=pp[4], delta_err=err[4],
                                  chi2r=chi / max(len(w) - (len(pp) - (1 if fondo_restado else 0)), 1),
                                  params=pp, fn=MODELOS[nombre])
    return resultados, aviso


# ----------------------------------------------------------------------------- flancos de la meseta

def ajustar_flancos(df, D, M):
    """Con la entrada más ancha que el spot, cada flanco de la meseta es la imagen del spot entero
    integrada: un escalón suavizado (función error). Su sigma en nm, /D/M, es el sigma del spot."""
    filas = []
    for (ci, w, wo), g in df.groupby(["config_index", "slit_in_mm", "slit_out_mm"]):
        if len(g) < 8:
            continue
        g = g.sort_values("wavelength_nm")
        x, y = g.wavelength_nm.to_numpy(), g.mean_v.to_numpy()
        fondo = np.mean(np.r_[y[:1], y[-1:]])
        top = np.median(y[y >= fondo + 0.9 * (y.max() - fondo)])
        r = (y - fondo) / (top - fondo)
        imax = int(np.argmax(r > 0.5))
        imin = len(r) - 1 - int(np.argmax(r[::-1] > 0.5))
        res = dict(config=ci, w=w)
        for lado, i0, fn in (("subida", imax, lambda l, c, s, h: h * norm.cdf((l - c) / s)),
                             ("bajada", imin, lambda l, c, s, h: h * norm.cdf((c - l) / s))):
            sel = slice(max(i0 - 3, 0), min(i0 + 4, len(x)))
            xs, rs = x[sel], r[sel]
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", OptimizeWarning)
                    pp, C = curve_fit(fn, xs, rs, p0=[x[i0], 0.3, 1.0], maxfev=20000)
                res[f"{lado}_centro_nm"] = pp[0]
                res[f"{lado}_sigma_nm"] = abs(pp[1])
            except (RuntimeError, ValueError):
                res[f"{lado}_centro_nm"] = res[f"{lado}_sigma_nm"] = np.nan
        res["ancho_meseta_nm"] = res["bajada_centro_nm"] - res["subida_centro_nm"]
        res["centro_nm"] = (res["bajada_centro_nm"] + res["subida_centro_nm"]) / 2
        s_nm = np.nanmean([res["subida_sigma_nm"], res["bajada_sigma_nm"]])
        res["fwhm_imagen_nm"] = 2.3548 * s_nm
        res["fwhm_spot_mm"] = 2.3548 * s_nm / D / M
        res["paso_nm"] = float(np.median(np.diff(x)))
        filas.append(res)
    return pd.DataFrame(filas)


# ----------------------------------------------------------------------------- gráfico

def estilo(a):
    a.set_facecolor(SUPERFICIE)
    for s in ("top", "right"):
        a.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        a.spines[s].set_color(TINTA2)
    a.tick_params(colors=TINTA2, labelsize=9)
    a.grid(True, color="#e6e5e1", lw=0.6)
    a.set_axisbelow(True)


def graficar(t, ajustes, flancos, df, ruta, titulo, spot_flancos):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    hay_flancos = flancos is not None and len(flancos) and np.isfinite(spot_flancos)
    fig, axs = plt.subplots(1, 2 if hay_flancos else 1, figsize=(12 if hay_flancos else 7, 4.6))
    axs = np.atleast_1d(axs)
    fig.patch.set_facecolor(SUPERFICIE)
    ax = axs[0]
    estilo(ax)
    Pmax = t.P.max()
    ax.errorbar(t.w, t.P / Pmax, yerr=t.err / Pmax, fmt="o", ms=6, color=SERIE, mec=SUPERFICIE, zorder=3,
                label="Medido (meseta − fondo)")
    wf = np.linspace(0, max(t.w.max(), 0.05) * 1.05, 600)
    if hay_flancos:
        s = spot_flancos / 2.3548
        d = ajustes.get("_delta_ref", 0.0)
        pred = p_simetrica(wf, 0, 1, 0, s, d)
        ax.plot(wf, pred, color=TINTA, lw=2,
                label=f"Spot gaussiano de {spot_flancos*1e3:.0f} µm (de los flancos), centrado, δ = {d*1e3:.0f} µm")
    ax.set_xlabel("Ancho nominal de la rendija de entrada (mm)", color=TINTA2)
    ax.set_ylabel("Fracción de la potencia transmitida", color=TINTA2)
    ax.set_title(titulo, loc="left", fontsize=11, color=TINTA)
    ax.set_ylim(-0.03, 1.08)
    ax.legend(frameon=False, fontsize=8.5, labelcolor=TINTA2, loc="lower right")

    if hay_flancos:
        ax2 = axs[1]
        estilo(ax2)
        ancha = flancos.w.max()
        g = df[df.slit_in_mm == ancha].sort_values("wavelength_nm")
        x, y = g.wavelength_nm.to_numpy(), g.mean_v.to_numpy()
        fondo = np.mean(np.r_[y[:1], y[-1:]])
        top = np.median(y[y >= fondo + 0.9 * (y.max() - fondo)])
        f = flancos[flancos.w == ancha].iloc[0]
        rel = x - f.subida_centro_nm
        ax2.plot(rel, (y - fondo) / (top - fondo), "o", color=SERIE, ms=6, mec=SUPERFICIE, label="Flanco de subida")
        rel2 = f.bajada_centro_nm - x
        ax2.plot(rel2, (y - fondo) / (top - fondo), "s", color=SERIE2, ms=5.5, mec=SUPERFICIE, label="Flanco de bajada (espejado)")
        u = np.linspace(-3, 3, 400)
        ax2.plot(u, norm.cdf(u / (f.fwhm_imagen_nm / 2.3548)), color=TINTA, lw=2,
                 label=f"Escalón gaussiano, imagen FWHM = {f.fwhm_imagen_nm:.2f} nm")
        ax2.set_xlim(-3, 3)
        ax2.set_ylim(-0.05, 1.12)
        ax2.set_xlabel("Distancia al centro del flanco (nm)", color=TINTA2)
        ax2.set_ylabel("Señal normalizada", color=TINTA2)
        ax2.set_title(f"Flancos con la entrada en {ancha:g} mm (pasa el spot entero)", loc="left", fontsize=11, color=TINTA)
        ax2.legend(frameon=False, fontsize=8.5, labelcolor=TINTA2, loc="upper left")
    fig.tight_layout()
    fig.savefig(ruta, dpi=160, facecolor=SUPERFICIE)
    plt.close(fig)


# ----------------------------------------------------------------------------- filo de cuchillo (sin modelo)

def detectar_saltos(w, P, umbral_rel=0.003):
    """Marca puntos que se apartan de una curva suave (transitorios de potencia, tirones del motor)."""
    n = len(P)
    malo = np.zeros(n, bool)
    if n < 5:
        return malo
    Pmax, Pmin = np.nanmax(P), np.nanmin(P)
    fr = (P - Pmin) / (Pmax - Pmin)
    # picos aislados, solo donde la curva ya es suave (lejos del codo del cero)
    r = np.zeros(n)
    r[1:-1] = P[1:-1] - (P[:-2] + P[2:]) / 2
    malo |= (fr > 0.3) & (np.abs(r) > 2.5 * umbral_rel * Pmax)
    # P(w) no puede bajar al abrir la rendija: se marcan los puntos que quedan por debajo del anterior
    malo |= np.r_[False, np.diff(P) < -(2 / 3) * umbral_rel * Pmax]
    return malo


def filo_de_cuchillo(t, excluir):
    """Métricas que no dependen de la forma del spot ni de cómo abre la rendija."""
    w, P = t.w.to_numpy(), t.P.to_numpy()
    malo = detectar_saltos(w, P)
    for we in excluir:
        malo |= np.isclose(w, we)
    ok = ~malo
    cerrado = w <= 0.005
    B = P[cerrado].mean() if cerrado.any() else np.nan
    if not np.isfinite(B):
        B = P[ok][0]
    Pmax = P[ok][-2:].mean()
    fr = (P - B) / (Pmax - B)
    # cero real: recta sobre el tramo inicial (fracción 3–30 %), donde el spot es casi uniforme
    sel = ok & (fr > 0.03) & (fr < 0.30)
    pend, ord_ = np.polyfit(w[sel], P[sel], 1)
    w0 = (B - ord_) / pend
    filas = []
    for q in (0.5, 0.8, 0.9, 0.95):
        idx = np.where(ok & (fr >= q))[0]
        if not len(idx) or idx[0] == 0:
            continue
        j = idx[0]
        k = np.where(ok[:j])[0][-1]
        wq = np.interp(q, [fr[k], fr[j]], [w[k], w[j]])
        # FWHM de una gaussiana centrada que deja pasar la fracción q por una abertura de ese ancho
        fwhm_eq = (wq - w0) * 2.3548 / (2 * norm.ppf(0.5 + q / 2))
        filas.append(dict(fraccion=q, w_nominal_mm=wq, w_real_mm=wq - w0, fwhm_gauss_equiv_mm=fwhm_eq))
    tabla = pd.DataFrame(filas)
    dw = np.diff(w)
    deriv = np.diff(fr) / dw
    wmid = (w[1:] + w[:-1]) / 2
    ok_d = ok[1:] & ok[:-1]
    return dict(B=B, Pmax=Pmax, w0=w0, pendiente=pend, fr=fr, ok=ok, tabla=tabla, wmid=wmid, deriv=deriv, ok_d=ok_d)


def graficar_filo(t, r, ruta, fwhm_ref):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    w = t.w.to_numpy()
    fig, (ax, ax2) = plt.subplots(1, 2, figsize=(12.5, 4.7))
    fig.patch.set_facecolor(SUPERFICIE)
    for q in (ax, ax2):
        estilo(q)
    ok = r["ok"]
    ax.plot(w[ok], r["fr"][ok], "o", ms=4.5, color=SERIE, mec=SUPERFICIE, label="Medido (promedio de las λ)")
    if (~ok).any():
        ax.plot(w[~ok], r["fr"][~ok], "o", ms=6, mfc="none", mec=SERIE2, mew=1.5, label="Excluido (salto de potencia)")
    s = fwhm_ref / 2.3548
    wf = np.linspace(0, w.max(), 600)
    ref = np.where(wf > r["w0"], 2 * norm.cdf((wf - r["w0"]) / (2 * s)) - 1, 0)
    ax.plot(wf, ref, color=TINTA, lw=1.6, ls="--", label=f"Gaussiana centrada de {fwhm_ref*1e3:.0f} µm (referencia)")
    for _, f in r["tabla"].iterrows():
        ax.plot([f.w_nominal_mm], [f.fraccion], marker="|", ms=14, color=TINTA2)
        ax.text(f.w_nominal_mm + 0.02, f.fraccion - 0.045, f"{f.fraccion*100:.0f} % a {f.w_nominal_mm:.2f} mm",
                fontsize=8.5, color=TINTA2)
    ax.set_xlabel("Ancho nominal de la rendija de entrada (mm)", color=TINTA2)
    ax.set_ylabel("Fracción de la potencia transmitida", color=TINTA2)
    ax.set_title("Filo de cuchillo: potencia que atraviesa la entrada", loc="left", fontsize=11, color=TINTA)
    ax.set_ylim(-0.03, 1.08)
    ax.legend(frameon=False, fontsize=8.5, labelcolor=TINTA2, loc="lower right")

    okd = r["ok_d"]
    ax2.plot(r["wmid"][okd], r["deriv"][okd], "o", ms=4.5, color=SERIE, mec=SUPERFICIE, label="Derivada de los datos")
    dref = np.gradient(ref, wf)
    ax2.plot(wf, dref, color=TINTA, lw=1.6, ls="--", label="Derivada de la referencia gaussiana")
    ax2.axvline(r["w0"], color=TINTA2, lw=0.8, ls=":")
    ax2.text(r["w0"] + 0.01, 0.05 * max(dref.max(), np.nanmax(r["deriv"][okd])), f"cero real ≈ {r['w0']*1e3:.0f} µm",
             fontsize=8.5, color=TINTA2)
    ax2.set_xlabel("Ancho nominal de la rendija de entrada (mm)", color=TINTA2)
    ax2.set_ylabel("dP/dw normalizada (1/mm)", color=TINTA2)
    ax2.set_title("Intensidad en los bordes de la abertura", loc="left", fontsize=11, color=TINTA)
    ax2.legend(frameon=False, fontsize=8.5, labelcolor=TINTA2, loc="upper right")
    fig.tight_layout()
    fig.savefig(ruta, dpi=160, facecolor=SUPERFICIE)
    plt.close(fig)


# ----------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--D", type=float, default=2.30, help="dispersión recíproca (nm/mm) cerca de 532 nm")
    ap.add_argument("--M", type=float, default=1.125, help="magnificación entrada -> salida a 532 nm")
    ap.add_argument("--delta-ref", type=float, default=-0.037,
                    help="error de cero de la entrada (mm) para la curva de referencia del gráfico")
    ap.add_argument("--excluir", default="", help="anchos a descartar, separados por coma (p. ej. 0.975,1.0)")
    ap.add_argument("--sin-graficos", action="store_true")
    a = ap.parse_args()

    df = pd.read_csv(a.csv)
    base = os.path.splitext(a.csv)[0]
    t = potencia_por_config(df)
    t["fraccion"] = t.P / t.P.max()
    print("\nPotencia transmitida vs ancho de entrada")
    print(t[["config", "w", "w_sal", "P", "err", "fondo", "fraccion"]].round(4).to_string(index=False))

    if t.tipo.iloc[0] == "lambda_fija" and len(t) >= 10:
        excl = [float(v) for v in a.excluir.split(",")] if a.excluir else []
        r = filo_de_cuchillo(t, excl)
        print(f"\nFilo de cuchillo (sin modelo)\n  Fondo (rendija cerrada): {r['B']:.4f} V   Máximo: {r['Pmax']:.4f} V")
        print(f"  Cero real de la entrada: {r['w0']*1e3:.1f} µm nominales "
              f"(pendiente inicial {r['pendiente']:.3f} V/mm = {r['pendiente']:.2f} mV/µm)")
        excluidos = t.w[~r["ok"]].round(3).tolist()
        if excluidos:
            print(f"  Excluidos por salto o descenso: {excluidos}")
        print(r["tabla"].round(3).to_string(index=False))
        fwhm_ref = float(np.median(r["tabla"].fwhm_gauss_equiv_mm))
        print(f"  Spot: FWHM equivalente (gaussiana centrada) ≈ {fwhm_ref*1e3:.0f} µm")
        r["tabla"].assign(cero_real_mm=r["w0"], fondo_V=r["B"], maximo_V=r["Pmax"]).to_csv(f"{base}_filo.csv", index=False)
        if not a.sin_graficos:
            graficar_filo(t, r, f"{base}_filo.png", fwhm_ref)
        t = t[r["ok"]].reset_index(drop=True)

    ajustes, aviso = ajustar_potencia(t)
    print("\nAjuste de P(w) con spot gaussiano (orientativo: supone forma gaussiana y rendija ideal)")
    if aviso:
        print("  AVISO:", aviso)
    for nombre, r in ajustes.items():
        print(f"  {nombre:9s}: FWHM = {r['fwhm_mm']*1e3:6.0f} ± {r['fwhm_err']*1e3:.0f} µm, "
              f"centro = {r['xc_mm']*1e3:6.0f} ± {r['xc_err']*1e3:.0f} µm, δ = {r['delta_mm']*1e3:5.0f} ± {r['delta_err']*1e3:.0f} µm, "
              f"chi2r = {r['chi2r']:.2f}")

    flancos = ajustar_flancos(df, a.D, a.M)
    spot_flancos = np.nan
    if len(flancos):
        print("\nFlancos de la meseta (imagen del spot)")
        print(flancos[["w", "subida_centro_nm", "bajada_centro_nm", "ancho_meseta_nm", "centro_nm",
                       "fwhm_imagen_nm", "fwhm_spot_mm", "paso_nm"]].round(3).to_string(index=False))
        # solo las configuraciones en las que pasa (casi) todo el spot: ahí el flanco es el spot entero
        anchas = flancos[flancos.config.isin(t[t.fraccion >= 0.97].config)]
        if anchas.empty:
            anchas = flancos[flancos.w == flancos.w.max()]
        spot_flancos = float(np.nanmedian(anchas.fwhm_spot_mm))
        print(f"\nSpot (FWHM horizontal en la rendija de entrada), desde los flancos: {spot_flancos*1e3:.0f} µm "
              f"(D = {a.D} nm/mm, M = {a.M})")
        if flancos.paso_nm.median() > 0.3 * flancos.fwhm_imagen_nm.median():
            print("  AVISO: el paso en λ es grueso frente al ancho del flanco; el valor es aproximado (±20 %).")

    resumen = []
    for nombre, r in ajustes.items():
        resumen.append({k: v for k, v in r.items() if k not in ("params", "fn")} | {"modelo": nombre})
    out = pd.DataFrame(resumen)
    out["spot_fwhm_flancos_mm"] = spot_flancos
    out["aviso"] = aviso
    out.to_csv(f"{base}_spot.csv", index=False)
    t.to_csv(f"{base}_potencia.csv", index=False)
    if not a.sin_graficos:
        ajustes["_delta_ref"] = a.delta_ref
        graficar(t, ajustes, flancos, df, f"{base}_spot.png", "Potencia que atraviesa la entrada (salida al máximo)",
                 spot_flancos)


if __name__ == "__main__":
    main()
