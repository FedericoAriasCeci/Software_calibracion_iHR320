# Validación de la entrega

## Fondo opcional en el barrido, 6 de noviembre de 2026

Se agregó **Medir fondo al iniciar (láser bloqueado)** en 2 · Barrido. Dentro del
mismo job del worker: lecturas iniciales (sin mover), N lecturas con el láser
bloqueado (confirmado por el usuario), relectura ICL que exige el mismo estado,
y espera de la confirmación de láser desbloqueado; recién entonces se crea el
CSV y empiezan los movimientos de `run_slit_sweep` (sin cambios). Cancelar en
cualquiera de esas fases no mueve el equipo ni crea archivos. El fondo se guarda
en la clave `background` del JSON (`null` si no se midió); el CSV conserva
tensiones crudas y **Restar fondo** sólo afecta gráfico y tabla. Es un único
fondo por barrido, medido con las rendijas en su posición previa.

**Resultado: 201 pruebas aprobadas en 56.8 s, sin omisiones** (195 anteriores + 5 de GUI
+ 1 de `sweep_export`): orden fondo → movimientos con dobles ICL, relectura que
aborta si cambia la salida, cancelación al bloquear y al desbloquear, resta sólo
visual y metadatos completos. Verificación visual con barrido SIM. **No se
ensayó con el láser, el iHR320 ni el SR830 reales.**

## Reorganización de la interfaz, 6 de noviembre de 2026

Línea base antes de los cambios: **198 pruebas OK en 90.8 s**.
**Resultado: 195 pruebas aprobadas en 51.3 s, sin omisiones** (con
`CALIBRATION_GUI_TESTS=1`): 134 sin cambios (núcleo, ICL, rendijas, salida,
gratings y barrido de rendijas), 55 de la GUI nueva (reemplazan a las 64 de la
anterior) y 6 nuevas de `tests/test_sweep_export.py`.

Cambios: la GUI se reorganizó en franja de **Estado**, **1 · Equipos** (ICL,
inicialización, SR830 y su lectura, grating, salida, rendijas manuales, límites
y timeouts) y **2 · Barrido** (barrido λ × rendijas unificado, con notas).
Se quitaron el barrido manual, el «HORIBA automático» (equivale a una sola
apertura), la medida/resta de fondo (luego reincorporada como paso opcional,
ver arriba), las sesiones y el análisis FWHM de la GUI;
`analyze`/`example` siguen para sesiones anteriores. El gráfico muestra sólo la
configuración en curso (sin dibujar sobre las anteriores; superposición
opcional) y conserva el zoom. **Guardar datos del barrido…** exporta CSV + JSON;
el JSON incluye configuración completa del HORIBA (con timeouts), estado inicial
y final, conexión y lectura del SR830, plan y notas (`sweep_export.py`).

Sin cambios en `slit_sweep.py`, `acquisition.py`, `instruments.py`, `models.py`,
`storage.py`, `analysis.py` ni en la lógica de `horiba.py` (sólo dos mensajes que
nombraban la pestaña eliminada). Los metadatos entran por el parámetro
`metadata` ya existente de `run_slit_sweep`. Única regla nueva en la GUI: con un
cambio de grating pendiente informado por ICL, el barrido no comienza (antes lo
impedía la validación del automático). Para el estado inicial se agrega una
lectura `read_slits` previa (sin movimiento). La GUI anterior y sus pruebas
quedaron en `respaldo_gui_etapa4/`.

Verificación visual: ventana real con barrido SIM de 3 configuraciones (pestañas,
franja de estado, gráfico por configuración y superposición). **No se ensayó con
el iHR320 ni con el SR830 reales**; el flujo ICL se probó con el doble `FakeICL`.

## Barrido en apertura de rendijas, 6 de noviembre de 2026

**198 pruebas aprobadas en 97.2 s, sin omisiones** (186 anteriores + 12 nuevas:
9 en `tests/test_slit_sweep.py` y 3 de GUI en `tests/test_gui.py`).
Cubren planes por rango y por listas (emparejado, valor único repetido, errores),
CSV único con columnas de entrada/salida, perfil más ancho a mayor apertura en
SIM, cancelación conservando filas, no sobrescritura, secuencia ICL con dobles
(rendijas antes de λ, sin `mono_init`/`mono_setPosition`), fallo de rendija sin
reintento, y desde la GUI: vista previa, bloqueo con barrido seleccionado y cancelación.
**No se ensayó con el iHR320 ni con el SR830 reales.**

Gráfica en vivo (misma fecha): 198 pruebas OK en 87.2 s. Las pruebas comprueban
el callback por punto, una curva por configuración, grises para las anteriores,
leyenda única de la actual y el regreso a la vista normal al adquirir un barrido.

