# Etapa 2 — Rendijas motorizadas del HORIBA iHR320

Fecha: 4 de noviembre de 2026. Alcance: control independiente de entrada y
salida. No se implementaron selección del espejo de salida ni cambio de grating.

## Causa original

El campo **Rendijas iguales [mm]** sólo construía los metadatos del experimento.
Cambiarlo no enviaba ningún comando de movimiento: el controlador únicamente
tenía `mono_getSlitPositionInMM`, sin `mono_moveSlitMM`. Los puertos por defecto
`0/0` desactivaban también la lectura de aperturas en el snapshot. Por eso una
edición aparentemente válida de la GUI no ajustaba el instrumento.

Había otras dos restricciones: `experiment()` copiaba una apertura común a
entrada y salida; importar el montaje rechazaba aperturas distintas. El modelo
de datos y los CSV ya admitían las dos por separado, por lo que se reutilizaron.
Además, la lectura de rendijas rechazaba 0 mm por usar validación estrictamente
positiva; ahora el control y sus lecturas aceptan el cierre documentado a cero.

## Evidencia técnica y equipo detectado

Se reutilizó ICL API 300, versión `2.1.0.19.50f46f97`, sin instalar paquetes,
modificar controladores ni cambiar configuración del sistema. La interfaz COM
antigua sigue sin utilizarse en esta aplicación.

Fuente de comandos y ubicaciones:
`..\EzSpec-python-sdk\docs\icl_api20251029.md`, secciones `mono_getConfig`,
`mono_getSlitPositionInMM` y `mono_moveSlitMM`. También se revisaron los métodos
correspondientes del SDK Python oficial instalado.

| Comando | Datos utilizados |
| --- | --- |
| `mono_getConfig` | `configuration.ports`: `locationId` y `slitType` de rendijas instaladas. La respuesta puede contener un objeto o JSON como texto. |
| `mono_getSlitPositionInMM` | `index`, `locationId`; resultado `position` en **milímetros**. |
| `mono_moveSlitMM` | `index`, `locationId`, `position` en **milímetros**. No se utiliza `mono_moveSlit`, que trabaja en pasos. |
| `mono_isBusy`, `mono_isInitialized` | Disponibilidad y estado antes de mover, espera de finalización y comprobación posterior. |

La API documenta 1=entrada axial, 2=entrada lateral, 3=salida axial,
4=salida lateral; `slitType=1` corresponde a 2 mm y `slitType=2` a 7 mm.
No se asumió que el equipo tendría unos puertos predeterminados: se consultó.

La unidad **2756D-1626-IHR320**, índice **0**, devolvió:

| Función | Puerto instalado | Tipo | Apertura inicial leída |
| --- | --- | --- | --- |
| Entrada axial | 1 | 2: 7 mm | 1.000 mm |
| Salida lateral | 4 | 2: 7 mm | 1.000 mm |

El [diagnóstico original](etapa2_diagnostico.json) conserva la respuesta completa
de configuración. No se enviaron órdenes al espejo ni a la torreta; identificar
un puerto lateral de rendija no selecciona la trayectoria óptica.

HORIBA especifica rangos **0–2 mm** y **0–7 mm**; su nota técnica da pasos de
2.25 µm y 6.25 µm respectivamente. Se utiliza el límite del tipo instalado,
con máximo absoluto de 7 mm. Un tipo desconocido bloquea movimiento.
Fuentes oficiales:

- [Especificaciones iHR 2026](https://static.horiba.com/fileadmin/Horiba/Products/Scientific/Optical_Components_and_OEM/iHR_Series/iHR_Series_Brochure_2026.pdf).
- [Nota técnica iHR: rangos y pasos de rendijas](https://www.horiba.com/fileadmin/Horiba/Products/Scientific/Optical_Components_and_OEM/iHR_Series/What_Makes_the_iHR_Series_Imaging_Spectrometers_Better.pdf).

## Implementación y compatibilidad

En `ICLMonochromator` se añadieron `read_slits()` y
`move_slits(entrance_mm=None, exit_mm=None, cancel=None)`. La detección exige
un puerto inequívoco de cada función o una selección explícita válida en
HORIBA automático. Las ubicaciones deben estar instaladas y tener tipo conocido.

La apertura y lectura no hacen homing implícito: un instrumento ocupado o sin
inicializar se rechaza. El controlador conserva configuración de puertos,
últimas aperturas leídas y el informe del último ajuste, diferenciando
`requested_mm`, `before_mm`, `actual_mm` y `confirmed_moves`. La GUI lee ese
estado; los valores pedidos no se consideran el estado físico del equipo.

Para aplicar ambas aperturas se validan ambos destinos antes del primer envío.
Los movimientos son secuenciales. Después de cada orden se espera disponibilidad,
se comprueba inicialización y se leen ambas aperturas. Se exige coincidencia
del destino dentro de **un paso documentado + 0.000001 mm** para admitir
cuantización y representación flotante; la solicitud nunca se sustituye por
el valor leído. La otra rendija debe conservar su lectura dentro de 0.000001 mm.
Estos umbrales son criterios de confirmación del software, no especificaciones
de exactitud metrológica del instrumento.

El timeout de ajustes es **30 s** por defecto, configurable como
**Timeout rendijas [s]**. Cubre los movimientos y sus confirmaciones; la apertura,
configuración y lectura previa usan el timeout de consulta por comando,
**8 s** por defecto. Las respuestas inválidas, no finitas o fuera de rango
no producen confirmación. Inicialización y ajustes de rendijas comparten
exclusión mutua; la GUI también utiliza el worker único existente.

Tras un fallo después de enviar movimiento se descarta el transporte y se
retira la lectura actual. No hay reintento ni rollback automático. Si falló
el segundo ajuste, el primero puede haberse completado: el informe y log
conservan qué se había confirmado y las lecturas obtenidas. Cancelar abandona
la espera, no detiene físicamente un motor ya accionado. Hay que consultar
antes de reintentar.

La nueva pestaña **Rendijas** presenta solicitudes independientes, lectura actual,
puerto, rango y botones para consultar/aplicar una o ambas. Los controles de
aplicación se habilitan después de una consulta válida con ICL y se bloquean
mientras exista un barrido seleccionado, incluso con fondo ya adquirido.
Así se conserva la interpretación de los datos anteriores. Se debe iniciar
un nuevo barrido para ajustar y luego importar el montaje real.

El montaje admite valores distintos; recuperación de sesiones, metadatos y CSV
siguen usando `slit_in_mm` / `slit_out_mm` existentes. Las leyendas de perfiles
ahora indican ambas aperturas. El cálculo individual de FWHM no se cambió y
la gráfica FWHM frente a apertura común sigue excluyendo rendijas desiguales,
tal como lo hacía `slit_series()`.

Se conserva la validación existente de experimentos: adquirir un barrido
espectral requiere aperturas declaradas positivas. Puede cerrar una rendija
a cero mediante el control, pero no registrar un nuevo barrido espectral con
esa apertura declarada. El cambio de aperturas no sobrescribe automáticamente
los metadatos: use **Leer montaje HORIBA (sin mover)** antes de medir.

## Archivos de esta etapa

- `horiba.py`: configuración, límites, lectura, movimiento y confirmación.
- `gui.py`: controles independientes, lectura/importación de montaje y estados.
- `plotting.py`: leyenda de perfiles con entrada y salida; sin cambios de cálculo.
- `tests/test_horiba.py`: ampliación del doble ICL para rendijas.
- `tests/test_slits.py` (nuevo): pruebas específicas del controlador.
- `tests/test_gui.py`: pruebas de controles, persistencia y adquisición desigual.
- `README.md`, `VALIDACION.md` y este informe: uso y validación.
- Registros nuevos: `etapa2_diagnostico.json`, `etapa2_rendijas_hardware.json`,
  `etapa2_gui_hardware.json`. Se conservan los registros de la etapa 1.

No se cambiaron `models.py`, `storage.py`, `analysis.py`, `acquisition.py`,
dependencias ni el lanzador.

## Pruebas y resultados

Antes de modificar: **82 pruebas aprobadas**. Resultado final:
**114 pruebas aprobadas en 44.530 s, sin omisiones**: 33 de núcleo, 32 de GUI,
27 de controlador/configuración ICL y 22 específicas de rendijas.

Las pruebas con dobles cubren entrada/salida independientes, ambas secuenciales,
límites 0/7 y límite 2 mm, unidades mm, coma decimal, tipo desconocido,
puertos ausentes/duplicados/ambiguos, selección explícita, equipo ocupado o
sin inicializar, acuse sin movimiento, cambio inesperado de la otra rendija,
timeout, fallo parcial, acuse perdido, reconexión, cancelación, cuantización,
respuesta inválida, exclusión mutua y reapertura. Tk verifica valores pedidos
separados de lecturas, botones, importación y persistencia de montaje desigual,
verificación durante adquisición, protección del fondo y respuesta a cancelación.

Comando desde `spectral_calibration`:

```powershell
$env:PATH = 'C:\ProgramData\Anaconda3;C:\ProgramData\Anaconda3\Library\bin;C:\ProgramData\Anaconda3\Scripts;' + $env:PATH
$env:PYTHONPATH = (Get-Item ..).FullName
$env:CALIBRATION_GUI_TESTS = '1'
.\.venv\Scripts\python.exe -B -m unittest discover -s tests
```

La suite conserva las advertencias preexistentes de NumPy/SciPy y PyVISA/`imp`
del entorno antiguo; terminó con `OK`. No se actualizaron paquetes.

### Ensayos reales

| Ensayo | Resultado de ICL |
| --- | --- |
| Entrada 1.000 → 1.025 mm | Lectura 1.024999976158142 mm; salida permaneció en 1.000 mm. |
| Restaurar entrada | Lectura 1.000 mm; salida permaneció en 1.000 mm. |
| Salida 1.000 → 1.025 mm | Lectura 1.024999976158142 mm; entrada permaneció en 1.000 mm. |
| Restaurar salida y reabrir comunicaciones | Ambas lecturas 1.000 mm, antes y después de reabrir. |
| GUI real en otro proceso, ventana oculta | Aplicó secuencialmente 1.025 / 1.050 mm; leyó 1.024999976158142 / 1.0499999523162842 mm e importó ambos valores al montaje sin exigir igualdad. |
| Restauración desde GUI | Ambas lecturas volvieron a 1.000 mm; desconexión normal. |

La [traza del controlador](etapa2_rendijas_hardware.json) contiene comandos,
parámetros, respuestas, fechas UTC e informes. El [registro GUI](etapa2_gui_hardware.json)
contiene solicitudes, lecturas, textos de estado e importación del montaje.
Los ensayos reales no utilizaron respuestas simuladas.

La confirmación disponible es **el valor informado por ICL**, junto con su
estado de disponibilidad. No se certifica que exista un encoder independiente
ni que esa lectura sea una medición óptica de apertura. No se probaron los
extremos físicos 0/7 mm, interrupción USB durante ajuste, recuperación tras
apagar/encender el instrumento o exactitud óptica. Los fallos y límites extremos
se cubrieron con mocks y documentación, no con ensayos físicos completos.

## Uso y verificación desde la GUI

1. Cerrar otros clientes de control, mantener ICL activo y abrir la aplicación
   actualizada. Elegir **HORIBA automático → Control de λ = ICL**, inicializar
   si hace falta y confirmar la serie. El SR830 no es necesario para ajustar.
2. En **Rendijas**, pulsar **Consultar rendijas (sin mover)**. Revisar entrada,
   salida, puertos, rango y lecturas. En esta unidad aparecen 1 y 4, de 7 mm;
   no se deben copiar esos índices a otra instalación sin consultarla.
3. Sin un barrido seleccionado, indicar la apertura de entrada y pulsar
   **Aplicar entrada**. Comprobar su lectura posterior y que la salida se
   mantenga. Repetir independientemente para salida. **Aplicar ambas** realiza
   dos ajustes sucesivos y verifica después de cada uno.
4. En una prueba controlada puede usar 1.000 / 1.025 mm y restaurar después
   los valores iniciales. No dé por hecho que son los valores adecuados para
   otro experimento. La solicitud y el valor leído se muestran por separado.
5. Pulsar **Leer montaje HORIBA (sin mover)**. Revisar **Entrada declarada** y
   **Salida declarada** en A · Montaje antes de crear un barrido. Se admiten
   aperturas distintas y se conservan por separado en los archivos.
6. Para cambiar rendijas después de medir, finalizar el barrido y pulsar
   **Iniciar nuevo barrido**. No reutilizar el fondo adquirido con otra apertura.
7. Ante error/cancelación, esperar y consultar aperturas antes de volver a
   aplicar. Un cambio parcial no se revierte automáticamente. No utilice
   SynerJY u otro programa para mover el equipo al mismo tiempo.

La etapa 2 termina aquí. La etapa 3 requiere autorización explícita del usuario.
