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

from autoshorts.config import OverwritePolicy, Settings, load_settings
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


class AutoShortsWindow:
    def __init__(self, root: tk.Tk, settings: Settings) -> None:
        self.root = root
        self.controller = JobController(settings)
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
        frame = ttk.Frame(root, padding=16)
        frame.pack(fill="both", expand=True)
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
        self.poll_id = self.root.after(75, self.poll)

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

    def poll(self) -> None:
        for _ in range(200):
            try:
                kind, payload = self.controller.messages.get_nowait()
            except Empty:
                break
            self.handle(kind, payload)
        if self.finishing and not self.controller.busy:
            self.finishing = False
            self.set_busy(False)
        if self.closing and not self.running and not self.controller.busy:
            self.destroy()
            return
        self.poll_id = self.root.after(75, self.poll)

    def close(self) -> None:
        if self.running:
            if not messagebox.askyesno(
                "Fechar AutoShorts",
                "Cancelar o processamento e fechar após encerrar com segurança?",
            ):
                return
            self.closing = True
            self.cancel()
        else:
            self.destroy()

    def destroy(self) -> None:
        self.root.after_cancel(self.poll_id)
        status_value = self.status.get()
        self.source = None
        self.output = None
        self.status = None
        # Finalize Tk variables while Tcl is still alive, never from a later
        # worker/finalizer phase after root.destroy().
        gc.collect()
        self.status = _StatusSnapshot(status_value)
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
