"""Minimal local desktop interface. All Tk calls stay on the main thread."""

from __future__ import annotations

import gc
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from queue import Empty
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from typing import cast

from autoshorts.automation_config import AutomationConfig, load_automation_config
from autoshorts.automation_events import AutomationEvent
from autoshorts.automation_jobs import AutomationController
from autoshorts.automation_pipeline import AutomationRequest
from autoshorts.config import OverwritePolicy, Settings, load_settings
from autoshorts.fake_analysis import fake_analysis_provider
from autoshorts.gui_jobs import JobController, JobRequest
from autoshorts.pipeline import PipelineResult
from autoshorts.render_events import RenderEvent
from autoshorts.render_plan import RenderPlan

LABELS = {
    "rendering": "Renderizando",
    "validating": "Conferindo vídeo",
    "generated": "Pronto",
    "skipped": "Reutilizado",
}
STAGE_PENDING = "○ pendente"
STAGE_RUNNING = "► executando"
STAGE_DONE = "✓ concluído"
STAGE_ERROR = "✕ erro"
POLICIES = {
    "Proteger existentes": "error",
    "Reutilizar válidos": "skip",
    "Substituir existentes": "replace",
}


class _StatusSnapshot:
    """Read-only status retained for callers after Tk teardown."""

    def __init__(self, value: str) -> None:
        self._value = value

    def get(self) -> str:
        return self._value


BG = "#0b1220"
PANEL = "#111827"
FIELD = "#182235"
FIELD_ACTIVE = "#22304a"
TEXT = "#e5e7eb"
MUTED = "#aab7c8"
ACCENT = "#60a5fa"


def _download_detail(event: AutomationEvent) -> str:
    parts: list[str] = []
    if event.downloaded_bytes is not None:
        downloaded = event.downloaded_bytes / 1024 / 1024
        if event.total_bytes is not None:
            total = event.total_bytes / 1024 / 1024
            suffix = " estimados" if event.total_is_estimate else ""
            parts.append(f"{downloaded:.1f}/{total:.1f} MB{suffix}")
        else:
            parts.append(f"{downloaded:.1f} MB")
    if event.speed:
        parts.append(f"{event.speed / 1024 / 1024:.1f} MB/s")
    if event.eta is not None:
        minutes, seconds = divmod(event.eta, 60)
        parts.append(f"ETA {minutes:02d}:{seconds:02d}")
    return " | ".join(parts) or "tamanho desconhecido"


