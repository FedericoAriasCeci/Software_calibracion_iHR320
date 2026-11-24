"""Ejemplo reproducible: tres aperturas, crudos, resumen y PNG."""
import json
import math
from dataclasses import asdict
import numpy as np
from .acquisition import acquire
from .analysis import analyze, fit_slits, slit_series
from .instruments import SimulatedReader
from .models import AcquisitionSettings, Experiment, Scan
from .plotting import export_plots
from .storage import SessionStore, atomic_text


def generate_example(output_root):
    store = SessionStore.create(output_root)
    truth = []
    try:
        settings = AcquisitionSettings(n=20,interval_s=0,wait_s=0)
        for index,w in enumerate((0.1,0.2,0.5)):
            width = math.sqrt(0.08**2+(1.8*w)**2)
            reader = SimulatedReader(fwhm_nm=width,noise_v=0.001,seed=830+index)
            reader.connect()
            scan = Scan(Experiment(experiment_id="ejemplo_sintetico",slit_in_mm=w,slit_out_mm=w),reader.describe())
            store.add_scan(scan)
            reader.blocked = True
            batch = acquire(reader,settings)
            store.update_scan(scan.id,lambda s:s.set_background(batch))
            reader.blocked = False
            for x in np.linspace(532-1.3*width,532+1.3*width,53):
                reader.wavelength_nm = float(x)
                batch = acquire(reader,settings)
                store.update_scan(scan.id,lambda s:s.add_point(float(x),batch))
            def finish(s):
                s.analysis,s.completed = analyze(s),True
            store.update_scan(scan.id,finish)
            a = store.get(scan.id).analysis
            truth.append(dict(scan_id=scan.id,slit_mm=w,true_fwhm_nm=width,
                              recovered_fwhm_nm=a["fwhm_nm"],absolute_error_nm=abs(width-a["fwhm_nm"])))
            reader.close()
        fits = []
        for key,rows in slit_series(store.scans).items():
            params,_ = fit_slits(rows)
            fits.append(dict(grating=key[0],laser_nominal_nm=key[1],**params))
        atomic_text(store.path/"synthetic_truth.json",json.dumps(dict(seed=830,profiles=truth,empirical_fits=fits),indent=2))
        export_plots(store)
        return store.path,truth
    finally:
        store.close()
