"""FWHM por interpolación, sin imponer una forma analítica a la línea."""
import math
import numpy as np
from .models import number

ANALYSIS_VERSION = "1.0"
SEED = 830
TRIALS = 400


def crossings(x, y):
    """Cruces más próximos al máximo global; no extrapola."""
    k = int(np.argmax(y))
    peak = float(y[k])
    if peak <= 0 or k == 0 or k == len(y)-1:
        return None
    half = peak/2
    left = next((i for i in range(k-1, -1, -1) if y[i] <= half), None)
    right = next((i for i in range(k+1, len(y)) if y[i] <= half), None)
    if left is None or right is None:
        return None

    def interp(i, j):
        if y[j] == y[i]:
            return float((x[i]+x[j])/2)
        return float(x[i]+(half-y[i])*(x[j]-x[i])/(y[j]-y[i]))

    return interp(left, left+1), interp(right-1, right), left, right


def profile_arrays(scan):
    """Agrupa visitas repetidas sin reducir artificialmente la dispersión."""
    groups = {}
    for p in scan.active_points:
        groups.setdefault(p.wavelength_nm, []).append(p)
    xs, ys, stds = [], [], []
    for x, points in sorted(groups.items()):
        means = np.array([p.batch.mean_v for p in points])
        xs.append(x)
        ys.append(float(np.mean(means)))
        # Dispersión intrapunto y entre visitas. No divide por sqrt(N).
        stds.append(math.sqrt(float(np.mean([p.batch.std_v**2 for p in points]))
                              + (float(np.var(means, ddof=1)) if len(points)>1 else 0)))
    return np.array(xs), np.array(ys), np.array(stds)


def analyze(scan, trials=TRIALS, seed=SEED):
    x, raw, std = profile_arrays(scan)
    bg = scan.background_v
    y = raw-bg
    warnings = []
    result = dict(version=ANALYSIS_VERSION, method="half_max_linear_interpolation",
                  seed=seed, trials=trials, background_applied=scan.use_background,
                  background_v=bg, unique_points=len(x), peak_v=None,
                  peak_nm=None, left_nm=None, right_nm=None, fwhm_nm=None,
                  asymmetry=None, mc_std_nm=None, mc_ci95_nm=None,
                  sampling_component_nm=None, fwhm_uncertainty_nm=None,
                  mc_valid_fraction=None, warnings=warnings)
    warnings.append("La dispersión consecutiva no es incertidumbre total; puede haber autocorrelación y drift.")
    if not scan.background or not scan.use_background:
        warnings.append("Sin fondo medido aplicado: el ancho puede estar sesgado por el offset.")
    if len(x) < len(scan.active_points):
        warnings.append("Longitudes repetidas: se promediaron las visitas con igual peso; revise deriva e histéresis.")
    if any(p.batch.saturated for p in scan.active_points):
        warnings.append("Posible saturación AUX (|V| >= 10.49 V); revise también el límite del detector.")
    if scan.background and scan.background.saturated:
        warnings.append("El fondo contiene lecturas próximas al límite de AUX.")
    if len(x) < 3:
        warnings.append("Puntos insuficientes: se requieren al menos tres longitudes distintas.")
        return result
    k = int(np.argmax(y))
    peak = float(y[k])
    result.update(peak_v=peak, peak_nm=float(x[k]), raw_peak_v=float(raw[k]))
    if len(x) < 9:
        warnings.append("Pocos puntos para caracterizar la respuesta y sus alas (menos de 9).")
    if peak <= 0:
        warnings.append("No hay un máximo positivo sobre el fondo; revise polaridad y offset.")
        return result
    if abs(bg) > peak*0.25:
        warnings.append("Fondo grande respecto de la altura de la línea (>25%).")
    if peak < 5*float(np.max(std)):
        warnings.append("Altura pequeña respecto de la dispersión; FWHM inestable.")
    if y[0] > peak*0.15 or y[-1] > peak*0.15:
        warnings.append("Alas incompletas: amplíe el barrido hasta el fondo en ambos extremos.")
    try:
        from scipy.signal import find_peaks
        peaks, _ = find_peaks(y, prominence=max(peak*0.2, 3*float(np.median(std))))
        if len(peaks) > 1:
            warnings.append("Múltiples máximos importantes: el ancho describe sólo el lóbulo del máximo global.")
    except ImportError:
        warnings.append("SciPy no disponible: no se evaluó la prominencia de máximos múltiples.")
    c = crossings(x, y)
    if c is None:
        warnings.append("FWHM no confiable: faltan ambos cruces de media altura alrededor del máximo.")
        return result
    left, right, li, ri = c
    width = right-left
    result.update(left_nm=left, right_nm=right, fwhm_nm=width,
                  asymmetry=float(((right-x[k])-(x[k]-left))/width))
    if width < 5*max(float(np.max(np.diff(x[li:ri+1]))), 1e-15):
        warnings.append("Muestreo grueso: menos de cinco intervalos por FWHM. Reduzca el paso.")
    # Heurística explícita de muestreo: cruce uniforme dentro de cada intervalo.
    grid_u = math.hypot(x[li+1]-x[li], x[ri]-x[ri-1])/math.sqrt(12)
    result["sampling_component_nm"] = float(grid_u)
    if trials >= 20:
        rng = np.random.RandomState(seed)
        widths = []
        bg_std = scan.background.std_v if scan.use_background and scan.background else 0.0
        for _ in range(trials):
            # El error de fondo es COMÚN a todos los puntos de la realización.
            sampled = raw + rng.normal(0, std) - (bg+rng.normal(0, bg_std))
            cc = crossings(x, sampled)
            if cc is not None:
                widths.append(cc[1]-cc[0])
        result["mc_valid_fraction"] = len(widths)/trials
        if len(widths) >= max(20, int(0.8*trials)):
            mc_std = float(np.std(widths, ddof=1))
            result.update(mc_std_nm=mc_std,
                          mc_ci95_nm=[float(v) for v in np.percentile(widths, [2.5, 97.5])],
                          fwhm_uncertainty_nm=math.hypot(mc_std, grid_u))
        else:
            warnings.append("Monte Carlo inestable: demasiadas realizaciones sin dos cruces válidos.")
    warnings.append("u(FWHM) combina MC condicional y una heurística de muestreo; no incluye calibración de lambda, drift ni ancho del láser.")
    return result


