# Calibración espectral del HORIBA iHR320

Aplicación independiente de `CodigoV4`. Mide la respuesta instrumental del
iHR320 barriendo λ, para una o varias aperturas de rendija, sobre una línea
láser estrecha. La tensión DC del fotodiodo entra por **AUX IN** del SR830 y se
lee con **OAUX? 1** (o AUX 2–4); el monocromador se controla mediante el
servicio ICL de EzSpec-SDK, que permite mover λ, rendijas, salida y grating y
leer su estado. Un modo simulado permite probar todo sin equipos.

## Interfaz

La ventana se organiza en:

- **Estado** (franja superior): HORIBA (listo/error, grating, λ, salida y
  rendijas leídas por ICL), SR830 y barrido en curso o último (guardado o no).
- **1 · Equipos**: conexión y estado de los equipos.
  - *Espectrómetro HORIBA iHR320 · ICL*: Control de λ (SIM/ICL), diagnóstico,
    consulta de estado, inicialización, lectura de estado sin mover y desconexión.
  - *Lock-in SR830 · lectura AUX IN*: conexión (interfaz, recurso, baud/paridad,
    timeout de consulta), prueba `*IDN?` + AUX y configuración de lectura:
    N, intervalo mínimo, espera tras mover y canal AUX.
  - *Grating (torreta)*, *Selector óptico de salida* y *Rendijas (ajuste manual)*.
  - *Comunicación ICL, límites y timeouts*: índice, timeouts de consulta,
    movimiento de λ, inicialización, rendijas, salida y torreta; tolerancia,
    límites de λ y puertos de rendija.
- **2 · Barrido**: grilla de λ, aperturas (rango o listas), **notas del barrido**,
  **fondo opcional** con el láser bloqueado, vista previa e inicio. Un barrido
  simple en λ es el caso de una sola apertura.
- **Visualización**: gráfico interactivo de la configuración en curso o de una
  anterior, tabla de puntos y **Guardar datos del barrido…**.

La interfaz no tiene barrido manual ni análisis automático (FWHM): al terminar
sólo se guardan los datos. Los comandos `analyze` y `example` se conservan para
sesiones del formato anterior (ver *Datos y recuperación*). La GUI previa quedó
como referencia en `respaldo_gui_etapa4/` (no se importa ni se prueba).

## Barrido con EzSpec-SDK

En esta PC se encontró EzSpec SDK 2.1.0.19 e ICL API 300. El SDK Python oficial
requiere Python >=3.9. Esta aplicación conserva compatibilidad con Python 3.7
utilizando directamente el protocolo WebSocket JSON documentado por HORIBA
en `EzSpec-python-sdk/docs/icl_api20251029.md`, mediante `websocket-client`.
No necesita importar el controlador de `CodigoV4`.

El lanzador prefiere `spectral_calibration/.venv`, un entorno local que comparte
las bibliotecas científicas existentes de Anaconda y agrega websocket-client
1.6.1, compatible con Python 3.7. Para instalaciones nuevas use un Python actual
y `pip install -r requirements-horiba.txt` en su propio entorno.

1. Abra `Iniciar_calibracion.bat`.
2. Sin equipos: deje **Control de λ = SIM** y conecte el simulador en
   **1 · Equipos → Configurar / conectar…** (interfaz SIM). Configure
   **2 · Barrido** y pulse **Iniciar barrido**.
3. Con hardware, abra ICL y conecte el iHR320 y los dongles necesarios. En
   **1 · Equipos** seleccione **Control de λ = ICL** y conecte un SR830 real.
4. **Diagnosticar ICL (sin mover)** muestra versión y monocromadores USB.
   El índice identifica al equipo en `mono_list`; el predeterminado es 0.
5. **Inicializar espectrómetro**: si ICL informa que ya está inicializado,
   se omite el homing; de lo contrario se envía `mono_init(force=false)` y se
   espera la confirmación y una lectura real de posición. No necesita abrir
   SynerJY; sí necesita ICL activo, controladores y licencia HORIBA.
   **Consultar estado** verifica sin inicializar y **Desconectar HORIBA**
   libera la sesión. El indicador distingue desconectado, inicializando, listo
   y error. El homing puede mover los mecanismos del instrumento.
6. **Leer estado (sin mover)** consulta rendijas, salida, gratings y λ; el
   resultado aparece en la franja **Estado**. En esta unidad se detectaron
   entrada axial 1 y salida lateral 4, ambas de 0–7 mm. No copie esos índices a
   otro equipo sin consultar su configuración; con varios puertos de
   entrada/salida elíjalos en *Comunicación ICL, límites y timeouts*.
7. Si hace falta, cambie grating y λ y la salida Axial/Lateral (secciones
   siguientes). Ajuste los límites de λ al rango utilizable de la red: los
   límites configurables 1–1500 nm son un filtro de software, no certifican ese
   rango. La tolerancia de posición predeterminada es 0.01 nm; ajústela a la
   resolución del equipo y a su paso experimental.