## Etapa 4: torreta y coherencia espectral, 4 de noviembre de 2026

**186 pruebas aprobadas en 75.886 s, sin omisiones**: 33 de núcleo, 61 de GUI,
27 de controlador/configuración ICL, 22 de rendijas, 18 de salida y 25 de gratings.
Las 144 pruebas anteriores pasaron antes de modificar esta etapa.

Se añadieron catálogo/selección de gratings, validación de posiciones instaladas,
espera de torreta, lectura de λ después del giro y ajuste explícito de λ con el
motor existente. Una secuencia incompleta bloquea adquisición; cambiar parámetros
de comunicación o tolerancia del mismo destino no borra ese estado pendiente.
No se instaló nada ni se modificaron controladores/configuración del sistema.

La unidad `2756D-1626-IHR320` informó 1200/600/300 líneas/mm para G1/G2/G3;
`blaze=0` en los tres casos no se convirtió en una longitud de blaze inventada.
En ICL, los puntos manuales y automáticos registran selección/propiedades y
comprueban el estado antes/después. Los CSV conservan su columna `grating`;
las propiedades y verificaciones completas quedan en JSON. Se conservan los
formatos, almacenamiento y cálculos previos, asociados a su red original.

**Hallazgo real de tolerancia:** G1→G2 dejó una lectura de 0.111678220 nm tras
girar. Después de ordenar 532.600524902 nm, ICL informó 532.6220703125 nm;
la diferencia de 0.0215454105 nm fue correctamente rechazada con el umbral
predeterminado de 0.01 nm. Se detuvo el ensayo y se restauró explícitamente
G1/λ inicial con ese umbral original. No se confirmó ni reenvió automáticamente.

Ensayo funcional posterior, con **0.05 nm sólo en la instancia de prueba**:
G1→G2→G3→G1, mediante el botón de la GUI oculta y comunicación real. Se obtuvo
respectivamente 532.6220703125, 532.60888671875 y 532.6005249023438 nm para el
mismo destino de 532.600524902 nm. Las operaciones torreta+λ duraron 52.1,
49.7 y 51.4 s; Tk siguió atendiendo eventos. Repetir G1/λ no movió; cerrar y
reabrir y un proceso Python nuevo recuperaron la selección correcta.

Estado final confirmado: **G1, 532.6005249023438 nm, Lateral, rendijas 3/3 mm**,
igual al estado inicial. La tolerancia predeterminada sigue en **0.01 nm**.
El umbral de ensayo 0.05 nm no certifica precisión ni corrige calibración.
La causa de las discrepancias y la verificación con una línea conocida siguen
pendientes; no se midió físicamente una línea espectral ni el rango completo
de las redes. Los demás fallos se simularon, sin forzar fallos mecánicos/USB.

Evidencias: `etapa4_diagnostico.json`, `etapa4_gratings_fallo_001nm.json`,
`etapa4_restauracion_hardware.json`, `etapa4_gratings_hardware.json` y
`etapa4_reapertura_hardware.json`. El [informe de etapa 4](ETAPA4_GRATINGS.md)
incluye procedimientos, restricciones y archivos. No se modificaron modelos,
adquisición base, almacenamiento, análisis, gráficos, dependencias ni lanzador.

## Etapa 3: selector de salida, 4 de noviembre de 2026

**144 pruebas aprobadas en 55.002 s, sin omisiones**: 33 de núcleo, 44 de GUI,
27 de controlador/configuración ICL, 22 de rendijas y 18 específicas de salida.
Las 114 pruebas de la etapa 2 pasaron antes de los cambios.

Se añadieron consulta y cambio Axial/Lateral exclusivamente del espejo de salida,
con `locationId=1` y posiciones 0/1 según ICL API 300 y el SDK oficial. No se
reutiliza el `2` que aparece en la lista `configuration.mirrors` ni los índices
de las rendijas. La GUI separa selección solicitada de lectura confirmada,
con timeout, cancelación y estado desconocido ante fallo. No se instalaron
paquetes ni se modificó configuración del sistema.

Ensayo real sobre `2756D-1626-IHR320`: **Lateral → Axial → Lateral**, confirmado
por lectura ICL, aproximadamente 2.6 s por movimiento. Repetir Lateral omitió
movimiento y reabrir la conexión volvió a leer Lateral. Un proceso GUI nuevo,
con ventana Tk oculta, accionó el botón en ambos sentidos y siguió atendiendo
eventos. En ambos ensayos se restauró Lateral; permanecieron las lecturas de
532.6005249023438 nm, G1 y rendijas 3.000/3.000 mm existentes al iniciar esta
etapa de pruebas. No se enviaron movimientos de λ, rendijas ni grating.

