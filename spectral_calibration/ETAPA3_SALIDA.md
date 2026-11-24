# Etapa 3 — Selector óptico de salida del HORIBA iHR320

Fecha: 4 de noviembre de 2026. Alcance: consulta y selección **Axial / Lateral**
del espejo de salida. La selección de grating queda para la etapa 4.

## Diagnóstico y mecanismo identificado

El proyecto de calibración no tenía comandos ni controles para el espejo de
salida. Ya utilizaba ICL API 300 por WebSocket para λ, inicialización y rendijas.
Se conservó esa interfaz y el worker único de `acquisition.py`; no se añadió
otro backend ni dependencia. El entorno sigue siendo Python 3.7.3 de 64 bits.

La documentación local oficial
`..\EzSpec-python-sdk\docs\icl_api20251029.md`, secciones
`mono_getMirrorPosition` y `mono_moveMirror`, define:

| Parámetro | Significado documentado |
| --- | --- |
| `locationId=0` en comandos de espejo | Espejo de entrada. No se controla en esta etapa. |
| `locationId=1` en comandos de espejo | Espejo de salida, objeto de esta implementación. |
| `position=0` | Axial. |
| `position=1` | Lateral. |

Los enums y métodos `Mirror.EXIT`, `MirrorPosition`, `get_mirror_position`
y `set_mirror_position` del SDK oficial corroboran esos valores. Fuente pública:
[código de HORIBA](https://github.com/HORIBAEzSpecSDK/python-sdk/blob/main/horiba_sdk/devices/single_devices/monochromator.py).
HORIBA también describe el control separado de espejos de entrada/salida en
[EzSpec-SDK](https://www.horiba.com/usa/scientific/products/detail/action/show/Product/ezspec-sdk-6853/).

Se consultó el equipo real **2756D-1626-IHR320**, índice 0, con ICL
`2.1.0.19.50f46f97`, API 300. `mono_getMirrorPosition(index=0, locationId=1)`
respondió `position=1`, **Lateral**.

Una diferencia importante: `mono_getConfig` enumera en esta unidad
`mirrors=[{"locationId":2}]`. No se copia ese número al comando ni se infiere
una regla general de conversión para configuraciones no documentadas. El
identificador de los comandos se toma directamente de la API y enum oficiales:
**EXIT=1**. Se verificó su lectura real antes de enviar movimientos.
El [diagnóstico](etapa3_diagnostico.json) conserva ambas respuestas.

Los `locationId` de rendijas describen componentes distintos. El puerto de
rendija 4 corresponde a salida lateral de esa rendija, no al identificador del
espejo de salida. Esta implementación nunca envía `locationId=4` ni el `2`
de configuración a un comando de espejo.

## Implementación

- `horiba.py`: `read_output()` abre y consulta el espejo de salida sin homing
  ni movimiento. `move_output(position, cancel=None)` acepta sólo **Axial** o
  **Lateral** y consulta la posición real antes de actuar.
- Si el destino ya coincide, se omite la orden de movimiento y se verifica de
  nuevo. De lo contrario se envía `mono_moveMirror`, se espera `mono_isBusy=false`,
  se exige inicialización y se relee el espejo. Sólo una coincidencia exacta
  confirma el ajuste; el acuse del comando no basta.
- Timeout de salida configurable: **30 s** para movimiento y confirmación.
  Apertura y consulta previa utilizan el timeout por comando, **8 s** por defecto.
  Se mantienen validación de API 300 y control del estado del instrumento.
- El estado actual se conserva en la capa de hardware, `output_position`.
  Desconexión, respuesta desconocida o fallo de movimiento retiran la confirmación.
  No se interpreta un valor desconocido como Axial. No se utiliza la selección
  del combo como sustituto del estado físico.
- `last_output_move` registra fecha UTC, destino, posición inicial, posición
  obtenida, envío y resultado. Los logs incluyen índice, espejo, solicitud,
  lectura y errores. La inicialización y los accesorios comparten exclusión
  mutua; la GUI serializa las operaciones mediante el worker existente.
- No hay reenvío ni reversión automática ante timeout, acuse perdido o cancelación.
  Cancelar abandona la espera; un espejo ya accionado puede continuar. Se debe
  consultar antes de repetir.
- `gui.py`: pestaña **Salida**, selector **Axial / Lateral**, **Consultar salida
  (sin mover)**, **Aplicar salida**, lectura actual y resultado del último ajuste.
  Al iniciar se muestra **desconocida**, con solicitud vacía. La lectura no
  sobrescribe la selección solicitada. Apertura, espera y consultas ocurren
  fuera del hilo Tk.
- No se permite cambiar la salida con un barrido seleccionado, incluido su fondo;
  hay que finalizar e iniciar otro. Una lectura válida activa el seguimiento de
  salida en snapshots y consultas periódicas. Los consumidores anteriores del
  controlador que nunca consultan salida conservan su comportamiento anterior.
- El barrido automático ICL consulta siempre la salida antes de empezar, la
  registra y la comprueba durante sus verificaciones previas/posteriores. Un
  cambio durante un punto lo descarta y detiene la secuencia, conservando puntos
  completos anteriores.

La trayectoria queda en JSON: `Scan.horiba.initial.output_port`,
`Scan.horiba.output_port` y `Point.instrument.output_port`, junto a su lectura
posterior en `after`. El informe del último cambio también acompaña los metadatos
del controlador. Se conserva el esquema existente de modelos y almacenamiento;
los CSV no incorporan una columna nueva de trayectoria.

No se modificaron los cálculos científicos. La gráfica FWHM frente a apertura
común mantiene su agrupación por grating y láser nominal; **no separa rutas
ópticas automáticamente**. Para comparar calibraciones de Axial y Lateral se
recomiendan sesiones separadas; la GUI lo indica.

## Pruebas automatizadas

Antes del cambio: **114 pruebas aprobadas**. Resultado de la suite ampliada:
**144 pruebas aprobadas en 55.002 s, sin omisiones**:

- 33 de núcleo, adquisición, modelos, análisis y almacenamiento.
- 44 de GUI, incluidas 12 nuevas de salida.
- 27 de controlador/configuración ICL y 22 de rendijas existentes.
- 18 nuevas del selector de salida en `tests/test_output.py`.

Los mocks prueban ambos sentidos, índices correctos, posición ya seleccionada,
solicitudes inválidas, ausencia de inicialización, equipo ocupado, posición
desconocida/malformada, comando no soportado, acuse sin cambio, rechazo, timeout
incluida la lectura final, pérdida de acuse, recuperación sin reenvío, cancelación,
concurrencia, nueva instancia y seguimiento en snapshots. Tk comprueba selección
separada de lectura, botones, protección del fondo, lectura periódica, nueva
ventana, persistencia de metadatos y descarte de puntos si cambia la ruta.

Comando desde `spectral_calibration`:

```powershell
$env:PATH = 'C:\ProgramData\Anaconda3;C:\ProgramData\Anaconda3\Library\bin;C:\ProgramData\Anaconda3\Scripts;' + $env:PATH
$env:PYTHONPATH = (Get-Item ..).FullName
$env:CALIBRATION_GUI_TESTS = '1'
.\.venv\Scripts\python.exe -B -m unittest discover -s tests
```

Persisten las advertencias preexistentes de NumPy/SciPy y PyVISA/`imp` del
entorno antiguo. No se instalaron ni actualizaron paquetes.

## Ensayos con el instrumento real

| Ensayo | Resultado informado por ICL |
| --- | --- |
| Consulta inicial | Lateral, código 1. |
| Lateral → Axial | Confirmado Axial, código 0; aproximadamente 2.6 s. |
| Axial → Lateral | Confirmado Lateral, código 1; aproximadamente 2.6 s. |
| Repetir Lateral | Confirmado sin enviar un nuevo movimiento. |
| Cerrar y reabrir comunicación | Nueva consulta devolvió Lateral. |
| GUI en proceso nuevo, ventana Tk oculta | Estado inicial desconocido; consulta Lateral, botón Aplicar a Axial y regreso a Lateral correctos. Tk atendió eventos durante ambas esperas. |
| Estado restante | λ=532.6005249023438 nm, G1, rendijas 3.000/3.000 mm, iguales antes/después. |

Las rendijas estaban en **3 mm al comenzar estas pruebas**, por ajustes
anteriores a este ensayo. No se restauraron a valores de otra etapa: se
conservaron sus lecturas iniciales. La salida quedó en **Lateral**, su posición
inicial. No se enviaron movimientos de λ, grating, rendijas, espejo de entrada
ni homing.

Evidencias:

- [Diagnóstico sin movimientos](etapa3_diagnostico.json).
- [Traza real del controlador](etapa3_salida_hardware.json): comandos, parámetros,
  respuestas, tiempos e informes de cambio/restauración.
- [Ensayo GUI real](etapa3_gui_hardware.json): posiciones, textos de estado,
  operaciones de cambio/restauración y atención del bucle Tk.

La validación real corresponde al **estado informado por ICL**. No se observó
ópticamente la trayectoria del haz ni se midió señal en ambos puertos; esa
comprobación requiere al operador y el sistema de detección del laboratorio.
Tampoco se forzaron fallos físicos, pérdida USB durante movimiento, apagado del
instrumento ni reinicio de Windows. Esos escenarios de error se cubrieron con
dobles, sin presentarlos como ensayos físicos.

## Archivos modificados y nuevos

- `horiba.py`: consulta, cambio, confirmación y seguimiento del espejo de salida.
- `gui.py`: controles, seguimiento y verificación durante el barrido automático.
- `tests/test_horiba.py`: doble ICL ampliado para espejos.
- `tests/test_output.py` (nuevo): pruebas específicas del selector.
- `tests/test_gui.py`: doce casos nuevos de interfaz y adquisición.
- `README.md`, `VALIDACION.md`, este informe y los tres registros JSON de arriba.

Sin cambios en `acquisition.py`, `models.py`, `storage.py`, `analysis.py`,
`plotting.py`, dependencias, lanzador, controladores ni configuración del sistema.
No se implementó selección de grating ni se alteraron sus parámetros.

## Procedimiento de uso y verificación óptica pendiente

1. Abrir la aplicación actualizada, con ICL activo y otros clientes de control
   cerrados. En **HORIBA automático**, elegir **Control de λ = ICL** e inicializar
   si corresponde. Confirmar la serie del equipo.
2. Entrar en **Salida** y pulsar **Consultar salida (sin mover)**. La posición
   actual debe aparecer como Axial o Lateral; una selección guardada o un valor
   desconocido no se consideran confirmación.
3. Sin un barrido seleccionado, elegir el destino y pulsar **Aplicar salida**.
   Esperar confirmación y verificar la lectura; la solicitud y el estado leído
   se presentan separados. El SR830 no es necesario para este control.
4. Para validación óptica, comprobar con el sistema de detección del laboratorio
   cuál de las salidas recibe señal en cada posición. Comparar Axial y Lateral
   y registrar el resultado junto con el estado de ICL. Esta prueba no se realizó
   en la sesión remota.
5. Repetir la posición actual debe omitir movimiento. Tras cerrar/reabrir la
   aplicación, consultar nuevamente: no debe asumirse la última selección como
   estado del instrumento. Después de un fallo, esperar y consultar antes de
   reintentar; cancelar no frena el espejo.
6. Para cambiar la trayectoria de una medición ya iniciada, finalizar el barrido
   y crear uno nuevo; volver a medir fondo. Utilizar sesiones separadas para
   comparaciones de calibración entre rutas distintas.

La etapa 3 termina aquí. La etapa 4 requiere autorización explícita del usuario.
