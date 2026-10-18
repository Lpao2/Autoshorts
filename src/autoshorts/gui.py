"""Minimal local desktop interface. All Tk calls stay on the main thread."""

from __future__ import annotations

import gc
import importlib.util
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from queue import Empty
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from typing import cast

from autoshorts.automation_config import AutomationConfig, ThemePolicy
from autoshorts.automation_events import AutomationEvent
from autoshorts.automation_jobs import AutomationController
from autoshorts.automation_pipeline import AutomationRequest
from autoshorts.automation_store import (
    AutomationStore,
    default_automation_config_path,
    suggest_output_subdirectory,
)
from autoshorts.collection import YouTubeAccess, resolve_deno
from autoshorts.config import OverwritePolicy, Settings, load_settings
from autoshorts.fake_analysis import fake_analysis_provider
from autoshorts.faster_whisper_provider import FasterWhisperProvider
from autoshorts.gui_jobs import JobController, JobRequest
from autoshorts.pipeline import PipelineResult
from autoshorts.render_events import RenderEvent
from autoshorts.render_plan import RenderPlan
from autoshorts.state_store import default_state_db_path

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
YOUTUBE_AUTH = {
    "Sem autenticação": None,
    "Brave": "brave",
    "Firefox": "firefox",
}
SKIP_REASONS = {
    "requires_authentication": "Requer autenticação",
    "unavailable": "Indisponível",
    "too_old": "Muito antigo",
    "live": "Transmissão ao vivo",
    "upcoming": "Estreia futura",
    "metadata_error": "Erro de metadados",
    "already_processed": "Já processado anteriormente",
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


class _EntityDialog:
    def __init__(self, parent: tk.Tk, title: str) -> None:
        self.window = tk.Toplevel(parent)
        self.window.title(title)
        self.window.transient(parent)
        self.window.grab_set()
        self.window.resizable(True, False)
        self.body = ttk.Frame(self.window, padding=16)
        self.body.pack(fill="both", expand=True)
        self.body.columnconfigure(1, weight=1)
        self.result: tuple[str, ...] | None = None

    def actions(self, save_command) -> None:
        actions = ttk.Frame(self.body)
        actions.grid(row=20, column=0, columnspan=2, sticky="e", pady=(14, 0))
        ttk.Button(actions, text="Cancelar", command=self.window.destroy).pack(
            side="right"
        )
        ttk.Button(actions, text="Salvar", command=save_command).pack(
            side="right", padx=(0, 8)
        )
        self.window.protocol("WM_DELETE_WINDOW", self.window.destroy)

    def show(self) -> tuple[str, ...] | None:
        self.window.wait_visibility()
        self.window.focus_force()
        self.window.wait_window()
        return self.result


class _ThemeDialog(_EntityDialog):
    def __init__(
        self,
        parent,
        *,
        name="",
        output="",
        prompt="",
        enabled=True,
        videos_per_creator=1,
        maximum_age_days=3650,
    ):
        super().__init__(parent, "Tema")
        self.name = tk.StringVar(master=self.window, value=name)
        self.output = tk.StringVar(master=self.window, value=output)
        self.prompt = tk.StringVar(master=self.window, value=prompt)
        self.enabled = tk.BooleanVar(master=self.window, value=enabled)
        self.videos_per_creator = tk.IntVar(
            master=self.window, value=videos_per_creator
        )
        self.maximum_age_days = tk.IntVar(master=self.window, value=maximum_age_days)
        self._output_is_suggested = not bool(output)
        for row, (label, variable) in enumerate(
            (
                ("Nome", self.name),
                ("Pasta de saída", self.output),
                ("Prompt", self.prompt),
            )
        ):
            ttk.Label(self.body, text=label).grid(row=row, column=0, sticky="w", pady=4)
            entry = ttk.Entry(self.body, textvariable=variable, width=46)
            entry.grid(row=row, column=1, sticky="ew", pady=4)
            if variable is self.output:
                entry.bind(
                    "<KeyPress>",
                    lambda _event: setattr(self, "_output_is_suggested", False),
                )
        ttk.Checkbutton(self.body, text="Ativo", variable=self.enabled).grid(
            row=3, column=1, sticky="w", pady=4
        )
        policy = ttk.Frame(self.body)
        policy.grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Label(policy, text="Vídeos por criador").pack(side="left")
        ttk.Spinbox(
            policy, from_=1, to=100, textvariable=self.videos_per_creator, width=6
        ).pack(side="left", padx=(5, 14))
        ttk.Label(policy, text="Últimos dias").pack(side="left")
        ttk.Spinbox(
            policy, from_=0, to=3650, textvariable=self.maximum_age_days, width=7
        ).pack(side="left", padx=5)
        self.name.trace_add("write", self._suggest_output)
        self.actions(self._save)

    def _suggest_output(self, *_args) -> None:
        if self._output_is_suggested:
            self.output.set(suggest_output_subdirectory(self.name.get()))

    def _save(self) -> None:
        self.result = (
            self.name.get(),
            self.output.get(),
            self.prompt.get(),
            "1" if self.enabled.get() else "0",
            str(self.videos_per_creator.get()),
            str(self.maximum_age_days.get()),
        )
        self.window.destroy()


class _CreatorDialog(_EntityDialog):
    def __init__(self, parent, *, name="", videos_url="", enabled=True):
        super().__init__(parent, "Criador")
        self.name = tk.StringVar(master=self.window, value=name)
        self.videos_url = tk.StringVar(master=self.window, value=videos_url)
        self.enabled = tk.BooleanVar(master=self.window, value=enabled)
        for row, (label, variable) in enumerate(
            (("Nome", self.name), ("URL da página de vídeos", self.videos_url))
        ):
            ttk.Label(self.body, text=label).grid(row=row, column=0, sticky="w", pady=4)
            ttk.Entry(self.body, textvariable=variable, width=58).grid(
                row=row, column=1, sticky="ew", pady=4
            )
        ttk.Checkbutton(self.body, text="Ativo", variable=self.enabled).grid(
            row=2, column=1, sticky="w", pady=4
        )
        self.actions(self._save)

    def _save(self) -> None:
        self.result = (
            self.name.get(),
            self.videos_url.get(),
            "1" if self.enabled.get() else "0",
        )
        self.window.destroy()


class _AccountDialog(_EntityDialog):
    def __init__(self, parent, *, name="", email="", slot=1, enabled=True):
        super().__init__(parent, "Conta de destino")
        self.name = tk.StringVar(master=self.window, value=name)
        self.email = tk.StringVar(master=self.window, value=email)
        self.slot = tk.IntVar(master=self.window, value=slot)
        self.enabled = tk.BooleanVar(master=self.window, value=enabled)
        ttk.Label(self.body, text="Slot").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Combobox(
            self.body,
            textvariable=self.slot,
            values=(1, 2, 3),
            state="readonly",
            width=6,
        ).grid(row=0, column=1, sticky="w", pady=4)
        for row, (label, variable) in enumerate(
            (("Nome", self.name), ("Email identificador", self.email)), start=1
        ):
            ttk.Label(self.body, text=label).grid(row=row, column=0, sticky="w", pady=4)
            ttk.Entry(self.body, textvariable=variable, width=52).grid(
                row=row, column=1, sticky="ew", pady=4
            )
        ttk.Checkbutton(self.body, text="Ativa", variable=self.enabled).grid(
            row=3, column=1, sticky="w", pady=4
        )
        ttk.Label(
            self.body,
            text="O slot define a associação: Short 1→Conta 1, 2→2, 3→3.",
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.actions(self._save)

    def _save(self) -> None:
        self.result = (
            self.name.get(),
            self.email.get(),
            str(self.slot.get()),
            "1" if self.enabled.get() else "0",
        )
        self.window.destroy()


class AutoShortsWindow:
    def __init__(self, root: tk.Tk, settings: Settings) -> None:
        self.root = root
        self.controller = JobController(settings)
        self.automation_controller = AutomationController(
            settings, state_db_path=default_state_db_path()
        )
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
        frame.rowconfigure(11, weight=1)
        frame.rowconfigure(14, weight=2)
        ttk.Label(frame, text="Automação", style="Title.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 12)
        )
        project_root = Path(__file__).resolve().parents[2]
        project_config = default_automation_config_path(project_root)
        self.auto_config_path = tk.StringVar(
            master=self.root,
            value=str(project_config),
        )
        self.automation_store = AutomationStore(project_config)
        self.auto_workspace = tk.StringVar(
            master=self.root, value=str(Path(settings.output_dir).parent / "automation")
        )
        self.auto_theme = tk.StringVar(master=self.root)
        self.auto_quantity = tk.IntVar(master=self.root, value=1)
        self.auto_age = tk.IntVar(master=self.root, value=3650)
        self.auto_youtube_auth = tk.StringVar(
            master=self.root, value="Sem autenticação"
        )
        self.auto_download_only = tk.BooleanVar(master=self.root, value=True)
        self.auto_fake = tk.BooleanVar(master=self.root, value=False)
        self.auto_transcription = tk.BooleanVar(master=self.root, value=False)
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
        theme_row = ttk.Frame(frame)
        theme_row.grid(row=2, column=1, sticky="ew", pady=4)
        theme_row.columnconfigure(0, weight=1)
        self.auto_theme_combo = ttk.Combobox(
            theme_row, textvariable=self.auto_theme, state="readonly"
        )
        self.auto_theme_combo.grid(row=0, column=0, sticky="ew")
        self.auto_theme_combo.bind(
            "<<ComboboxSelected>>", lambda _event: self.refresh_theme_details()
        )
        ttk.Button(theme_row, text="Novo", command=self.add_theme_ui).grid(
            row=0, column=1, padx=(6, 0)
        )
        ttk.Button(theme_row, text="Editar", command=self.edit_theme_ui).grid(
            row=0, column=2, padx=(6, 0)
        )
        ttk.Button(theme_row, text="Excluir", command=self.remove_theme_ui).grid(
            row=0, column=3, padx=(6, 0)
        )
        ttk.Label(frame, text="Criadores").grid(row=3, column=0, sticky="nw", pady=4)
        creators_panel = ttk.Frame(frame)
        creators_panel.grid(row=3, column=1, columnspan=2, sticky="nsew", pady=4)
        creators_panel.columnconfigure(0, weight=1)
        creators_panel.rowconfigure(0, weight=1)
        self.auto_creators = ttk.Treeview(
            creators_panel,
            columns=("enabled", "name", "url"),
            show="headings",
            height=3,
            selectmode="extended",
        )
        for column, label, width in (
            ("enabled", "Ativo", 60),
            ("name", "Criador", 180),
            ("url", "URL", 430),
        ):
            self.auto_creators.heading(column, text=label)
            self.auto_creators.column(column, width=width, stretch=column == "url")
        self.auto_creators.grid(row=0, column=0, sticky="nsew")
        creator_actions = ttk.Frame(creators_panel)
        creator_actions.grid(row=1, column=0, sticky="w", pady=(5, 0))
        ttk.Button(creator_actions, text="Adicionar", command=self.add_creator_ui).pack(
            side="left"
        )
        ttk.Button(creator_actions, text="Editar", command=self.edit_creator_ui).pack(
            side="left", padx=6
        )
        ttk.Button(
            creator_actions, text="Remover", command=self.remove_creator_ui
        ).pack(side="left")
        ttk.Label(frame, text="Contas").grid(row=4, column=0, sticky="nw", pady=4)
        accounts_panel = ttk.Frame(frame)
        accounts_panel.grid(row=4, column=1, columnspan=2, sticky="nsew", pady=4)
        accounts_panel.columnconfigure(0, weight=1)
        self.auto_accounts = ttk.Treeview(
            accounts_panel,
            columns=("slot", "enabled", "name", "email"),
            show="headings",
            height=3,
            selectmode="browse",
        )
        for column, label, width in (
            ("slot", "Slot", 48),
            ("enabled", "Ativo", 55),
            ("name", "Conta", 180),
            ("email", "Email", 360),
        ):
            self.auto_accounts.heading(column, text=label)
            self.auto_accounts.column(column, width=width, stretch=column == "email")
        self.auto_accounts.grid(row=0, column=0, sticky="nsew")
        account_actions = ttk.Frame(accounts_panel)
        account_actions.grid(row=1, column=0, sticky="ew", pady=(5, 0))
        ttk.Button(account_actions, text="Adicionar", command=self.add_account_ui).pack(
            side="left"
        )
        ttk.Button(account_actions, text="Editar", command=self.edit_account_ui).pack(
            side="left", padx=6
        )
        ttk.Button(
            account_actions, text="Remover", command=self.remove_account_ui
        ).pack(side="left")
        ttk.Label(
            account_actions, text="Short 1→Slot 1  •  Short 2→Slot 2  •  Short 3→Slot 3"
        ).pack(side="right")
        limits = ttk.Frame(frame)
        limits.grid(row=5, column=0, columnspan=3, sticky="ew", pady=5)
        ttk.Label(limits, text="Vídeos por criador:").pack(side="left")
        ttk.Spinbox(
            limits, from_=1, to=100, textvariable=self.auto_quantity, width=6
        ).pack(side="left", padx=(5, 18))
        ttk.Label(limits, text="Idade máxima (dias):").pack(side="left")
        ttk.Spinbox(limits, from_=0, to=3650, textvariable=self.auto_age, width=7).pack(
            side="left", padx=5
        )
        ttk.Label(limits, text="Autenticação YouTube:").pack(side="left", padx=(18, 5))
        self.auto_auth_combo = ttk.Combobox(
            limits,
            textvariable=self.auto_youtube_auth,
            values=tuple(YOUTUBE_AUTH),
            state="readonly",
            width=18,
        )
        self.auto_auth_combo.pack(side="left")
        self._automation_path_row(
            frame, 6, "Workspace", self.auto_workspace, self.choose_automation_workspace
        )
        modes = ttk.Frame(frame)
        modes.grid(row=7, column=0, columnspan=3, sticky="ew", pady=6)
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
        transcription_available = importlib.util.find_spec("faster_whisper") is not None
        self.transcription_check = ttk.Checkbutton(
            modes,
            text="Transcrição local (Whisper)",
            variable=self.auto_transcription,
            state="normal" if transcription_available else "disabled",
        )
        self.transcription_check.pack(side="left")
        actions = ttk.Frame(frame)
        actions.grid(row=8, column=0, columnspan=3, sticky="ew", pady=8)
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
            row=9, column=0, columnspan=3, sticky="w"
        )
        self.auto_progress = ttk.Progressbar(
            frame, maximum=100, style="Accent.Horizontal.TProgressbar"
        )
        self.auto_progress.grid(
            row=10, column=0, columnspan=3, sticky="ew", pady=(4, 8)
        )
        self.auto_tree = ttk.Treeview(
            frame,
            columns=("creator", "video", "stage", "progress", "status"),
            show="headings",
            height=6,
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
        self.auto_tree.grid(row=11, column=0, columnspan=3, sticky="nsew")
        ttk.Label(frame, text="Logs da automação", style="Section.TLabel").grid(
            row=12, column=0, columnspan=3, sticky="w", pady=(8, 3)
        )
        self.auto_logs = ScrolledText(
            frame,
            height=5,
            state="disabled",
            wrap="word",
            background="#0a101c",
            foreground="#cbd5e1",
            font=("TkFixedFont", 10),
        )
        self.auto_logs.grid(row=14, column=0, columnspan=3, sticky="nsew")
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
        if importlib.util.find_spec("faster_whisper") is not None:
            self.transcription_check.configure(
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
            self.automation_store = AutomationStore(path)
            self.load_automation_config_ui()

    def choose_automation_workspace(self) -> None:
        path = filedialog.askdirectory(title="Workspace da automação", mustexist=False)
        if path:
            self.auto_workspace.set(path)

    def load_automation_config_ui(self, *, silent: bool = False) -> None:
        try:
            self.automation_store = AutomationStore(self.auto_config_path.get())
            self.automation_config = self.automation_store.load()
            labels = [
                theme.name if theme.enabled else f"{theme.name} (inativo)"
                for theme in self.automation_config.themes
            ]
            self.auto_theme_combo.configure(values=labels)
            current = self._current_theme_name()
            names = [theme.name for theme in self.automation_config.themes]
            selected = current if current in names else (names[0] if names else "")
            self._set_theme(selected)
            self.refresh_theme_details()
            self.auto_status.set(f"Configuração pronta: {len(names)} tema(s).")
            if not silent:
                self.auto_log(f"Configuração carregada: {self.auto_config_path.get()}")
        except Exception as exc:
            self.automation_config = None
            self.auto_theme_combo.configure(values=[])
            self.auto_theme.set("")
            for tree in (self.auto_creators, self.auto_accounts):
                for row in tree.get_children():
                    tree.delete(row)
            self.auto_status.set(f"Configuração inválida: {exc}")
            if not silent:
                messagebox.showerror("Configuração inválida", str(exc))

    def refresh_creators(self) -> None:
        for row in self.auto_creators.get_children():
            self.auto_creators.delete(row)
        if self.automation_config is None or not self.auto_theme.get():
            return
        theme = self.automation_config.theme(self._current_theme_name())
        for index, creator in enumerate(theme.creators):
            iid = f"creator-{index}"
            self.auto_creators.insert(
                "",
                "end",
                iid=iid,
                values=(
                    "✓" if creator.enabled else "—",
                    creator.name,
                    creator.videos_url,
                ),
                tags=("enabled" if creator.enabled else "disabled",),
            )
            if creator.enabled:
                self.auto_creators.selection_add(iid)
        self.auto_creators.tag_configure("disabled", foreground=MUTED)

    def refresh_accounts(self) -> None:
        for row in self.auto_accounts.get_children():
            self.auto_accounts.delete(row)
        if self.automation_config is None or not self.auto_theme.get():
            return
        theme = self.automation_config.theme(self._current_theme_name())
        for account in sorted(theme.accounts, key=lambda item: item.slot):
            self.auto_accounts.insert(
                "",
                "end",
                iid=f"account-{account.slot}-{account.name}",
                values=(
                    account.slot,
                    "✓" if account.enabled else "—",
                    account.name,
                    account.email,
                ),
                tags=("enabled" if account.enabled else "disabled",),
            )
        self.auto_accounts.tag_configure("disabled", foreground=MUTED)

    def refresh_theme_details(self) -> None:
        self.refresh_creators()
        self.refresh_accounts()
        if self.automation_config is None or not self._current_theme_name():
            return
        policy = self.automation_config.theme(self._current_theme_name()).policy
        self.auto_quantity.set(policy.videos_per_creator)
        self.auto_age.set(policy.maximum_age_days)

    def _current_theme_name(self) -> str:
        return self.auto_theme.get().removesuffix(" (inativo)")

    def _set_theme(self, name: str) -> None:
        if not name or self.automation_config is None:
            self.auto_theme.set("")
            return
        theme = self.automation_config.theme(name)
        self.auto_theme.set(name if theme.enabled else f"{name} (inativo)")

    def add_theme_ui(self) -> None:
        result = _ThemeDialog(self.root).show()
        if not result:
            return
        name, output, prompt, enabled, quantity, age = result
        try:
            config = self.automation_config or self.automation_store.load()
            self.automation_config = self.automation_store.add_theme(
                config,
                name=name,
                output_subdirectory=output,
                prompt=prompt,
                enabled=enabled == "1",
                policy=ThemePolicy(int(quantity), int(age)),
            )
        except (OSError, ValueError) as exc:
            messagebox.showerror("Tema inválido", str(exc))
            return
        self.load_automation_config_ui(silent=True)
        self._set_theme(name.strip())
        self.refresh_theme_details()

    def edit_theme_ui(self) -> None:
        if self.automation_config is None or not self._current_theme_name():
            messagebox.showinfo("Tema", "Selecione um tema para editar.")
            return
        original = self._current_theme_name()
        theme = self.automation_config.theme(original)
        result = _ThemeDialog(
            self.root,
            name=theme.name,
            output=theme.output_subdirectory,
            prompt=theme.prompt,
            enabled=theme.enabled,
            videos_per_creator=theme.policy.videos_per_creator,
            maximum_age_days=theme.policy.maximum_age_days,
        ).show()
        if not result:
            return
        name, output, prompt, enabled, quantity, age = result
        try:
            self.automation_config = self.automation_store.edit_theme(
                self.automation_config,
                original,
                name=name,
                output_subdirectory=output,
                prompt=prompt,
                enabled=enabled == "1",
                policy=ThemePolicy(int(quantity), int(age)),
            )
        except (OSError, ValueError) as exc:
            messagebox.showerror("Tema inválido", str(exc))
            return
        self.load_automation_config_ui(silent=True)
        self._set_theme(name.strip())
        self.refresh_theme_details()

    def remove_theme_ui(self) -> None:
        name = self._current_theme_name()
        if self.automation_config is None or not name:
            return
        if not messagebox.askyesno("Excluir tema", f"Excluir o tema '{name}'?"):
            return
        self.automation_config = self.automation_store.remove_theme(
            self.automation_config, name
        )
        self.load_automation_config_ui(silent=True)

    def _selected_creator_name(self) -> str | None:
        selected = self.auto_creators.selection()
        if not selected:
            return None
        values = self.auto_creators.item(selected[0], "values")
        return str(values[1]) if values else None

    def add_creator_ui(self) -> None:
        theme_name = self._current_theme_name()
        if self.automation_config is None or not theme_name:
            messagebox.showinfo("Criador", "Crie ou selecione um tema primeiro.")
            return
        result = _CreatorDialog(self.root).show()
        if not result:
            return
        name, url, enabled = result
        try:
            self.automation_config = self.automation_store.add_creator(
                self.automation_config,
                theme_name,
                name=name,
                videos_url=url,
                enabled=enabled == "1",
            )
        except (OSError, ValueError) as exc:
            messagebox.showerror("Criador inválido", str(exc))
            return
        self.load_automation_config_ui(silent=True)
        self._set_theme(theme_name)
        self.refresh_theme_details()

    def edit_creator_ui(self) -> None:
        theme_name = self._current_theme_name()
        creator_name = self._selected_creator_name()
        if self.automation_config is None or not theme_name or not creator_name:
            messagebox.showinfo("Criador", "Selecione um criador para editar.")
            return
        theme = self.automation_config.theme(theme_name)
        creator = next(item for item in theme.creators if item.name == creator_name)
        result = _CreatorDialog(
            self.root,
            name=creator.name,
            videos_url=creator.videos_url,
            enabled=creator.enabled,
        ).show()
        if not result:
            return
        name, url, enabled = result
        try:
            self.automation_config = self.automation_store.edit_creator(
                self.automation_config,
                theme_name,
                creator_name,
                name=name,
                videos_url=url,
                enabled=enabled == "1",
            )
        except (OSError, ValueError) as exc:
            messagebox.showerror("Criador inválido", str(exc))
            return
        self.load_automation_config_ui(silent=True)
        self._set_theme(theme_name)
        self.refresh_theme_details()

    def remove_creator_ui(self) -> None:
        theme_name = self._current_theme_name()
        creator_name = self._selected_creator_name()
        if self.automation_config is None or not theme_name or not creator_name:
            return
        if not messagebox.askyesno(
            "Remover criador", f"Remover '{creator_name}' do tema '{theme_name}'?"
        ):
            return
        self.automation_config = self.automation_store.remove_creator(
            self.automation_config, theme_name, creator_name
        )
        self.load_automation_config_ui(silent=True)
        self._set_theme(theme_name)
        self.refresh_theme_details()

    def _selected_account_name(self) -> str | None:
        selected = self.auto_accounts.selection()
        if not selected:
            return None
        values = self.auto_accounts.item(selected[0], "values")
        return str(values[2]) if values else None

    def add_account_ui(self) -> None:
        theme_name = self._current_theme_name()
        if self.automation_config is None or not theme_name:
            messagebox.showinfo("Conta", "Crie ou selecione um tema primeiro.")
            return
        result = _AccountDialog(self.root).show()
        if not result:
            return
        name, email, slot, enabled = result
        try:
            self.automation_config = self.automation_store.add_account(
                self.automation_config,
                theme_name,
                name=name,
                email=email,
                slot=int(slot),
                enabled=enabled == "1",
            )
        except (OSError, ValueError) as exc:
            messagebox.showerror("Conta inválida", str(exc))
            return
        self.load_automation_config_ui(silent=True)
        self._set_theme(theme_name)
        self.refresh_theme_details()

    def edit_account_ui(self) -> None:
        theme_name = self._current_theme_name()
        account_name = self._selected_account_name()
        if self.automation_config is None or not theme_name or not account_name:
            messagebox.showinfo("Conta", "Selecione uma conta para editar.")
            return
        theme = self.automation_config.theme(theme_name)
        account = next(item for item in theme.accounts if item.name == account_name)
        result = _AccountDialog(
            self.root,
            name=account.name,
            email=account.email,
            slot=account.slot,
            enabled=account.enabled,
        ).show()
        if not result:
            return
        name, email, slot, enabled = result
        try:
            self.automation_config = self.automation_store.edit_account(
                self.automation_config,
                theme_name,
                account_name,
                name=name,
                email=email,
                slot=int(slot),
                enabled=enabled == "1",
            )
        except (OSError, ValueError) as exc:
            messagebox.showerror("Conta inválida", str(exc))
            return
        self.load_automation_config_ui(silent=True)
        self._set_theme(theme_name)
        self.refresh_theme_details()

    def remove_account_ui(self) -> None:
        theme_name = self._current_theme_name()
        account_name = self._selected_account_name()
        if self.automation_config is None or not theme_name or not account_name:
            return
        if not messagebox.askyesno(
            "Remover conta", f"Remover '{account_name}' do tema '{theme_name}'?"
        ):
            return
        self.automation_config = self.automation_store.remove_account(
            self.automation_config, theme_name, account_name
        )
        self.load_automation_config_ui(silent=True)
        self._set_theme(theme_name)
        self.refresh_theme_details()

    def validate_automation(self) -> None:
        quantity = self.auto_quantity.get()
        age = self.auto_age.get()
        self.load_automation_config_ui(silent=True)
        self.auto_quantity.set(quantity)
        self.auto_age.set(age)
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
            str(self.auto_creators.item(row, "values")[1])
            for row in self.auto_creators.selection()
            if self.auto_creators.item(row, "values")[0] == "✓"
        )
        if not selected:
            raise ValueError("Selecione pelo menos um criador.")
        theme = self.automation_config.theme(self._current_theme_name())
        if not theme.enabled:
            raise ValueError("Ative o tema antes de executar a automação.")
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
            self._current_theme_name(),
            quantity,
            age,
            workspace,
            download_only=self.auto_download_only.get(),
            selected_creators=selected,
            youtube_access=YouTubeAccess(YOUTUBE_AUTH[self.auto_youtube_auth.get()]),
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
        transcribe = None
        if resolve_deno() is None:
            warning = (
                "Runtime JavaScript Deno não encontrado. "
                "A extração do YouTube pode falhar."
            )
            self.auto_status.set(warning)
            self.auto_log(warning)
            messagebox.showwarning("Deno não encontrado", warning)
        if not request.download_only:
            if not self.auto_fake.get():
                messagebox.showerror(
                    "Provider ausente",
                    "Ative explicitamente o provider TEST / DEVELOPMENT ONLY "
                    "ou use Somente download.",
                )
                return
            analyze = fake_analysis_provider
            if self.auto_transcription.get():
                transcribe = FasterWhisperProvider()
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
            request,
            self.automation_config,
            analyze=analyze,
            transcribe=transcribe,
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
        if event.kind == "candidate_skipped":
            reason = SKIP_REASONS.get(event.message or "", event.message or "Ignorado")
            row = f"auto-{len(self.automation_rows) + 1}"
            self.auto_tree.insert(
                "",
                "end",
                iid=row,
                values=(event.creator, event.title, "Ignorado", "-", reason),
            )
            self.auto_log(f"{event.creator} | {event.title} | Ignorado | {reason}")
            detail = event.data.get("technical_detail")
            if detail:
                self.auto_log(f"Detalhe técnico: {detail}")
            if event.message == "requires_authentication" and not YOUTUBE_AUTH.get(
                self.auto_youtube_auth.get()
            ):
                self.auto_status.set(
                    "YouTube exige autenticação. Selecione Brave ou Firefox em "
                    "Autenticação YouTube."
                )
            return
        if event.kind == "recovery_started":
            self.auto_status.set(f"Retomando vídeo em {event.message}")
            self.auto_log(
                f"Retomada: {event.title} | {event.message} | "
                f"{event.data.get('reason', '')}"
            )
            return
        if event.kind == "source_reused":
            self.auto_status.set("Source validado e reutilizado na retomada")
            self.auto_log(f"Retomada sem novo download: {event.path}")
            return
        if event.kind == "recovery_failed":
            self.auto_status.set("Retomada exige intervenção")
            self.auto_log(f"Retomada bloqueada: {event.message}")
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
        if event.kind == "transcription_started":
            self.auto_status.set("Transcrevendo...")
            self.auto_log(f"Transcrição local iniciada: {event.title}")
            return
        if event.kind == "transcription_progress":
            if event.progress is None:
                self.auto_status.set("Transcrevendo...")
            else:
                self.auto_progress.stop()
                self.auto_progress.configure(
                    mode="determinate", value=event.progress * 100
                )
                self.auto_status.set(f"Transcrevendo {event.progress:.0%}")
            return
        if event.kind == "transcription_finished":
            self.auto_status.set("Transcrição concluída")
            self.auto_log(
                f"Transcrição concluída ({event.data.get('language', '?')}): "
                f"{event.path}"
            )
            return
        if event.kind == "render_progress":
            progress = event.progress or 0.0
            clip = event.index or 0
            total = event.total or 3
            self.auto_progress.stop()
            self.auto_progress.configure(
                mode="determinate", value=((clip - 1) + progress) * 100 / total
            )
            self.auto_status.set(f"Render {clip}/{total}")
            if row:
                values = list(self.auto_tree.item(row, "values"))
                values[2:] = [
                    "Render",
                    f"{clip}/{total}",
                    event.message or "Renderizando",
                ]
                self.auto_tree.item(row, values=values)
            return
        if event.kind == "queue_finished":
            self.auto_status.set("QUEUED — 3 Shorts nas filas das contas")
        elif event.kind == "cleanup_warning":
            self.auto_log(
                f"Aviso: Shorts continuam QUEUED, mas o source não foi removido: "
                f"{event.message}"
            )
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
        self.auto_transcription = None
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