8. En **2 · Barrido** defina la grilla de λ, las aperturas y las notas. **Vista
   previa del plan** no mueve nada; **Iniciar barrido** pide confirmación.
   Para cada configuración de rendijas se ajustan y confirman ambas aperturas;
   para cada λ: `mono_moveToPosition`, espera de `mono_isBusy=false`, lectura de
   posición/grating/rendijas/salida, espera configurada, N lecturas AUX y nueva
   lectura para comprobar que nada cambió. Cada punto se agrega al CSV de
   respaldo antes de dibujarse.
9. Al terminar, pulse **Guardar datos del barrido…** para exportar el CSV y su
   JSON de metadatos (HORIBA, SR830, notas) donde elija.

El paso puede ser negativo. No se sobrepasa el extremo: si el intervalo no es
múltiplo del paso, la última λ es el último punto de la grilla dentro del rango.
Un fallo de comunicación, posición o escritura detiene el barrido; las filas
completas quedan en el CSV de respaldo y su JSON registra el estado `error`.
No hay reintento automático.

**Cancelar operación** impide nuevos puntos y descarta el lote incompleto.
El protocolo utilizado no proporciona aquí un comando de frenado: una orden
de motor ya enviada puede continuar. No vuelva a mover hasta verificar que el
equipo quedó libre. Una respuesta de movimiento puede esperar hasta su timeout
(180 s por defecto); la interfaz sigue respondiendo.

La aplicación mueve λ, rendijas, salida y grating sólo desde **1 · Equipos** o
durante un barrido iniciado por el usuario. El homing sólo se solicita mediante
el botón de inicialización cuando ICL informa que falta; conectar, leer o
iniciar un barrido nunca lo solicitan implícitamente. No se redefine el origen
de λ (`mono_setPosition`).

El timeout de inicialización es independiente del de movimiento (180 s por
defecto) e incluye la espera y las lecturas de confirmación. La apertura y el
descubrimiento usan el timeout de consulta por comando (8 s por defecto).
En reposo con sesión abierta se renueva el estado cada 5 s mediante el worker;
un fallo se detecta al consultar y puede tardar hasta el timeout de comunicación.
**Cancelar operación** también abandona la espera de inicialización, pero
no detiene un homing ya enviado. Tras error, no se reenvía la orden: restaure la
conexión y use **Consultar estado** antes de reintentar. No controle el
instrumento simultáneamente con SynerJY u otro cliente.

El [informe de la etapa 1](ETAPA1_INICIALIZACION.md) incluye evidencias locales,
pruebas con el equipo conectado y el procedimiento de arranque en frío pendiente.
La implementación prescinde de la GUI de SynerJY, pero depende del servidor
propietario ICL; no es un controlador USB independiente del fabricante.

### Control independiente de rendijas — etapa 2

El barrido ajusta las rendijas automáticamente. **1 · Equipos → Rendijas (ajuste
manual)** permite además consultarlas y aplicarlas a mano (alineación,
verificación), con **Control de λ = ICL** y el instrumento inicializado.
Primero consulte y después aplique. No requiere conectar el SR830.

La API utiliza milímetros: `mono_getSlitPositionInMM` / `mono_moveSlitMM`.
El rango se deriva de `mono_getConfig`: tipo 1, 0–2 mm; tipo 2, 0–7 mm.
Se rechazan puertos ambiguos, tipos desconocidos, valores no finitos y destinos
fuera de rango. El tipo 7 mm tiene un paso documentado de 0.00625 mm: se muestran
la solicitud y el valor informado por ICL, aceptando como tolerancia un paso
más 0.000001 mm. No se redondea ni sustituye la solicitud por la lectura.

El timeout de rendijas es **30 s** por defecto para movimientos y verificación;
apertura, detección y consultas previas usan el timeout de consulta por comando.
Se espera disponibilidad y se releen ambas rendijas después de cada ajuste.
Si la otra cambia, la lectura no alcanza el destino o se pierde comunicación,
se informa error, se retira la confirmación actual y no se repite la orden.
Un fallo del segundo movimiento puede dejar aplicado el primero; no se intenta
revertirlo automáticamente. Consulte antes de reintentar.

Los controles manuales de rendijas, salida y grating se deshabilitan mientras
haya una operación en curso, incluido un barrido. Cada barrido registra su
propio estado inicial y final, de modo que cambiar el montaje entre barridos no
altera datos ya guardados. Las aperturas solicitadas y leídas se guardan por
separado en el CSV (`slit_in_mm`/`slit_in_read_mm`, `slit_out_mm`/`slit_out_read_mm`).
El control manual permite cerrar a 0 mm; el barrido exige aperturas positivas.

El [informe de etapa 2](ETAPA2_RENDIJAS.md) incluye causa original, comandos,
pruebas simuladas y ensayos reales con restauración a 1 mm en ambas rendijas.

### Selector óptico de salida — etapa 3