El barrido automático consulta y registra la salida en JSON, la verifica antes
y después de adquirir y descarta el punto si cambia. Se conservan los modelos,
CSV y cálculos científicos; el FWHM frente a apertura común no agrupa por ruta,
por lo que se recomiendan sesiones separadas para comparar rutas distintas.

Registros: `etapa3_diagnostico.json`, `etapa3_salida_hardware.json` (traza completa)
y `etapa3_gui_hardware.json`. **La trayectoria real del haz no se verificó
ópticamente**; la confirmación experimental realizada corresponde a las
respuestas del controlador. Fallos de motor, pérdida de respuesta, estado
desconocido y cancelación se probaron con mocks, sin forzar fallos físicos.

El [informe de etapa 3](ETAPA3_SALIDA.md) contiene comandos, archivos y uso desde
la GUI. No se avanzó a la selección del grating de la etapa 4.

## Etapa 2: rendijas, 4 de noviembre de 2026

**114 pruebas aprobadas en 44.530 s, sin omisiones**: 33 de núcleo, 32 de GUI,
27 de controlador/configuración ICL y 22 específicas de rendijas. Se partió
de las 82 pruebas de la etapa 1, aprobadas de nuevo antes de los cambios.
No se instalaron paquetes ni se modificaron controladores o configuración ICL.

Se añadieron detección de puertos, límites según tipo, lectura y movimiento
en mm, confirmación posterior independiente, timeout, cancelación, prevención
de duplicados, control de cambios inesperados en la otra rendija y registro de
resultados parciales. La GUI permite aperturas distintas y las conserva en el
montaje; el análisis de FWHM y el almacenamiento existentes se preservaron.

Prueba real, iHR320 serie `2756D-1626-IHR320`: `mono_getConfig` informó entrada
axial 1 y salida lateral 4, ambas `slitType=2` (7 mm). Se ensayó por separado
**entrada 1 → 1.025 → 1 mm** y **salida 1 → 1.025 → 1 mm**; se comprobó por
lectura ICL que la otra permanecía estable y se reabrió la comunicación.
En otro proceso Python, la GUI oculta aplicó ambas secuencialmente a 1.025 y
1.050 mm, importó el montaje desigual y restauró **1.000 / 1.000 mm**.

Registros: `etapa2_diagnostico.json`, `etapa2_rendijas_hardware.json`
(incluye traza de comandos) y `etapa2_gui_hardware.json`.
El rango 0–7 mm y la cuantización se probaron con mocks y documentación;
**no se ensayaron físicamente los extremos**. Tampoco se verificó con metrología
óptica la apertura ni se desconectó USB durante un ajuste. La confirmación real
es la lectura informada por ICL, no una certificación de encoder o calibración.

El [informe de etapa 2](ETAPA2_RENDIJAS.md) detalla implementación, archivos,
resultados y procedimiento GUI. No se implementaron las etapas 3 y 4.

## Etapa 1: inicialización, 4 de noviembre de 2026

Se integró `mono_init(force=false)` en el controlador ICL existente, con
comprobación previa, timeout, espera cancelable y verificación posterior de
estado, posición y montaje leído. La GUI utiliza el worker existente y conserva
la sesión para evitar aperturas superpuestas. No se modificó el control de
rendijas, espejo de salida ni selección de grating.

**Resultado final: 82 pruebas aprobadas en 36.984 s, sin omisiones** (56 antes
del cambio). El [informe detallado](ETAPA1_INICIALIZACION.md) distingue pruebas
simuladas de comunicación real. Se detectó el iHR320 serie `2756D-1626-IHR320`,
índice 0, sin proceso SynerJY activo. ICL API 300 informó `initialized=true`,
posición `538.6141357421875 nm` y `G1`. Dos llamadas de inicialización omitieron
homing; cierre y reapertura conservaron la lectura. Se comprobó el flujo de GUI
en una ventana Tk oculta con comunicación real, en otro proceso Python, y la
recuperación de una desconexión local WebSocket con la misma instancia.
ICL no estaba activo en un intento previo: se registró `WinError 10061` y se
inició nuevamente con autorización.

**No se ejecutó homing físico** porque ICL ya informaba inicialización. Quedan
pendientes el arranque en frío, desconexión USB física, reinicio de Windows y
corroboración óptica de la posición. No se certifica esa validación a partir
del éxito de COM/WebSocket o de la lectura de `initialized` solamente.

