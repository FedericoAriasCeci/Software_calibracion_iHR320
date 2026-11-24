# Software de calibración del HORIBA iHR320

Software para medir la respuesta instrumental del monocromador HORIBA iHR320:
barre λ para una o varias aperturas de rendija sobre una línea láser estrecha,
leyendo la tensión del fotodiodo por AUX IN de un lock-in SR830. El
monocromador se controla con el servicio ICL de EzSpec-SDK. Incluye un modo
simulado para probar todo sin equipos.

La documentación completa está en
[`spectral_calibration/README.md`](spectral_calibration/README.md).

## Contenido

| Carpeta | Qué contiene |
|---|---|
| `spectral_calibration/` | Paquete Python: interfaz gráfica, control del iHR320 por ICL, lectura del SR830, almacenamiento y pruebas (`tests/`). |
| `spectral_calibration/ETAPA*.md`, `VALIDACION.md` | Registro de las etapas de desarrollo y de la validación con el equipo. |
| `calibration_data/` | Carpeta de datos. En el repositorio solo están los scripts de análisis (`ajustar_barrido.py`, `analizar_flanco.py`, `analizar_spot.py`, `Ajustar_barridos.bat`). |

## Uso rápido (Windows)

Desde la carpeta del repositorio:

```powershell
.\spectral_calibration\Iniciar_calibracion.bat
```

o directamente con Python:

```powershell
python -m pip install -r spectral_calibration\requirements-horiba.txt
python -m spectral_calibration gui
```

Pruebas:

```powershell
python -B -m unittest discover -s spectral_calibration\tests -v
```

## Mediciones (no se suben)

Las mediciones **no se versionan**. El programa las guarda en
`calibration_data/` (barridos en `calibration_data/slit_sweeps/`, sesiones del
formato anterior en `calibration_data/session_*/`) y el `.gitignore` excluye
todo lo que hay ahí salvo los scripts. También quedan fuera los CSV en
cualquier carpeta, los diagnósticos e informes de ensayos con el equipo
(`diagnostico_horiba.json`, `etapa*_*.json`), los PNG de resultados y la
sesión sintética de `examples/` (se regenera con
`python -m spectral_calibration example`).

Por eso los enlaces a `etapa*_*.json` que aparecen en los documentos de
etapas apuntan a archivos que solo existen en la computadora del laboratorio.
