"""Background jobs with immutable inputs; no dependency on Tk."""

from dataclasses import dataclass, replace
from pathlib import Path
from queue import Queue
from threading import Event, Thread

from autoshorts.config import OverwritePolicy, Settings
from autoshorts.media_probe import probe_media
from autoshorts.pipeline import run_pipeline
from autoshorts.render_events import RenderCancelled, check_cancelled
from autoshorts.render_plan import RenderPlan, build_render_plan
from autoshorts.timestamp_parser import Clip, parse_cuts
from autoshorts.video_processor import OutputExistsError


@dataclass(frozen=True, slots=True)
class JobRequest:
    source: str
    cuts: str
    output: str
    overwrite: OverwritePolicy = "error"
    workers: int = 1


def prepare_job(
    request: JobRequest,
    defaults: Settings,
) -> tuple[Path, list[Clip], Settings, RenderPlan]:
    """Validate without creating a directory, manifest, or media file."""
    if not request.source.strip() or not request.output.strip():
        raise ValueError("Selecione o vídeo e a pasta de saída.")
    if request.overwrite not in {"error", "skip", "replace"}:
        raise ValueError("Política de arquivos inválida.")
    if not 1 <= request.workers <= 32:
        raise ValueError("Use de 1 a 32 processos.")
    source = Path(request.source).expanduser().resolve()
    output = Path(request.output).expanduser().resolve()
    if output.exists() and not output.is_dir():
        raise ValueError("A saída precisa ser uma pasta.")
    settings = replace(
        defaults,
        output_dir=output,
        overwrite_policy=request.overwrite,
        max_workers=request.workers,
    )
    try:
        clips = parse_cuts(request.cuts)
    except ValueError as exc:
        raise ValueError(
            "Cortes inválidos. Use [MM:SS - MM:SS] - Título, com fim após início. "
            f"Detalhe: {exc}"
        ) from exc
    if not source.is_file():
        raise FileNotFoundError(f"Vídeo não encontrado: {source}")
    plan = build_render_plan(
        probe_media(source, settings.ffprobe_path), clips, settings
    )
    if any(item.output_path.resolve() == source for item in plan.clips):
        raise ValueError(
            "A saída sobrescreveria o vídeo original. Escolha outra pasta."
        )
    if request.overwrite == "error":
        existing = next(
            (item.output_path for item in plan.clips if item.output_path.exists()), None
        )
        if existing is not None:
            raise OutputExistsError(
                f"Já existe: {existing.name}. Escolha outra pasta ou política."
            )
    return source, clips, settings, plan


class JobController:
    """One background job at a time; consumers poll messages on their UI thread."""

    def __init__(self, defaults: Settings) -> None:
        self.defaults = defaults
        self.messages: Queue[tuple[str, object]] = Queue()
        self.cancel_event = Event()
        self.thread: Thread | None = None

    @property
    def busy(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self, request: JobRequest, *, validate_only: bool = False) -> None:
        if self.busy:
            raise RuntimeError("Já existe um processamento ativo.")
        self.cancel_event = Event()
        self.thread = Thread(
            target=self._work,
            args=(request, validate_only),
            name="autoshorts-job",
            daemon=False,
        )
        self.thread.start()

    def cancel(self) -> None:
        self.cancel_event.set()

    def _work(self, request: JobRequest, validate_only: bool) -> None:
        try:
            check_cancelled(self.cancel_event)
            source, clips, settings, plan = prepare_job(request, self.defaults)
            check_cancelled(self.cancel_event)
            self.messages.put(("plan", plan))
            if validate_only:
                self.messages.put(("validated", plan))
                return
            result = run_pipeline(
                source,
                clips,
                settings,
                cancel=self.cancel_event,
                on_progress=lambda event: self.messages.put(("progress", event)),
            )
            self.messages.put(("completed", result))
        except RenderCancelled as exc:
            self.messages.put(("cancelled", exc))
        except Exception as exc:
            self.messages.put(("failed", exc))
        finally:
            self.messages.put(("finished", None))
