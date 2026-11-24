import argparse
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="Calibración espectral del HORIBA iHR320: barridos λ × rendijas mediante ICL.")
    sub = parser.add_subparsers(dest="command")
    gui = sub.add_parser("gui",help="Abrir la interfaz (predeterminado)")
    gui.add_argument("--data-root",default=str(Path(__file__).resolve().parent.parent/"calibration_data"))
    example = sub.add_parser("example",help="Crear calibración sintética reproducible")
    example.add_argument("--output",default="calibration_data")
    review = sub.add_parser("analyze",help="Reanalizar una sesión anterior (formato session_*) y exportar CSV y PNG")
    review.add_argument("session")
    diagnostic = sub.add_parser("horiba-diagnose", help="Consultar ICL y enumerar monocromadores sin mover motores")
    diagnostic.add_argument("--output", help="Guardar informe JSON, incluidos errores")
    args = parser.parse_args()
    if args.command=="horiba-diagnose":
        import json
        from .horiba import ICLMonochromator
        from .models import utc_now
        from .storage import atomic_text
        mono = ICLMonochromator()
        report = dict(utc=utc_now())
        try:
            report.update(mono.diagnose())
        except Exception as exc:
            report.update(icl=mono.info, error=str(exc))
        finally:
            mono.close()
        text = json.dumps(report, ensure_ascii=False, indent=2)
        if args.output:
            atomic_text(args.output, text)
        print(text)
        if "error" in report:
            raise SystemExit(1)
    elif args.command=="example":
        from .example import generate_example
        folder,truth = generate_example(args.output)
        print(folder)
        for row in truth:
            print("w={slit_mm} mm: FWHM real={true_fwhm_nm:.6f}, recuperado={recovered_fwhm_nm:.6f} nm".format(**row))
    elif args.command=="analyze":
        from .storage import SessionStore
        from .plotting import export_plots
        store = SessionStore(args.session)
        try:
            store.reanalyze()
            export_plots(store)
            print(store.path/"analysis_summary.csv")
        finally:
            store.close()
    else:
        from .gui import launch
        launch(**({"data_root":args.data_root} if args.command else {}))


if __name__=="__main__":
    main()