**1 · Equipos → Selector óptico de salida** controla exclusivamente el espejo de
salida del iHR320. Seleccione **Control de λ = ICL**, consulte y elija **Axial**
o **Lateral**. **Aplicar salida** espera disponibilidad y exige que la lectura
posterior coincida exactamente con el destino. Si ya estaba en esa posición,
consulta sin repetir el movimiento. No requiere conectar el SR830.

La API documenta `mono_getMirrorPosition` / `mono_moveMirror` con `locationId=1`
para salida; `0=Axial`, `1=Lateral`. Estos índices no son los puertos de rendija
ni deben copiarse de `configuration.mirrors`: en esta unidad esa configuración
enumera `2`, mientras que los comandos oficiales identifican salida con `1`.

El timeout de salida es **30 s** para movimiento y confirmación, configurable
en **1 · Equipos → Timeout salida [s]**; la apertura/consulta previa usa
el timeout de consulta por comando. No se inicializa ni cambia trayectoria
automáticamente al consultar. Una posición desconocida, falta de inicialización,
error o timeout bloquea la confirmación; no hay reenvío ni reversión automática.
Cancelar abandona la espera y no frena un espejo ya accionado.

Al iniciar la aplicación, la posición aparece **desconocida** hasta consultarla;
no se toma una selección guardada como estado físico. Después de una consulta
válida, las consultas periódicas y snapshots actualizan la lectura. El barrido
ICL consulta la salida antes de empezar, la registra en el JSON
(`horiba.initial_state.output_port`) y en la columna `output_port` de cada fila,
y la verifica antes y después de cada punto: si cambia, el barrido se detiene.

El [informe de etapa 3](ETAPA3_SALIDA.md) documenta pruebas, trazas y límites.
Se verificó con ICL el cambio en ambos sentidos y desde la GUI, restaurando
**Lateral**. La trayectoria física del haz requiere verificación óptica.

### Selección de grating y coherencia de λ — etapa 4

**1 · Equipos → Grating (torreta)** muestra tres posiciones posibles, los datos
recibidos de ICL y la selección actual. El combo permite elegir sólo posiciones
instaladas. En esta unidad la configuración informa **1200, 600 y 300 líneas/mm**
en Grating 1/2/3. Las tres devuelven `blaze=0`: no se inventa una longitud de
blaze. Se muestra el dato recibido sin asignar unidades que la API local no documenta.

**Aplicar grating y λ** requiere equipo inicializado y una **λ final [nm]**
explícita dentro de los límites configurados. Se valida la posición instalada,
se ordena `mono_moveGrating` con índice 0/1/2 para Grating 1/2/3, se espera la
torreta, se consulta su posición y la λ informada tras el giro, y después se
ordena `mono_moveToPosition` para la λ final. No se usa `mono_setPosition` ni se
redefine el origen de calibración. Si ya coinciden red y λ, no repite movimientos.
Con el mismo grating y otra λ final, sólo mueve λ.

El timeout de torreta es **180 s** por defecto, independiente del timeout de
movimiento de λ, también **180 s**. Las consultas y el acuse de torreta usan
el timeout de comunicación por comando (**8 s** por defecto), limitado además
por el presupuesto de torreta. La GUI sigue respondiendo durante estas fases.
La λ final se confirma con **Tolerancia λ [nm]**, predeterminada **0.01 nm**;
ese umbral no certifica precisión óptica ni un rango utilizable para cada red.

Una secuencia interrumpida deja **configuración pendiente**. Consultar no borra
esa condición en la misma instancia: hay que completar explícitamente grating
y λ. Mientras exista ese estado no comienza ningún barrido: se detiene antes de
crear archivos o mover rendijas. No se reenvían ni revierten movimientos
automáticamente. Cancelar abandona la espera, no frena la torreta ni el motor de λ.

Cada barrido registra en su JSON el grating y el catálogo leídos al comenzar
(`horiba.initial_state.grating_info`, `horiba.configuration.gratings`), y la
columna `grating` del CSV la red leída en cada punto. Si la red cambia durante
un punto, el barrido se detiene. No hay coeficientes globales de calibración de
λ que este software aplique automáticamente; compruebe una línea conocida con
cada red.

El [informe de etapa 4](ETAPA4_GRATINGS.md) incluye ensayos, restricciones y un
fallo real documentado: en G2, 532.600525 nm solicitados produjeron una lectura
532.622070 nm, rechazada por la tolerancia de 0.01 nm. La comprobación funcional
con un umbral de ensayo distinto no reemplaza una verificación óptica y no
cambia el valor predeterminado del programa.

### Barrido λ × rendijas — pestaña 2 · Barrido

Recorre una o varias configuraciones de rendijas y, para cada una, la **misma
grilla de λ** (λ mínima, λ máxima y paso, con la regla de extremos descrita
arriba). Las aperturas se definen de dos modos; los campos del modo no elegido
se deshabilitan:

- **Rango (entrada = salida):** apertura mínima, máxima y paso constante; ambas
  rendijas toman el mismo valor. El máximo se incluye sólo si pertenece a la grilla.
- **Listas entrada/salida:** valores separados por `;` (o espacios), emparejados
  en orden. Una lista de un único valor se repite para todas (p. ej. entrada fija
  y salida variable). Se admiten comas decimales. Aperturas entre >0 y 7 mm.

