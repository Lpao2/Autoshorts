"""UI-independent lifecycle events for automated production."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import Any


class AutomationCancelled(RuntimeError):
    """Raised cooperatively when an automated job is cancelled."""


@dataclass(frozen=True, slots=True)
class AutomationEvent:
    kind: str
    creator: str | None = None
    title: str | None = None
    url: str | None = None
    index: int | None = None
    total: int | None = None
    path: Path | None = None
    progress: float | None = None
    downloaded_bytes: int | None = None
    total_bytes: int | None = None
    total_is_estimate: bool = False
    speed: float | None = None
    eta: int | None = None
    message: str | None = None
    data: dict[str, Any] = field(default_factory=dict)


def check_automation_cancelled(cancel: Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise AutomationCancelled("Automação cancelada pelo usuário.")