def slit_series(scans):
    """Sólo barridos finalizados, rendijas iguales, mismo grating y láser nominal."""
    groups = {}
    for scan in scans:
        exp = scan.experiment
        a = scan.analysis
        if scan.completed and a.get("fwhm_nm") is not None and abs(exp.slit_in_mm-exp.slit_out_mm) < 1e-12:
            key = (exp.grating, exp.laser_nominal_nm)
            groups.setdefault(key, []).append(dict(slit_mm=exp.slit_in_mm,
                fwhm_nm=a["fwhm_nm"], uncertainty_nm=a.get("fwhm_uncertainty_nm"), scan_id=scan.id))
    for rows in groups.values():
        rows.sort(key=lambda row: row["slit_mm"])
    return groups


def fit_slits(rows):
    """Aproximación empírica opcional, ajuste no ponderado por calidad desconocida."""
    if len(set(r["slit_mm"] for r in rows)) < 3:
        raise ValueError("El ajuste requiere al menos tres aperturas distintas.")
    from scipy.optimize import curve_fit
    w = np.array([number(r["slit_mm"], "Apertura", 0, True) for r in rows])
    y = np.array([number(r["fwhm_nm"], "FWHM", 0, True) for r in rows])

    def model(w, delta0, a):
        return np.sqrt(delta0**2+(a*w)**2)

    pars, _ = curve_fit(model, w, y, p0=[min(y)/2, max(y)/max(w)], bounds=(0, np.inf), maxfev=10000)
    return dict(delta0_nm=float(pars[0]), a_nm_per_mm=float(pars[1]),
                rmse_nm=float(np.sqrt(np.mean((model(w, *pars)-y)**2)))), model