Un barrido simple en λ (el antiguo «HORIBA automático») es el caso de una sola
apertura: mínima = máxima en modo rango, o un único valor en cada lista.

**Vista previa del plan** muestra configuraciones y puntos sin mover nada.
Se usan N, intervalo, espera y canal del SR830 y el control de λ, límites,
tolerancia y timeouts de **1 · Equipos**. Con ICL hacen falta un SR830 real y el
equipo inicializado; con SIM, el lector simulado. Antes de crear archivos se
leen (sin mover) grating, salida, rendijas y λ; si ICL informa un cambio de
grating pendiente, el barrido no comienza. Por cada configuración: `move_slits`
con confirmación por relectura, y por cada λ: `mono_moveToPosition`, adquisición
de N lecturas y nueva lectura para comprobar que no cambiaron λ, rendijas,
grating ni salida. Con SIM, el FWHM simulado sigue
`√(0.08² + (1.8·max(entrada, salida))²)` nm y se restaura al terminar.

No se calcula FWHM en la interfaz. Cancelar o un error conservan las filas
completas; no hay reintento ni reversión, y las rendijas quedan en la última
posición alcanzada: consulte antes de volver a mover.

#### Fondo opcional (láser bloqueado)

Con **Medir fondo al iniciar (láser bloqueado)** marcado, después de confirmar
el barrido se pide bloquear el láser. Se leen N tensiones con la misma
configuración de lectura (espera, intervalo, canal) en el estado actual del
HORIBA, **antes de crear archivos y de cualquier movimiento**. Con ICL se relee
el estado al terminar y, si cambiaron λ, grating, salida o rendijas, el barrido
no comienza. Luego se muestra el fondo y se pide **desbloquear el láser**:
confirmar inicia el barrido; cancelar lo detiene sin mover el equipo ni crear
archivos. En el simulador, el lector se bloquea durante esa medida.

El fondo se guarda completo en la clave `background` del JSON (lecturas, tiempos,
media, σ, N, canal, saturación y estado del HORIBA antes/después); sin la casilla
queda `background: null`. El CSV conserva siempre las tensiones crudas. Es un
único fondo por barrido, medido con las rendijas en su posición actual (no en
cada configuración). **Restar fondo** (Visualización) resta su media sólo en el
gráfico y en la columna «V − fondo» de **Puntos**.

#### Respaldo automático y Guardar datos

Cada punto se agrega y sincroniza a disco en un **CSV de respaldo**,
`calibration_data/slit_sweeps/barrido_rendijas_FECHA_HORA_id.csv`, una fila por
punto, junto con un `.json` del mismo nombre. Las columnas no cambiaron:

| Columna | Contenido |
| --- | --- |
| `point_index`, `config_index` | Orden global y configuración de rendijas. |
| `slit_in_mm`, `slit_out_mm` | Aperturas solicitadas de entrada y salida. |
| `slit_in_read_mm`, `slit_out_read_mm` | Aperturas leídas por ICL (vacías en SIM sin lectura). |
| `wavelength_target_nm`, `wavelength_nm` | λ solicitada y leída. |
| `mean_v`, `std_v`, `n`, `channel` | Media, σ muestral (ddof=1), N y canal AUX. |
| `grating`, `output_port` | Red y salida leídas. |
| `started_utc`, `finished_utc`, `readings_v` | Tiempos del lote y lecturas individuales separadas por `;`. |

**Guardar datos del barrido…** (debajo del gráfico) abre un diálogo para elegir
nombre y carpeta, y escribe `nombre.csv` (copia idéntica del respaldo) y
`nombre.json` (todos los metadatos), con escritura atómica. Las notas vigentes
en ese momento reemplazan a las registradas al iniciar, también en el JSON de
respaldo. Puede guardarse un barrido cancelado o interrumpido: su JSON indica el
estado. Al cerrar la ventana o iniciar otro barrido se avisa si el último no se
guardó; el respaldo permanece igualmente en `slit_sweeps`.

#### Metadatos (JSON)