Evidencias: `etapa1_diagnostico.json`, `etapa1_error_servidor.json`,
`etapa1_inicializacion_hardware.json` (incluye traza de comandos),
`etapa1_gui_hardware.json` y `etapa1_recuperacion_hardware.json`.

La suite se amplió con homing simulado, omisión de duplicados, concurrencia,
equipo ocupado, respuesta inválida, rechazo de comando, timeout incluida la
lectura final, cancelación, pérdida de respuesta, reconexión y API no soportada.
Tk verifica respuesta de la interfaz, estados, consulta periódica, reutilización
de sesión y cierre en el worker. Comando de ejecución: el mismo documentado
debajo, con `CALIBRATION_GUI_TESTS=1`. Persisten las advertencias conocidas del
entorno científico antiguo; la suite terminó con `OK`.

## Integración ICL, 3 de noviembre de 2026

Se agregó control automático de longitud de onda mediante el protocolo JSON
de ICL API 300, compatible con el Python 3.7.3 existente. Se creó `.venv` dentro
de esta carpeta con acceso a las bibliotecas de Anaconda y websocket-client
1.6.1. No se cambiaron paquetes de Anaconda ni archivos de `CodigoV4`.

Diagnóstico real, sin instrumentos ni dongles: `icl_info` respondió versión
`2.1.0.19.50f46f97`. Inicialmente, `mono_discover`/`mono_list` devolvían
`[E];-1` («no parser found»). Se encontró `Model=Duetta`, `Mono=false` en
la configuración activa de EzSpec y un directorio llamado `icl_settings.ini`
en la ubicación SDK donde debía haber un archivo.

Se corrigieron ambas configuraciones con autorización del sistema y respaldo:

- `C:\ProgramData\HORIBA Scientific\SDK\ICL\icl_settings.ini.backup_20261103_155509_661387`
- `C:\ProgramData\HORIBA Scientific\EzSpec\ICL\icl_settings.ini.backup_20261103_155509_661387`

Después del reinicio normal de ICL, `mono_discover` respondió `count=0` y
`mono_list` respondió `devices=[]`, ambos sin errores. El informe con fecha
UTC quedó en `diagnostico_horiba.json`. Se confirma el acceso al módulo Mono;
no se certifica la licencia de operaciones sobre hardware desconectado.

Pruebas nuevas: dirección/extremos del barrido, ID de respuestas ICL, timeout,
error de módulo, equipo ocupado/sin inicializar, límites, posición fuera de
tolerancia, cancelación y timeout de motor. En la GUI: barrido automático
simulado con fondo, siete puntos, FWHM y exportaciones; cancelación; fallo de
snapshot con reintento sin mover; fallo de CSV conservando el punto canónico;
flujo ICL con dobles explícitos y discrepancia de montaje antes de mover.
Se comprueba la recuperación JSON de los metadatos del equipo y de cada punto.

**Resultado final: 56 pruebas aprobadas en 30.896 s**, sin omisiones:
33 de núcleo, 13 de GUI y 10 de ICL/configuración. Comando desde esta carpeta:

```powershell
$env:PATH = 'C:\ProgramData\Anaconda3;C:\ProgramData\Anaconda3\Library\bin;C:\ProgramData\Anaconda3\Scripts;' + $env:PATH
$env:PYTHONPATH = (Get-Item ..).FullName
$env:CALIBRATION_GUI_TESTS = '1'
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v
```

Persisten las advertencias del entorno científico antiguo descritas en la
entrega original; la suite terminó con `OK`.

La suite Tk comparte un intérprete raíz entre casos para impedir que el recolector
de ciclos destruya un intérprete Tcl anterior desde un worker. Las ventanas de
prueba permanecen ocultas.

Pendiente con hardware: conectar dongles/iHR320/SR830, confirmar el número de
serie e índice, inicialización, grating y ubicaciones de rendijas; comparar λ
informada con el panel; efectuar un barrido corto con AUX real y comprobar
cancelación/desconexión. No se ejecutó movimiento físico, homing ni cambio del
origen de calibración. Rendijas manuales siguen siendo datos declarados.

## Entrega manual original

Fecha: 2 de noviembre de 2026. Sistema: Windows, Python 3.7.3 de
`C:\ProgramData\Anaconda3`. NumPy 1.16.4, SciPy 1.2.1,
Matplotlib 3.1.0 y PyVISA 1.10.0. No se instalaron paquetes ni se modificaron
archivos de `CodigoV4` ni su entorno de pump-probe.

**Resultado: 40 pruebas aprobadas en 16.886 s**, incluidas 7 pruebas Tkinter
con ventanas ocultas. La suite está en `tests/test_core.py` (33) y
`tests/test_gui.py` (7). Comando utilizado desde la raíz:

```powershell
$env:PATH = 'C:\ProgramData\Anaconda3;C:\ProgramData\Anaconda3\Library\bin;C:\ProgramData\Anaconda3\Scripts;' + $env:PATH
$env:CALIBRATION_GUI_TESTS = '1'
& 'C:\ProgramData\Anaconda3\python.exe' -B -m unittest discover -s spectral_calibration\tests -v
```

El entorno Anaconda antiguo emitió advertencias de compatibilidad binaria al
importar algunos módulos de SciPy (`numpy.ufunc size changed`) y una advertencia
de deprecación de `imp` desde PyVISA/win32. Las pruebas terminaron sin errores.
Para una instalación nueva, el README documenta un entorno independiente con
Python actual. No se actualizó el entorno existente del laboratorio.

## Correspondencia con las pruebas solicitadas

| Prueba | Resultado y alcance |
| --- | --- |
| Conexión SR830 | Verificados con recursos VISA falsos: OUTX antes de IDN, identidad, OAUX, unidades, CR serial/LF GPIB, 8 bits, ausencia de handshake, timeout, cierre y fallo de apertura. **No se validó comunicación física.** |
| Línea sintética | Triángulo sin ruido: FWHM 0.400000 nm recuperado con tolerancia 1e-9 nm. Triángulo con ruido: error menor de 0.015 nm. Gaussiana: FWHM 0.8 nm recuperado por interpolación, sin ajuste gaussiano. |
| Interrupción | Un subproceso guardó un punto y terminó mediante `os._exit(0)`. La recuperación conservó el punto, liberó el bloqueo y permitió agregar otro. |
| Robustez GUI | Entradas inválidas, longitudes repetidas, sustitución, exclusión, pocos puntos, error de comunicación, canal incompatible, paso negativo y cancelación. Un evento Tk se procesó durante la espera y otro durante guardado deliberadamente lento. |
| Varias aperturas | Serie FWHM(w), separación por grating/láser y recuperación de parámetros empíricos conocidos. |
| Reproducibilidad | Igualdad de resultados completos al recargar las mismas muestras y metadatos, con semilla MC fija, en el mismo entorno. |
| Persistencia | Fallo de snapshot mantiene memoria/disco anteriores; fallo de CSV conserva el JSON canónico. Reintento de guardado GUI conserva el lote sin volver a medir. Se impiden dos escritores sobre una sesión. |
| Flujo completo GUI | Fondo → siete puntos → finalización → FWHM → CSV/PNG → recuperación de sesión. |
| Gráficos | Exportaciones PNG verificadas por las pruebas; inspeccionados visualmente el perfil de 0.1 mm y FWHM(w) del ejemplo. |

## Ejemplo entregado

Carpeta: `examples/session_20261102_142251_2c71b4/`.
Tres curvas triangulares, ruido σ=0.001 V, N=20, 53 puntos por curva y fondo
independiente por barrido. Se guardaron 159 puntos y 3240 lecturas incluyendo
las 60 muestras de fondo.

| Apertura [mm] | FWHM verdadero [nm] | FWHM recuperado [nm] | Error absoluto [nm] |
| --- | --- | --- | --- |
| 0.10 | 0.196977156 | 0.197012862 | 0.000035705 |
| 0.20 | 0.368781778 | 0.369037345 | 0.000255566 |
| 0.50 | 0.903548560 | 0.904202994 | 0.000654434 |

Ajuste empírico a esas curvas: Δλ₀=0.079814171 nm y a=1.801365017 nm/mm;
valores generadores 0.08 nm y 1.8 nm/mm. El error relativo de cada ancho es
menor de 0.08%. La verdad sintética y el resultado del ajuste están en
`synthetic_truth.json`. El resumen, crudos y PNG se incluyen en la misma carpeta.

Para regenerar otro ejemplo, con el entorno activo:

```powershell
python -B -m spectral_calibration example --output spectral_calibration\examples
```

Cada ejecución crea una carpeta distinta. La GUI actual no abre sesiones de
este formato; para reanalizar el ejemplo incluido:

```powershell
python -B -m spectral_calibration analyze spectral_calibration\examples\session_20261102_142251_2c71b4
```

## Pendiente de hardware

No se hicieron consultas a un SR830 real ni movimientos del HORIBA. El
procedimiento físico de identidad, AUX IN 1, comparación de tensión, fondo,
desconexión y barrido manual está en el README. La estimación de u(FWHM) es
condicional a la dispersión medida y al modelo de muestreo documentado; no
sustituye una evaluación experimental de drift, correlación y calibración de λ.
