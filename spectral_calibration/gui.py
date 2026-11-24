"""Tkinter: equipos (HORIBA por ICL y SR830), barrido λ × rendijas y visualización.

Toda operación de hardware o de disco se ejecuta en el worker; Tk sólo se toca
en el hilo principal. La lógica de barrido (`slit_sweep.run_slit_sweep`) y la de
comunicación (`horiba.py`, `instruments.py`) se usan sin cambios.
"""
import copy
from dataclasses import asdict
import itertools
import json
from pathlib import Path
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure

from .acquisition import AcquisitionCancelled, Worker, acquire
from .horiba import (GRATING_POSITIONS, OUTPUT_POSITIONS, HoribaError, HoribaSettings,
                     ICLMonochromator, wavelength_plan)
from .instruments import (CommunicationError, ConnectionSettings, SR830Reader, SimulatedReader,
                          discover_resources)
from .models import AcquisitionSettings, number
from .slit_sweep import (SimulatedSlitInstrument, new_sweep_path, run_slit_sweep,
                         slit_plan_from_lists, slit_plan_from_range)
from .sweep_export import background_metadata, horiba_metadata, record_final_state, save_sweep, sr830_metadata

DEFAULT_ROOT = Path(__file__).resolve().parent.parent / "calibration_data"
SWEEP_MODES = ["Rango (entrada = salida)", "Listas entrada/salida"]
FOLLOW_CURRENT = "Configuración en curso"
STATE_NAMES = dict(disconnected="Desconectado", initializing="Inicializando", ready="Listo", error="Error")
WRAP = 330
MUTED = "#555555"


class ScrollablePanel(ttk.Frame):
    def __init__(self, parent, width=380):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, highlightthickness=0, width=width)
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.content = ttk.Frame(self.canvas, padding=4)
        self.content.columnconfigure(0, weight=1)
        window = self.canvas.create_window((0, 0), window=self.content, anchor="nw")
        self.content.bind("<Configure>", lambda event: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda event: self.canvas.itemconfigure(window, width=event.width))
        # La rueda del mouse se resuelve en CalibrationApp._on_wheel buscando este atributo.
        self.canvas.scroll_panel = self.content.scroll_panel = self

    def scroll(self, delta):
        if self.canvas.yview() != (0.0, 1.0):
            self.canvas.yview_scroll(-1 if delta > 0 else 1, "units")


