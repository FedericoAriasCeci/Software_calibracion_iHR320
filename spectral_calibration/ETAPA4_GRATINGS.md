# Etapa 4 — Torreta de gratings del HORIBA iHR320

Fecha: 4 de noviembre de 2026. Se conserva la comunicación ICL API 300 por
WebSocket, el worker existente y Python 3.7.3 de 64 bits. No se instalaron
dependencias ni se modificaron controladores, licencias o configuración ICL.

## Diagnóstico e interfaces

El controlador podía leer `mono_getGratingPosition`, pero no seleccionar una
red. El campo G1/G2/G3 del montaje sólo declaraba un dato del experimento.
El modelo de datos ya distinguía gratings y el análisis agrupaba por red;
se reutilizaron esas capacidades.

Se revisaron `mono_getConfig`, `mono_getGratingPosition`, `mono_moveGrating`,
`mono_isBusy` y `mono_isInitialized` en
`..\EzSpec-python-sdk\docs\icl_api20251029.md`. El SDK oficial confirma
`Grating.FIRST=0`, `SECOND=1`, `THIRD=2`; la GUI presenta **Grating 1/2/3** y
convierte a esos índices únicamente en la capa de hardware.
Referencias oficiales:

- [Código Python de HORIBA](https://github.com/HORIBAEzSpecSDK/python-sdk/blob/main/horiba_sdk/devices/single_devices/monochromator.py).
- [Manual iHR: torreta triple y operación](https://www.horiba.com/fileadmin/uploads/Scientific/Downloads/UserArea/OSD/Manuals/iHR_Series_Manual.pdf).

La API advierte que la posición de torreta puede no ser fiable antes de
inicializar y que el sensor no vuelve a medir físicamente la posición en cada
movimiento. Se exige el estado de inicialización de ICL; las lecturas no se
presentan como certificación óptica ni de un encoder independiente.

El [diagnóstico inicial](etapa4_diagnostico.json), equipo
**2756D-1626-IHR320**, índice 0, ICL `2.1.0.19.50f46f97`, informó:

| GUI | Índice API | Densidad configurada | Blaze recibido |
| --- | --- | --- | --- |
| Grating 1 | 0 | 1200 líneas/mm | 0 |
| Grating 2 | 1 | 600 líneas/mm | 0 |
| Grating 3 | 2 | 300 líneas/mm | 0 |

No se inventaron identificación, descripción ni longitud de blaze. La API
local no documenta aquí las unidades del campo `blaze`; se conserva como
`blaze_reported`. En esta unidad, cero no aporta una longitud utilizable y
no se importa como 0 nm. Descripción y densidad ausentes se muestran como
datos no informados. Sólo las posiciones configuradas pueden seleccionarse.

## Implementación y coherencia espectral

`read_gratings()` consulta catálogo, posición y λ sin mover ni inicializar.
Rechaza catálogos vacíos, posiciones inválidas/duplicadas y datos no finitos.
El estado actual reside en el controlador, no en el combo de la GUI.

`move_grating(position, wavelength_nm, cancel=None)` aplica una secuencia:

1. Validar posición instalada, λ final y límites de software antes de mover.
2. Exigir instrumento libre e inicializado.
3. Enviar `mono_moveGrating` si la posición es distinta y esperar disponibilidad.
4. Confirmar posición e inicialización, y registrar la λ informada tras girar.
5. Ordenar explícitamente `mono_moveToPosition` para la λ final después de
   cualquier cambio de red; reutiliza la lógica de movimiento existente.
6. Verificar grating y λ final, y sólo entonces confirmar la configuración.

No se supone compensación automática de λ. No se utiliza `mono_setPosition`
ni se redefine el origen/calibración del instrumento. Si la posición y λ
ya coinciden y no hay operación pendiente, se evitan movimientos duplicados.

El timeout de torreta es **180 s** por defecto; la fase de λ utiliza el timeout
de movimiento existente, **180 s**. Consultas y acuse de torreta usan **8 s**
por comando, acotados por el plazo de torreta. La GUI mantiene el procesamiento
de eventos durante las dos fases. La tolerancia de λ sigue siendo **0.01 nm**
por defecto; es un criterio de aceptación del software, no exactitud óptica.
La configuración no informa límites espectrales por red: el operador debe
ajustar los límites existentes al rango documentado de su montaje.

Un envío fallido, timeout o cancelación puede dejar una fase incompleta.
`grating_change_pending` bloquea movimientos de adquisición hasta completar
explícitamente grating y λ. Consultar no confirma esa secuencia; cambiar
timeouts/tolerancia para el mismo destino ICL tampoco borra el estado pendiente.
No hay reenvío ni reversión automática. Cancelar abandona la espera, no detiene
un motor ya accionado. La protección de estado se mantiene en esa instancia
y al reemplazarla por cambios de parámetros del mismo destino; una aplicación
nueva consulta el equipo y cada punto automático vuelve a ordenar/verificar λ.

## GUI, adquisición y calibraciones anteriores

La pestaña **Gratings** muestra tres filas, catálogo recibido, selección leída,
λ actual, solicitud y resultado. El selector sólo habilita posiciones instaladas.
**Aplicar grating y λ** requiere revisar la λ final; consultarla no mueve motores.
La apertura, giro, espera y ajuste utilizan el worker existente.

Se bloquea el cambio con un barrido seleccionado, incluido su fondo. Hay que
finalizar e iniciar uno nuevo. Al confirmar se actualizan grating, λ y densidad
del montaje. Un blaze manual de otra red se limpia; una consulta de la misma
red conserva datos manuales que ICL no puede completar.

No hay coeficientes globales de calibración de λ que este programa aplique al
hardware: calcula respuesta instrumental/FWHM y ajustes empíricos por red.
Los resultados anteriores conservan su grating, fondo, muestras e identidad;
no se reasignan ni aplican a una red nueva. El agrupamiento por grating ya
existente evita combinar G1/G2/G3 en el mismo ajuste FWHM. Verifique una línea
conocida y obtenga una calibración nueva para cada configuración. Si reemplaza
físicamente redes o cambia su configuración manteniendo el mismo número,
utilice una sesión nueva; no identifique una red sólo por la etiqueta G1.

Las adquisiciones ICL automáticas y manuales con lector real consultan y
comprueban la selección antes/después. En manual ICL también se exige que la
λ declarada coincida con la leída; tomar el punto no mueve el motor. Un cambio
de red durante adquisición descarta el lote. Una configuración distinta entre
puntos manuales se detecta y exige un barrido nuevo.

El registro usa estructuras existentes: catálogo e informe del último cambio
en `Scan.horiba`; grating y `grating_info` antes/después en `Point.instrument`.
La columna CSV `grating` conserva su formato y coincide con la selección
verificada en ICL. El JSON contiene las propiedades completas. El flujo manual
sin ICL y el simulador conservan su carácter declarado/simulado; no se presentan
como confirmación física de la torreta.

## Pruebas automatizadas

La base anterior de **144 pruebas** pasó antes de modificar. Resultado final:
**186 pruebas aprobadas en 75.886 s, sin omisiones**: 33 de núcleo, 61 de GUI,
27 de controlador/configuración ICL, 22 de rendijas, 18 de salida y 25 de gratings.
La ampliación
incluye catálogo/propiedades, índices, tres posiciones, secuencia torreta/λ,
λ que cambia o vuelve a cero, red ya seleccionada, destinos fuera de rango,
red ausente, catálogo inválido, estado desconocido, falta de inicialización,
acuse sin movimiento, timeout, fallo de λ, recuperación, acuse perdido,
cancelación entre fases, concurrencia y reapertura.

Tk comprueba selección y lectura separadas, protección del fondo, metadatos
JSON/CSV, adquisición manual/automática, cambio durante un punto, parámetros
de catálogo modificados entre puntos, conservación del blaze manual de la
misma red y persistencia del bloqueo al cambiar la tolerancia. Una regresión
reproduce la discrepancia real de G2 y confirma que aceptar otro umbral es
una elección explícita y que el valor predeterminado continúa en 0.01 nm.

Comando desde `spectral_calibration`:

```powershell
$env:PATH = 'C:\ProgramData\Anaconda3;C:\ProgramData\Anaconda3\Library\bin;C:\ProgramData\Anaconda3\Scripts;' + $env:PATH
$env:PYTHONPATH = (Get-Item ..).FullName
$env:CALIBRATION_GUI_TESTS = '1'
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -q
```

El resultado también se registra en `VALIDACION.md`. Persisten las
advertencias conocidas de NumPy/SciPy y PyVISA/`imp`; no se actualizaron paquetes.

## Ensayos reales y límite de tolerancia encontrado

Primero se probó G1→G2 con destino **532.600524902 nm** y tolerancia **0.01 nm**.
Tras girar, ICL informó **0.111678220 nm**; después del movimiento de λ informó
**532.6220703125 nm**. La diferencia de **0.0215454105 nm** fue rechazada y el
programa mantuvo la configuración pendiente. El ensayo se detuvo: no informó
éxito ni reintentó el movimiento automáticamente. Se consultó el estado y se
restauró explícitamente G1 y su λ inicial, con la tolerancia original.

Luego se hizo un ensayo funcional **G1→G2→G3→G1**, desde el botón de la GUI
en una ventana Tk oculta, con umbral de ensayo **0.05 nm** sólo en esa instancia:

| Destino | λ tras torreta [nm] | λ final informada [nm] | Diferencia al destino [nm] | Tiempo torreta + λ |
| --- | --- | --- | --- | --- |
| G2, 600 líneas/mm | 0.111678220 | 532.6220703125 | +0.0215454105 | 52.1 s |
| G3, 300 líneas/mm | 0.241128534 | 532.60888671875 | +0.0083618168 | 49.7 s |
| G1, 1200 líneas/mm | 0.067006931 | 532.6005249023438 | ~0 | 51.4 s |

Tk atendió eventos durante las operaciones. Repetir G1/λ final omitió nuevos
movimientos. Se reabrió la comunicación y otro proceso Python consultó de nuevo
G1 y **532.6005249023438 nm**. La tolerancia predeterminada del programa sigue
en **0.01 nm**. Se conservaron salida **Lateral** y rendijas **3.000/3.000 mm**,
los valores presentes al comenzar esta etapa.

Estos datos muestran que ICL **no conservó la λ informada al girar** en los
ensayos. No establecen la causa de las diferencias finales ni una resolución
o precisión óptica. El umbral de 0.05 nm sólo valida funcionalmente la secuencia;
**no es una recomendación de precisión ni una corrección de calibración**.
Antes de adquirir, investigue/valide con una línea conocida la tolerancia
adecuada para cada red. No se midió una línea espectral real, no se comprobó
ópticamente la λ ni se validaron todos los rangos espectrales. Los fallos
físicos de comunicación/motor se simularon, salvo el rechazo real por tolerancia.

Registros conservados:

- [Diagnóstico inicial](etapa4_diagnostico.json).
- [Fallo real con 0.01 nm](etapa4_gratings_fallo_001nm.json).
- [Restauración explícita con el umbral original](etapa4_restauracion_hardware.json).
- [Recorrido completo y traza GUI/ICL con 0.05 nm](etapa4_gratings_hardware.json).
- [Consulta en otro proceso y confirmación del valor predeterminado](etapa4_reapertura_hardware.json).

## Archivos y uso

Modificados: `horiba.py`, `gui.py`, `tests/test_horiba.py`, `tests/test_gui.py`,
`README.md`, `VALIDACION.md`. Nuevos: `tests/test_gratings.py`, este informe y
los cinco registros. Sin cambios en adquisición base, modelos, almacenamiento,
cálculos científicos, gráficos, dependencias o lanzador.

1. Abrir la aplicación actualizada con ICL activo, otros clientes cerrados,
   **Control de λ = ICL** e instrumento inicializado.
2. En **Gratings**, pulsar **Consultar gratings (sin mover)**. Confirmar catálogo,
   selección y λ. La consulta no presupone que estén ocupadas las tres posiciones.
3. Sin un barrido seleccionado, elegir red instalada y revisar **λ final [nm]**,
   límites y tolerancia apropiados para el experimento. Pulsar **Aplicar grating y λ**.
4. Esperar confirmación de ambas fases. Una configuración pendiente impide
   adquirir; consultar primero y completar el ajuste explícitamente. Cancelar
   no frena motores. No deducir éxito del acuse ni de la selección del combo.
5. Leer el montaje, comprobar una línea conocida y crear un barrido/fondo nuevos.
   Revisar el grating registrado en JSON/CSV. Los datos de otra red conservan
   su validez histórica, sin transferirse a la nueva configuración.

La etapa 4 termina aquí; quedan señaladas las validaciones ópticas pendientes.
