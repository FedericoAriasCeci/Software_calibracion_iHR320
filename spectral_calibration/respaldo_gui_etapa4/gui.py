"""Tkinter: calibración manual o barrido automático ICL; I/O fuera de Tk."""
import copy
from dataclasses import asdict
import json
from pathlib import Path
import queue
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure
from .acquisition import AcquisitionCancelled, Worker, acquire
from .analysis import analyze
from .instruments import CommunicationError, ConnectionSettings, SR830Reader, SimulatedReader, discover_resources
from .horiba import HoribaError, HoribaSettings, ICLMonochromator, SimulatedMonochromator, OUTPUT_POSITIONS, GRATING_POSITIONS, wavelength_plan
from .models import AcquisitionSettings, Experiment, Scan, number
from .plotting import draw_profiles, draw_slits, export_plots
from .storage import ExportError, SessionStore, atomic_text
from .slit_sweep import (SimulatedSlitInstrument, new_sweep_path, run_slit_sweep,
                         slit_plan_from_lists, slit_plan_from_range)

DEFAULT_ROOT = Path(__file__).resolve().parent.parent / "calibration_data"
SWEEP_MODES = ["Rango (entrada = salida)", "Listas entrada/salida"]


class ScrollablePanel(ttk.Frame):
    def __init__(self,parent):
        super().__init__(parent)
        self.canvas = tk.Canvas(self,highlightthickness=0,width=350)
        scrollbar = ttk.Scrollbar(self,orient="vertical",command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right",fill="y")
        self.canvas.pack(side="left",fill="both",expand=True)
        self.content = ttk.Frame(self.canvas,padding=4)
        window = self.canvas.create_window((0,0),window=self.content,anchor="nw")
        self.content.bind("<Configure>",lambda event:self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>",lambda event:self.canvas.itemconfigure(window,width=event.width))


class CalibrationApp:
    def __init__(self, root, data_root=DEFAULT_ROOT):
        self.root, self.data_root = root, Path(data_root)
        root.title("HORIBA iHR320 · Calibración espectral")
        root.geometry("{}x{}".format(min(1220,root.winfo_screenwidth()-60),min(820,root.winfo_screenheight()-80)))
        root.minsize(1000, 680)
        self.worker = Worker()
        self.reader = self.store = self.scan_id = self.pending = self.unsaved = None
        self.connected = self.closing = self.awaiting = False
        self.conn = ConnectionSettings()
        self.horiba_settings = HoribaSettings()
        self.automatic = self.monochromator = None
        self.horiba_controller = None
        self.horiba_check_at = 0
        self.auto_cancelled = self.auto_finalize = False
        self.pending_instrument = {}
        self.sweep_live = None
        self.sim =dict(center_nm=532.0, fwhm_nm=0.4, amplitude_v=1.0, background_v=0.03, noise_v=0.002, seed=830)
        self.vars, self.buttons, self.mount_entries = {}, [], []
        self._build()
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._poll_id = root.after(50, self.poll)
        self.refresh()

    def var(self, name, value):
        self.vars[name] = tk.StringVar(value=str(value))
        return self.vars[name]

    def field(self, parent, row, label, name, default, values=None, locked=False):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=5, pady=3)
        v = self.var(name, default)
        entry = ttk.Combobox(parent, textvariable=v, values=values, width=21, state="readonly") if values else ttk.Entry(parent, textvariable=v, width=24)
        entry.grid(row=row, column=1, sticky="ew", padx=5, pady=3)
        if locked:
            self.mount_entries.append((entry, bool(values)))
        return entry

    def button(self, parent, text, command, **pack):
        b = ttk.Button(parent, text=text, command=lambda: self.safe(command))
        b.pack(**pack)
        self.buttons.append(b)
        return b

    def safe(self, function):
        try:
            function()
        except ExportError as exc:
            self.refresh()
            messagebox.showwarning("Datos guardados; exportación pendiente", str(exc), parent=self.root)
        except Exception as exc:
            messagebox.showerror("Operación no realizada", str(exc), parent=self.root)

    def _build(self):
        top = ttk.Frame(self.root, padding=5)
        top.pack(fill="x")
        for text, cmd in [("Nueva sesión", self.new_session), ("Recuperar sesión…", self.open_session), ("Exportar CSV + PNG", self.export)]:
            self.button(top, text, cmd, side="left", padx=3)
        self.session_label = ttk.Label(top, text="Guardado automático al iniciar el primer barrido.")
        self.session_label.pack(side="left", padx=10)
        body = ttk.Panedwindow(self.root, orient="horizontal")
        body.pack(fill="both", expand=True, padx=6, pady=4)
        left = ttk.Frame(body, width=355)
        right = ttk.LabelFrame(body, text="D · Visualización y análisis", padding=5)
        body.add(left, weight=0)
        body.add(right, weight=1)
        config = ttk.Notebook(left)
        config.pack(fill="both", expand=True)
        a_box,b_box = ScrollablePanel(config),ScrollablePanel(config)
        a,b = a_box.content,b_box.content
        config.add(a_box,text="A \u00b7 Montaje")
        config.add(b_box,text="B \u00b7 Adquisici\u00f3n")
        h_box = ScrollablePanel(config)
        config.add(h_box, text="HORIBA automático")
        h = h_box.content
        self.field(h, 0, "Control de λ", "horiba_mode", "SIM", ["SIM", "ICL"])
        self.field(h, 1, "Inicio [nm]", "auto_start", 531.4)
        self.field(h, 2, "Fin [nm]", "auto_end", 532.6)
        self.field(h, 3, "Paso [nm]", "auto_step", .05)
        for row, (key, label) in enumerate([
                ("index", "Índice ICL"), ("timeout_s", "Timeout consulta [s]"),
                ("move_timeout_s", "Timeout movimiento [s]"), ("tolerance_nm", "Tolerancia λ [nm]"),
                ("minimum_nm", "Límite inferior [nm]"), ("maximum_nm", "Límite superior [nm]"),
                ("slit_in", "Entrada ICL (0=auto)"), ("slit_out", "Salida ICL (0=auto)"),
                ("initialize_timeout_s", "Timeout inicialización [s]"), ("slit_timeout_s", "Timeout rendijas [s]"),
                ("output_timeout_s", "Timeout salida [s]"), ("grating_timeout_s", "Timeout torreta [s]")], 4):
            self.field(h, row, label, "horiba_"+key, getattr(self.horiba_settings, key))
        self.horiba_status_label = ttk.Label(h, text="Desconectado · Sin sesión con el instrumento.", wraplength=315)
        self.horiba_status_label.grid(row=row+1, column=0, columnspan=2, sticky="w", padx=5, pady=5)
        hp = ttk.Frame(h)
        hp.grid(row=row+2, column=0, columnspan=2, sticky="ew", pady=6)
        self.button(hp, "Diagnosticar ICL (sin mover)", self.diagnose_horiba, fill="x", pady=2)
        self.button(hp, "Inicializar espectrómetro", self.initialize_horiba, fill="x", pady=2)
        self.button(hp, "Consultar estado HORIBA", self.check_horiba, fill="x", pady=2)
        self.button(hp, "Desconectar HORIBA", self.disconnect_horiba, fill="x", pady=2)
        self.button(hp, "Leer montaje HORIBA (sin mover)", self.read_horiba_mount, fill="x", pady=2)
        self.button(hp, "Iniciar barrido automático", self.start_automatic, fill="x", pady=4)
        self.horiba_label = ttk.Label(h, text="SIM requiere lector SIM. ICL requiere HORIBA y licencia.\n"
            "El barrido mueve λ, espera, lee AUX y guarda cada punto.\n"
            "Límites: ajústelos al rango de su red. Consulte rendijas para detectar los puertos y sus aperturas.\n"
            "Cancelar impide nuevos puntos; una orden de motor ya enviada puede continuar.", wraplength=315)
        self.horiba_label.grid(row=row+3, column=0, columnspan=2, sticky="w", padx=5)
        slit_box = ScrollablePanel(config)
        config.add(slit_box, text="Rendijas")
        sl = slit_box.content
        ttk.Label(sl, text="Control ICL · Aperturas independientes\nConsulte antes de aplicar. Requiere equipo inicializado.",
                  wraplength=315).grid(row=0, column=0, columnspan=2, sticky="w", padx=5, pady=5)
        self.slit_actual_labels = {}
        for line, key, title in ((1, "slit_in_mm", "Entrada"), (4, "slit_out_mm", "Salida")):
            self.field(sl, line, title+" solicitada [mm]", "target_"+key, .1)
            label = ttk.Label(sl, text=title+": sin lectura confirmada", wraplength=315)
            label.grid(row=line+1, column=0, columnspan=2, sticky="w", padx=5, pady=5)
            self.slit_actual_labels[key] = label
        actions = ttk.Frame(sl)
        actions.grid(row=7, column=0, columnspan=2, sticky="ew", pady=5)
        self.button(actions, "Consultar rendijas (sin mover)", self.read_horiba_slits, fill="x", pady=2)
        self.slit_apply_buttons = [
            self.button(actions, text, lambda part=part: self.apply_horiba_slits(part), fill="x", pady=2)
            for text, part in (("Aplicar entrada", "entrance"), ("Aplicar salida", "exit"), ("Aplicar ambas", "both"))]
        self.slit_move_label = ttk.Label(sl, text="Las solicitudes no son lecturas del equipo.", wraplength=315)
        self.slit_move_label.grid(row=8, column=0, columnspan=2, sticky="w", padx=5, pady=5)
        self.button(actions, "Leer montaje HORIBA (sin mover)", self.read_horiba_mount, fill="x", pady=2)
        ttk.Label(sl, text="Rango según rendija instalada: 0–2 o 0–7 mm.\n"
            "Para cambiar aperturas, inicie un nuevo barrido. Después, lea el montaje antes de medir.\n"
            "Cancelar abandona la espera; un movimiento enviado puede continuar.", wraplength=315).grid(
                row=9, column=0, columnspan=2, sticky="w", padx=5, pady=5)
        output_box = ScrollablePanel(config)
        config.add(output_box, text="Salida")
        out = output_box.content
        ttk.Label(out, text="Selector óptico de salida\nEspejo de salida del iHR320", wraplength=315).grid(
            row=0, column=0, columnspan=2, sticky="w", padx=5, pady=5)
        self.output_selector = self.field(out, 1, "Salida solicitada", "output_target", "", list(OUTPUT_POSITIONS))
        self.output_actual_label = ttk.Label(out, text="Salida leída: desconocida · consulte el equipo.", wraplength=315)
        self.output_actual_label.grid(row=2, column=0, columnspan=2, sticky="w", padx=5, pady=5)
        output_actions = ttk.Frame(out)
        output_actions.grid(row=3, column=0, columnspan=2, sticky="ew", pady=5)
        self.button(output_actions, "Consultar salida (sin mover)", self.read_horiba_output, fill="x", pady=2)
        self.output_apply_button = self.button(output_actions, "Aplicar salida", self.apply_horiba_output, fill="x", pady=2)
        self.output_move_label = ttk.Label(out, text="Seleccione Axial o Lateral después de consultar.", wraplength=315)
        self.output_move_label.grid(row=4, column=0, columnspan=2, sticky="w", padx=5, pady=5)
        ttk.Label(out, text="Este control cambia el espejo de salida. Las aperturas se ajustan en Rendijas.\n"
            "Para cambiar la trayectoria, inicie un nuevo barrido. El programa confirma la posición informada por ICL.\n"
            "Use sesiones separadas para comparar calibraciones de las dos rutas.\n"
            "Cancelar abandona la espera; el espejo puede continuar su movimiento.", wraplength=315).grid(
                row=5, column=0, columnspan=2, sticky="w", padx=5, pady=5)
        grating_box = ScrollablePanel(config)
        config.add(grating_box, text="Gratings")
        gr = grating_box.content
        ttk.Label(gr, text="Torreta de gratings · configuración de ICL", wraplength=315).grid(
            row=0, column=0, columnspan=2, sticky="w", padx=5, pady=5)
        self.grating_table = ttk.Treeview(gr, columns=("position", "density", "blaze"), show="headings", height=3, selectmode="none")
        for key, label, width in (("position", "Posición", 80), ("density", "Líneas/mm", 85), ("blaze", "Blaze informado", 135)):
            self.grating_table.heading(key, text=label)
            self.grating_table.column(key, width=width, stretch=False)
        self.grating_table.grid(row=1, column=0, columnspan=2, sticky="w", padx=5, pady=5)
        for n in GRATING_POSITIONS:
            self.grating_table.insert("", "end", iid=str(n), values=("Grating {}".format(n), "sin consulta", "sin consulta"))
        self.grating_selector = self.field(gr, 2, "Grating solicitado", "grating_target", "", ["Grating {}".format(n) for n in GRATING_POSITIONS])
        self.field(gr, 3, "λ final [nm]", "grating_target_nm", "")
        self.grating_actual_label = ttk.Label(gr, text="Grating leído: desconocido.", wraplength=315)
        self.grating_actual_label.grid(row=4, column=0, columnspan=2, sticky="w", padx=5, pady=5)
        ga = ttk.Frame(gr)
        ga.grid(row=5, column=0, columnspan=2, sticky="ew", pady=5)
        self.button(ga, "Consultar gratings (sin mover)", self.read_horiba_gratings, fill="x", pady=2)
        self.grating_apply_button = self.button(ga, "Aplicar grating y λ", self.apply_horiba_grating, fill="x", pady=2)
        self.grating_move_label = ttk.Label(gr, text="Consulte los gratings instalados antes de aplicar.", wraplength=315)
        self.grating_move_label.grid(row=6, column=0, columnspan=2, sticky="w", padx=5, pady=5)
        ttk.Label(gr, text="Tras cambiar la torreta se ordena explícitamente la λ final. Revise sus límites para la red elegida.\n"
            "Blaze=0 no aporta una longitud de blaze utilizable. Se conserva el valor de ICL sin asignarle unidades no confirmadas.\n"
            "Inicie un nuevo barrido antes de cambiar. Los resultados previos conservan su grating; verifique una línea conocida con la nueva red.\n"
            "Cancelar abandona la espera; el movimiento puede continuar.", wraplength=315).grid(
                row=7, column=0, columnspan=2, sticky="w", padx=5, pady=5)
        sweep_box = ScrollablePanel(config)
        config.add(sweep_box, text="Barrido rendijas")
        sw = sweep_box.content
        ttk.Label(sw, text="Barrido en apertura de rendijas\nPara cada configuración se recorre la misma grilla de λ.",
                  wraplength=315).grid(row=0, column=0, columnspan=2, sticky="w", padx=5, pady=5)
        self.field(sw, 1, "λ mínima [nm]", "sweep_start", 531.4)
        self.field(sw, 2, "λ máxima [nm]", "sweep_end", 532.6)
        self.field(sw, 3, "Paso λ [nm]", "sweep_step", .05)
        self.field(sw, 4, "Modo de aperturas", "sweep_mode", SWEEP_MODES[0], SWEEP_MODES)
        self.field(sw, 5, "Apertura mínima [mm]", "sweep_slit_min", .1)
        self.field(sw, 6, "Apertura máxima [mm]", "sweep_slit_max", .5)
        self.field(sw, 7, "Paso apertura [mm]", "sweep_slit_step", .1)
        self.field(sw, 8, "Lista entrada [mm]", "sweep_list_in", "0.1; 0.2; 0.5")
        self.field(sw, 9, "Lista salida [mm]", "sweep_list_out", "0.1; 0.2; 0.5")
        sweep_actions = ttk.Frame(sw)
        sweep_actions.grid(row=10, column=0, columnspan=2, sticky="ew", pady=5)
        self.button(sweep_actions, "Vista previa del plan", self.preview_slit_sweep, fill="x", pady=2)
        self.button(sweep_actions, "Iniciar barrido de rendijas", self.start_slit_sweep, fill="x", pady=2)
        self.sweep_label = ttk.Label(sw, text="Sin barrido de rendijas en esta sesión.", wraplength=315)
        self.sweep_label.grid(row=11, column=0, columnspan=2, sticky="w", padx=5, pady=5)
        ttk.Label(sw, text="Rango: entrada = salida, desde la mínima hasta la máxima con paso constante.\n"
            "Listas: pares en orden (separe con ';'). Una lista de un único valor se repite para todas.\n"
            "Usa N, intervalo, espera y canal de B · Adquisición, y Control de λ / límites / tolerancia de HORIBA automático.\n"
            "Los datos se guardan en un único CSV en calibration_data/slit_sweeps, punto a punto.\n"
            "Requiere no tener un barrido seleccionado. Al terminar, las rendijas quedan en la última configuración.\n"
            "Cancelar conserva las filas completas; un movimiento enviado puede continuar.", wraplength=315).grid(
                row=12, column=0, columnspan=2, sticky="w", padx=5, pady=5)
        self.field(a, 0, "Experimento", "experiment_id", "calibracion", locked=True)
        self.field(a, 1, "Grating declarado", "grating", "G1", ["G1", "G2", "G3"], True)
        self.field(a, 2, "Grooves/mm (opcional)", "grooves_per_mm", "", locked=True)
        self.field(a, 3, "Blaze [nm] (opcional)", "blaze_nm", "", locked=True)
        self.field(a, 4, "Entrada declarada [mm]", "slit_mm", 0.1, locked=True)
        self.field(a, 5, "Salida declarada [mm]", "slit_out_mm", 0.1, locked=True)
        self.field(a, 6, "Láser nominal [nm]", "laser_nominal_nm", 532.0, locked=True)
        ttk.Label(a, text="Notas experimentales").grid(row=7, column=0, sticky="w", padx=5)
        self.notes = tk.Text(a, width=32, height=4, wrap="word")
        self.notes.grid(row=8, column=0, columnspan=2, padx=5, pady=4, sticky="ew")
        ttk.Label(a, text="Rendijas: consulte y ajuste en la pestaña Rendijas.\n"
            "Lea el montaje real antes de medir con ICL. FWHM vs. apertura agrupa sólo rendijas iguales.",
            wraplength=325).grid(row=9, column=0, columnspan=2, sticky="w", padx=5, pady=8)
        for row, (label, key, value) in enumerate([("Lecturas por punto N", "n", 20), ("Intervalo mínimo [s]", "interval_s", .05), ("Espera previa [s]", "wait_s", .3)]):
            self.field(b, row, label, key, value)
        self.field(b, 3, "Canal AUX IN", "channel", 1, [1,2,3,4])
        panel = ttk.Frame(b)
        panel.grid(row=4, column=0, columnspan=2, sticky="ew", pady=8)
        self.button(panel, "Configurar / conectar…", self.connection_dialog, fill="x", pady=2)
        row = ttk.Frame(panel)
        row.pack(fill="x")
        self.button(row, "Probar IDN + AUX", self.test_connection, side="left", padx=2)
        self.button(row, "Desconectar", self.disconnect, side="left", padx=2)
        self.button(panel, "Medir fondo (bloquear láser)", lambda: self.start_acquire("background"), fill="x", pady=4)
        self.connection_label = ttk.Label(b, text="Desconectado", wraplength=320)
        self.connection_label.grid(row=5, column=0, columnspan=2, sticky="w", padx=5, pady=4)
        self.voltage_label = ttk.Label(b, text="Tensión actual: — V", font=("Segoe UI", 12))
        self.voltage_label.grid(row=6, column=0, columnspan=2, sticky="w", padx=5, pady=5)
        self.background_label = ttk.Label(b, text="Fondo: sin medir", wraplength=320)
        self.background_label.grid(row=7, column=0, columnspan=2, sticky="w", padx=5)
        ttk.Label(b, text="σ: dispersión muestral (ddof=1).\nNo equivale a incertidumbre total.", wraplength=320).grid(row=8, column=0, columnspan=2, sticky="w", padx=5, pady=6)
        c = ttk.LabelFrame(left, text="C · Barrido espectral manual", padding=5)
        c.pack(fill="x", pady=6)
        self.field(c, 0, "λ HORIBA actual [nm]", "wavelength_nm", 531.4)
        self.field(c, 1, "Paso sugerido [nm]", "step_nm", .05)
        self.next_label = ttk.Label(c, text="Siguiente sugerida: —", wraplength=325)
        self.next_label.grid(row=2, column=0, columnspan=2, sticky="w", padx=5, pady=3)
        controls = ttk.Frame(c)
        controls.grid(row=3, column=0, columnspan=2, sticky="ew")
        for text, cmd in [("Adquirir punto · HORIBA ya ajustado", lambda: self.start_acquire("point")), ("Repetir último punto…", lambda: self.start_acquire("repeat")), ("Eliminar último punto…", self.delete_last), ("Finalizar barrido y analizar", self.finish), ("Iniciar nuevo barrido", self.new_scan)]:
            self.button(controls, text, cmd, fill="x", pady=2)
        ttk.Label(c, text="Cada clic confirma un ajuste manual.\nNo hay avance automático.", wraplength=320).grid(row=4, column=0, columnspan=2, padx=5, pady=4)
        self.scan_selector = ttk.Combobox(right,state="readonly")
        self.scan_selector.pack(fill="x",pady=3)
        self.scan_selector.bind("<<ComboboxSelected>>",lambda event:self.safe(self.select_scan))
        switches = ttk.Frame(right)
        switches.pack(fill="x")
        self.corrected = tk.BooleanVar(value=True)
        self.raw_also = tk.BooleanVar(value=False)
        self.normalize = tk.BooleanVar(value=False)
        self.errors = tk.BooleanVar(value=True)
        self.overlay = tk.BooleanVar(value=True)
        self.empirical = tk.BooleanVar(value=False)
        self.view_checks = []
        for text, var, cmd in [("Restar fondo", self.corrected, self.toggle_background), ("Ver cruda", self.raw_also, self.redraw), ("Normalizar", self.normalize, self.redraw), ("Barras σ", self.errors, self.redraw), ("Superponer", self.overlay, self.redraw)]:
            cb = ttk.Checkbutton(switches, text=text, variable=var, command=lambda f=cmd: self.safe(f))
            cb.pack(side="left", padx=2)
            self.view_checks.append(cb)
        plots = ttk.Notebook(right)
        plots.pack(fill="both", expand=True, pady=4)
        self.profile_ax, self.profile_canvas = self.make_plot(plots, "Respuesta espectral")
        widths = ttk.Frame(plots)
        plots.add(widths, text="FWHM vs apertura")
        ttk.Checkbutton(widths, text="Ajuste empírico opcional √(Δλ₀² + (a·w)²)", variable=self.empirical, command=lambda: self.safe(self.redraw)).pack(anchor="w")
        self.fit_label = ttk.Label(widths, text="Sólo barridos finalizados.", wraplength=650)
        self.fit_label.pack(fill="x")
        self.slit_ax, self.slit_canvas = self.make_plot(widths)
        table_frame = ttk.Frame(plots)
        plots.add(table_frame, text="Puntos / lecturas")
        self.table = ttk.Treeview(table_frame, columns=("nm", "mean", "std", "corr", "n"), show="headings", height=10)
        for col, title in [("nm", "λ [nm]"), ("mean", "V media [V]"), ("std", "σ [V]"), ("corr", "V corr [V]"), ("n", "N")]:
            self.table.heading(col, text=title)
            self.table.column(col, width=90, stretch=True)
        self.table.pack(fill="both", expand=True)
        self.table.bind("<<TreeviewSelect>>", self.show_readings)
        self.readings = tk.Text(table_frame, height=6, wrap="word", state="disabled")
        self.readings.pack(fill="x")
        report_frame = ttk.Frame(right)
        report_frame.pack(fill="x",pady=3)
        self.report = tk.Text(report_frame,height=7,wrap="word",state="disabled")
        report_scroll = ttk.Scrollbar(report_frame,orient="vertical",command=self.report.yview)
        self.report.configure(yscrollcommand=report_scroll.set)
        report_scroll.pack(side="right",fill="y")
        self.report.pack(side="left",fill="x",expand=True)
        foot = ttk.Frame(self.root, padding=5)
        foot.pack(fill="x")
        self.status = ttk.Label(foot, text="Listo. Modo predeterminado: simulación.")
        self.status.pack(side="left", fill="x", expand=True)
        self.retry_button = ttk.Button(foot, text="Reintentar guardado", command=lambda: self.safe(self.save_pending))
        self.retry_button.pack(side="right", padx=4)
        self.cancel_button = ttk.Button(foot, text="Cancelar adquisición", command=self.cancel_acquisition)
        self.cancel_button.pack(side="right")

    def make_plot(self, parent, title=None):
        frame = ttk.Frame(parent)
        if title:
            parent.add(frame, text=title)
        else:
            frame.pack(fill="both", expand=True)
        fig = Figure(figsize=(6,4), dpi=90, tight_layout=True)
        canvas = FigureCanvasTkAgg(fig, master=frame)
        toolbar = NavigationToolbar2Tk(canvas, frame)
        toolbar.update()
        canvas.get_tk_widget().pack(fill="both", expand=True)
        return fig.add_subplot(111), canvas

    @property
    def scan(self):
        return self.store.get(self.scan_id) if self.store and self.scan_id else None

    def require_idle(self):
        if self.awaiting:
            raise ValueError("Espere a que termine la operación actual.")
        if self.unsaved:
            raise ValueError("Hay un lote sin guardar: pulse Reintentar guardado antes de continuar.")

    def settings(self):
        return AcquisitionSettings(**{k:self.vars[k].get() for k in ("n","interval_s","wait_s","channel")}).validate()

    def experiment(self):
        w = number(self.vars["slit_mm"].get(), "Rendija [mm]", 0, True)
        out = number(self.vars["slit_out_mm"].get(), "Rendija salida [mm]", 0, True)
        return Experiment(experiment_id=self.vars["experiment_id"].get(), grating=self.vars["grating"].get(), slit_in_mm=w, slit_out_mm=out, laser_nominal_nm=self.vars["laser_nominal_nm"].get(), grooves_per_mm=self.vars["grooves_per_mm"].get(), blaze_nm=self.vars["blaze_nm"].get(), notes=self.notes.get("1.0","end-1c")).validate()

    def new_session(self):
        self.require_idle()
        if self.scan and not self.scan.completed and (self.scan.points or self.scan.backgrounds):
            if not messagebox.askyesno("Cambiar sesión", "El barrido queda guardado sin finalizar. ¿Crear otra sesión?", parent=self.root):
                return
        store = SessionStore.create(self.data_root)
        if self.store:
            self.store.close()
        self.store, self.scan_id = store, None
        self.refresh()

    def open_session(self, path=None):
        self.require_idle()
        path = path or filedialog.askdirectory(title="Seleccione la carpeta session_…", initialdir=str(self.data_root))
        if not path or (self.store and Path(path).resolve()==self.store.path):
            return
        store = SessionStore(path)
        if self.store:
            self.store.close()
        self.store = store
        unfinished = [s for s in store.scans if not s.completed]
        self.scan_id = (unfinished[-1] if unfinished else store.scans[-1]).id if store.scans else None
        self.load_scan_fields()
        self.status.configure(text="Sesión recuperada. Reconecte el lector para continuar el barrido.")
        self.refresh()

    def load_scan_fields(self):
        if self.scan:
            exp = self.scan.experiment
            for key in ("experiment_id","grating","laser_nominal_nm","grooves_per_mm","blaze_nm"):
                self.vars[key].set(getattr(exp,key))
            self.vars["slit_mm"].set(exp.slit_in_mm)
            self.vars["slit_out_mm"].set(exp.slit_out_mm)
            self.notes.configure(state="normal")
            self.notes.delete("1.0","end")
            self.notes.insert("1.0",exp.notes)
            if self.scan.connection.get("interface")=="SIM":
                self.conn = ConnectionSettings()
                self.sim = {key:self.scan.connection[key] for key in self.sim}
            else:
                self.conn = ConnectionSettings(**{k:v for k,v in self.scan.connection.items() if k in asdict(ConnectionSettings())})
            batch = self.scan.active_points[-1].batch if self.scan.active_points else self.scan.background
            if batch:
                for key,value in batch.settings.items():
                    self.vars[key].set(value)
            if self.scan.active_points:
                self.vars["wavelength_nm"].set(self.scan.active_points[-1].wavelength_nm)
            self.corrected.set(self.scan.use_background)

    def select_scan(self):
        self.require_idle()
        self.scan_id = self.scan_selector.get().split(" | ")[0]
        self.sweep_live = None
        self.load_scan_fields()
        self.refresh()

    def new_scan(self):
        self.require_idle()
        if self.scan and not self.scan.completed and (self.scan.points or self.scan.backgrounds):
            raise ValueError("Finalice el barrido actual antes de iniciar otro.")
        self.scan_id = None
        self.refresh()
        self.status.configure(text="Configure la nueva apertura/grating y pulse Adquirir punto o Medir fondo.")

    def ensure_scan(self):
        if not self.connected or self.reader is None:
            raise ValueError("Conecte el SR830 o el simulador en B · Adquisición.")
        self.sweep_live = None
        if self.scan is None:
            exp = self.experiment()
            if self.store is None:
                self.store = SessionStore.create(self.data_root)
            scan = Scan(exp,self.reader.describe(),use_background=self.corrected.get())
            self.store.add_scan(scan)
            self.scan_id = scan.id
        if self.scan.completed:
            raise ValueError("Inicie un nuevo barrido para adquirir más puntos.")
        if self.scan.connection != self.reader.describe():
            raise ValueError("El lector/configuración difiere del barrido guardado. Reconecte con sus parámetros originales o inicie otro barrido.")
        channels = {p.batch.settings["channel"] for p in self.scan.points}
        channels.update(b["batch"]["settings"]["channel"] for b in self.scan.backgrounds)
        if channels and self.settings().channel not in channels:
            raise ValueError("El canal AUX no puede cambiar dentro de un barrido.")

    def submit(self, operation, function):
        self.require_idle()
        self.worker.submit(operation,function)
        self.awaiting = True
        self.status.configure(text="En curso: "+operation)
        self.refresh_controls()

    def connection_dialog(self):
        self.require_idle()
        dialog = tk.Toplevel(self.root)
        dialog.title("SR830 / simulación · conexión")
        dialog.transient(self.root)
        controls, sim_controls = ttk.Frame(dialog,padding=8),ttk.Frame(dialog,padding=8)
        controls.grid(row=0,column=0,sticky="n")
        sim_controls.grid(row=0,column=1,sticky="n")
        variables,entries = {},{}
        labels = [("interface","Interfaz",["SIM","RS232","GPIB"]), ("resource","Recurso VISA (vacío: construir)",None), ("com_port","Puerto COM",None), ("gpib_board","Placa GPIB",None), ("gpib_address","Dirección GPIB",None), ("baud_rate","Baud",[300,600,1200,2400,4800,9600,19200]), ("parity","Paridad",["N","E","O"]), ("stop_bits","Bits de parada",[1,2]), ("timeout_ms","Timeout [ms]",None), ("visa_library","Backend VISA (vacío: sistema)",None)]
        for row,(key,label,choices) in enumerate(labels):
            ttk.Label(controls,text=label).grid(row=row,column=0,sticky="w",pady=3)
            var = tk.StringVar(value=str(getattr(self.conn,key)))
            variables[key] = var
            entry = ttk.Combobox(controls,textvariable=var,values=choices or [],width=28)
            if choices:
                entry.configure(state="readonly")
            entry.grid(row=row,column=1,padx=4)
            entries[key] = entry
        sv = {}
        ttk.Label(sim_controls,text="Simulador triangular: FWHM conocido").grid(row=0,column=0,columnspan=2)
        for row,(key,label) in enumerate([("center_nm","Centro [nm]"),("fwhm_nm","FWHM [nm]"),("amplitude_v","Altura [V]"),("background_v","Fondo [V]"),("noise_v","Ruido σ [V]"),("seed","Semilla")],1):
            ttk.Label(sim_controls,text=label).grid(row=row,column=0,sticky="w",pady=3)
            sv[key] = tk.StringVar(value=str(self.sim[key]))
            ttk.Entry(sim_controls,textvariable=sv[key],width=16).grid(row=row,column=1)
        ttk.Label(sim_controls,text="RS232: 8 bits, sin control de flujo (3 hilos).\nIguale baud/paridad en el panel del SR830.\nCOM→ASRL; use cable directo DTE–DCE.\nCambiar λ aquí nunca mueve el HORIBA.",wraplength=300).grid(row=8,column=0,columnspan=2,pady=12,sticky="w")
        found = ttk.Label(dialog,text="Detectar recursos no envía comandos a instrumentos.",wraplength=700)
        found.grid(row=1,column=0,columnspan=2,padx=8,pady=5,sticky="w")

        def detected(result):
            if dialog.winfo_exists():
                resources,errors = result
                entries["resource"].configure(values=[r for r in resources if not r.startswith("COM")])
                entries["com_port"].configure(values=[r for r in resources if r.startswith("COM")])
                found.configure(text="Disponibles: "+(", ".join(resources) or "ninguno")+("\n"+"; ".join(errors) if errors else ""))

        def discover():
            self.discovery_callback = detected
            library = variables["visa_library"].get()
            self.submit("discover",lambda:discover_resources(library))

        def connect():
            self.require_idle()
            settings = ConnectionSettings(**{k:v.get() for k,v in variables.items()}).validate()
            sim = {k:v.get() for k,v in sv.items()}
            reader = SimulatedReader(**sim) if settings.interface=="SIM" else SR830Reader(settings)
            old_reader = self.reader
            self.conn,self.sim = settings,sim
            def job():
                if old_reader:
                    old_reader.close()
                return reader,reader.connect()
            self.connected = False
            self.submit("connect",job)
            dialog.destroy()
        buttons = ttk.Frame(dialog,padding=8)
        buttons.grid(row=2,column=0,columnspan=2,sticky="ew")
        ttk.Button(buttons,text="Detectar VISA / COM",command=lambda:self.safe(discover)).pack(side="left",padx=4)
        ttk.Button(buttons,text="Conectar y probar *IDN?",command=lambda:self.safe(connect)).pack(side="right",padx=4)

    def test_connection(self):
        if not self.connected:
            raise ValueError("Primero conecte el lector.")
        settings,reader = self.settings(),self.reader
        self.submit("test",lambda:(reader.test(),reader.read_voltage(settings.channel)))

    def disconnect(self):
        self.require_idle()
        if self.reader:
            self.submit("disconnect",self.reader.close)

    def start_acquire(self,kind):
        self.require_idle()
        settings = self.settings()
        wavelength = number(self.vars["wavelength_nm"].get(),"λ HORIBA",0,True)
        step = number(self.vars["step_nm"].get(),"Paso")
        if step==0:
            raise ValueError("El paso sugerido debe ser positivo o negativo, distinto de cero.")
        self.ensure_scan()
        if kind=="repeat":
            if not self.scan.active_points:
                raise ValueError("No hay un último punto para repetir.")
            wavelength = self.scan.active_points[-1].wavelength_nm
            if not messagebox.askyesno("Repetir punto","Confirme el ajuste manual del HORIBA en {} nm.\nLa lectura anterior quedará en el historial.".format(wavelength),parent=self.root):
                return
            self.vars["wavelength_nm"].set(wavelength)
        elif kind=="point" and any(abs(p.wavelength_nm-wavelength)<1e-9 for p in self.scan.active_points):
            if not messagebox.askyesno("Longitud repetida","Esta longitud ya fue medida. ¿Guardar otra visita?\nEl análisis promediará las visitas con igual peso.",parent=self.root):
                return
        if kind=="background" and not messagebox.askokcancel("Medir fondo","Bloquee el láser y confirme.\nEl último fondo se aplicará a todo este barrido; los anteriores se conservan.",parent=self.root):
            return
        reader = self.reader
        self.pending_instrument = {}
        self.pending = (self.scan_id,kind,wavelength,step)
        def job():
            if isinstance(reader,SimulatedReader):
                reader.wavelength_nm,reader.blocked = wavelength,kind=="background"
            try:
                return acquire(reader,settings,self.worker.cancel,self.worker.progress)
            finally:
                if isinstance(reader,SimulatedReader):
                    reader.blocked = False
        if self.vars["horiba_mode"].get() == "ICL" and not isinstance(reader, SimulatedReader):
            def icl_job(mono):
                mono.read_gratings()
                mono.read_output()
                snapshot = mono.snapshot()
                self.validate_mount(snapshot)
                if kind != "background" and abs(snapshot["wavelength_nm"]-wavelength) > mono.settings.tolerance_nm:
                    raise HoribaError("La λ manual declarada no coincide con la lectura de HORIBA; ajuste y consulte antes de adquirir.")
                if not self.scan.horiba:
                    metadata = dict(mono.describe(), initial=snapshot)
                    self.store.update_scan(self.scan_id, lambda scan: setattr(scan, "horiba", metadata))
                batch = job()
                after = mono.snapshot()
                self.validate_mount(after)
                if abs(after["wavelength_nm"]-snapshot["wavelength_nm"]) > mono.settings.tolerance_nm:
                    raise HoribaError("HORIBA cambió durante la adquisición manual; lote descartado.")
                return batch, dict(snapshot, after=after)
            controller, work = self.horiba_action(icl_job)
            self.submit("manual_icl_acquire", work)
        else:
            self.submit("acquire",job)

    def get_horiba_settings(self):
        self.horiba_settings = HoribaSettings(**{key: self.vars["horiba_"+key].get()
            for key in asdict(self.horiba_settings) if key != "url"}).validate()
        return self.horiba_settings

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
        if self.vars["horiba_mode"].get() != "ICL":
            raise ValueError("Seleccione Control de λ = ICL para inicializar el espectrómetro real.")
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

    def refresh_horiba_status(self):
        controller = self.horiba_controller
        names = dict(disconnected="Desconectado", initializing="Inicializando", ready="Listo", error="Error")
        text = "Desconectado · Sin sesión con el instrumento."
        if controller:
            state = controller.status
            text = names[state.state] + " · " + state.detail
        self.horiba_status_label.configure(text=text)
        for key, label in self.slit_actual_labels.items():
            value = controller.slit_positions.get(key) if controller else None
            port = controller.slit_ports.get(key) if controller else None
            name = "Entrada" if key == "slit_in_mm" else "Salida"
            actual = "{} leída: {} mm".format(name, format(value, ".9g")) if value is not None else name+": sin lectura confirmada"
            if port:
                actual += "\n{} · puerto {} · rango {}–{} mm".format(port["label"], port["locationId"], port["minimum_mm"], port["maximum_mm"])
            label.configure(text=actual)
        enabled = (controller and controller.opened and controller.slit_ports and controller.status.state == "ready"
                   and self.vars["horiba_mode"].get() == "ICL" and not self.awaiting and self.scan is None)
        for button in self.slit_apply_buttons:
            button.configure(state="normal" if enabled else "disabled")
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
        output_enabled = (controller and controller.opened and position in OUTPUT_POSITIONS
                          and controller.status.state == "ready" and self.vars["output_target"].get() in OUTPUT_POSITIONS
                          and self.vars["horiba_mode"].get() == "ICL" and not self.awaiting and self.scan is None)
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
        number = controller.grating_position if controller else None
        nm = controller.grating_wavelength_nm if controller else None
        grating_text = "Grating leído: desconocido." if number is None else "Grating {} leído · λ={} nm.".format(number, nm)
        if number in catalog and catalog[number]["description"]:
            grating_text += "\n" + catalog[number]["description"]
        if controller and controller.grating_change_pending:
            grating_text += "\nConfiguración pendiente: aplique grating y λ antes de adquirir."
        self.grating_actual_label.configure(text=grating_text)
        if number is not None:
            self.grating_table.selection_set(str(number))
        else:
            self.grating_table.selection_remove(*self.grating_table.selection())
        grating_enabled = (controller and controller.opened and number in catalog
            and (controller.status.state == "ready" or controller.grating_change_pending)
            and self.vars["grating_target"].get() in choices and self.vars["horiba_mode"].get() == "ICL"
            and not self.awaiting and self.scan is None)
        self.grating_apply_button.configure(state="normal" if grating_enabled else "disabled")
        if controller and controller.last_grating_move:
            report = controller.last_grating_move
            state = {"checking": "Verificando", "moving_turret": "Moviendo torreta", "moving_wavelength": "Ajustando λ", "confirmed": "Confirmado por ICL", "error": "Sin confirmar"}[report["state"]]
            self.grating_move_label.configure(text="Último ajuste · {} · Grating {}, λ={} nm".format(state, report["requested"], report["target_nm"])+
                ("\n"+report["error"] if report.get("error") else ""))
        else:
            self.grating_move_label.configure(text="Consulte los gratings instalados antes de aplicar.")

    def read_horiba_gratings(self):
        self.require_idle()
        if self.vars["horiba_mode"].get() != "ICL":
            raise ValueError("Seleccione Control de λ = ICL para consultar la torreta real.")
        controller, job = self.horiba_action(lambda mono: mono.read_gratings())
        self.submit("horiba_gratings", job)

    def apply_horiba_grating(self):
        self.require_idle()
        if self.vars["horiba_mode"].get() != "ICL":
            raise ValueError("Seleccione Control de λ = ICL para mover la torreta real.")
        if self.scan is not None:
            raise ValueError("Inicie un nuevo barrido antes de cambiar grating; no se reutilizan calibración ni fondo de otra red.")
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
        if self.vars["horiba_mode"].get() != "ICL":
            raise ValueError("Seleccione Control de λ = ICL para consultar la salida real.")
        controller, job = self.horiba_action(lambda mono: mono.read_output())
        self.submit("horiba_output", job)

    def apply_horiba_output(self):
        self.require_idle()
        if self.vars["horiba_mode"].get() != "ICL":
            raise ValueError("Seleccione Control de λ = ICL para cambiar la salida real.")
        if self.scan is not None:
            raise ValueError("Inicie un nuevo barrido antes de cambiar la salida; el montaje y fondo guardados deben conservarse.")
        target = self.vars["output_target"].get()
        if target not in OUTPUT_POSITIONS:
            raise ValueError("Seleccione Axial o Lateral antes de aplicar.")
        controller, job = self.horiba_action(lambda mono: mono.move_output(target, self.worker.cancel))
        self.submit("horiba_output_move", job)

    def read_horiba_slits(self):
        self.require_idle()
        if self.vars["horiba_mode"].get() != "ICL":
            raise ValueError("Seleccione Control de λ = ICL para consultar las rendijas reales.")
        controller, job = self.horiba_action(lambda mono: mono.read_slits())
        self.submit("horiba_slits", job)

    def apply_horiba_slits(self, part):
        self.require_idle()
        if self.vars["horiba_mode"].get() != "ICL":
            raise ValueError("Seleccione Control de λ = ICL para ajustar las rendijas reales.")
        if self.scan is not None:
            raise ValueError("Inicie un nuevo barrido antes de cambiar aperturas; el montaje y fondo guardados deben conservarse.")
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
        self.require_idle()
        if self.scan:
            raise ValueError("Inicie un nuevo barrido antes de importar el montaje del HORIBA.")
        def read(mono):
            mono.read_slits()
            mono.read_output()
            mono.read_gratings()
            return mono.snapshot()
        controller, job = self.horiba_action(read)
        self.submit("horiba_mount", job)

    def start_automatic(self):
        self.require_idle()
        settings = self.settings()
        targets = wavelength_plan(*(self.vars[key].get() for key in ("auto_start", "auto_end", "auto_step")))
        mode = self.vars["horiba_mode"].get()
        if not self.connected:
            raise ValueError("Primero conecte el lector de tensión en B · Adquisición.")
        if mode == "SIM":
            if not isinstance(self.reader, SimulatedReader):
                raise ValueError("El control SIM de λ requiere un lector SIM.")
            controller = SimulatedMonochromator(self.reader, self.experiment())
        else:
            if isinstance(self.reader, SimulatedReader):
                raise ValueError("Para mover HORIBA real conecte un SR830 real.")
            hs = self.get_horiba_settings()
            if min(targets) < hs.minimum_nm or max(targets) > hs.maximum_nm:
                raise ValueError("El barrido excede los límites de λ configurados.")
        self.ensure_scan()
        if self.scan.active_points:
            raise ValueError("El automático requiere un barrido sin puntos. Finalice el actual e inicie otro; puede medir fondo antes.")
        if mode != "SIM":
            def connect_for_scan(mono):
                mono.read_gratings()
                mono.read_output()
                return mono.snapshot()
            controller, connect_horiba = self.horiba_action(connect_for_scan, hs)
        self.automatic = dict(targets=targets, index=0, settings=settings)
        self.auto_cancelled = False
        self.monochromator = controller
        def job():
            snapshot = controller.connect() if mode == "SIM" else connect_horiba()
            self.validate_mount(snapshot)
            metadata = dict(controller.describe(), initial=snapshot, targets_nm=targets,
                            acquisition=asdict(settings))
            self.store.update_scan(self.scan_id, lambda scan: setattr(scan, "horiba", metadata))
            return snapshot
        self.submit("auto_connect", job)

    def slit_sweep_plan(self):
        wavelengths = wavelength_plan(*(self.vars[key].get() for key in ("sweep_start", "sweep_end", "sweep_step")))
        if self.vars["sweep_mode"].get() == SWEEP_MODES[0]:
            pairs = slit_plan_from_range(*(self.vars[key].get() for key in ("sweep_slit_min", "sweep_slit_max", "sweep_slit_step")))
        else:
            pairs = slit_plan_from_lists(self.vars["sweep_list_in"].get(), self.vars["sweep_list_out"].get())
        return wavelengths, pairs

    def preview_slit_sweep(self):
        wavelengths, pairs = self.slit_sweep_plan()
        text = "{} configuraciones × {} λ = {} puntos.\nλ: {} → {} nm.\nAperturas (entrada/salida) [mm]: {}".format(
            len(pairs), len(wavelengths), len(pairs)*len(wavelengths), wavelengths[0], wavelengths[-1],
            ", ".join("{:g}/{:g}".format(*p) for p in pairs))
        self.sweep_label.configure(text=text)

    def start_slit_sweep(self):
        self.require_idle()
        if self.scan is not None:
            raise ValueError("Inicie un nuevo barrido (sin barrido seleccionado) antes de mover rendijas; el montaje y fondo guardados deben conservarse.")
        if not self.connected:
            raise ValueError("Primero conecte el lector de tensión en B · Adquisición.")
        settings = self.settings()
        wavelengths, pairs = self.slit_sweep_plan()
        mode = self.vars["horiba_mode"].get()
        if mode != "SIM" and isinstance(self.reader, SimulatedReader):
            raise ValueError("Para mover HORIBA real conecte un SR830 real.")
        if not messagebox.askyesno("Barrido de rendijas",
                "Se moverán rendijas y λ: {} configuraciones × {} λ = {} puntos.\n¿Iniciar?".format(
                    len(pairs), len(wavelengths), len(pairs)*len(wavelengths)), parent=self.root):
            return
        path = new_sweep_path(self.data_root / "slit_sweeps")
        cancel, progress = self.worker.cancel, self.worker.progress
        def status(text):
            self.worker.events.put(("status", "slit_sweep", text))
        def on_point(point):
            self.worker.events.put(("sweep_point", "slit_sweep", point))
        reader = self.reader
        if mode == "SIM":
            if not isinstance(reader, SimulatedReader):
                raise ValueError("El control SIM requiere un lector SIM.")
            instrument = SimulatedSlitInstrument(reader, grating=self.vars["grating"].get())
            def job():
                try:
                    return run_slit_sweep(instrument, reader, settings, wavelengths, pairs, path,
                                          cancel, progress, status, on_point=on_point)
                finally:
                    instrument.close()
        else:
            hs = self.get_horiba_settings()
            if min(wavelengths) < hs.minimum_nm or max(wavelengths) > hs.maximum_nm:
                raise ValueError("El barrido excede los límites de λ configurados.")
            def run(mono):
                mono.read_gratings()
                mono.read_output()
                return run_slit_sweep(mono, reader, settings, wavelengths, pairs, path, cancel, progress,
                                      status, tolerance_nm=mono.settings.tolerance_nm, on_point=on_point)
            controller, job = self.horiba_action(run, hs)
        self.auto_cancelled = False
        self.sweep_live = dict(curves={}, current=None)
        self.redraw()
        self.sweep_label.configure(text="En curso. CSV: " + str(path))
        self.submit("slit_sweep", job)

    def validate_mount(self, snapshot):
        if snapshot.get("grating_change_pending"):
            raise HoribaError("Configuración espectral pendiente: aplique grating y λ antes de adquirir.")
        exp = self.scan.experiment
        for key in ("grating", "slit_in_mm", "slit_out_mm"):
            if key in snapshot:
                actual, expected = snapshot[key], getattr(exp, key)
                equal = actual == expected if key == "grating" else abs(actual-expected) <= 1e-4
                if not equal:
                    raise HoribaError("Montaje HORIBA distinto del barrido: {} = {}, declarado {}. Lea el montaje antes de comenzar.".format(key, actual, expected))
        output = self.scan.horiba.get("initial", {}).get("output_port")
        if output is not None and snapshot.get("output_port") != output:
            raise HoribaError("La salida óptica cambió: esperaba {}, leída {}. Punto descartado.".format(
                output, snapshot.get("output_port", "desconocida")))
        info = self.scan.horiba.get("initial", {}).get("grating_info")
        if info is not None and snapshot.get("grating_info") != info:
            raise HoribaError("La configuración del grating cambió; inicie un nuevo barrido.")

    def stop_automatic(self, finalize=False):
        self.automatic = None
        if self.monochromator:
            controller = self.monochromator
            self.monochromator = None
            self.auto_finalize = finalize
            # El cierre también puede esperar una respuesta ICL: fuera del hilo Tk.
            self.worker.submit("auto_close", controller.close)
            self.awaiting = True

    def cancel_acquisition(self):
        self.auto_cancelled = True
        self.worker.cancel.set()

    def next_automatic_point(self):
        if not self.automatic:
            return
        if self.closing or self.auto_cancelled or self.worker.cancel.is_set():
            self.stop_automatic()
            self.status.configure(text="Barrido cancelado; puntos completos guardados.")
            return
        state = self.automatic
        if state["index"] >= len(state["targets"]):
            self.stop_automatic(finalize=True)
            return
        target = state["targets"][state["index"]]
        controller, reader = self.monochromator, self.reader
        self.pending = (self.scan_id, "point", target, 0)
        def job():
            snapshot = controller.move_to(target, self.worker.cancel)
            self.validate_mount(snapshot)
            batch = acquire(reader, state["settings"], self.worker.cancel, self.worker.progress)
            # Compruebe que el motor no cambió de posición ni montaje durante las lecturas.
            after = controller.snapshot()
            self.validate_mount(after)
            tolerance = controller.settings.tolerance_nm if isinstance(controller, ICLMonochromator) else 1e-9
            if abs(after["wavelength_nm"]-snapshot["wavelength_nm"]) > tolerance:
                raise HoribaError("HORIBA cambió de posición durante la adquisición; punto descartado.")
            if self.worker.cancel.is_set():
                raise AcquisitionCancelled("Punto cancelado antes de guardar.")
            return batch, dict(snapshot, after=after)
        self.submit("auto_acquire", job)

    def delete_last(self):
        self.require_idle()
        if not self.scan or not self.scan.active_points:
            raise ValueError("No hay puntos para eliminar.")
        if messagebox.askyesno("Eliminar último punto","¿Excluir el último punto de la curva?\nSus lecturas seguirán guardadas en el historial.",parent=self.root):
            self.store.update_scan(self.scan_id,lambda s:s.delete_last())
            self.refresh()

    def finish(self):
        self.require_idle()
        if not self.scan or not self.scan.active_points:
            raise ValueError("Adquiera al menos un punto antes de finalizar.")
        scan,store,scan_id = copy.deepcopy(self.scan),self.store,self.scan_id
        def job():
            result = analyze(scan)
            def change(s):
                s.analysis,s.completed = result,True
            store.update_scan(scan_id,change)
            return result
        self.submit("analyze",job)

    def toggle_background(self):
        self.require_idle()
        if self.scan:
            enabled,scan_id,store = self.corrected.get(),self.scan_id,self.store
            def change(scan):
                scan.use_background = enabled
                scan.analysis = analyze(scan) if scan.completed else {}
            self.submit("background_toggle",lambda:store.update_scan(scan_id,change))
        else:
            self.redraw()

    def export(self):
        self.require_idle()
        if not self.store:
            raise ValueError("No hay una sesión para exportar.")
        def job():
            self.store.export_tables()
            return export_plots(self.store)
        self.submit("export",job)

    def poll(self):
        try:
            while True:
                event,operation,value = self.worker.events.get_nowait()
                if event=="progress":
                    i,total,voltage = value
                    self.voltage_label.configure(text="Tensión actual: {:.7g} V".format(voltage))
                    self.status.configure(text="Adquiriendo {}/{} · {:.7g} V".format(i,total,voltage))
                    continue
                if event=="sweep_point":
                    if self.sweep_live is not None:
                        curve = self.sweep_live["curves"].setdefault(value["config_index"], dict(
                            slit_in_mm=value["slit_in_mm"], slit_out_mm=value["slit_out_mm"], nm=[], v=[], std=[]))
                        curve["nm"].append(value["wavelength_nm"])
                        curve["v"].append(value["mean_v"])
                        curve["std"].append(value["std_v"])
                        self.sweep_live["current"] = value["config_index"]
                        self.redraw()
                    continue
                if event=="status":
                    self.status.configure(text=value)
                    if operation=="slit_sweep":
                        self.sweep_label.configure(text=value)
                    continue
                self.awaiting = False
                if event=="error":
                    if self.automatic:
                        self.stop_automatic()
                    if operation=="save" and isinstance(value,ExportError):
                        self.saved_pending()
                    if operation=="save" and self.unsaved:
                        value = RuntimeError("El lote queda en memoria. Pulse Reintentar guardado: "+str(value))
                    if (isinstance(value,CommunicationError) and not isinstance(value,HoribaError)) or operation=="connect":
                        self.connected = False
                    self.status.configure(text=str(value))
                    if operation=="slit_sweep":
                        self.sweep_label.configure(text="Barrido de rendijas interrumpido; filas completas conservadas en el CSV. "
                            "Consulte rendijas antes de reintentar. " + str(value))
                    if not isinstance(value,AcquisitionCancelled):
                        messagebox.showerror("Error de operación",str(value),parent=self.root)
                    self.refresh_controls()
                    continue
                self.safe(lambda op=operation,result=value:self.completed(op,result))
        except queue.Empty:
            pass
        self.refresh_horiba_status()
        # La última confirmación se renueva sin bloquear Tk y sin competir con
        # adquisición, guardado, inicialización u otra operación instrumental.
        controller = self.horiba_controller
        if (controller and controller.opened and not self.awaiting and not self.worker.busy
                and not self.automatic and not self.closing and not self.unsaved
                and time.monotonic() >= self.horiba_check_at):
            self.horiba_check_at = time.monotonic() + 5
            self.submit("horiba_status", controller.connect)
        if self.closing and not self.awaiting:
            if self.unsaved:
                self.closing = False
                messagebox.showerror("Lote sin guardar","Reintente el guardado antes de cerrar.",parent=self.root)
            else:
                self._destroy()
                return
        self._poll_id = self.root.after(50,self.poll)

    def completed(self,operation,result):
        if operation in ("horiba_slits", "horiba_slits_move", "horiba_mount") and self.horiba_controller:
            self.vars["horiba_slit_in"].set(self.horiba_controller.settings.slit_in)
            self.vars["horiba_slit_out"].set(self.horiba_controller.settings.slit_out)
        if operation=="horiba_diagnose":
            messagebox.showinfo("Diagnóstico ICL", json.dumps(result, ensure_ascii=False, indent=2), parent=self.root)
        elif operation=="horiba_initialize":
            prefix = "Ya estaba inicializado; no se repitió homing." if result["already_initialized"] else "Inicialización confirmada por ICL."
            self.status.configure(text=prefix + " λ={} nm. Lea el montaje antes de medir.".format(result["snapshot"]["wavelength_nm"]))
        elif operation in ("horiba_status", "horiba_close"):
            self.refresh_horiba_status()
        elif operation in ("horiba_slits", "horiba_slits_move"):
            self.refresh_horiba_status()
            self.status.configure(text="Rendijas leídas." if operation == "horiba_slits" else "Aperturas confirmadas. Lea el montaje antes de medir.")
        elif operation in ("horiba_output", "horiba_output_move"):
            self.refresh_horiba_status()
            position = result["position"] if operation == "horiba_output" else result["actual"]
            self.status.configure(text="Salida óptica confirmada por ICL: "+position+".")
        elif operation in ("horiba_gratings", "horiba_grating_move"):
            if operation == "horiba_gratings":
                if not self.vars["grating_target_nm"].get().strip():
                    self.vars["grating_target_nm"].set(format(result["wavelength_nm"], ".12g"))
            else:
                actual = result["actual"]
                reset_manual = result["turret_movement_sent"] or self.vars["grating"].get() != actual["grating"]
                self.vars["grating"].set(actual["grating"])
                self.vars["wavelength_nm"].set(actual["wavelength_nm"])
                self.import_grating_metadata(actual["grating_info"], reset_manual)
            self.refresh_horiba_status()
            self.status.configure(text="Gratings consultados." if operation == "horiba_gratings" else "Grating y λ confirmados. Inicie una calibración nueva para esta configuración.")
        elif operation=="horiba_mount":
            reset_manual = self.vars["grating"].get() != result["grating"]
            self.vars["grating"].set(result["grating"])
            self.vars["wavelength_nm"].set(result["wavelength_nm"])
            if "grating_info" in result:
                self.import_grating_metadata(result["grating_info"], reset_manual)
            if "slit_in_mm" in result:
                self.vars["slit_mm"].set(result["slit_in_mm"])
                self.vars["slit_out_mm"].set(result["slit_out_mm"])
            self.horiba_label.configure(text="Montaje leído: " + json.dumps(result, ensure_ascii=False))
        elif operation=="slit_sweep":
            text = "Barrido de rendijas completo: {points} puntos en {configurations} configuraciones.\nCSV: {csv}\n".format(**result)
            text += "Rendijas en {} / {} mm. Lea el montaje antes de medir.".format(*result["last_slits_mm"])
            self.sweep_label.configure(text=text)
            self.status.configure(text="Barrido de rendijas completo: " + result["csv"])
            self.refresh_horiba_status()
        elif operation=="auto_connect":
            self.next_automatic_point()
        elif operation=="auto_acquire":
            batch, self.pending_instrument = result
            scan_id, kind, target, step = self.pending
            self.pending = (scan_id, kind, self.pending_instrument["wavelength_nm"], step)
            self.unsaved = (self.pending, batch)
            self.save_pending()
        elif operation=="auto_close":
            if self.auto_finalize and not self.closing and not self.auto_cancelled:
                self.auto_finalize = False
                self.finish()
        elif operation=="connect":
            self.reader,identity = result
            self.connected = True
            self.connection_label.configure(text="Conectado: "+identity)
        elif operation=="disconnect":
            self.connected = False
        elif operation=="discover":
            self.discovery_callback(result)
        elif operation=="test":
            identity,voltage = result
            self.voltage_label.configure(text="Tensión actual: {:.7g} V".format(voltage))
            self.status.configure(text="{} · AUX = {:.7g} V".format(identity,voltage))
        elif operation=="acquire":
            self.unsaved = (self.pending,result)
            self.save_pending()
        elif operation=="manual_icl_acquire":
            batch, self.pending_instrument = result
            self.unsaved = (self.pending, batch)
            self.save_pending()
        elif operation=="save":
            self.saved_pending()
        elif operation=="analyze":
            self.submit("export",lambda:export_plots(self.store))
        elif operation=="export":
            if self.empirical.get():
                fits = draw_slits(self.slit_ax,self.store.scans,True)
                atomic_text(self.store.path/"empirical_fits.json",json.dumps(fits,ensure_ascii=False,indent=2))
            self.status.configure(text="CSV y PNG exportados en "+str(self.store.path))
        if operation not in ("test","export","auto_close","horiba_initialize","horiba_status","horiba_close","horiba_slits","horiba_slits_move","horiba_output","horiba_output_move","horiba_gratings","horiba_grating_move") and not self.awaiting:
            self.status.configure(text="Listo · "+operation)
        self.refresh()

    def import_grating_metadata(self, info, reset_manual=False):
        density = info.get("grooves_per_mm")
        if density or reset_manual:
            self.vars["grooves_per_mm"].set(density if density else "")
        # No hereda un blaze manual de otra red; ICL no documenta aquí sus unidades.
        if reset_manual:
            self.vars["blaze_nm"].set("")

    def save_pending(self):
        if not self.unsaved:
            return
        if self.awaiting:
            raise ValueError("Espere a que termine la operacion actual.")
        (scan_id,kind,wavelength,step),batch = self.unsaved
        store = self.store
        instrument = copy.deepcopy(self.pending_instrument)
        def change(scan):
            if kind=="background":
                scan.set_background(batch)
            else:
                scan.add_point(wavelength,batch,replace_last=kind=="repeat",instrument=instrument)
        self.worker.submit("save",lambda:store.update_scan(scan_id,change))
        self.awaiting = True
        self.status.configure(text="Guardando punto y lecturas...")
        self.refresh_controls()

    def saved_pending(self):
        (scan_id,kind,wavelength,step),batch = self.unsaved
        self.unsaved = None
        if self.automatic:
            self.automatic["index"] += 1
            self.vars["wavelength_nm"].set(format(wavelength, ".12g"))
            self.refresh()
            self.next_automatic_point()
            return
        if kind!="background":
            self.next_label.configure(text="Siguiente sugerida: {:.9g} nm. Ajuste el HORIBA manualmente.".format(wavelength+step))
            self.vars["wavelength_nm"].set(format(wavelength+step,".12g"))
        self.refresh()

    def refresh_controls(self):
        for b in self.buttons:
            b.configure(state="disabled" if self.awaiting else "normal")
        self.cancel_button.configure(state="normal" if self.awaiting else "disabled")
        self.retry_button.configure(state="normal" if self.unsaved and not self.awaiting else "disabled")
        for cb in self.view_checks:
            cb.configure(state="disabled" if self.awaiting else "normal")
        self.scan_selector.configure(state="disabled" if self.awaiting else "readonly")
        locked = self.scan is not None
        for entry,combo in self.mount_entries:
            entry.configure(state="disabled" if locked else ("readonly" if combo else "normal"))
        self.notes.configure(state="disabled" if locked else "normal")
        if not self.connected:
            self.connection_label.configure(text="Desconectado · simulación o SR830 en Configurar / conectar…")
        self.refresh_horiba_status()

    def refresh(self):
        labels = ["{} | {} | entrada/salida={}/{} mm{}".format(s.id,s.experiment.grating,s.experiment.slit_in_mm,s.experiment.slit_out_mm," - finalizado" if s.completed else " - en curso") for s in self.store.scans] if self.store else []
        self.scan_selector.configure(values=labels)
        self.scan_selector.set(next((label for label in labels if label.startswith(self.scan_id+" | ")),"Nuevo barrido") if self.scan_id else "Nuevo barrido")
        self.refresh_controls()
        if self.store:
            self.session_label.configure(text=str(self.store.path),wraplength=600)
        if self.scan:
            bg = self.scan.background
            self.background_label.configure(text="Fondo: {:.7g} V · σ {:.3g} V · {} lecturas".format(bg.mean_v,bg.std_v,len(bg.values_v)) if bg else "Fondo: sin medir")
            self.table.delete(*self.table.get_children())
            for p in self.scan.active_points:
                self.table.insert("","end",iid=p.id,values=(format(p.wavelength_nm,".9g"),format(p.batch.mean_v,".7g"),format(p.batch.std_v,".3g"),format(p.batch.mean_v-self.scan.background_v,".7g"),len(p.batch.values_v)))
            a = self.scan.analysis
            if a:
                w,u = a.get("fwhm_nm"),a.get("fwhm_uncertainty_nm")
                text = "FWHM: "+("{:.6g} nm".format(w) if w is not None else "no determinable")
                if u is not None:
                    text += " · u estimada {:.3g} nm".format(u)
                if a.get("peak_nm") is not None:
                    text += "\nMáximo: {:.6g} V en {:.9g} nm · asimetría {}".format(a["peak_v"],a["peak_nm"],a.get("asymmetry"))
                text += "\n"+"\n".join("• "+s for s in a["warnings"])
            else:
                text = "Barrido en curso: {} puntos activos. Finalice para calcular FWHM.\nDatos y lecturas individuales se guardan después de cada punto.".format(len(self.scan.active_points))
        else:
            self.table.delete(*self.table.get_children())
            self.background_label.configure(text="Fondo: sin medir")
            text = "Configure el montaje y conecte el lector. Ajuste el HORIBA e ingrese su λ antes de cada adquisición.\nEn simulación, configure FWHM y ruido en la conexión."
        self.report.configure(state="normal")
        self.report.delete("1.0","end")
        self.report.insert("1.0",text)
        self.report.configure(state="disabled")
        self.redraw()

    def draw_slit_sweep(self):
        """Curva de la configuración actual en color; las anteriores en gris, sin leyenda."""
        ax = self.profile_ax
        ax.clear()
        live = self.sweep_live
        normalize = self.normalize.get()
        for index in sorted(live["curves"]):
            curve = live["curves"][index]
            values, errors = curve["v"], curve["std"]
            if normalize and values and max(values) > 0:
                peak = max(values)
                values, errors = [v/peak for v in values], [e/peak for e in errors]
            current = index == live["current"]
            style = dict(color="C0", lw=1.6, marker="o", ms=3, zorder=3) if current else dict(color="0.7", lw=1, marker=".", ms=2, zorder=1)
            if current:
                style["label"] = "Barrido actual · entrada {:g} mm / salida {:g} mm".format(curve["slit_in_mm"], curve["slit_out_mm"])
            if current and self.errors.get():
                ax.errorbar(curve["nm"], values, yerr=errors, capsize=2, **style)
            else:
                ax.plot(curve["nm"], values, **style)
        ax.set_xlabel("λ [nm]")
        ax.set_ylabel("Señal normalizada" if normalize else "Tensión AUX [V]")
        ax.set_title("Barrido en apertura de rendijas")
        ax.grid(True, alpha=.3)
        if live["current"] is not None:
            ax.legend(loc="best", fontsize=8)
        self.profile_canvas.draw_idle()

    def redraw(self):
        if self.sweep_live is not None and self.scan is None:
            self.draw_slit_sweep()
            draw_slits(self.slit_ax,self.store.scans if self.store else [],self.empirical.get())
            self.slit_canvas.draw_idle()
            return
        scans = []
        if self.scan:
            scans = [self.scan]
            if self.overlay.get():
                exp = self.scan.experiment
                scans = [s for s in self.store.scans if s.experiment.grating==exp.grating and s.experiment.laser_nominal_nm==exp.laser_nominal_nm]
        draw_profiles(self.profile_ax,scans,self.corrected.get(),self.normalize.get(),self.errors.get(),self.raw_also.get())
        self.profile_canvas.draw_idle()
        fits = draw_slits(self.slit_ax,self.store.scans if self.store else [],self.empirical.get())
        self.fit_label.configure(text="\n".join(str(f) for f in fits) if fits else "Agrupa por grating y láser nominal. Ajuste empírico: al menos 3 aperturas distintas.")
        self.slit_canvas.draw_idle()

    def show_readings(self,event=None):
        selected = self.table.selection()
        if not selected or not self.scan:
            return
        point = next(p for p in self.scan.points if p.id==selected[0])
        self.readings.configure(state="normal")
        self.readings.delete("1.0","end")
        self.readings.insert("1.0","Lecturas [V]: "+", ".join(format(v,".8g") for v in point.batch.values_v))
        self.readings.configure(state="disabled")

    def _destroy(self):
        self.root.after_cancel(self._poll_id)
        if self.reader:
            self.reader.close()
        if self.monochromator:
            self.monochromator.close()
        if self.horiba_controller:
            # El cierre normal de la sesión ocurre en close(), fuera de Tk.
            self.horiba_controller.drop()
        if self.store:
            self.store.close()
        self.root.destroy()

    def close(self):
        if self.awaiting:
            if messagebox.askyesno("Cerrar","¿Cancelar y cerrar al finalizar la operación actual?\nUna consulta en curso puede tardar hasta el timeout.",parent=self.root):
                self.closing = True
                self.cancel_acquisition()
            return
        if self.unsaved:
            self.safe(self.save_pending)
            if self.unsaved:
                return
        if self.horiba_controller and self.horiba_controller.socket is not None:
            self.closing = True
            self.submit("horiba_close", self.horiba_controller.close)
            return
        self._destroy()


def launch(data_root=DEFAULT_ROOT,session=None):
    root = tk.Tk()
    app = CalibrationApp(root,data_root)
    if session:
        app.safe(lambda:app.open_session(session))
    root.mainloop()