class AutoShortsWindow:
    def __init__(self, root: tk.Tk, settings: Settings) -> None:
        self.root = root
        self.controller = JobController(settings)
        self.automation_controller = AutomationController(settings)
        self.automation_config: AutomationConfig | None = None
        self.automation_running = False
        self.automation_finishing = False
        self.automation_rows: dict[tuple[str, str], str] = {}
        self.automation_found = 0
        self.automation_completed = 0
        self.running = False
        self.closing = False
        self.finishing = False
        self.output_folder: Path | None = None
        self.fractions: dict[int, float] = {}
        self.root.title("AutoShorts — Pipeline Monitor")
        self.root.geometry("1040x860")
        self.root.minsize(820, 680)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self._configure_style()
        notebook = ttk.Notebook(root)
        notebook.pack(fill="both", expand=True)
        frame = ttk.Frame(notebook, padding=16)
        automatic = ttk.Frame(notebook, padding=16)
        notebook.add(frame, text="Manual")
        notebook.add(automatic, text="Automático")
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(9, weight=1)
        frame.rowconfigure(10, weight=1)
        frame.rowconfigure(12, weight=3)
        ttk.Label(frame, text="AutoShorts", style="Title.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 10)
        )
        self.controls: list[ttk.Widget] = []
        self.source = tk.StringVar(master=root)
        self.output = tk.StringVar(master=root, value=str(settings.output_dir))
        self._path_row(frame, 1, "Vídeo", self.source, self.choose_source)
        self._path_row(frame, 2, "Pasta de saída", self.output, self.choose_output)
        bar = ttk.Frame(frame)
        bar.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(12, 4))
        ttk.Label(bar, text="Cortes", style="Section.TLabel").pack(side="left")
        button = ttk.Button(bar, text="Abrir arquivo...", command=self.open_cuts)
        button.pack(side="right", padx=3)
        self.controls.append(button)
        self.cuts = ScrolledText(
            frame,
            height=7,
            wrap="none",
            undo=True,
            background=FIELD,
            foreground=TEXT,
            insertbackground=TEXT,
            selectbackground="#2563eb",
            selectforeground="#ffffff",
            borderwidth=0,
            highlightthickness=1,
            highlightbackground="#334155",
            highlightcolor=ACCENT,
            font=("TkFixedFont", 10),
        )
        self.cuts.grid(row=4, column=0, columnspan=3, sticky="nsew")
        self.cuts.insert("1.0", "[00:00 - 00:05] - Primeiro corte\n")
        ttk.Label(
            frame,
            text=f"Formato: [MM:SS - MM:SS] - Título | "
            f"Extensão: +{settings.extra_clip_seconds}s | "
            f"Máximo: {settings.maximum_clip_duration}s | "
            f"Saída: {settings.output_width}×{settings.output_height}",
        ).grid(row=5, column=0, columnspan=3, sticky="w", pady=4)
        options = ttk.Frame(frame)
        options.grid(row=6, column=0, columnspan=3, sticky="ew", pady=8)
        self.policy = ttk.Combobox(
            options, values=list(POLICIES), state="readonly", width=24
        )
        self.policy.set("Proteger existentes")
        self.policy.pack(side="left")
        ttk.Label(options, text="  Processos:").pack(side="left")
        self.workers = ttk.Combobox(
            options, values=[1, 2, 3, 4], state="readonly", width=3
        )
        self.workers.set("1")
        self.workers.pack(side="left")
        self.validate_button = ttk.Button(
            options, text="Validar (desenvolvimento)", command=lambda: self.start(True)
        )
        self.run_button = ttk.Button(
            options, text="Executar", command=lambda: self.start(False)
        )
        self.cancel_button = ttk.Button(
            options, text="Cancelar", command=self.cancel, state="disabled"
        )
        for button in (self.validate_button, self.run_button, self.cancel_button):
            button.pack(side="left", padx=5)
        self.controls.extend([self.validate_button, self.run_button])
        progress_frame = ttk.Frame(frame)
        progress_frame.grid(row=7, column=0, columnspan=3, sticky="ew")
        self.status = tk.StringVar(
            master=root, value="Selecione um vídeo e confira os cortes."
        )
        ttk.Label(progress_frame, textvariable=self.status).pack(anchor="w")
        self.progress = ttk.Progressbar(
            progress_frame, maximum=100, style="Accent.Horizontal.TProgressbar"
        )
        self.progress.pack(fill="x", pady=6)
        ttk.Label(frame, text="Pipeline / checkpoints", style="Section.TLabel").grid(
            row=8, column=0, columnspan=3, sticky="w", pady=(10, 3)
        )
        self.pipeline = ttk.Treeview(
            frame, columns=("stage", "state", "detail"), show="headings", height=7
        )
        for name, label, width in (
            ("stage", "Etapa", 180),
            ("state", "Estado", 150),
            ("detail", "Detalhe", 600),
        ):
            self.pipeline.heading(name, text=label)
            self.pipeline.column(name, width=width, stretch=name == "detail")
        self.pipeline.grid(row=9, column=0, columnspan=3, sticky="nsew", pady=(0, 8))
        for stage in (
            "Configuração",
            "Entrada",
            "FFprobe",
            "Cortes",
            "Planejamento",
            "Renderização",
            "Manifest",
            "Finalização",
        ):
            self.pipeline.insert(
                "", "end", iid=stage, values=(stage, STAGE_PENDING, "")
            )

        table = ttk.Frame(frame)
        table.grid(row=10, column=0, columnspan=3, sticky="nsew", pady=6)
        self.tree = ttk.Treeview(
            table,
            columns=("title", "duration", "status", "pct"),
            show="headings",
            height=6,
        )
        for name, label, width in (
            ("title", "Clipe", 350),
            ("duration", "Duração", 80),
            ("status", "Estado", 150),
            ("pct", "Progresso", 90),
        ):
            self.tree.heading(name, text=label)
            self.tree.column(name, width=width, stretch=name == "title")
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        table.rowconfigure(0, weight=1)
        table.columnconfigure(0, weight=1)
        ttk.Label(frame, text="Terminal / logs", style="Section.TLabel").grid(
            row=11, column=0, columnspan=3, sticky="w", pady=(8, 3)
        )
        self.logs = ScrolledText(
            frame,
            height=5,
            state="disabled",
            wrap="word",
            background="#0a101c",
            foreground="#cbd5e1",
            insertbackground="#cbd5e1",
            font=("TkFixedFont", 10),
        )
        self.logs.grid(row=12, column=0, columnspan=3, sticky="nsew")
        self.folder_button = ttk.Button(
            frame, text="Abrir pasta gerada", command=self.open_output, state="disabled"
        )
        self.folder_button.grid(row=13, column=0, columnspan=3, sticky="e", pady=(8, 0))
        self._build_automatic_tab(automatic, settings)
        self.poll_id = self.root.after(75, self.poll)

    def _build_automatic_tab(self, frame: ttk.Frame, settings: Settings) -> None:
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(9, weight=1)
        frame.rowconfigure(12, weight=2)
        ttk.Label(frame, text="Automação", style="Title.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 12)
        )
        default_config = (
            Path(settings.output_dir).parents[1] / "config" / "automation.example.json"
        )
        project_config = (
            Path(__file__).resolve().parents[2] / "config" / "automation.example.json"
        )
        self.auto_config_path = tk.StringVar(
            master=self.root,
            value=str(project_config if project_config.exists() else default_config),
        )
        self.auto_workspace = tk.StringVar(
            master=self.root, value=str(Path(settings.output_dir).parent / "automation")
        )
        self.auto_theme = tk.StringVar(master=self.root)
        self.auto_quantity = tk.IntVar(master=self.root, value=1)
        self.auto_age = tk.IntVar(master=self.root, value=3650)
        self.auto_download_only = tk.BooleanVar(master=self.root, value=True)
        self.auto_fake = tk.BooleanVar(master=self.root, value=False)
        self.auto_status = tk.StringVar(
            master=self.root, value="Carregue uma configuração."
        )
        self._automation_path_row(
            frame,
            1,
            "Configuração",
            self.auto_config_path,
            self.choose_automation_config,
        )
        ttk.Button(
            frame, text="Recarregar", command=self.load_automation_config_ui
        ).grid(row=2, column=2, sticky="e")
        ttk.Label(frame, text="Tema").grid(row=2, column=0, sticky="w")
        self.auto_theme_combo = ttk.Combobox(
            frame, textvariable=self.auto_theme, state="readonly"
        )
        self.auto_theme_combo.grid(row=2, column=1, sticky="ew", pady=4)
        self.auto_theme_combo.bind(
            "<<ComboboxSelected>>", lambda _event: self.refresh_creators()
        )
        ttk.Label(frame, text="Criadores").grid(row=3, column=0, sticky="nw", pady=4)
        self.auto_creators = tk.Listbox(
            frame,
            selectmode="multiple",
            exportselection=False,
            height=4,
            background=FIELD,
            foreground=TEXT,
            selectbackground="#2563eb",
            borderwidth=0,
        )
        self.auto_creators.grid(row=3, column=1, columnspan=2, sticky="nsew", pady=4)
        limits = ttk.Frame(frame)
        limits.grid(row=4, column=0, columnspan=3, sticky="ew", pady=5)
        ttk.Label(limits, text="Vídeos por criador:").pack(side="left")
        ttk.Spinbox(
            limits, from_=1, to=100, textvariable=self.auto_quantity, width=6
        ).pack(side="left", padx=(5, 18))
        ttk.Label(limits, text="Idade máxima (dias):").pack(side="left")
        ttk.Spinbox(limits, from_=0, to=3650, textvariable=self.auto_age, width=7).pack(
            side="left", padx=5
        )
        self._automation_path_row(
            frame, 5, "Workspace", self.auto_workspace, self.choose_automation_workspace
        )
        modes = ttk.Frame(frame)
        modes.grid(row=6, column=0, columnspan=3, sticky="ew", pady=6)
        ttk.Checkbutton(
            modes,
            text="Somente download",
            variable=self.auto_download_only,
            command=self._sync_analysis_mode,
        ).pack(side="left")
        self.fake_check = ttk.Checkbutton(
            modes,
            text="TEST / DEVELOPMENT ONLY — análise fake",
            variable=self.auto_fake,
        )
        self.fake_check.pack(side="left", padx=14)
        actions = ttk.Frame(frame)
        actions.grid(row=7, column=0, columnspan=3, sticky="ew", pady=8)
        ttk.Button(
            actions, text="Validar configuração", command=self.validate_automation
        ).pack(side="left")
        self.auto_run_button = ttk.Button(
            actions, text="Executar automação", command=self.start_automation
        )
        self.auto_run_button.pack(side="left", padx=6)
        self.auto_cancel_button = ttk.Button(
            actions, text="Cancelar", command=self.cancel_automation, state="disabled"
        )
        self.auto_cancel_button.pack(side="left")
        ttk.Button(
            actions, text="Abrir workspace", command=self.open_automation_workspace
        ).pack(side="right")
        ttk.Label(frame, textvariable=self.auto_status).grid(
            row=8, column=0, columnspan=3, sticky="w"
        )
        self.auto_progress = ttk.Progressbar(
            frame, maximum=100, style="Accent.Horizontal.TProgressbar"
        )
        self.auto_progress.grid(row=9, column=0, columnspan=3, sticky="ew", pady=(4, 8))
        self.auto_tree = ttk.Treeview(
            frame,
            columns=("creator", "video", "stage", "progress", "status"),
            show="headings",
            height=8,
        )
        for name, label, width in (
            ("creator", "Criador", 130),
            ("video", "Vídeo", 330),
            ("stage", "Etapa", 110),
            ("progress", "Progresso", 90),
            ("status", "Status", 170),
        ):
            self.auto_tree.heading(name, text=label)
            self.auto_tree.column(name, width=width, stretch=name == "video")
        self.auto_tree.grid(row=10, column=0, columnspan=3, sticky="nsew")
        ttk.Label(frame, text="Logs da automação", style="Section.TLabel").grid(
            row=11, column=0, columnspan=3, sticky="w", pady=(8, 3)
        )
        self.auto_logs = ScrolledText(
            frame,
            height=8,
            state="disabled",
            wrap="word",
            background="#0a101c",
            foreground="#cbd5e1",
            font=("TkFixedFont", 10),
        )
        self.auto_logs.grid(row=12, column=0, columnspan=3, sticky="nsew")
        self._sync_analysis_mode()
        self.load_automation_config_ui(silent=True)

    def _automation_path_row(self, parent, row, label, variable, command) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(parent, textvariable=variable).grid(
            row=row, column=1, sticky="ew", pady=4
        )
        ttk.Button(parent, text="Selecionar...", command=command).grid(
            row=row, column=2, padx=(8, 0)
        )

    def _sync_analysis_mode(self) -> None:
        self.fake_check.configure(
            state="disabled" if self.auto_download_only.get() else "normal"
        )

    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", background=BG, foreground=TEXT, borderwidth=0)
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Title.TLabel", font=("sans", 21, "bold"), foreground="#f8fafc")
        style.configure("Section.TLabel", font=("sans", 11, "bold"), foreground=ACCENT)
        style.configure(
            "TButton",
            padding=(10, 6),
            background="#1e293b",
            foreground=TEXT,
            bordercolor="#475569",
            lightcolor="#475569",
            darkcolor="#0f172a",
        )
        style.map(
            "TButton",
            background=[
                ("pressed", "#1d4ed8"),
                ("active", FIELD_ACTIVE),
                ("disabled", "#172033"),
            ],
            foreground=[("disabled", "#64748b"), ("active", "#ffffff")],
        )
        style.configure("Accent.TButton", background="#2563eb", foreground="white")
        style.configure(
            "TEntry",
            fieldbackground=FIELD,
            foreground=TEXT,
            insertcolor=TEXT,
            bordercolor="#334155",
            lightcolor="#334155",
            darkcolor="#0f172a",
            padding=6,
        )
        style.map(
            "TEntry",
            fieldbackground=[("disabled", "#121a2a")],
            foreground=[("disabled", MUTED)],
        )
        style.configure(
            "TCombobox",
            fieldbackground=FIELD,
            background=FIELD,
            foreground=TEXT,
            arrowcolor=TEXT,
            bordercolor="#334155",
            lightcolor="#334155",
            darkcolor="#0f172a",
            padding=4,
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", FIELD), ("disabled", "#121a2a")],
            foreground=[("readonly", TEXT), ("disabled", MUTED)],
            selectbackground=[("readonly", FIELD_ACTIVE)],
            selectforeground=[("readonly", TEXT)],
        )
        style.configure(
            "Accent.Horizontal.TProgressbar",
            troughcolor="#1f2937",
            background="#3b82f6",
            bordercolor="#1f2937",
            lightcolor="#60a5fa",
            darkcolor="#1d4ed8",
        )
        style.configure(
            "Treeview",
            background=PANEL,
            fieldbackground=PANEL,
            foreground=TEXT,
            rowheight=27,
        )
        style.configure(
            "Treeview.Heading",
            background="#263449",
            foreground="#f8fafc",
            padding=(6, 6),
        )
        style.map(
            "Treeview",
            background=[("selected", "#1d4ed8")],
            foreground=[("selected", "#ffffff")],
        )
        self.root.configure(background=BG)
        self.root.option_add("*TCombobox*Listbox.background", FIELD)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", "#2563eb")
        self.root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")

    def _path_row(self, parent, row, label, variable, command) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8))
        entry = ttk.Entry(parent, textvariable=variable)
        entry.grid(row=row, column=1, sticky="ew", pady=4)
        button = ttk.Button(parent, text="Selecionar...", command=command)
        button.grid(row=row, column=2, padx=(8, 0))
        self.controls.extend([entry, button])

    def choose_source(self) -> None:
        path = filedialog.askopenfilename(title="Selecionar vídeo")
        if path:
            self.source.set(path)

    def choose_automation_config(self) -> None:
        path = filedialog.askopenfilename(
            title="Configuração da automação",
            filetypes=[("JSON", "*.json"), ("Todos", "*")],
        )
        if path:
            self.auto_config_path.set(path)
            self.load_automation_config_ui()

    def choose_automation_workspace(self) -> None:
        path = filedialog.askdirectory(title="Workspace da automação", mustexist=False)
        if path:
            self.auto_workspace.set(path)

    def load_automation_config_ui(self, *, silent: bool = False) -> None:
        try:
            self.automation_config = load_automation_config(self.auto_config_path.get())
            names = [theme.name for theme in self.automation_config.themes]
            self.auto_theme_combo.configure(values=names)
            self.auto_theme.set(names[0])
            self.refresh_creators()
            self.auto_status.set(f"Configuração válida: {len(names)} tema(s).")
            if not silent:
                self.auto_log(f"Configuração carregada: {self.auto_config_path.get()}")
        except Exception as exc:
            self.automation_config = None
            self.auto_theme_combo.configure(values=[])
            self.auto_theme.set("")
            self.auto_creators.delete(0, "end")
            self.auto_status.set(f"Configuração inválida: {exc}")
            if not silent:
                messagebox.showerror("Configuração inválida", str(exc))

    def refresh_creators(self) -> None:
        self.auto_creators.delete(0, "end")
        if self.automation_config is None or not self.auto_theme.get():
            return
        theme = self.automation_config.theme(self.auto_theme.get())
        for creator in theme.creators:
            self.auto_creators.insert("end", creator.name)
        self.auto_creators.selection_set(0, "end")

    def validate_automation(self) -> None:
        self.load_automation_config_ui(silent=True)
        if self.automation_config is None:
            messagebox.showerror("Automação", self.auto_status.get())
            return
        try:
            self._automation_request()
        except (ValueError, tk.TclError) as exc:
            messagebox.showerror("Automação", str(exc))
            return
        self.auto_status.set("Configuração pronta para execução.")
        self.auto_log(f"Tema selecionado: {self.auto_theme.get()}")

    def _automation_request(self) -> AutomationRequest:
        selected = tuple(
            self.auto_creators.get(index) for index in self.auto_creators.curselection()
        )
        if not selected:
            raise ValueError("Selecione pelo menos um criador.")
        workspace = Path(self.auto_workspace.get()).expanduser()
        if workspace.exists() and not workspace.is_dir():
            raise ValueError("O workspace precisa ser uma pasta.")
        quantity = int(self.auto_quantity.get())
        age = int(self.auto_age.get())
        if quantity < 1:
            raise ValueError("A quantidade deve ser pelo menos 1.")
        if not 0 <= age <= 3650:
            raise ValueError("A idade máxima deve ficar entre 0 e 3650 dias.")
        return AutomationRequest(
            self.auto_theme.get(),
            quantity,
            age,
            workspace,
            download_only=self.auto_download_only.get(),
            selected_creators=selected,
        )

    def start_automation(self) -> None:
        if self.automation_controller.busy:
            return
        self.validate_automation()
        if self.automation_config is None:
            return
        try:
            request = self._automation_request()
        except (ValueError, tk.TclError) as exc:
            messagebox.showerror("Automação", str(exc))
            return
        analyze = None
        if not request.download_only:
            if not self.auto_fake.get():
                messagebox.showerror(
                    "Provider ausente",
                    "Ative explicitamente o provider TEST / DEVELOPMENT ONLY "
                    "ou use Somente download.",
                )
                return
            analyze = fake_analysis_provider
        self.automation_rows.clear()
        self.automation_found = 0
        self.automation_completed = 0
        for row in self.auto_tree.get_children():
            self.auto_tree.delete(row)
        self.auto_progress.configure(mode="determinate", value=0)
        self.automation_running = True
        self.auto_run_button.configure(state="disabled")
        self.auto_cancel_button.configure(state="normal")
        self.auto_status.set("Iniciando automação...")
        self.automation_controller.start(
            request, self.automation_config, analyze=analyze
        )

    def cancel_automation(self) -> None:
        self.automation_controller.cancel()
        self.auto_cancel_button.configure(state="disabled")
        self.auto_status.set("Cancelando automação...")
        self.auto_log("Cancelamento solicitado.")

    def auto_log(self, text: str) -> None:
        from datetime import datetime

        self.auto_logs.configure(state="normal")
        self.auto_logs.insert("end", f"[{datetime.now():%H:%M:%S}] {text}\n")
        self.auto_logs.see("end")
        self.auto_logs.configure(state="disabled")

    def open_automation_workspace(self) -> None:
        path = Path(self.auto_workspace.get()).expanduser()
        try:
            path.mkdir(parents=True, exist_ok=True)
            subprocess.Popen(["xdg-open", str(path)])
        except OSError as exc:
            messagebox.showerror("Não foi possível abrir o workspace", str(exc))

    def choose_output(self) -> None:
        path = filedialog.askdirectory(title="Pasta de saída", mustexist=False)
        if path:
            self.output.set(path)

    def open_cuts(self) -> None:
        path = filedialog.askopenfilename(
            title="Abrir cortes", filetypes=[("Texto", "*.txt"), ("Todos", "*")]
        )
        if path:
            try:
                text = Path(path).read_text(encoding="utf-8-sig")
            except (OSError, UnicodeError) as exc:
                messagebox.showerror("Não foi possível abrir", str(exc))
                return
            self.cuts.delete("1.0", "end")
            self.cuts.insert("1.0", text)

    def save_cuts(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Salvar cortes", defaultextension=".txt"
        )
        if path:
            try:
                Path(path).write_text(self.cuts.get("1.0", "end-1c"), encoding="utf-8")
            except OSError as exc:
                messagebox.showerror("Não foi possível salvar", str(exc))

    def set_busy(self, busy: bool) -> None:
        self.running = busy
        for control in self.controls:
            control.configure(state="disabled" if busy else "normal")
        for combo in (self.policy, self.workers):
            combo.configure(state="disabled" if busy else "readonly")
        self.cuts.configure(state="disabled" if busy else "normal")
        self.cancel_button.configure(state="normal" if busy else "disabled")

    def start(self, validate_only: bool) -> None:
        if self.running:
            return
        if not validate_only and self.policy.get() == "Substituir existentes":
            if not messagebox.askyesno(
                "Substituir arquivos",
                "Os clipes existentes com "
                "os mesmos nomes serão substituídos. Continuar?",
            ):
                return
        request = JobRequest(
            self.source.get(),
            self.cuts.get("1.0", "end-1c"),
            self.output.get(),
            cast(OverwritePolicy, POLICIES[self.policy.get()]),
            int(self.workers.get()),
        )
        self.output_folder = None
        self.folder_button.configure(state="disabled")
        self.progress["value"] = 0
        self.fractions.clear()
        for row in self.tree.get_children():
            self.tree.delete(row)
        self.set_busy(True)
        self.status.set("Validando vídeo e cortes...")
        self.log("Validando entradas. Nenhum arquivo é gerado na validação.")
        self.controller.start(request, validate_only=validate_only)

    def cancel(self) -> None:
        self.controller.cancel()
        self.cancel_button.configure(state="disabled")
        self.status.set("Cancelando... aguardando encerramento seguro.")
        self.log("Cancelamento solicitado. Clipes já prontos serão preservados.")

    def log(self, text: str) -> None:
        from datetime import datetime

        text = f"[{datetime.now():%H:%M:%S}] {text}"
        self.logs.configure(state="normal")
        self.logs.insert("end", text + "\n")
        self.logs.see("end")
        self.logs.configure(state="disabled")

    def handle(self, kind: str, payload: object) -> None:
        if kind == "plan":
            self._stage("Entrada", STAGE_DONE, "Caminho de entrada verificado")
            self._stage("FFprobe", STAGE_DONE, "Mídia inspecionada e válida")
            self._stage("Cortes", STAGE_DONE, "Lista de cortes analisada")
            self._stage("Planejamento", STAGE_DONE, "Plano de renderização criado")
            self._stage("Renderização", STAGE_RUNNING, "Aguardando clipes")
            plan = cast(RenderPlan, payload)
            for item in plan.clips:
                self.fractions[item.index] = 0
                self.tree.insert(
                    "",
                    "end",
                    iid=str(item.index),
                    values=(
                        item.clip.title,
                        f"{item.duration_seconds:.2f}s",
                        "Na fila",
                        "0%",
                    ),
                )
            self.log(f"Plano válido: {len(plan.clips)} clipe(s).")
            self.status.set("Processando...")
        elif kind == "progress":
            event = cast(RenderEvent, payload)
            self._stage(
                "Renderização", STAGE_RUNNING, f"Clipe {event.index} em processamento"
            )
            self.fractions[event.index] = event.progress
            values = list(self.tree.item(str(event.index), "values"))
            old = values[2]
            values[2:] = [
                LABELS.get(event.status, event.status),
                f"{event.progress:.0%}",
            ]
            self.tree.item(str(event.index), values=values)
            self.progress["value"] = (
                100 * sum(self.fractions.values()) / len(self.fractions)
            )
            if old != values[2]:
                self.log(f"Clipe {event.index}: {values[2]} - {values[0]}")
            if event.status in {"generated", "skipped"}:
                done = sum(value >= 1 for value in self.fractions.values())
                self._stage(
                    "Renderização",
                    STAGE_DONE if done == len(self.fractions) else STAGE_RUNNING,
                    f"{done}/{len(self.fractions)} clipe(s) concluído(s)",
                )
        elif kind == "validated":
            self._stage(
                "Manifest", STAGE_DONE, "Plano validado; nenhum vídeo renderizado"
            )
            self._stage("Finalização", STAGE_DONE, "Validação concluída")
            self.status.set(
                "Validação concluída. Confira o plano e clique em Executar."
            )
            self.log("Validação concluída; nenhum MP4 ou manifest foi criado.")
        elif kind == "completed":
            result = cast(PipelineResult, payload)
            self.output_folder = result.manifest_path.parent
            self.folder_button.configure(state="normal")
            self.progress["value"] = 100
            self.status.set(f"Sucesso: {len(result.renders)} clipe(s) pronto(s).")
            self.log(f"Concluído. Manifest: {result.manifest_path}")
            self._stage(
                "Renderização",
                STAGE_DONE,
                f"{len(result.renders)} clipe(s) processado(s)",
            )
            self._stage("Manifest", STAGE_DONE, str(result.manifest_path.name))
            self._stage("Finalização", STAGE_DONE, "Execução concluída")
        elif kind in {"failed", "cancelled"}:
            label = "Cancelado" if kind == "cancelled" else "Erro"
            self.status.set(f"{label}. Veja os detalhes abaixo.")
            self.log(f"{label}: {payload}")
            self._stage(
                "Renderização",
                STAGE_ERROR if kind == "failed" else "■ cancelado",
                str(payload),
            )
            manifest = getattr(payload, "manifest_path", None)
            if manifest is not None:
                self.output_folder = manifest.parent
                self.folder_button.configure(state="normal")
                self.log(f"Manifest: {manifest}")
            for row in self.tree.get_children():
                values = list(self.tree.item(row, "values"))
                if values[2] not in {"Pronto", "Reutilizado"}:
                    values[2] = "Cancelado" if kind == "cancelled" else "Não concluído"
                    self.tree.item(row, values=values)
        elif kind == "finished":
            self.finishing = True

    def _stage(self, stage: str, state: str, detail: str) -> None:
        if self.pipeline.exists(stage):
            self.pipeline.item(stage, values=(stage, state, detail))

    def handle_automation_event(self, event: AutomationEvent) -> None:
        labels = {
            "discovery_started": "Descobrindo",
            "download_started": "Baixando",
            "analysis_started": "Analisando",
            "render_started": "Renderizando",
            "automation_completed": "Concluído",
        }
        if event.kind == "candidate_found":
            self.automation_found += 1
            key = (event.creator or "", event.url or event.title or "")
            row = f"auto-{len(self.automation_rows) + 1}"
            self.automation_rows[key] = row
            self.auto_tree.insert(
                "",
                "end",
                iid=row,
                values=(event.creator, event.title, "Encontrado", "-", "Na fila"),
            )
            self.auto_log(f"Encontrado: {event.title}")
            return
        if event.kind == "discovery_started":
            self.auto_status.set(f"Descobrindo vídeos de {event.creator}...")
            self.auto_log(f"Descobrindo vídeos de {event.creator}")
            return
        if event.kind == "discovery_finished":
            self.auto_log(f"{event.creator}: {event.total or 0} vídeo(s) encontrado(s)")
            return
        key = (event.creator or "", event.url or event.title or "")
        row = self.automation_rows.get(key)
        if row is None and event.creator and event.title:
            for candidate_key, candidate_row in self.automation_rows.items():
                if candidate_key[0] == event.creator:
                    values = self.auto_tree.item(candidate_row, "values")
                    if values and values[1] == event.title:
                        row = candidate_row
                        break
        if event.kind == "download_progress":
            if event.progress is None:
                self.auto_progress.configure(mode="indeterminate")
                self.auto_progress.start(12)
                progress_text = "-"
            else:
                self.auto_progress.stop()
                self.auto_progress.configure(
                    mode="determinate", value=event.progress * 100
                )
                progress_text = f"{event.progress:.0%}"
            if row:
                values = list(self.auto_tree.item(row, "values"))
                values[2:] = ["Download", progress_text, "Baixando"]
                self.auto_tree.item(row, values=values)
            detail = _download_detail(event)
            self.auto_status.set(f"Baixando {event.title or ''} — {detail}")
            return
        if event.kind in labels:
            self.auto_status.set(labels[event.kind])
        if row:
            values = list(self.auto_tree.item(row, "values"))
            stage = event.kind.replace("_started", "").replace("_finished", "")
            status = (
                "Concluído"
                if event.kind.endswith("finished")
                else labels.get(event.kind, event.kind)
            )
            progress_text = "100%" if event.kind == "item_finished" else values[3]
            values[2:] = [stage.title(), progress_text, status]
            self.auto_tree.item(row, values=values)
        if event.kind == "item_finished":
            self.automation_completed += 1
            total = max(self.automation_found, 1)
            self.auto_progress.configure(
                mode="determinate", value=100 * self.automation_completed / total
            )
            self.auto_status.set(
                f"{self.automation_completed} / {self.automation_found} "
                "vídeos concluídos"
            )
        elif event.kind.endswith("_failed"):
            self.auto_log(f"Erro em {event.title or event.creator}: {event.message}")
        elif event.kind not in {"download_progress", "automation_started"}:
            subject = event.title or event.creator or ""
            self.auto_log(f"{labels.get(event.kind, event.kind)}: {subject}")

    def handle_automation_message(self, kind: str, payload: object) -> None:
        if kind == "event":
            self.handle_automation_event(cast(AutomationEvent, payload))
        elif kind == "completed":
            results = cast(tuple, payload)
            failures = sum(bool(getattr(item, "error", None)) for item in results)
            successes = len(results) - failures
            self.auto_status.set(
                f"Automação concluída: {successes} sucesso(s), {failures} falha(s)."
            )
            self.auto_log(self.auto_status.get())
        elif kind == "cancelled":
            self.auto_status.set("Automação cancelada.")
            self.auto_log(str(payload))
        elif kind == "failed":
            self.auto_status.set(f"Automação falhou: {payload}")
            self.auto_log(self.auto_status.get())
        elif kind == "finished":
            self.automation_finishing = True

    def poll(self) -> None:
        for _ in range(200):
            try:
                kind, payload = self.controller.messages.get_nowait()
            except Empty:
                break
            self.handle(kind, payload)
        for _ in range(300):
            try:
                kind, payload = self.automation_controller.messages.get_nowait()
            except Empty:
                break
            self.handle_automation_message(kind, payload)
        if self.finishing and not self.controller.busy:
            self.finishing = False
            self.set_busy(False)
        if self.automation_finishing and not self.automation_controller.busy:
            self.automation_finishing = False
            self.automation_running = False
            self.auto_run_button.configure(state="normal")
            self.auto_cancel_button.configure(state="disabled")
        if (
            self.closing
            and not self.running
            and not self.controller.busy
            and not self.automation_running
            and not self.automation_controller.busy
        ):
            self.destroy()
            return
        self.poll_id = self.root.after(75, self.poll)

    def close(self) -> None:
        if self.running or self.automation_running:
            if not messagebox.askyesno(
                "Fechar AutoShorts",
                "Cancelar o processamento e fechar após encerrar com segurança?",
            ):
                return
            self.closing = True
            if self.running:
                self.cancel()
            if self.automation_running:
                self.cancel_automation()
        else:
            self.destroy()

    def destroy(self) -> None:
        self.root.after_cancel(self.poll_id)
        status_value = self.status.get()
        auto_status_value = self.auto_status.get()
        self.source = None
        self.output = None
        self.status = None
        self.auto_config_path = None
        self.auto_workspace = None
        self.auto_theme = None
        self.auto_quantity = None
        self.auto_age = None
        self.auto_download_only = None
        self.auto_fake = None
        self.auto_status = None
        # Finalize Tk variables while Tcl is still alive, never from a later
        # worker/finalizer phase after root.destroy().
        gc.collect()
        self.status = _StatusSnapshot(status_value)
        self.auto_status = _StatusSnapshot(auto_status_value)
        self.root.destroy()

    def open_output(self) -> None:
        if self.output_folder is not None:
            try:
                subprocess.Popen(["xdg-open", str(self.output_folder)])
            except OSError as exc:
                messagebox.showerror("Não foi possível abrir a pasta", str(exc))


def main() -> int:
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"AutoShorts: abra numa sessão gráfica Linux. {exc}", file=sys.stderr)
        return 2
    try:
        settings = load_settings()
    except (OSError, ValueError, RuntimeError) as exc:
        root.withdraw()
        messagebox.showerror("Configuração inválida", str(exc))
        root.destroy()
        return 2
    AutoShortsWindow(root, settings)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