| Clave | Contenido |
| --- | --- |
| `notes` | Notas del barrido. |
| `background` | Fondo con láser bloqueado (lecturas, tiempos, media, σ, N, canal, estado del HORIBA antes/después) o `null` si no se midió. |
| `plan` | Modo de aperturas, grilla de λ ingresada, rango o listas de aperturas y cantidades. |
| `horiba.control` | `ICL` o `SIM`. |
| `horiba.configuration` | Configuración completa al comenzar: parámetros ICL (índice, límites, tolerancia, puertos y **todos los timeouts**), versión ICL, equipo y serie, inicialización, puertos y rangos de rendijas, catálogo de gratings, salida y últimos ajustes. |
| `horiba.initial_state` | Lectura confirmada antes del primer movimiento: λ, grating y sus datos, rendijas de entrada/salida y salida óptica. |
| `horiba.final_state`, `horiba.final_configuration` | Último estado confirmado y configuración al terminar, también tras error o cancelación. |
| `sr830.connection` | Interfaz, recurso, puerto/placa/dirección, baud, paridad, bits de parada, **timeout**, backend VISA e identidad `*IDN?` (o parámetros del simulador). |
| `sr830.acquisition`, `sr830.query`, `sr830.visa_resource` | N, intervalo, espera y canal AUX; consulta `OAUX? n`; recurso VISA resuelto. |
| `wavelengths_nm`, `slit_pairs_mm`, `configurations` | Grilla, pares de rendijas e informe de cada ajuste de rendijas. |
| `state`, `points_written`, `error` | `running`, `completed`, `cancelled` o `error`; filas escritas; mensaje. |
| `acquisition`, `reader`, `instrument` | Claves anteriores, conservadas por compatibilidad. |
| `saved_utc`, `csv`, `backup_csv` | En la copia exportada: fecha de guardado, nombre del CSV exportado y ruta del respaldo. |

Los metadatos se copian de lecturas ya realizadas; armarlos no envía comandos.

#### Visualización

El gráfico muestra **sólo la configuración en curso**: al empezar una nueva
configuración de rendijas, o un nuevo barrido, el gráfico se limpia y no se
dibuja sobre las curvas anteriores. **Mostrar** permite volver a una
configuración anterior del barrido; **Superponer otras configuraciones**
(desactivado por defecto) las agrega en gris. **Normalizar** usa el máximo de
cada curva y **Barras σ** muestra la dispersión muestral de la curva destacada.
La barra de Matplotlib permite zoom, desplazamiento y guardar la imagen; el zoom
se conserva mientras llegan puntos de la misma configuración y **Autoescalar**
lo restablece. La pestaña **Puntos** lista λ leída, media y σ de la configuración
mostrada. Cada punto se dibuja después de escribirse en el CSV.

### Diagnóstico y configuración local de ICL

Desde la carpeta `Software_calibracióniHR320`, con el entorno configurado:

```powershell
.\spectral_calibration\.venv\Scripts\python.exe -B -m spectral_calibration horiba-diagnose --output spectral_calibration\diagnostico_horiba.json
```

El 3/11/2026 se observó que ICL respondía `icl_info`, pero `mono_discover` y
`mono_list` devolvían `[E];-1`, «no parser found». La configuración activa
en `C:\ProgramData\HORIBA Scientific\EzSpec\ICL\icl_settings.ini` decía
`Model=Duetta`, `Mono=false`. En la carpeta SDK el destino `icl_settings.ini`
era una carpeta que contenía otro archivo del mismo nombre.

`repair_icl.py` permite revisar y corregir específicamente esos dos destinos.
Sin opciones sólo muestra una vista previa. `--apply` conserva respaldos con
fecha, normaliza el destino SDK y habilita el modelo SDK/módulo Mono en la
configuración activa. `--restart` solicita el cierre normal de ICL y lo vuelve
a iniciar. Esta corrección cambia la selección de módulos de Duetta a SDK;
para volver a usar una Duetta debe restaurarse su configuración respaldada.
No modifica archivos de licencia. Las escrituras en ProgramData requieren
permisos del sistema. No debe ejecutarse durante una medición.

```powershell
.\spectral_calibration\.venv\Scripts\python.exe -B -m spectral_calibration.repair_icl
.\spectral_calibration\.venv\Scripts\python.exe -B -m spectral_calibration.repair_icl --apply --restart
```

Que ICL responda o enumere cero equipos no demuestra por sí solo que estén
habilitadas todas las funciones licenciadas. La comprobación definitiva es
conectar el iHR320 y sus dongles, leer el montaje y hacer un barrido real.

Corrección aplicada el 3/11/2026, respaldo `backup_20261103_155509_661387` en
ambas carpetas. Tras reiniciar, `mono_discover` respondió `count=0` sin errores
y `mono_list` respondió `devices=[]`. Es el resultado esperado sin hardware.
El informe local está en `diagnostico_horiba.json`.

