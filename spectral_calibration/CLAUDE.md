# Contexto para Claude — `spectral_calibration`

Aplicación Tkinter (Python) para **calibración espectral del monocromador HORIBA
iHR320**: mide la respuesta instrumental y su FWHM barriendo λ sobre una línea
láser estrecha, leyendo la tensión DC de un fotodiodo por **AUX IN del lock-in
SR830** (`OAUX? n`). Todo el proyecto, la documentación y la GUI están en
**español**; mantener ese idioma en código, mensajes y documentos.

Documentación de referencia (leer la sección pertinente antes de tocar algo):
- `README.md` — uso completo, datos, análisis, arquitectura.
- `VALIDACION.md` — historial de entregas y conteo de pruebas por etapa.
- `ETAPA1_INICIALIZACION.md` … `ETAPA4_GRATINGS.md` — informe por etapa
  (diagnóstico, implementación, ensayos reales, pendientes).
- Protocolo ICL: `..\EzSpec-python-sdk\docs\icl_api20251029.md`.

## Hardware y entorno de esta PC

- iHR320 serie `2756D-1626-IHR320`, índice 0 en `mono_list`.
  Gratings: G1=1200, G2=600, G3=300 l/mm (`blaze=0` reportado → no inventar).
  Rendijas: entrada axial `locationId=1`, salida lateral `locationId=4`, ambas
  tipo 2 (0–7 mm, paso 6.25 µm). Espejo de salida: `locationId=1`
  (0=Axial, 1=Lateral) — **no** usar el `2` de `configuration.mirrors`.
- Control vía **ICL de EzSpec-SDK** (`icl.exe`, API 300, v2.1.0.19) por
  WebSocket JSON en `ws://127.0.0.1:25010`, con `websocket-client`. No se usa el
  SDK Python oficial (requiere ≥3.9) ni COM/SynerJY (`JYMono.dll` es 32 bits).
  ICL debe estar corriendo; la app no lo inicia.
- **Python 3.7.3 64 bits** de Anaconda (`C:\ProgramData\Anaconda3`): NumPy 1.16,
  SciPy 1.2, Matplotlib 3.1, PyVISA 1.10. `.venv` local hereda esas libs y añade
  websocket-client 1.6.1. **Mantener compatibilidad con Python 3.7** (sin
  walrus, sin `dict |`, etc.). No instalar/actualizar paquetes sin pedirlo.
- Hay advertencias conocidas e inocuas (`numpy.ufunc size changed`, `imp`).

## Arquitectura (`spectral_calibration/`)

| Archivo | Rol |
| --- | --- |
| `__main__.py` | CLI: `gui` (default), `example`, `analyze <sesión>`, `horiba-diagnose`. |
| `gui.py` | Tk: franja de Estado + pestañas **1 · Equipos** (ICL, init, SR830 y su lectura, grating, salida, rendijas manuales, límites/timeouts) y **2 · Barrido** (λ × rendijas unificado + notas + fondo opcional con láser bloqueado, medido en el mismo job antes de crear archivos o mover) + Visualización por configuración (resta de fondo sólo visual) y botón Guardar datos. Sin barrido manual, sesiones ni FWHM. GUI anterior en `respaldo_gui_etapa4/`. |
| `sweep_export.py` | Metadatos del barrido (`notes`, `plan`, `horiba`, `sr830`, `background`) vía `run_slit_sweep(metadata=…)`, estado final y exportación CSV + JSON. Sin Tk ni comandos. |
| `horiba.py` | `ICLMonochromator` (transporte, `initialize`, `snapshot`, `move_to`, `read/move_slits`, `read/move_output`, `read/move_grating`), `HoribaSettings`, `InstrumentStatus`, `wavelength_plan`, `SimulatedMonochromator`. |
| `instruments.py` | Lector SR830 (VISA GPIB/RS232) y simulador; contrato `read_voltage(channel)`. |
| `acquisition.py` | Worker único con cola; lotes cancelables. Tk sólo se toca en el hilo principal. |
| `models.py` | Montaje, lotes, puntos con historial (`active`, `replaces_id`). |
| `slit_sweep.py` | Barrido rendijas × λ (pestaña "Barrido rendijas"); CSV único en `calibration_data\slit_sweeps` + JSON de estado. |
| `storage.py` | Sesiones del formato anterior en `..\calibration_data\session_*` (sólo CLI `analyze`/`example`); `session.json` canónico, CSV derivados, lock. |
| `analysis.py`, `plotting.py` | FWHM por interpolación, MC (400, semilla 830), ajuste `sqrt(δ0²+(a·w)²)`; PNG. Sólo CLI. |
| `repair_icl.py` | Corrige `icl_settings.ini` (Duetta→SDK/Mono). Toca ProgramData. |
| `tests/` | `test_core`, `test_gui`, `test_horiba`, `test_slits`, `test_output`, `test_gratings`, `test_slit_sweep`, `test_sweep_export`. |

