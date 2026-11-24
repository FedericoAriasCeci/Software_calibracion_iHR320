# Etapa 1 — Inicialización del HORIBA iHR320

Fecha: 4 de noviembre de 2026. Alcance: inicialización exclusivamente.

## Diagnóstico y plan aplicado

| Módulo | Responsabilidad y situación inicial |
| --- | --- |
| `horiba.py` | `ICLMonochromator`: JSON/WebSocket, descubrimiento, apertura/cierre, estado/montaje y movimientos de λ. `connect()` abría comunicaciones pero rechazaba el instrumento sin inicializar; no enviaba `mono_init`. |
| `gui.py` | Tkinter: diagnóstico, lectura de montaje y barrido automático. No tenía control de inicialización ni indicador propio del espectrómetro. El estado del lector SR830 es independiente. |
| `acquisition.py` | Worker único, cola de resultados y adquisición cancelable. Se reutilizó sin cambios. |
| `instruments.py` | SR830 por VISA y simulador. Se conservó sin cambios. |
| `models.py`, `storage.py`, `analysis.py`, `plotting.py` | Datos, persistencia y calibración/FWHM. Se conservaron sin cambios. |
| `requirements-horiba.txt`, `.venv`, `Iniciar_calibracion.bat` | Entorno Python 3.7.3 **64 bits**, bibliotecas de Anaconda y `websocket-client`. No se instalaron paquetes ni se modificó el lanzador. |

La calibración actual no importa el bridge COM de `CodigoV4`: utiliza ICL en
`ws://127.0.0.1:25010`. Ya podía leer grating y, configurando ubicaciones,
aperturas; no enviaba órdenes para cambiarlos. No tenía control del espejo de
salida. Su corrección queda para las etapas siguientes; aquí no se diagnosticó
la causa del fallo físico de rendijas.

Plan ejecutado: añadir inicialización al controlador existente, invocarla con
el worker de la GUI, reutilizar la sesión y verificar con dobles y consultas
reales. No se creó una segunda capa de comunicación.

## Interfaces investigadas y evidencias

**COM antiguo de HORIBA.** Registro de `JYMono.Monochromator.1`, CLSID
`{BFF968D4-7B20-4D16-BD3A-BDEFEB0863A4}`, con servidor `InprocServer32`
en vista **32 bits**:
`C:\Program Files (x86)\Jobin Yvon\Common\JY Components\Monos\JYMono\JYMono.dll`.
La vista 64 bits no tiene ese servidor. No se instanció COM ni se probó una
sesión física por esa vía. El Python actual de 64 bits no puede cargar esa
DLL directamente; requeriría cliente de 32 bits o un puente entre procesos.

Los ejemplos oficiales instalados en
`C:\Program Files (x86)\Jobin Yvon\SDK\Examples\NET\C#\Mono_Cs_NET\Mono_Cs_NET\Form1.cs`
y `Examples\C++\Mono_Cpp_2010\Mono_Cpp_2010Dlg.cpp` muestran configuración
por `Uniqueid`, `Load`, `OpenCommunications`, `Initialize` y eventos/consultas
de finalización. Documentan automatización sin operar manualmente la GUI del
fabricante, conservando sus componentes propietarios. No se añadió este backend
porque ICL ya está integrado y evita introducir el puente de arquitecturas.

**EzSpec-SDK/ICL.** Se encontró
`C:\Program Files\HORIBA Scientific\SDK\icl.exe`, con respuesta real
`nodeApiVersion=300`, versión `2.1.0.19.50f46f97`. La configuración existente
tenía `Mono=true`, `Model=SDK`. No se alteraron registros, configuración del
sistema ni licencias. Hay archivos de drivers en
`C:\Program Files (x86)\Jobin Yvon\Drivers\32` y `Drivers\64`; además ICL
detectó y consultó el instrumento físico. La existencia de archivos no se usa
sola como prueba de funcionamiento ni de licencia.

**Documentación del protocolo.** Se revisó
`..\EzSpec-python-sdk\docs\icl_api20251029.md`, también instalado dentro del
SDK. Documenta `mono_open`, `mono_init`, `mono_isBusy`, `mono_isInitialized`,
`mono_getPosition` y `mono_close`. `mono_init` inicia homing asíncrono, con
`force=false` en su ejemplo; hay que consultar cuándo termina. El método
`initialize` de `horiba_sdk/devices/single_devices/monochromator.py` confirma
esa operación. El paquete oficial requiere Python >=3.9: se mantuvo el
transporte directo documentado compatible con Python 3.7.

Referencias públicas oficiales consultadas:

- [EzSpec-SDK: compatibilidad con iHR320](https://www.horiba.com/usa/scientific/products/detail/action/show/Product/ezspec-sdk-6853/).
- [SDK Python: ICL, licencia y requisitos](https://github.com/HORIBAEzSpecSDK/python-sdk).
- [Historial oficial de comandos .NET](https://github.com/HORIBAEzSpecSDK/dotnet-sdk/blob/main/index.md): señala limitaciones históricas para determinar si hubo homing desde el último encendido, en versiones ICL 2.0 anteriores. No prueba el comportamiento de esta versión 2.1; motiva validar el arranque en frío.

### Grado de independencia

| Posibilidad | Resultado |
| --- | --- |
| Prescindir de la GUI SynerJY | Se puede solicitar inicialización por la API oficial ICL sin abrir esa GUI. Durante las consultas reales no se observó proceso SynerJY activo; el equipo ya estaba inicializado. Falta probar el arranque en frío. |
| Utilizar componentes propietarios de HORIBA | Sí: se depende de `icl.exe`, SDK, controladores, configuración y licencia. ICL debe estar activo. Durante la sesión dejó de estarlo y se inició nuevamente con autorización, en segundo plano. La aplicación no lo inicia automáticamente. |
| Prescindir completamente del software propietario | No se implementó ni verificó. No se dispone aquí de un protocolo USB independiente documentado para reemplazar ICL. No se añadieron comandos de bajo nivel. |

## Implementación

- `horiba.py`: `initialize()` consulta primero el estado real. Envía
  `mono_init(index, force=false)` sólo cuando no está inicializado y está libre.
  Espera `busy=false` y exige `initialized=true` más lecturas válidas de posición
  y montaje. Abrir WebSocket/COM o recibir el acuse del comando no bastan.
- Timeout de inicialización separado del de movimiento: **180 s** por defecto
  para comprobaciones, homing y lectura final. La apertura y descubrimiento
  anteriores utilizan **8 s por comando** por defecto. Ambos se configuran
  desde la GUI.
- Estado en la capa de hardware mediante `InstrumentStatus`: `disconnected`,
  `initializing`, `ready`, `error`. Se conservan último snapshot e informe de
  inicialización exitosa con fecha UTC. La GUI refleja este estado, no lo usa
  como sustituto de las consultas al instrumento.
- Exclusión de inicializaciones concurrentes y controles GUI deshabilitados
  durante operaciones. La decisión de omitir homing se basa en la consulta a
  ICL, no en una variable de la ventana ni en un estado guardado en disco.
- Sin reenvíos automáticos tras errores o acuses perdidos. Se descarta el
  transporte y se registran comando, ID, índice y, cuando existe, serie.
  Cancelar abandona la espera; no frena motores. Si el resultado es incierto,
  se informa que el homing puede continuar y se exige consultar antes de repetir.
- `gui.py`: botones **Inicializar espectrómetro**, **Consultar estado HORIBA**
  y **Desconectar HORIBA**, indicador y timeout, en **HORIBA automático**.
  Apertura, espera, consultas y cierre utilizan el worker existente.
- Se reutiliza la sesión para leer montaje y comenzar el barrido ICL; si cambian
  parámetros se cierra la anterior fuera de Tk. En reposo se consulta cada **5 s**
  sin competir con adquisición/guardado. La detección de pérdida de conexión
  depende de la siguiente consulta y su timeout. Durante barridos siguen las
  comprobaciones ya existentes.
- El informe acompaña los metadatos de futuros barridos si se reutiliza esa
  sesión. Conectar/leer/iniciar un barrido no solicita homing implícito.
  Inicializar tampoco sobrescribe el montaje declarado: debe leerse y revisarse
  antes de adquirir.

Archivos modificados: `horiba.py`, `gui.py`, `tests/test_horiba.py`,
`tests/test_gui.py`, `README.md`, `VALIDACION.md`. Se añadieron este informe y
los cinco registros JSON de la tabla siguiente. No se modificaron comandos de
rendijas, espejo ni selección de grating.

## Pruebas automatizadas

Antes del cambio: **56 pruebas aprobadas**. Resultado final:
**82 pruebas aprobadas en 36.984 s, sin omisiones**:
33 de núcleo, 22 de GUI y 27 de controlador/configuración ICL.

Los casos nuevos cubren homing simulado, lectura final obligatoria, omisión de
duplicados, concurrencia, equipo ocupado/ausente, respuesta inválida, rechazo
del comando, timeout incluida la lectura final, cancelación, acuse perdido,
recuperación con la misma instancia, reapertura y API no admitida. Tk verifica
respuesta de la interfaz, estados, consulta periódica, reutilización de sesión,
cambio de parámetros, ausencia de fugas en un inicio de barrido rechazado y
cierre fuera del hilo principal. Son dobles del hardware; no certifican motores.

Desde `spectral_calibration`:

```powershell
$env:PATH = 'C:\ProgramData\Anaconda3;C:\ProgramData\Anaconda3\Library\bin;C:\ProgramData\Anaconda3\Scripts;' + $env:PATH
$env:PYTHONPATH = (Get-Item ..).FullName
$env:CALIBRATION_GUI_TESTS = '1'
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v
```

Persisten advertencias preexistentes de NumPy/SciPy e `imp`/PyVISA del entorno
antiguo. No se actualizaron bibliotecas. Las pruebas concluyeron con `OK`.

## Validación real

Instrumento: **HORIBA iHR**, serie **2756D-1626-IHR320**, índice **0**.

| Comprobación | Resultado y evidencia |
| --- | --- |
| Detección sin abrir SynerJY durante esta sesión | ICL detectó un dispositivo, `count=1`. [Diagnóstico](etapa1_diagnostico.json). No demuestra cómo se inicializó previamente. |
| Servidor ICL no activo | `WinError 10061`, estado `error`, sin órdenes de homing. [Registro](etapa1_error_servidor.json). |
| Inicialización sobre el estado actual y repetición | ICL respondió `initialized=true`; ambas llamadas devolvieron `already_initialized=true`, `initialization_sent=false`. Lectura real **538.6141357421875 nm**, **G1**. [Traza completa](etapa1_inicializacion_hardware.json). |
| Cerrar y reconectar | La misma instancia recuperó posición/estado después de `mono_close` y nueva apertura; registro en la misma traza. |
| Flujo de GUI con respuestas reales | Otro proceso Python, ventana Tk oculta: inicialización, indicador **Listo**, consulta y desconexión correctas. No se falsificaron respuestas. [Registro](etapa1_gui_hardware.json). |
| Pérdida y recuperación del transporte local | Se cerró deliberadamente el WebSocket; la consulta siguiente falló y marcó error. La misma instancia reconectó, leyó posición y omitió un nuevo homing. No se desconectó USB. [Registro](etapa1_recuperacion_hardware.json). |

**No se ejecutó `mono_init` sobre hardware físico**: ICL ya informaba
inicialización y se respetó la prevención de duplicados. El camino de homing
está documentado y cubierto con mocks, pero falta probarlo desde un equipo
recién encendido. También quedan pendientes la desconexión USB física, el
reinicio de Windows y la verificación óptica de la posición leída.

## Procedimiento experimental desde la GUI

1. Cerrar SynerJY y otros clientes de control. Preparar el montaje para homing;
   puede haber movimiento mecánico. Iniciar ICL con licencia y conectar el
   iHR320. Para el ensayo pendiente, encender el equipo desde cero sin
   inicializarlo en SynerJY, con el operador presente en el laboratorio.
2. Abrir `Iniciar_calibracion.bat`, pestaña **HORIBA automático**, elegir
   **Control de λ = ICL**. Inicializar no requiere conectar el SR830.
3. Pulsar **Diagnosticar ICL (sin mover)** y confirmar serie e índice. No asumir
   que el índice 0 identifica siempre al mismo dispositivo.
4. Configurar **Timeout inicialización [s]** (180 s iniciales) y pulsar
   **Inicializar espectrómetro**. Comprobar **Inicializando**, ventana receptiva
   y luego **Listo**, con lectura de posición y serie.
5. Si ICL informa que ya estaba inicializado después de un encendido en frío,
   registrar el comportamiento: eso no prueba un homing nuevo. Verificar con
   HORIBA qué significa ese estado para este firmware/ICL antes de forzar una
   reinicialización. Esta entrega sólo utiliza `force=false`.
6. Pulsar **Consultar estado HORIBA** y comparar con una referencia experimental
   u observación óptica. Repetir **Inicializar espectrómetro** debe omitir homing
   cuando ICL informa inicialización y el equipo está libre.
7. Usar **Leer montaje HORIBA (sin mover)** y revisar valores antes de crear
   un barrido. Ajustes de rendijas, espejo y grating quedan para sus etapas.
8. Probar **Desconectar HORIBA** y luego **Consultar estado HORIBA**. En un ensayo
   controlado sin adquisición, desconectar/reconectar USB y exigir error seguido
   de lectura válida. Tras timeout/cancelación de homing, esperar y consultar
   antes de reintentar: abandonar la espera no detiene el equipo.
9. Cerrar/reabrir la aplicación y repetir diagnóstico/consulta. Con el operador,
   repetir después de apagar/encender el instrumento y de reiniciar Windows.
   Registrar software activo, estado inicial, duración, resultado y lectura
   posterior. Las consultas correctas no certifican la calibración óptica.

La etapa 2 requiere autorización explícita del usuario. No se avanzó a ella.