Fuentes oficiales: [EzSpec-SDK](https://www.horiba.com/int/scientific/products/detail/action/show/Product/ezspec-sdk-6853/)
y [código Python del fabricante](https://github.com/HORIBAEzSpecSDK/horiba-python-sdk).

## Ejecución en esta computadora

Desde PowerShell:

```powershell
cd C:\Users\LEC\Desktop\Fede\Software_calibracióniHR320
.\spectral_calibration\Iniciar_calibracion.bat
```

El lanzador activa un entorno existente o el entorno local de calibración, si lo
creó. No instala paquetes ni cambia dependencias del programa de pump-probe.

Para ejecutar directamente con el Anaconda disponible:

```powershell
cd C:\Users\LEC\Desktop\Fede\Software_calibracióniHR320
$env:PATH = 'C:\ProgramData\Anaconda3;C:\ProgramData\Anaconda3\Library\bin;C:\ProgramData\Anaconda3\Scripts;' + $env:PATH
& 'C:\ProgramData\Anaconda3\python.exe' -B -m spectral_calibration gui
```

## Instalación independiente en Windows

Para una instalación nueva, use Python 3.10 o posterior de python.org con Tcl/Tk
y pip. El código también funciona con los Python 3.7 de este laboratorio.
Cree un entorno separado; no instale dentro del entorno de pump-probe.
El código se obtiene del repositorio de GitHub (las mediciones no están
versionadas: cada computadora genera las suyas en `calibration_data/`).

```powershell
git clone https://github.com/FedericoAriasCeci/Software_calibracion_iHR320.git
cd Software_calibracion_iHR320
python -m venv .venv_calibration
.\.venv_calibration\Scripts\python.exe -m pip install -r spectral_calibration\requirements.txt
.\.venv_calibration\Scripts\python.exe -m spectral_calibration gui
```

No hace falta activar el entorno. Tkinter se incluye en el instalador de Python;
no es un paquete instalable por pip. El modo SIM no requiere VISA ni hardware.
La aplicación usa NumPy y Matplotlib; SciPy permite detectar máximos por
prominencia y realizar el ajuste opcional frente a apertura. Si falta SciPy, el
FWHM por interpolación sigue funcionando e informa qué evaluación se omitió.

## Conexión del SR830

Fuente: [manual oficial SRS, revisión 2.5](https://www.thinksrs.com/downloads/PDFs/Manuals/SR830m.pdf),
secciones 4-24, 4-27, 5-1/2, 5-9/10 y 5-16.

Conecte la **salida de tensión** del fotodiodo amplificado al BNC posterior
**AUX IN 1**, distinto de la entrada A/B de señal y de AUX OUT. El rango nominal
de AUX es ±10.5 V, impedancia 1 MΩ, resolución aproximada 1/3 mV. El detector
puede saturar a una tensión inferior: revise también su especificación y señal.
No hacen falta chopper, referencia, auto gain ni ajuste de la constante de tiempo
del lock-in para esta lectura DC.

Instale el controlador del adaptador y una implementación VISA compatible
(por ejemplo NI-VISA). PyVISA es la interfaz Python; no sustituye al controlador
VISA/GPIB del fabricante. Puede usar un backend alternativo ya instalado desde
el campo `Backend VISA`; vacío usa el del sistema. `@py` requiere instalar
`pyvisa-py` aparte y verificar la compatibilidad del adaptador. No se instala ni
selecciona automáticamente.

En **1 · Equipos → Lock-in SR830 → Configurar / conectar…**:

- **GPIB:** seleccione GPIB; indique placa (normalmente 0) y dirección elegida
  en Setup del SR830 (por ejemplo 8). Deje recurso vacío para construir
  `GPIB0::8::INSTR`, o escriba el recurso detectado completo.
- **RS232:** use un cable directo para PC DTE ↔ SR830 DCE, con adaptador de
  conectores apropiado; no un null modem. Seleccione COM, por ejemplo COM3
  (`ASRL3::INSTR`). Configure **el mismo baud y paridad en el panel** del SR830 y
  en la aplicación; el valor habitual es 9600, 8 bits, N, 1 stop. El programa
  permite paridad N/E/O, velocidades 300–19200 admitidas y 1/2 bits de parada.
  Usa el modo de tres hilos admitido por el manual, **sin control de flujo**.
  El manual describe CTS/DTR opcionales y pines 2/3/7 en su conector de 25 pines;
  no traslade esos números directamente a un conector de 9 pines.
- Timeout por consulta y apertura: configurable, 3000 ms por defecto. Ante
  timeout se cierra la conexión, se descarta el lote parcial y debe reconectar.
  No hay reintentos automáticos que puedan confundir respuestas tardías.

`Detectar VISA / COM` enumera recursos sin consultar instrumentos. Conectar
abre el recurso, configura el transporte, envía **OUTX 0** para RS232 o
**OUTX 1** para GPIB y consulta `*IDN?`; se exige una identidad SR830.
Las terminaciones de lectura/escritura son **CR (`\r`) en RS232** y
**LF (`\n`) en GPIB**, con EOI normal de VISA en GPIB. Después, `Probar IDN + AUX`
comprueba identidad y toma una lectura del canal elegido en voltios.

El único comando de configuración enviado al SR830 es OUTX. No se envían
reset, FMOD, SENS, OFLT, ajustes de entrada ni auto phase al SR830.
No ejecute dos aplicaciones contra el mismo recurso SR830 simultáneamente.

### Verificación física pendiente

Esta entrega no certifica la comunicación física. Con el hardware presente:

1. Encienda el SR830, conecte su interfaz y configure dirección o baud/paridad.
2. Seleccione el recurso, conecte y confirme que la identidad contiene SR830.
3. Con el fotodiodo conectado a AUX IN 1, pulse `Probar IDN + AUX`. Compare
   orden de magnitud y polaridad con un voltímetro apropiado en la misma señal.
4. Haga un barrido corto de una sola apertura con **Medir fondo al iniciar**:
   bloquee el láser cuando se pida y desbloquéelo antes de los movimientos.
   Verifique fondo, tensión, desviación, N y lecturas guardadas (CSV y JSON).
5. Desconecte el cable de comunicación durante un punto: la GUI debe responder,
   informar error y conservar las filas completas. Reconecte antes de reintentar.
6. Haga un barrido completo y confirme ambos cruces de media altura, sin
   recorte de la señal ni saturación del amplificador.

## Uso de la interfaz

La organización está descrita en *Interfaz*. Orden de trabajo habitual:

1. **1 · Equipos:** conecte el SR830 (o el simulador) y fije N, intervalo,
   espera y canal. N debe ser ≥2. El intervalo es la pausa mínima desde el final
   de una consulta al inicio de la siguiente; la latencia del instrumento se
   suma y queda registrada en los tiempos. La espera se aplica después de cada
   movimiento de λ, antes de leer. La tensión se actualiza durante cada lote,
   sin consultas continuas adicionales.
2. **1 · Equipos:** con ICL, consulte o inicialice el HORIBA, lea su estado y
   ajuste grating/λ y salida si hace falta. Revise límites, tolerancia y timeouts.
3. **2 · Barrido:** grilla de λ, aperturas y notas (por ejemplo láser, filtros,
   detector, alineación). **Vista previa del plan** e **Iniciar barrido**.
4. **Visualización:** siga la curva en curso o elija configuraciones anteriores
   en **Mostrar**. La franja **Estado** indica el progreso.
5. **Guardar datos del barrido…** al terminar (o tras cancelar, si interesa).

Durante una operación se deshabilitan los botones de equipos y barrido;
**Cancelar operación** abandona la espera. La rueda del mouse desplaza los
paneles sin cambiar la selección de los combos.

Para probar sin equipos, seleccione **SIM** en la conexión del SR830 y **Control
de λ = SIM**. Puede configurar centro, FWHM verdadero, amplitud, fondo, ruido σ
y semilla; la forma es triangular y en el barrido su FWHM depende de la apertura.
No simula drift ni autocorrelación del detector real.

El fondo con láser bloqueado es opcional y forma parte del inicio del barrido
(ver *Fondo opcional*); su resta es sólo visual.

## Datos y recuperación

Los barridos de la interfaz se guardan como se describe en *Barrido λ ×
rendijas*: CSV de respaldo y JSON en `calibration_data/slit_sweeps/`, escritos
durante el barrido, y la copia elegida con **Guardar datos del barrido…**.
Tras un cierre inesperado, las filas completas y el JSON (con estado `running`)
permanecen en el respaldo; ábralos directamente. Todas las tensiones están en V,
las longitudes en nm, las aperturas en mm y las fechas en UTC con zona explícita.

### Sesiones del formato anterior

Las versiones previas de la GUI (barrido manual y «HORIBA automático») guardaban
sesiones en `calibration_data/session_FECHA_HORA_id/`:

```text
session.json                         # snapshot canónico completo: muestras, historial y análisis
metadata.json                        # montaje, conexión y resultados por barrido
analysis_summary.csv                 # una fila por barrido, incluidas advertencias
 grating_G1_slit_0p1mm_id.csv          # puntos, media, σ, corregida, canal y estado
 grating_G1_slit_0p1mm_id_readings.csv # una fila por lectura con tiempos
 grating_G1_slit_0p1mm_id_backgrounds.csv
plots/                               # PNG de curvas y FWHM(w), nombres únicos
synthetic_truth.json                  # sólo el ejemplo: anchos verdaderos y recuperados
```

`session.json` es la fuente de verdad; los CSV son derivados y se regeneran.
`active=False` identifica muestras excluidas o sustituidas y `replaces_id`
enlaza las repeticiones. `wavelength_horiba_nm` es la λ declarada en modo manual
y la posición informada por ICL en automático; `Point.instrument` guarda las
comprobaciones antes/después y `Scan.horiba` la identificación del equipo.

La GUI actual no abre esas sesiones. Para reanalizarlas y exportar resumen y PNG:

```powershell
python -m spectral_calibration analyze "calibration_data\session_FECHA_HORA_id"
```

Un bloqueo del sistema operativo impide abrir la misma sesión desde dos
instancias y se libera al terminar el proceso, incluso tras una interrupción.

## Análisis y alcance de la incertidumbre

La interfaz actual no calcula FWHM. Esta sección describe el análisis de los
comandos `analyze` y `example`, aplicable a sesiones del formato anterior.

El método principal agrupa longitudes idénticas con igual peso por visita,
resta el fondo elegido, identifica el máximo y busca los cruces más próximos con
la mitad de su altura a ambos lados. Interpola linealmente en cada intervalo y
resta las posiciones; no extrapola. Sin ambos cruces informa FWHM no determinable.
La posición del máximo corresponde a la muestra más alta, sin precisión adicional
por ajuste. La asimetría es `(semiancho_derecho - semiancho_izquierdo)/FWHM`.
El perfil debe presentar un máximo positivo sobre el fondo.

`std_v` usa ddof=1. No se interpreta σ/√N como error total de la media.
La estimación disponible de u(FWHM) combina:

- 400 realizaciones Monte Carlo con semilla fija 830: cada punto se perturba
  con su σ muestral completa (sin dividir por √N), y el fondo se perturba una
  sola vez por realización, común a todos los puntos, con su σ muestral.
  En λ repetidas se incorpora también la dispersión entre visitas, sin asumir
  que promediar elimina drift. Se requieren ≥80% de realizaciones válidas.
- Un término heurístico de muestreo `sqrt(paso_izq² + paso_der²)/sqrt(12)`, que
  representa cruces uniformemente desconocidos dentro de sus intervalos. No es
  una cota rigurosa del error por interpolación ni una calibración de λ.

Se guardan por separado `mc_std_nm`, intervalo percentil MC 95%, término de
muestreo y su suma cuadrática `fwhm_uncertainty_nm`. Es una estimación condicional,
no incertidumbre metrológica completa; no incluye drift entre puntos,
autocorrelación, incertidumbre de λ/rendija, estabilidad del fondo ni ancho del
láser. El intervalo MC tampoco incluye el término de muestreo. Un ancho con muchos
decimales no agrega información al paso experimental.

Se advierte por pocos puntos, paso grueso (<5 intervalos por FWHM), alas truncadas,
saturación AUX, fondo grande, baja señal y múltiples picos prominentes. La
saturación del fotodiodo a valores inferiores no puede inferirse automáticamente.
El modelo opcional `sqrt(delta0²+(a*w)²)` es una aproximación fenomenológica,
ajustada sin ponderación; no predice una resolución universal ni valida
extrapolaciones a rendijas más estrechas. No se implementan ajustes gaussianos o
triangulares secundarios en esta primera versión. El ancho medido incluye la
convolución con el láser y sólo aproxima Δλ instrumental si éste es suficientemente
estrecho. Es necesaria otra fuente estrecha para caracterizar otras λ; un láser
verde no calibra por sí solo todo el rango espectral del monocromador.

## Pruebas y ejemplo ejecutable

En un entorno con las dependencias instaladas, desde la raíz:

```powershell
python -B -m unittest discover -s spectral_calibration\tests -v
$env:CALIBRATION_GUI_TESTS = '1'
python -B -m unittest spectral_calibration.tests.test_gui -v
python -B -m spectral_calibration example --output calibration_data
```

Las pruebas GUI requieren un escritorio Windows con Tcl/Tk; crean ventanas ocultas
y no conectan instrumentos. El primer comando omite esas pruebas salvo que esté
activa la variable. Se comprueban protocolo serial/GPIB con recursos falsos,
timeouts y liberación de recursos, cancelación, entradas inválidas, repeticiones,
cruce ausente, anchos conocidos triangular y gaussiano, muestreo grueso,
saturación, picos múltiples, diferentes aperturas y ajuste empírico, exportación,
guardado fallido, recuperación después de `os._exit(0)` y reproducibilidad exacta
al recargar las mismas muestras en el mismo entorno de análisis. Las pruebas de
la GUI cubren equipos (inicialización, rendijas, salida, gratings con dobles ICL),
barridos SIM e ICL, metadatos, fondo opcional (antes de todo movimiento, con
relectura y cancelación), gráfico limpio por configuración, zoom, cancelación,
fallos de escritura y **Guardar datos**.

El ejemplo genera tres aperturas con anchos verdaderos conocidos según
`delta0=0.08 nm`, `a=1.8 nm/mm`, ruido 0.001 V y 20 lecturas por punto. Guarda
53 puntos por curva, las muestras, un fondo por curva, PNG y la verdad sintética.
No requiere abrir una ventana. Consulte `VALIDACION.md` para resultados de esta
entrega y limitaciones del entorno usado.

## Arquitectura

- `models.py`: lotes, configuración de lectura y validaciones compartidas
  (también montaje y puntos de las sesiones anteriores).
- `instruments.py`: lector SR830 y simulador, ambos con `read_voltage(channel)`;
  ese contrato admite un futuro lector de `OUTP? 3`, no implementado ahora.
- `acquisition.py`: lotes cancelables y un trabajador con cola; Tk sólo se actualiza
  en su hilo principal.
- `horiba.py`: transporte ICL, inicialización, estado, rendijas, selector de salida, torreta,
  verificación de posición/montaje y simulación.
- `slit_sweep.py`: planes de aperturas, barrido rendijas × λ y CSV de respaldo.
- `sweep_export.py`: metadatos del barrido (notas, plan, HORIBA, SR830), estado
  final y exportación CSV + JSON de **Guardar datos**; sin Tk ni comandos.
- `gui.py`: franja de estado, pestañas **1 · Equipos** y **2 · Barrido**,
  visualización por configuración y guardado; toda E/S en el worker.
- `storage.py`, `analysis.py` y `plotting.py`: sesiones del formato anterior,
  análisis FWHM y figuras, usados por los comandos `analyze` y `example`.
- `example.py` y `tests/`: demostración y validaciones ejecutables.