## Principios de diseño que hay que respetar

Estas reglas se aplicaron en las 4 etapas; los cambios nuevos deben seguirlas:

- **Separar lo solicitado de lo leído.** El estado físico es la lectura de ICL,
  nunca el valor del combo/campo de la GUI ni algo guardado en disco.
- **Confirmar por relectura**, no por el acuse: esperar `mono_isBusy=false`,
  exigir `initialized`, releer y comparar con tolerancia.
- **Sin reenvíos ni reversiones automáticas** tras error/timeout/cancelación.
  Cancelar abandona la espera, no frena motores. Se exige consultar antes de reintentar.
- **Sin homing implícito**: `mono_init(force=false)` sólo desde el botón y sólo si
  ICL dice que no está inicializado. Nunca `mono_setPosition`.
- Operaciones de hardware siempre en el worker; exclusión mutua entre accesorios.
- Cambios de rendijas/salida/grating **bloqueados durante cualquier operación**
  (incluido un barrido). Cada barrido registra su estado inicial y final en su
  JSON; con `grating_change_pending` el barrido no comienza (antes de crear
  archivos o mover rendijas).
- No romper formatos existentes: columnas CSV del barrido se conservan; datos
  nuevos van al JSON (claves `notes`, `plan`, `horiba`, `sr830`, `background`;
  el CSV guarda tensiones crudas). En sesiones
  anteriores, `session.json` sigue siendo la fuente de verdad.
- No inventar datos que el equipo no reporta; ser explícito sobre qué se validó
  con hardware real y qué sólo con mocks.
- `CodigoV4` (programa pump-probe, PyQt) es **independiente**: no importarlo ni
  modificarlo desde aquí.

## Pruebas

Desde `spectral_calibration` en PowerShell (última cifra conocida: **201 OK**,
tras reemplazar las pruebas de la GUI anterior y agregar el fondo opcional):

```powershell
$env:PATH = 'C:\ProgramData\Anaconda3;C:\ProgramData\Anaconda3\Library\bin;C:\ProgramData\Anaconda3\Scripts;' + $env:PATH
$env:PYTHONPATH = (Get-Item ..).FullName
$env:CALIBRATION_GUI_TESTS = '1'
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -q
```

Sin `CALIBRATION_GUI_TESTS=1` se omiten las pruebas Tk. Correr la suite antes y
después de cada cambio y anotar el resultado en `VALIDACION.md`.

## Hardware: precauciones

- Cualquier comando que mueva motores (λ, rendijas, espejo, torreta, init)
  requiere autorización explícita del usuario; las consultas "sin mover" no.
- No usar SynerJY ni otro cliente a la vez. No ejecutar `repair_icl --apply`
  durante una medición.
- Estado del equipo al cierre de la etapa 4: G1, 532.6005 nm, Lateral,
  rendijas 3/3 mm (puede haber cambiado; consultar).

## Pendientes conocidos

- Discrepancia de λ tras cambio de grating (G2: +0.0215 nm > tolerancia 0.01 nm);
  causa no investigada. Validar con línea espectral conocida por red.
- Sin probar: homing en frío, desconexión USB física, extremos 0/7 mm,
  verificación óptica de rendijas/salida/λ, comunicación física con el SR830.
- Lectura `OUTP? 3` (R del lock-in) prevista por el contrato del lector, no implementada.

## Repositorio git

La raíz git es `Software_calibracióniHR320` (rama `main`), publicada en
https://github.com/FedericoAriasCeci/Software_calibracion_iHR320 (público).
Se versionan el paquete `spectral_calibration/` y los scripts `.py`/`.bat` de
`calibration_data/`. Las mediciones **no se versionan**: el `.gitignore` de la
raíz excluye el resto de `calibration_data/`, los CSV, `etapa*_*.json`,
`diagnostico_horiba*.json`, los PNG de resultados y `examples/`. No agregar
datos al repositorio ni hacer commits sin pedido explícito.