class CalibrationApp:
    def __init__(self, root, data_root=DEFAULT_ROOT):
        self.root, self.data_root = root, Path(data_root)
        root.title("HORIBA iHR320 · Barrido espectral")
        root.geometry("{}x{}".format(min(1280, root.winfo_screenwidth()-60), min(860, root.winfo_screenheight()-80)))
        root.minsize(1050, 700)
        self.worker = Worker()
        self.reader = None
        self.identity = ""
        self.connected = self.closing = self.awaiting = False
        self.conn = ConnectionSettings()
        self.horiba_settings = HoribaSettings()
        self.horiba_controller = None
        self.horiba_check_at = 0
        self.sweep = self.previous_sweep = None
        self.sweep_status = ""
        self.discovery_callback = None
        self._serial = itertools.count(1)
        self._table_key = None
        self.sim = dict(center_nm=532.0, fwhm_nm=0.4, amplitude_v=1.0, background_v=0.03, noise_v=0.002, seed=830)
        self.vars, self.buttons = {}, []
        self._build()
        root.bind_all("<MouseWheel>", self._on_wheel, add="+")
        # La rueda no debe cambiar selecciones (p. ej. Control de λ) al desplazar paneles.
        root.unbind_class("TCombobox", "<MouseWheel>")
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._poll_id = root.after(50, self.poll)
        self.refresh_controls()
        self.redraw(reset=True)

    # ------------------------------------------------------------------ widgets
    def var(self, name, value):
        self.vars[name] = tk.StringVar(value=str(value))
        return self.vars[name]

    def field(self, parent, row, label, name, default, values=None):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=5, pady=3)
        v = self.var(name, default)
        entry = (ttk.Combobox(parent, textvariable=v, values=values, width=21, state="readonly") if values
                 else ttk.Entry(parent, textvariable=v, width=24))
        entry.grid(row=row, column=1, sticky="ew", padx=5, pady=3)
        return entry

    def note(self, parent, row, text, muted=False):
        label = ttk.Label(parent, text=text, wraplength=WRAP, justify="left", foreground=MUTED if muted else "")
        label.grid(row=row, column=0, columnspan=2, sticky="w", padx=5, pady=3)
        return label

    def section(self, parent, title):
        frame = ttk.LabelFrame(parent, text=title, padding=4)
        frame.grid(row=parent.grid_size()[1], column=0, sticky="ew", pady=4)
        frame.columnconfigure(1, weight=1)
        return frame

    def button_grid(self, parent, row, items, columns=2):
        frame = ttk.Frame(parent)
        frame.grid(row=row, column=0, columnspan=2, sticky="ew", pady=3)
        for column in range(columns):
            frame.columnconfigure(column, weight=1, uniform="buttons")
        created = []
        for index, (text, command) in enumerate(items):
            b = ttk.Button(frame, text=text, command=lambda c=command: self.safe(c))
            b.grid(row=index // columns, column=index % columns, sticky="ew", padx=2, pady=2)
            self.buttons.append(b)
            created.append(b)
        return created

    def safe(self, function):
        try:
            function()
        except Exception as exc:
            messagebox.showerror("Operación no realizada", str(exc), parent=self.root)

    def _on_wheel(self, event):
        try:
            widget = self.root.winfo_containing(event.x_root, event.y_root)
        except (KeyError, tk.TclError):
            return
        while widget is not None:
            if isinstance(widget, tk.Text):
                return
            panel = getattr(widget, "scroll_panel", None)
            if panel is not None:
                panel.scroll(event.delta)
                return
            widget = getattr(widget, "master", None)

    def _build(self):
        self._build_summary()
        body = ttk.Panedwindow(self.root, orient="horizontal")
        body.pack(fill="both", expand=True, padx=6, pady=4)
        left = ttk.Frame(body, width=400)
        right = ttk.LabelFrame(body, text="Visualización", padding=5)
        body.add(left, weight=0)
        body.add(right, weight=1)
        config = ttk.Notebook(left)
        config.pack(fill="both", expand=True)
        equipment, sweep = ScrollablePanel(config), ScrollablePanel(config)
        config.add(equipment, text="1 · Equipos")
        config.add(sweep, text="2 · Barrido")
        self._build_equipment(equipment.content)
        self._build_sweep(sweep.content)
        self._build_view(right)
        foot = ttk.Frame(self.root, padding=5)
        foot.pack(fill="x")
        self.status = ttk.Label(foot, text="Listo. Modo predeterminado: simulación.")
        self.status.pack(side="left", fill="x", expand=True)
        self.cancel_button = ttk.Button(foot, text="Cancelar operación", command=self.cancel_acquisition)
        self.cancel_button.pack(side="right")

    def _build_summary(self):
        bar = ttk.LabelFrame(self.root, text="Estado", padding=(6, 2))
        bar.pack(fill="x", padx=6, pady=(6, 0))
        self.summary_labels = {}
        for index, key in enumerate(("horiba", "sr830", "sweep")):
            if index:
                ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=8)
            label = ttk.Label(bar, text="—")
            label.pack(side="left")
            self.summary_labels[key] = label

    def _build_equipment(self, parent):
        hb = self.section(parent, "Espectrómetro HORIBA iHR320 · ICL")
        self.field(hb, 0, "Control de λ", "horiba_mode", "SIM", ["SIM", "ICL"])
        self.horiba_status_label = self.note(hb, 1, "Desconectado · Sin sesión con el instrumento.")
        self.button_grid(hb, 2, [("Diagnosticar ICL (sin mover)", self.diagnose_horiba),
                                 ("Consultar estado", self.check_horiba),
                                 ("Inicializar espectrómetro", self.initialize_horiba),
                                 ("Leer estado (sin mover)", self.read_horiba_mount),
                                 ("Desconectar HORIBA", self.disconnect_horiba)])
        self.note(hb, 3, "SIM simula λ y rendijas con el lector SIM. ICL requiere icl.exe activo, el iHR320 "
                  "y su licencia. Inicializar envía homing sólo si ICL informa que falta.", muted=True)

        sr = self.section(parent, "Lock-in SR830 · lectura AUX IN")
        self.connection_label = self.note(sr, 0, "Desconectado")
        self.button_grid(sr, 1, [("Configurar / conectar…", self.connection_dialog),
                                 ("Probar IDN + AUX", self.test_connection),
                                 ("Desconectar SR830", self.disconnect)])
        self.voltage_label = ttk.Label(sr, text="Tensión actual: — V", font=("Segoe UI", 12))
        self.voltage_label.grid(row=2, column=0, columnspan=2, sticky="w", padx=5, pady=4)
        for row, (label, key, value) in enumerate([("Lecturas por punto N", "n", 20), ("Intervalo mínimo [s]", "interval_s", .05),
                                                   ("Espera tras mover [s]", "wait_s", .3)], 3):
            self.field(sr, row, label, key, value)
        self.field(sr, 6, "Canal AUX IN", "channel", 1, [1, 2, 3, 4])
        self.note(sr, 7, "Conexión: interfaz, recurso, baud/paridad y timeout de consulta en Configurar / conectar. "
                  "σ: dispersión muestral (ddof=1), no incertidumbre total.", muted=True)

        gr = self.section(parent, "Grating (torreta)")
        self.grating_table = ttk.Treeview(gr, columns=("position", "density", "blaze"), show="headings", height=3, selectmode="none")
        for key, label, width in (("position", "Posición", 80), ("density", "Líneas/mm", 85), ("blaze", "Blaze informado", 135)):
            self.grating_table.heading(key, text=label)
            self.grating_table.column(key, width=width, stretch=False)
        self.grating_table.grid(row=0, column=0, columnspan=2, sticky="w", padx=5, pady=4)
        for n in GRATING_POSITIONS:
            self.grating_table.insert("", "end", iid=str(n), values=("Grating {}".format(n), "sin consulta", "sin consulta"))
        self.grating_selector = self.field(gr, 1, "Grating solicitado", "grating_target", "", ["Grating {}".format(n) for n in GRATING_POSITIONS])
        self.field(gr, 2, "λ final [nm]", "grating_target_nm", "")
        self.grating_actual_label = self.note(gr, 3, "Grating leído: desconocido.")
        buttons = self.button_grid(gr, 4, [("Consultar gratings (sin mover)", self.read_horiba_gratings),
                                           ("Aplicar grating y λ", self.apply_horiba_grating)])
        self.grating_apply_button = buttons[1]
        self.grating_move_label = self.note(gr, 5, "Consulte los gratings instalados antes de aplicar.")
        self.note(gr, 6, "Tras girar la torreta se ordena explícitamente la λ final (con el mismo grating sólo se ajusta λ). "
                  "Blaze=0: ICL no informa una λ de blaze utilizable. Cancelar abandona la espera; el movimiento puede continuar.", muted=True)

        out = self.section(parent, "Selector óptico de salida")
        self.output_selector = self.field(out, 0, "Salida solicitada", "output_target", "", list(OUTPUT_POSITIONS))
        self.output_actual_label = self.note(out, 1, "Salida leída: desconocida · consulte el equipo.")
        buttons = self.button_grid(out, 2, [("Consultar salida (sin mover)", self.read_horiba_output),
                                            ("Aplicar salida", self.apply_horiba_output)])
        self.output_apply_button = buttons[1]
        self.output_move_label = self.note(out, 3, "Seleccione Axial o Lateral después de consultar.")

        sl = self.section(parent, "Rendijas (ajuste manual)")
        self.slit_actual_labels = {}
        for row, key, title in ((0, "slit_in_mm", "Entrada"), (2, "slit_out_mm", "Salida")):
            self.field(sl, row, title+" solicitada [mm]", "target_"+key, .1)
            self.slit_actual_labels[key] = self.note(sl, row+1, title+": sin lectura confirmada")
        self.slit_apply_buttons = self.button_grid(sl, 4, [
            ("Aplicar entrada", lambda: self.apply_horiba_slits("entrance")),
            ("Aplicar salida", lambda: self.apply_horiba_slits("exit")),
            ("Aplicar ambas", lambda: self.apply_horiba_slits("both")),
            ("Consultar rendijas (sin mover)", self.read_horiba_slits)])[:3]
        self.slit_move_label = self.note(sl, 5, "Las solicitudes no son lecturas del equipo.")
        self.note(sl, 6, "El barrido ajusta las rendijas automáticamente; este control sirve para alinear o verificar.", muted=True)

        pr = self.section(parent, "Comunicación ICL, límites y timeouts")
        for row, (key, label) in enumerate([
                ("index", "Índice ICL"), ("timeout_s", "Timeout consulta [s]"),
                ("move_timeout_s", "Timeout movimiento λ [s]"), ("initialize_timeout_s", "Timeout inicialización [s]"),
                ("slit_timeout_s", "Timeout rendijas [s]"), ("output_timeout_s", "Timeout salida [s]"),
                ("grating_timeout_s", "Timeout torreta [s]"), ("tolerance_nm", "Tolerancia λ [nm]"),
                ("minimum_nm", "Límite inferior λ [nm]"), ("maximum_nm", "Límite superior λ [nm]"),
                ("slit_in", "Puerto entrada (0=auto)"), ("slit_out", "Puerto salida (0=auto)")]):
            self.field(pr, row, label, "horiba_"+key, getattr(self.horiba_settings, key))
        self.note(pr, row+1, "Los límites de λ son un filtro de software: ajústelos a la red instalada. "
                  "Cambiar un parámetro reabre la sesión ICL en la próxima operación.", muted=True)

    def _build_sweep(self, parent):
        wl = self.section(parent, "Longitudes de onda")
        self.field(wl, 0, "λ mínima [nm]", "sweep_start", 531.4)
        self.field(wl, 1, "λ máxima [nm]", "sweep_end", 532.6)
        self.field(wl, 2, "Paso λ [nm]", "sweep_step", .05)

        sl = self.section(parent, "Aperturas de rendijas")
        self.field(sl, 0, "Modo de aperturas", "sweep_mode", SWEEP_MODES[0], SWEEP_MODES)
        self.range_entries = [self.field(sl, 1, "Apertura mínima [mm]", "sweep_slit_min", .1),
                              self.field(sl, 2, "Apertura máxima [mm]", "sweep_slit_max", .5),
                              self.field(sl, 3, "Paso apertura [mm]", "sweep_slit_step", .1)]
        self.list_entries = [self.field(sl, 4, "Lista entrada [mm]", "sweep_list_in", "0.1; 0.2; 0.5"),
                             self.field(sl, 5, "Lista salida [mm]", "sweep_list_out", "0.1; 0.2; 0.5")]
        self.note(sl, 6, "Rango: entrada = salida, de la mínima a la máxima con paso constante. "
                  "Listas: pares en orden (separe con ';'); una lista de un único valor se repite para todas. "
                  "Barrido simple en λ: una sola apertura (mínima = máxima, o un valor por lista).", muted=True)
        self.vars["sweep_mode"].trace_add("write", lambda *args: self.update_mode_fields())

        nt = self.section(parent, "Notas del barrido")
        self.notes = tk.Text(nt, width=36, height=5, wrap="word", font="TkDefaultFont")
        self.notes.grid(row=0, column=0, columnspan=2, sticky="ew", padx=5, pady=4)
        self.note(nt, 1, "Se guardan en los metadatos al iniciar y, con su texto vigente, al pulsar Guardar datos.", muted=True)

        bg = self.section(parent, "Fondo (opcional)")
        self.measure_background = tk.BooleanVar(value=False)
        ttk.Checkbutton(bg, text="Medir fondo al iniciar (láser bloqueado)", variable=self.measure_background).grid(
            row=0, column=0, columnspan=2, sticky="w", padx=5, pady=3)
        self.background_label = self.note(bg, 1, "Sin fondo en el barrido mostrado.")
        self.note(bg, 2, "Al iniciar se pide bloquear el láser y se leen N tensiones en el estado actual, sin mover el "
                  "equipo (con ICL se verifica por relectura que nada cambió). Luego se pide desbloquearlo antes del "
                  "primer movimiento. El fondo se guarda en los metadatos (clave background); el CSV conserva las "
                  "tensiones crudas.", muted=True)

        ex = self.section(parent, "Ejecución")
        self.button_grid(ex, 0, [("Vista previa del plan", self.preview_slit_sweep),
                                 ("Iniciar barrido", self.start_slit_sweep)])
        self.sweep_label = self.note(ex, 1, "Sin barridos en esta ventana.")
        self.note(ex, 2, "Usa N, intervalo, espera y canal del SR830, y el control de λ, límites, tolerancia y "
                  "timeouts de 1 · Equipos. Por cada configuración ajusta las rendijas y las confirma por lectura; "
                  "por cada λ mueve, espera, lee N tensiones y verifica que λ, rendijas, grating y salida no cambiaron. "
                  "Cada punto se agrega a un CSV de respaldo en calibration_data/slit_sweeps. Al terminar, las rendijas "
                  "quedan en la última configuración. Cancelar conserva las filas completas; un movimiento ya enviado "
                  "puede continuar.", muted=True)

    def _build_view(self, right):
        top = ttk.Frame(right)
        top.pack(fill="x")
        ttk.Label(top, text="Mostrar").pack(side="left")
        self.view_choice = tk.StringVar(value=FOLLOW_CURRENT)
        self.view_selector = ttk.Combobox(top, textvariable=self.view_choice, values=[FOLLOW_CURRENT], state="readonly", width=38)
        self.view_selector.pack(side="left", padx=4)
        self.view_selector.bind("<<ComboboxSelected>>", lambda event: self.safe(lambda: self.redraw(reset=True)))
        self.overlay = tk.BooleanVar(value=False)
        self.normalize = tk.BooleanVar(value=False)
        self.errors = tk.BooleanVar(value=True)
        for text, var, reset in (("Superponer otras configuraciones", self.overlay, False),
                                 ("Normalizar", self.normalize, True), ("Barras σ", self.errors, False)):
            ttk.Checkbutton(top, text=text, variable=var,
                            command=lambda r=reset: self.safe(lambda: self.redraw(reset=r))).pack(side="left", padx=3)
        self.subtract = tk.BooleanVar(value=True)
        self.subtract_check = ttk.Checkbutton(top, text="Restar fondo", variable=self.subtract,
                                              command=lambda: self.safe(lambda: self.redraw(reset=True)))
        self.subtract_check.pack(side="left", padx=3)
        ttk.Button(top, text="Autoescalar", command=lambda: self.safe(lambda: self.redraw(reset=True))).pack(side="left", padx=3)
        plots = ttk.Notebook(right)
        plots.pack(fill="both", expand=True, pady=4)
        self.profile_ax, self.profile_canvas, self.toolbar = self.make_plot(plots, "Respuesta espectral")
        table_frame = ttk.Frame(plots)
        plots.add(table_frame, text="Puntos")
        self.table = ttk.Treeview(table_frame, columns=("index", "nm", "mean", "std", "corr"), show="headings")
        for column, title in (("index", "#"), ("nm", "λ leída [nm]"), ("mean", "V media [V]"), ("std", "σ [V]"),
                              ("corr", "V − fondo [V]")):
            self.table.heading(column, text=title)
            self.table.column(column, width=60 if column == "index" else 120, stretch=column != "index")
        table_scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=table_scroll.set)
        table_scroll.pack(side="right", fill="y")
        self.table.pack(fill="both", expand=True)
        bottom = ttk.Frame(right)
        bottom.pack(fill="x")
        self.save_button = ttk.Button(bottom, text="Guardar datos del barrido…", command=lambda: self.safe(self.save_sweep_data))
        self.save_button.pack(side="right", padx=4)
        self.sweep_info = ttk.Label(bottom, text="", wraplength=640, justify="left")
        self.sweep_info.pack(side="left", fill="x", expand=True)

    def make_plot(self, parent, title):
        frame = ttk.Frame(parent)
        parent.add(frame, text=title)
        fig = Figure(figsize=(6, 4), dpi=90, tight_layout=True)
        canvas = FigureCanvasTkAgg(fig, master=frame)
        toolbar = NavigationToolbar2Tk(canvas, frame)
        toolbar.update()
        canvas.get_tk_widget().pack(fill="both", expand=True)
        return fig.add_subplot(111), canvas, toolbar

    # --------------------------------------------------------------- utilidades
    def require_idle(self):
        if self.awaiting:
            raise ValueError("Espere a que termine la operación actual.")

    def settings(self):
        return AcquisitionSettings(**{k: self.vars[k].get() for k in ("n", "interval_s", "wait_s", "channel")}).validate()

    def notes_text(self):
        return self.notes.get("1.0", "end-1c")

    def submit(self, operation, function):
        self.require_idle()
        self.worker.submit(operation, function)
        self.awaiting = True
        self.status.configure(text="En curso: "+operation)
        self.refresh_controls()

    def update_mode_fields(self):
        ranged = self.vars["sweep_mode"].get() == SWEEP_MODES[0]
        for entry in self.range_entries:
            entry.configure(state="normal" if ranged else "disabled")
        for entry in self.list_entries:
            entry.configure(state="disabled" if ranged else "normal")

    # -------------------------------------------------------------------- SR830
    def connection_dialog(self):
        self.require_idle()
        dialog = tk.Toplevel(self.root)
        dialog.title("SR830 / simulación · conexión")
        dialog.transient(self.root)
        controls, sim_controls = ttk.Frame(dialog, padding=8), ttk.Frame(dialog, padding=8)
        controls.grid(row=0, column=0, sticky="n")
        sim_controls.grid(row=0, column=1, sticky="n")
        variables, entries = {}, {}
        labels = [("interface", "Interfaz", ["SIM", "RS232", "GPIB"]), ("resource", "Recurso VISA (vacío: construir)", None),
                  ("com_port", "Puerto COM", None), ("gpib_board", "Placa GPIB", None), ("gpib_address", "Dirección GPIB", None),
                  ("baud_rate", "Baud", [300, 600, 1200, 2400, 4800, 9600, 19200]), ("parity", "Paridad", ["N", "E", "O"]),
                  ("stop_bits", "Bits de parada", [1, 2]), ("timeout_ms", "Timeout [ms]", None),
                  ("visa_library", "Backend VISA (vacío: sistema)", None)]
        for row, (key, label, choices) in enumerate(labels):
            ttk.Label(controls, text=label).grid(row=row, column=0, sticky="w", pady=3)
            var = tk.StringVar(value=str(getattr(self.conn, key)))
            variables[key] = var
            entry = ttk.Combobox(controls, textvariable=var, values=choices or [], width=28)
            if choices:
                entry.configure(state="readonly")
            entry.grid(row=row, column=1, padx=4)
            entries[key] = entry
        sv = {}
        ttk.Label(sim_controls, text="Simulador triangular: FWHM conocido").grid(row=0, column=0, columnspan=2)
        for row, (key, label) in enumerate([("center_nm", "Centro [nm]"), ("fwhm_nm", "FWHM [nm]"), ("amplitude_v", "Altura [V]"),
                                            ("background_v", "Fondo [V]"), ("noise_v", "Ruido σ [V]"), ("seed", "Semilla")], 1):
            ttk.Label(sim_controls, text=label).grid(row=row, column=0, sticky="w", pady=3)
            sv[key] = tk.StringVar(value=str(self.sim[key]))
            ttk.Entry(sim_controls, textvariable=sv[key], width=16).grid(row=row, column=1)
        ttk.Label(sim_controls, text="RS232: 8 bits, sin control de flujo (3 hilos).\nIguale baud/paridad en el panel del SR830.\n"
                  "COM→ASRL; use cable directo DTE–DCE.\nEl centro simulado nunca mueve el HORIBA.",
                  wraplength=300).grid(row=8, column=0, columnspan=2, pady=12, sticky="w")
        found = ttk.Label(dialog, text="Detectar recursos no envía comandos a instrumentos.", wraplength=700)
        found.grid(row=1, column=0, columnspan=2, padx=8, pady=5, sticky="w")

        def detected(result):
            if dialog.winfo_exists():
                resources, errors = result
                entries["resource"].configure(values=[r for r in resources if not r.startswith("COM")])
                entries["com_port"].configure(values=[r for r in resources if r.startswith("COM")])
                found.configure(text="Disponibles: "+(", ".join(resources) or "ninguno")+("\n"+"; ".join(errors) if errors else ""))

        def discover():
            self.discovery_callback = detected
            library = variables["visa_library"].get()
            self.submit("discover", lambda: discover_resources(library))

        def connect():
            self.require_idle()
            settings = ConnectionSettings(**{k: v.get() for k, v in variables.items()}).validate()
            sim = {k: v.get() for k, v in sv.items()}
            reader = SimulatedReader(**sim) if settings.interface == "SIM" else SR830Reader(settings)
            old_reader = self.reader
            self.conn, self.sim = settings, sim

            def job():
                if old_reader:
                    old_reader.close()
                return reader, reader.connect()
            self.connected = False
            self.submit("connect", job)
            dialog.destroy()
        buttons = ttk.Frame(dialog, padding=8)
        buttons.grid(row=2, column=0, columnspan=2, sticky="ew")
        ttk.Button(buttons, text="Detectar VISA / COM", command=lambda: self.safe(discover)).pack(side="left", padx=4)
        ttk.Button(buttons, text="Conectar y probar *IDN?", command=lambda: self.safe(connect)).pack(side="right", padx=4)

    def test_connection(self):
        if not self.connected or self.reader is None:
            raise ValueError("Primero conecte el SR830 (o el simulador).")
        settings, reader = self.settings(), self.reader
        self.submit("test", lambda: (reader.test(), reader.read_voltage(settings.channel)))

    def disconnect(self):
        self.require_idle()
        if self.reader:
            self.submit("disconnect", self.reader.close)

    def connection_summary(self):
        c = self.conn
        if c.interface == "SIM":
            where = "Simulador · FWHM {} nm, centro {} nm".format(self.sim["fwhm_nm"], self.sim["center_nm"])
        else:
            try:
                where = c.resource_name()
            except ValueError:
                where = c.interface
            where += " · timeout {} ms".format(c.timeout_ms)
            if c.interface == "RS232":
                where += " · {} baud, paridad {}, {} stop".format(c.baud_rate, c.parity, c.stop_bits)
        state = ("Conectado: " + self.identity) if self.connected else "Desconectado"
        return state + "\n" + where

    # ------------------------------------------------------------------- HORIBA
    def get_horiba_settings(self):
        self.horiba_settings = HoribaSettings(**{key: self.vars["horiba_"+key].get()
            for key in asdict(self.horiba_settings) if key != "url"}).validate()
        return self.horiba_settings

    def require_icl(self, action):
        if self.vars["horiba_mode"].get() != "ICL":
            raise ValueError("Seleccione Control de λ = ICL para {}.".format(action))

    def diagnose_horiba(self):
        self.require_idle()
        controller = ICLMonochromator(self.get_horiba_settings())

        def job():
            try:
                return controller.diagnose()
            finally:
                controller.close()
        self.submit("horiba_diagnose", job)

    def horiba_action(self, function, settings=None):
        """Reutiliza la sesión; los cierres y consultas se ejecutan en el worker."""
        settings = settings or self.get_horiba_settings()
        previous = self.horiba_controller
        controller = previous if previous and previous.settings == settings else ICLMonochromator(settings)
        if previous and previous is not controller and (previous.settings.url, previous.settings.index) == (settings.url, settings.index):
            # Cambiar un parámetro de software no confirma una secuencia física.
            controller.grating_change_pending = previous.grating_change_pending
            controller.last_grating_move = copy.deepcopy(previous.last_grating_move)
        self.horiba_controller = controller
        self.horiba_check_at = time.monotonic() + 5

        def job():
            if previous and previous is not controller:
                previous.close()
            return function(controller)
        return controller, job

    def initialize_horiba(self):
        self.require_idle()
        self.require_icl("inicializar el espectrómetro real")
        controller, job = self.horiba_action(lambda mono: mono.initialize(self.worker.cancel))
        self.submit("horiba_initialize", job)

    def check_horiba(self):
        self.require_idle()
        controller, job = self.horiba_action(lambda mono: mono.connect())
        self.submit("horiba_status", job)

    def disconnect_horiba(self):
        self.require_idle()
        if self.horiba_controller:
            self.submit("horiba_close", self.horiba_controller.close)

    def read_horiba_gratings(self):
        self.require_idle()
        self.require_icl("consultar la torreta real")
        controller, job = self.horiba_action(lambda mono: mono.read_gratings())
        self.submit("horiba_gratings", job)

    def apply_horiba_grating(self):
        self.require_idle()
        self.require_icl("mover la torreta real")
        choices = {"Grating {}".format(n): n for n in GRATING_POSITIONS}
        position = choices.get(self.vars["grating_target"].get())
        if position is None:
            raise ValueError("Seleccione Grating 1, 2 o 3 entre los instalados.")
        target_nm = number(self.vars["grating_target_nm"].get(), "λ final [nm]", 0, True)
        settings = self.get_horiba_settings()
        if not settings.minimum_nm <= target_nm <= settings.maximum_nm:
            raise ValueError("λ final fuera de los límites configurados para HORIBA.")
        controller, job = self.horiba_action(lambda mono: mono.move_grating(position, target_nm, self.worker.cancel), settings)
        self.submit("horiba_grating_move", job)

    def read_horiba_output(self):
        self.require_idle()
        self.require_icl("consultar la salida real")
        controller, job = self.horiba_action(lambda mono: mono.read_output())
        self.submit("horiba_output", job)

    def apply_horiba_output(self):
        self.require_idle()
        self.require_icl("cambiar la salida real")
        target = self.vars["output_target"].get()
        if target not in OUTPUT_POSITIONS:
            raise ValueError("Seleccione Axial o Lateral antes de aplicar.")
        controller, job = self.horiba_action(lambda mono: mono.move_output(target, self.worker.cancel))
        self.submit("horiba_output_move", job)

    def read_horiba_slits(self):
        self.require_idle()
        self.require_icl("consultar las rendijas reales")
        controller, job = self.horiba_action(lambda mono: mono.read_slits())
        self.submit("horiba_slits", job)

    def apply_horiba_slits(self, part):
        self.require_idle()
        self.require_icl("ajustar las rendijas reales")
        targets = {}
        for key, field, enabled in (("entrance_mm", "slit_in_mm", part in ("entrance", "both")),
                                    ("exit_mm", "slit_out_mm", part in ("exit", "both"))):
            if enabled:
                value = number(self.vars["target_"+field].get(), "Apertura solicitada [mm]", 0)
                if value > 7:
                    raise ValueError("Apertura máxima: 7 mm.")
                targets[key] = value
        controller, job = self.horiba_action(lambda mono: mono.move_slits(cancel=self.worker.cancel, **targets))
        self.submit("horiba_slits_move", job)

    def read_horiba_mount(self):
        """Rendijas, salida, gratings y λ: sólo consultas, sin homing ni movimientos."""
        self.require_idle()
        self.require_icl("leer el estado real")

        def read(mono):
            mono.read_slits()
            mono.read_output()
            mono.read_gratings()
            return mono.snapshot()
        controller, job = self.horiba_action(read)
        self.submit("horiba_mount", job)

    # ------------------------------------------------------------------ barrido
    def slit_sweep_plan(self):
        wavelengths = wavelength_plan(*(self.vars[key].get() for key in ("sweep_start", "sweep_end", "sweep_step")))
        if self.vars["sweep_mode"].get() == SWEEP_MODES[0]:
            pairs = slit_plan_from_range(*(self.vars[key].get() for key in ("sweep_slit_min", "sweep_slit_max", "sweep_slit_step")))
        else:
            pairs = slit_plan_from_lists(self.vars["sweep_list_in"].get(), self.vars["sweep_list_out"].get())
        return wavelengths, pairs

    def sweep_plan_metadata(self, wavelengths, pairs):
        mode = self.vars["sweep_mode"].get()
        plan = dict(slit_mode=mode, configurations=len(pairs), wavelengths=len(wavelengths),
                    points=len(pairs)*len(wavelengths))
        for key, name in (("sweep_start", "wavelength_start_nm"), ("sweep_end", "wavelength_end_nm"), ("sweep_step", "wavelength_step_nm")):
            plan[name] = number(self.vars[key].get(), name)
        if mode == SWEEP_MODES[0]:
            plan["slit_range_mm"] = {name: number(self.vars[key].get(), name) for key, name in
                                     (("sweep_slit_min", "minimum"), ("sweep_slit_max", "maximum"), ("sweep_slit_step", "step"))}
        else:
            plan["slit_lists_mm"] = dict(entrance=self.vars["sweep_list_in"].get(), exit=self.vars["sweep_list_out"].get())
        return plan

    def preview_slit_sweep(self):
        wavelengths, pairs = self.slit_sweep_plan()
        text = "{} configuraciones × {} λ = {} puntos.\nλ: {} → {} nm.\nAperturas (entrada/salida) [mm]: {}".format(
            len(pairs), len(wavelengths), len(pairs)*len(wavelengths), wavelengths[0], wavelengths[-1],
            ", ".join("{:g}/{:g}".format(*p) for p in pairs))
        self.sweep_label.configure(text=text)

    def sim_grating(self):
        target = self.vars["grating_target"].get()
        return "G" + target.split()[-1] if target.startswith("Grating ") else "G1"

    def unsaved_sweep(self):
        sweep = self.sweep
        return bool(sweep and sweep["state"] != "running" and not sweep.get("saved") and Path(sweep["csv"]).exists())

    def start_slit_sweep(self):
        self.require_idle()
        if not self.connected or self.reader is None:
            raise ValueError("Primero conecte el SR830 (o el simulador) en 1 · Equipos.")
        settings = self.settings()
        wavelengths, pairs = self.slit_sweep_plan()
        plan = self.sweep_plan_metadata(wavelengths, pairs)
        mode = self.vars["horiba_mode"].get()
        reader = self.reader
        if mode == "SIM":
            if not isinstance(reader, SimulatedReader):
                raise ValueError("El control SIM de λ requiere el lector SIM.")
        else:
            if isinstance(reader, SimulatedReader):
                raise ValueError("Para mover el HORIBA real conecte un SR830 real.")
            hs = self.get_horiba_settings()
            if min(wavelengths) < hs.minimum_nm or max(wavelengths) > hs.maximum_nm:
                raise ValueError("El barrido excede los límites de λ configurados.")
        total = len(pairs)*len(wavelengths)
        question = "Se moverán rendijas y λ: {} configuraciones × {} λ = {} puntos.\n¿Iniciar?".format(len(pairs), len(wavelengths), total)
        if self.unsaved_sweep():
            question += "\n\nEl barrido anterior no se guardó con «Guardar datos»; su respaldo queda en:\n" + str(self.sweep["csv"])
        if not messagebox.askyesno("Iniciar barrido", question, parent=self.root):
            return
        measure_bg = self.measure_background.get()
        if measure_bg and not messagebox.askokcancel(
                "Medir fondo", "Bloquee el láser y confirme.\nSe leerán {} tensiones en el estado actual, sin mover "
                "el equipo; después se pedirá desbloquearlo.".format(settings.n), parent=self.root):
            return
        path = new_sweep_path(self.data_root / "slit_sweeps")
        notes, sr830 = self.notes_text(), sr830_metadata(reader, settings)
        cancel, progress = self.worker.cancel, self.worker.progress
        unblock = threading.Event()

        def status(text):
            self.worker.events.put(("status", "slit_sweep", text))

        def on_point(point):
            self.worker.events.put(("sweep_point", "slit_sweep", point))

        def metadata(control, instrument, initial, background):
            return dict(notes=notes, plan=plan, horiba=horiba_metadata(control, instrument, initial), sr830=sr830,
                        background=background)

        def measure(state, read_after=None):
            """Fondo antes de crear archivos y de cualquier movimiento; espera el desbloqueo del láser."""
            status("Fondo: {} lecturas con el láser bloqueado…".format(settings.n))
            simulated = isinstance(reader, SimulatedReader)
            if simulated:
                reader.blocked = True
            try:
                batch = acquire(reader, settings, cancel, progress)
            finally:
                if simulated:
                    reader.blocked = False
            background = background_metadata(batch, state, read_after() if read_after else None)
            self.worker.events.put(("background", "slit_sweep", background))
            status("Fondo medido. Esperando confirmación de láser desbloqueado…")
            while not unblock.wait(.1):
                if cancel.is_set():
                    break
            if cancel.is_set():
                raise AcquisitionCancelled("Barrido cancelado antes de desbloquear el láser; no se movió el equipo.")
            return background
        if mode == "SIM":
            instrument = SimulatedSlitInstrument(reader, grating=self.sim_grating())

            def job():
                try:
                    initial = instrument.snapshot()
                    background = measure(initial) if measure_bg else None
                    return run_slit_sweep(instrument, reader, settings, wavelengths, pairs, path, cancel, progress,
                                          status, on_point=on_point, metadata=metadata("SIM", instrument, initial, background))
                finally:
                    record_final_state(path, instrument.snapshot(), instrument.describe())
                    instrument.close()
        else:
            def run(mono):
                # Sólo lecturas: completan el estado inicial (grating, salida, rendijas, λ) para los metadatos.
                mono.read_gratings()
                mono.read_output()
                mono.read_slits()
                initial = mono.snapshot()
                if mono.grating_change_pending:
                    # Antes de crear archivos o mover rendijas: la configuración espectral es incierta.
                    raise HoribaError("Configuración espectral pendiente: aplique grating y λ en 1 · Equipos antes de medir.")

                def read_after():
                    # Confirmación por relectura: el fondo pertenece al estado leído antes de medirlo.
                    after = mono.snapshot()
                    if abs(after["wavelength_nm"]-initial["wavelength_nm"]) > mono.settings.tolerance_nm:
                        raise HoribaError("λ cambió durante la medida de fondo; barrido no iniciado.")
                    for key in ("grating", "output_port", "slit_in_mm", "slit_out_mm"):
                        if key in initial and key in after:
                            changed = (abs(after[key]-initial[key]) > 1e-6 if key.startswith("slit")
                                       else after[key] != initial[key])
                            if changed:
                                raise HoribaError("{} cambió durante la medida de fondo; barrido no iniciado.".format(key))
                    return after
                background = measure(initial, read_after) if measure_bg else None
                try:
                    return run_slit_sweep(mono, reader, settings, wavelengths, pairs, path, cancel, progress, status,
                                          tolerance_nm=mono.settings.tolerance_nm, on_point=on_point,
                                          metadata=metadata("ICL", mono, initial, background))
                finally:
                    record_final_state(path, mono.last_snapshot, mono.describe())
            controller, job = self.horiba_action(run, hs)
        self.previous_sweep = self.sweep
        self.sweep = dict(serial=next(self._serial), csv=path, json=path.with_suffix(".json"), curves={}, labels={},
                          current=None, state="running", total=total, configurations=len(pairs), written=0,
                          n=settings.n, control=mode, saved=None, background=None, unblock=unblock)
        self.sweep_status = ""
        self.view_choice.set(FOLLOW_CURRENT)
        self.update_view_choices()
        self.redraw(reset=True)
        self.sweep_label.configure(text="En curso. Respaldo automático: " + str(path))
        self.submit("slit_sweep", job)

    def background_measured(self, value):
        """El worker espera aquí la confirmación de láser desbloqueado (o la cancelación)."""
        sweep = self.sweep
        if sweep is None:
            return
        sweep["background"] = value
        self.redraw()
        text = "Fondo medido: {:.7g} V · σ {:.3g} V · {} lecturas.".format(value["mean_v"], value["std_v"], value["n"])
        if value["saturated"]:
            text += "\nAdvertencia: lectura AUX saturada."
        self.sweep_label.configure(text=text + " Esperando desbloqueo del láser.")
        if self.closing:
            self.worker.cancel.set()
            return
        if messagebox.askokcancel("Desbloquear láser", text + "\n\nDesbloquee el láser y confirme para iniciar el barrido.\n"
                                  "Cancelar detiene el barrido sin mover el equipo.", parent=self.root):
            sweep["unblock"].set()
        else:
            self.worker.cancel.set()

    def add_sweep_point(self, value):
        sweep = self.sweep
        if sweep is None:
            return
        index = value["config_index"]
        new_config = index not in sweep["curves"]
        curve = sweep["curves"].setdefault(index, dict(slit_in_mm=value["slit_in_mm"], slit_out_mm=value["slit_out_mm"], nm=[], v=[], std=[]))
        curve["nm"].append(value["wavelength_nm"])
        curve["v"].append(value["mean_v"])
        curve["std"].append(value["std_v"])
        sweep["current"] = index
        sweep["written"] += 1
        following = self.view_choice.get() == FOLLOW_CURRENT
        if new_config:
            sweep["labels"][index] = "{} · entrada {:g} / salida {:g} mm".format(index+1, curve["slit_in_mm"], curve["slit_out_mm"])
            self.update_view_choices()
        # Cada configuración nueva empieza un gráfico limpio (sin las anteriores ni su zoom).
        self.redraw(reset=new_config and following)

    def sweep_failed(self, value):
        sweep = self.sweep
        if sweep is None:
            return
        if not Path(sweep["csv"]).exists():
            # No llegó a crear archivos: se vuelve a mostrar el barrido anterior.
            self.sweep = self.previous_sweep
            self.sweep_label.configure(text="El barrido no comenzó: " + str(value))
            self.view_choice.set(FOLLOW_CURRENT)
            self.update_view_choices()
            self.redraw(reset=True)
            return
        sweep["state"] = "cancelled" if isinstance(value, AcquisitionCancelled) else "error"
        self.sweep_label.configure(text="Barrido {}: {} puntos completos conservados en el respaldo. Consulte el estado "
            "del equipo antes de reintentar. {}".format("cancelado" if sweep["state"] == "cancelled" else "interrumpido",
                                                      sweep["written"], value))
        self.redraw()

    def save_sweep_data(self):
        self.require_idle()
        sweep = self.sweep
        if not sweep or sweep["state"] == "running" or not Path(sweep["csv"]).exists():
            raise ValueError("No hay datos de barrido para guardar.")
        target = filedialog.asksaveasfilename(parent=self.root, title="Guardar datos del barrido", defaultextension=".csv",
                                              filetypes=[("CSV", "*.csv")], initialdir=str(self.data_root),
                                              initialfile=Path(sweep["csv"]).name)
        if not target:
            return
        source, notes = sweep["csv"], self.notes_text()
        self.submit("save_sweep", lambda: save_sweep(source, target, notes))

    def cancel_acquisition(self):
        self.worker.cancel.set()

    # ------------------------------------------------------------ visualización
    def update_view_choices(self):
        labels = self.sweep["labels"] if self.sweep else {}
        values = [FOLLOW_CURRENT] + [labels[k] for k in sorted(labels)]
        self.view_selector.configure(values=values)
        if self.view_choice.get() not in values:
            self.view_choice.set(FOLLOW_CURRENT)

    def view_focus(self):
        sweep = self.sweep
        if not sweep or not sweep["curves"]:
            return None
        choice = self.view_choice.get()
        for index, label in sweep["labels"].items():
            if label == choice:
                return index
        return sweep["current"]

    def redraw(self, reset=False):
        ax = self.profile_ax
        # Tras zoom o desplazamiento con la barra, Matplotlib desactiva la autoescala: se respetan esos límites.
        keep = not reset and not (ax.get_autoscalex_on() and ax.get_autoscaley_on())
        limits = ax.get_xlim(), ax.get_ylim()
        ax.clear()
        sweep, focus = self.sweep, self.view_focus()
        normalize = self.normalize.get()
        offset = self.background_v()
        shift = offset if offset is not None and self.subtract.get() else 0.
        if focus is not None:
            others = [k for k in sorted(sweep["curves"]) if k != focus] if self.overlay.get() else []
            for index in others + [focus]:
                curve = sweep["curves"][index]
                # Resta sólo en la vista: el CSV y los metadatos conservan tensiones crudas.
                values, errors = [v-shift for v in curve["v"]], curve["std"]
                if normalize and values and max(values) > 0:
                    peak = max(values)
                    values, errors = [v/peak for v in values], [e/peak for e in errors]
                if index != focus:
                    ax.plot(curve["nm"], values, color="0.7", lw=1, marker=".", ms=2, zorder=1)
                    continue
                style = dict(color="C0", lw=1.6, marker="o", ms=3, zorder=3,
                             label="Entrada {:g} mm / salida {:g} mm".format(curve["slit_in_mm"], curve["slit_out_mm"]))
                if self.errors.get():
                    ax.errorbar(curve["nm"], values, yerr=errors, capsize=2, **style)
                else:
                    ax.plot(curve["nm"], values, **style)
            title = "Configuración {}/{}".format(focus+1, sweep["configurations"])
            if sweep["state"] == "running" and focus == sweep["current"]:
                title += " · en curso"
            ax.set_title(title)
            ax.legend(loc="best", fontsize=8)
        else:
            ax.set_title("Sin datos de barrido")
        ax.set_xlabel("λ [nm]")
        ax.set_ylabel("Señal normalizada" if normalize else ("Tensión AUX − fondo [V]" if shift else "Tensión AUX [V]"))
        ax.grid(True, alpha=.3)
        if keep and focus is not None:
            ax.set_xlim(limits[0])
            ax.set_ylim(limits[1])
        if reset:
            self.toolbar.update()  # Reinicia la pila de vistas (Inicio) del gráfico nuevo.
        self.profile_canvas.draw_idle()
        self.refresh_table(focus)
        self.refresh_sweep_info()

    def background_v(self):
        background = self.sweep.get("background") if self.sweep else None
        return background["mean_v"] if background else None

    def refresh_table(self, focus):
        curve = self.sweep["curves"].get(focus) if self.sweep and focus is not None else None
        rows = len(curve["nm"]) if curve else 0
        offset = self.background_v()
        key = (self.sweep["serial"], focus, offset) if self.sweep else None
        shown = self.table.get_children()
        if key != self._table_key or len(shown) > rows:
            self.table.delete(*shown)
            self._table_key, shown = key, ()
        for i in range(len(shown), rows):
            corrected = format(curve["v"][i]-offset, ".7g") if offset is not None else "—"
            self.table.insert("", "end", values=(i+1, format(curve["nm"][i], ".9g"), format(curve["v"][i], ".7g"),
                                                 format(curve["std"][i], ".3g"), corrected))

    def refresh_sweep_info(self):
        sweep = self.sweep
        if not sweep:
            text = "Sin barrido. Configure 1 · Equipos y 2 · Barrido, y pulse Iniciar barrido."
        else:
            text = "Respaldo automático: {}".format(sweep["csv"])
            if sweep.get("saved"):
                text += "\nGuardado en: {}".format(sweep["saved"])
            elif sweep["state"] != "running":
                text += "\nSin guardar: pulse «Guardar datos del barrido…» para exportar CSV + metadatos (HORIBA, SR830, notas)."
        background = self.sweep.get("background") if self.sweep else None
        if background:
            label = "Fondo del barrido mostrado: {:.7g} V · σ {:.3g} V · {} lecturas{}.".format(
                background["mean_v"], background["std_v"], background["n"], " · SATURADO" if background["saturated"] else "")
        else:
            label = "Sin fondo en el barrido mostrado."
        if self.background_label.cget("text") != label:
            self.background_label.configure(text=label)
        self.subtract_check.configure(state="normal" if background else "disabled")
        if self.sweep_info.cget("text") != text:
            self.sweep_info.configure(text=text)

    # ------------------------------------------------------------------- estado
    def refresh_status(self):
        controller = self.horiba_controller
        text = "Desconectado · Sin sesión con el instrumento."
        if controller:
            text = STATE_NAMES[controller.status.state] + " · " + controller.status.detail
        self.horiba_status_label.configure(text=text)
        idle_icl = (controller and controller.opened and controller.status.state == "ready"
                    and self.vars["horiba_mode"].get() == "ICL" and not self.awaiting)
        for key, label in self.slit_actual_labels.items():
            value = controller.slit_positions.get(key) if controller else None
            port = controller.slit_ports.get(key) if controller else None
            name = "Entrada" if key == "slit_in_mm" else "Salida"
            actual = "{} leída: {} mm".format(name, format(value, ".9g")) if value is not None else name+": sin lectura confirmada"
            if port:
                actual += "\n{} · puerto {} · rango {}–{} mm".format(port["label"], port["locationId"], port["minimum_mm"], port["maximum_mm"])
            label.configure(text=actual)
        slits_enabled = idle_icl and bool(controller.slit_ports)
        for button in self.slit_apply_buttons:
            button.configure(state="normal" if slits_enabled else "disabled")
        if controller and controller.last_slit_move:
            report = controller.last_slit_move
            state = {"checking": "Verificando", "moving": "Ajustando", "confirmed": "Confirmado por ICL", "error": "Sin confirmar"}[report["state"]]
            requested = ", ".join(("entrada" if key == "slit_in_mm" else "salida")+"={} mm".format(value)
                                  for key, value in report["requested_mm"].items())
            self.slit_move_label.configure(text="Último ajuste · "+state+" · solicitado: "+requested+
                                           ("\n"+report["error"] if report.get("error") else ""))
        else:
            self.slit_move_label.configure(text="Las solicitudes no son lecturas del equipo.")
        position = controller.output_position if controller else None
        self.output_actual_label.configure(text="Salida leída: " + (position or "desconocida · consulte el equipo."))
        output_enabled = idle_icl and position in OUTPUT_POSITIONS and self.vars["output_target"].get() in OUTPUT_POSITIONS
        self.output_apply_button.configure(state="normal" if output_enabled else "disabled")
        self.output_selector.configure(state="disabled" if self.awaiting else "readonly")
        if controller and controller.last_output_move:
            report = controller.last_output_move
            state = {"checking": "Verificando", "moving": "Moviendo", "confirmed": "Confirmado por ICL", "error": "Sin confirmar"}[report["state"]]
            self.output_move_label.configure(text="Último ajuste · "+state+" · solicitado: "+report["requested"]+
                                             ("\n"+report["error"] if report.get("error") else ""))
        else:
            self.output_move_label.configure(text="Seleccione Axial o Lateral después de consultar.")
        catalog = controller.gratings if controller else {}
        for n in GRATING_POSITIONS:
            item = catalog.get(n)
            if item:
                density = format(item["grooves_per_mm"], ".9g") if item["grooves_per_mm"] is not None else "no informado"
                blaze = str(item["blaze_reported"]) if item["blaze_reported"] is not None else "no informado"
                if item["blaze_reported"] == 0:
                    blaze += " (sin λ conocida)"
                values = ("Grating {}".format(n), density, blaze)
            else:
                values = ("Grating {}".format(n), "no configurado" if catalog else "sin consulta", "—")
            if tuple(self.grating_table.item(str(n), "values")) != values:
                self.grating_table.item(str(n), values=values)
        choices = ["Grating {}".format(n) for n in sorted(catalog)]
        self.grating_selector.configure(values=choices, state="disabled" if self.awaiting else "readonly")
        grating = controller.grating_position if controller else None
        nm = controller.grating_wavelength_nm if controller else None
        grating_text = "Grating leído: desconocido." if grating is None else "Grating {} leído · λ={} nm.".format(grating, nm)
        if grating in catalog and catalog[grating]["description"]:
            grating_text += "\n" + catalog[grating]["description"]
        if controller and controller.grating_change_pending:
            grating_text += "\nConfiguración pendiente: aplique grating y λ antes de medir."
        self.grating_actual_label.configure(text=grating_text)
        if grating is not None:
            self.grating_table.selection_set(str(grating))
        else:
            self.grating_table.selection_remove(*self.grating_table.selection())
        grating_enabled = (controller and controller.opened and grating in catalog
                           and (controller.status.state == "ready" or controller.grating_change_pending)
                           and self.vars["grating_target"].get() in choices and self.vars["horiba_mode"].get() == "ICL"
                           and not self.awaiting)
        self.grating_apply_button.configure(state="normal" if grating_enabled else "disabled")
        if controller and controller.last_grating_move:
            report = controller.last_grating_move
            state = {"checking": "Verificando", "moving_turret": "Moviendo torreta", "moving_wavelength": "Ajustando λ",
                     "confirmed": "Confirmado por ICL", "error": "Sin confirmar"}[report["state"]]
            self.grating_move_label.configure(text="Último ajuste · {} · Grating {}, λ={} nm".format(state, report["requested"], report["target_nm"])+
                                              ("\n"+report["error"] if report.get("error") else ""))
        else:
            self.grating_move_label.configure(text="Consulte los gratings instalados antes de aplicar.")
        self.refresh_summary()

    def refresh_summary(self):
        controller = self.horiba_controller
        if self.vars["horiba_mode"].get() == "SIM":
            horiba = "HORIBA: simulación (SIM)"
        elif not controller:
            horiba = "HORIBA: sin sesión ICL"
        else:
            def show(value, pattern):
                return "?" if value is None else pattern.format(value)
            slits = controller.slit_positions
            horiba = "HORIBA: {} · {} · λ {} · salida {} · rendijas {}/{} mm".format(
                STATE_NAMES[controller.status.state], show(controller.grating_position, "G{}"),
                show(controller.grating_wavelength_nm, "{:.4f} nm"), controller.output_position or "?",
                show(slits.get("slit_in_mm"), "{:g}"), show(slits.get("slit_out_mm"), "{:g}"))
        sr830 = ("SR830: conectado · AUX {}".format(self.vars["channel"].get()) if self.connected
                 else "SR830: desconectado")
        if self.connected and self.conn.interface == "SIM":
            sr830 = "SR830: simulador · AUX {}".format(self.vars["channel"].get())
        sweep = self.sweep
        if not sweep:
            text = "Barrido: —"
        elif sweep["state"] == "running":
            text = "Barrido: en curso · {}/{} puntos".format(sweep["written"], sweep["total"])
            if sweep["current"] is not None:
                text += " · configuración {}/{}".format(sweep["current"]+1, sweep["configurations"])
        else:
            names = dict(completed="completo", cancelled="cancelado", error="interrumpido")
            text = "Barrido: {} · {} puntos · {}".format(names[sweep["state"]], sweep["written"],
                                                        "guardado" if sweep.get("saved") else "sin guardar")
        for key, value in (("horiba", horiba), ("sr830", sr830), ("sweep", text)):
            if self.summary_labels[key].cget("text") != value:
                self.summary_labels[key].configure(text=value)

    def refresh_controls(self):
        for b in self.buttons:
            b.configure(state="disabled" if self.awaiting else "normal")
        self.cancel_button.configure(state="normal" if self.awaiting else "disabled")
        sweep = self.sweep
        can_save = (not self.awaiting and sweep is not None and sweep["state"] != "running" and Path(sweep["csv"]).exists())
        self.save_button.configure(state="normal" if can_save else "disabled")
        self.update_mode_fields()
        self.connection_label.configure(text=self.connection_summary())
        self.refresh_status()
        self.refresh_sweep_info()

    # -------------------------------------------------------------- eventos
    def poll(self):
        try:
            while True:
                event, operation, value = self.worker.events.get_nowait()
                if event == "progress":
                    i, total, voltage = value
                    self.voltage_label.configure(text="Tensión actual: {:.7g} V".format(voltage))
                    prefix = self.sweep_status + " · " if self.sweep_status and self.awaiting else ""
                    self.status.configure(text=prefix+"lectura {}/{} · {:.7g} V".format(i, total, voltage))
                    continue
                if event == "sweep_point":
                    self.add_sweep_point(value)
                    continue
                if event == "background":
                    self.background_measured(value)
                    continue
                if event == "status":
                    self.status.configure(text=value)
                    if operation == "slit_sweep":
                        self.sweep_status = value
                        self.sweep_label.configure(text=value)
                    continue
                self.awaiting = False
                if event == "error":
                    self.failed(operation, value)
                    continue
                self.safe(lambda op=operation, result=value: self.completed(op, result))
        except queue.Empty:
            pass
        self.refresh_status()
        # La última confirmación se renueva sin bloquear Tk y sin competir con
        # un barrido, una inicialización u otra operación instrumental.
        controller = self.horiba_controller
        if (controller and controller.opened and not self.awaiting and not self.worker.busy
                and not self.closing and time.monotonic() >= self.horiba_check_at):
            self.horiba_check_at = time.monotonic() + 5
            self.submit("horiba_status", controller.connect)
        if self.closing and not self.awaiting:
            self._destroy()
            return
        self._poll_id = self.root.after(50, self.poll)

    def failed(self, operation, value):
        if (isinstance(value, CommunicationError) and not isinstance(value, HoribaError)) or operation == "connect":
            self.connected = False
        self.status.configure(text=str(value))
        if operation == "slit_sweep":
            self.sweep_failed(value)
        if not isinstance(value, AcquisitionCancelled):
            messagebox.showerror("Error de operación", str(value), parent=self.root)
        self.refresh_controls()

    def completed(self, operation, result):
        if operation in ("horiba_slits", "horiba_slits_move", "horiba_mount", "slit_sweep") and self.horiba_controller:
            # Puertos detectados (0=auto → 1/4): la próxima operación reutiliza la misma sesión.
            self.vars["horiba_slit_in"].set(self.horiba_controller.settings.slit_in)
            self.vars["horiba_slit_out"].set(self.horiba_controller.settings.slit_out)
        if operation == "horiba_diagnose":
            messagebox.showinfo("Diagnóstico ICL", json.dumps(result, ensure_ascii=False, indent=2), parent=self.root)
        elif operation == "horiba_initialize":
            prefix = "Ya estaba inicializado; no se repitió homing." if result["already_initialized"] else "Inicialización confirmada por ICL."
            self.status.configure(text=prefix + " λ={} nm.".format(result["snapshot"]["wavelength_nm"]))
        elif operation in ("horiba_status", "horiba_close"):
            pass
        elif operation in ("horiba_slits", "horiba_slits_move"):
            self.status.configure(text="Rendijas leídas." if operation == "horiba_slits" else "Aperturas confirmadas por ICL.")
        elif operation in ("horiba_output", "horiba_output_move"):
            position = result["position"] if operation == "horiba_output" else result["actual"]
            self.status.configure(text="Salida óptica confirmada por ICL: "+position+".")
        elif operation in ("horiba_gratings", "horiba_grating_move"):
            if operation == "horiba_gratings" and not self.vars["grating_target_nm"].get().strip():
                self.vars["grating_target_nm"].set(format(result["wavelength_nm"], ".12g"))
            self.status.configure(text="Gratings consultados." if operation == "horiba_gratings" else "Grating y λ confirmados por ICL.")
        elif operation == "horiba_mount":
            self.status.configure(text="Estado leído: {} · λ={} nm · salida {} · rendijas {}/{} mm.".format(
                result["grating"], result["wavelength_nm"], result.get("output_port", "?"),
                result.get("slit_in_mm", "?"), result.get("slit_out_mm", "?")))
        elif operation == "slit_sweep":
            self.sweep["state"] = "completed"
            self.sweep_label.configure(text="Barrido completo: {points} puntos en {configurations} configuraciones. "
                "Rendijas en {} / {} mm.{}\nRespaldo: {csv}".format(
                    *result["last_slits_mm"],
                    " Fondo: {:.7g} V.".format(self.background_v()) if self.background_v() is not None else "",
                    **result))
            self.status.configure(text="Barrido completo. Pulse «Guardar datos del barrido…» para exportar CSV + metadatos.")
            self.redraw()
        elif operation == "save_sweep":
            self.sweep["saved"] = result["csv"]
            self.status.configure(text="Datos guardados: {} y {}".format(result["csv"], Path(result["json"]).name))
        elif operation == "connect":
            self.reader, self.identity = result
            self.connected = True
            self.status.configure(text="SR830 conectado: " + self.identity)
        elif operation == "disconnect":
            self.connected = False
            self.status.configure(text="SR830 desconectado.")
        elif operation == "discover":
            self.discovery_callback(result)
        elif operation == "test":
            identity, voltage = result
            self.voltage_label.configure(text="Tensión actual: {:.7g} V".format(voltage))
            self.status.configure(text="{} · AUX = {:.7g} V".format(identity, voltage))
        self.refresh_controls()

    # ----------------------------------------------------------------- cierre
    def _destroy(self):
        self.root.after_cancel(self._poll_id)
        try:
            self.root.unbind_all("<MouseWheel>")
        except tk.TclError:
            pass
        if self.reader:
            self.reader.close()
        if self.horiba_controller:
            # El cierre normal de la sesión ocurre en close(), fuera de Tk.
            self.horiba_controller.drop()
        self.root.destroy()

    def close(self):
        if self.awaiting:
            if messagebox.askyesno("Cerrar", "¿Cancelar y cerrar al finalizar la operación actual?\n"
                                   "Una consulta en curso puede tardar hasta el timeout.", parent=self.root):
                self.closing = True
                self.cancel_acquisition()
            return
        if self.unsaved_sweep() and not messagebox.askyesno(
                "Cerrar", "El último barrido no se guardó con «Guardar datos del barrido…».\n"
                "Su respaldo automático queda en:\n{}\n¿Cerrar igualmente?".format(self.sweep["csv"]), parent=self.root):
            return
        if self.horiba_controller and self.horiba_controller.socket is not None:
            self.closing = True
            self.submit("horiba_close", self.horiba_controller.close)
            return
        self._destroy()


def launch(data_root=DEFAULT_ROOT):
    root = tk.Tk()
    CalibrationApp(root, data_root)
    root.mainloop()
