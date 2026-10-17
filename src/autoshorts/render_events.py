"""UI-independent progress and cooperative cancellation contracts."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Event


class RenderCancelled(RuntimeError):
    """The user requested cancellation; completed outputs remain valid."""

    manifest_path: Path | None = None


def check_cancelled(cancel: Event) -> None:
    if cancel.is_set():
        raise RenderCancelled("Processamento cancelado pelo usuário.")


@dataclass(frozen=True, slots=True)
class RenderEvent:
    index: int
    status: str
    progress: float = 0.0


ProgressCallback = Callable[[RenderEvent], None]
