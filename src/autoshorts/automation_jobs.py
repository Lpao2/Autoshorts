"""Background controller for automated jobs; independent of Tk."""

from __future__ import annotations

from pathlib import Path
from queue import Queue
from threading import Event, Thread

from autoshorts.automation_config import AutomationConfig
from autoshorts.automation_events import AutomationCancelled
from autoshorts.automation_pipeline import (
    AnalysisProvider,
    AutomationRequest,
    run_automation,
)
from autoshorts.config import Settings
from autoshorts.state_store import StateStore
from autoshorts.transcription import TranscriptionProvider


class AutomationController:
    def __init__(
        self, settings: Settings, *, state_db_path: str | Path | None = None
    ) -> None:
        self.settings = settings
        self.messages: Queue[tuple[str, object]] = Queue()
        self.cancel_event = Event()
        self.thread: Thread | None = None
        self.state_db_path = Path(state_db_path) if state_db_path is not None else None

    @property
    def busy(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(
        self,
        request: AutomationRequest,
        config: AutomationConfig,
        *,
        analyze: AnalysisProvider | None = None,
        transcribe: TranscriptionProvider | None = None,
    ) -> None:
        if self.busy:
            raise RuntimeError("Já existe uma automação ativa.")
        self.cancel_event = Event()
        self.thread = Thread(
            target=self._work,
            args=(request, config, analyze, transcribe),
            name="autoshorts-automation",
            daemon=False,
        )
        self.thread.start()

    def cancel(self) -> None:
        self.cancel_event.set()

    def _work(
        self,
        request: AutomationRequest,
        config: AutomationConfig,
        analyze: AnalysisProvider | None,
        transcribe: TranscriptionProvider | None,
    ) -> None:
        try:
            if self.state_db_path is None:
                result = self._run(request, config, analyze, transcribe, None)
            else:
                with StateStore(self.state_db_path) as state_store:
                    result = self._run(
                        request, config, analyze, transcribe, state_store
                    )
            self.messages.put(("completed", result))
        except AutomationCancelled as exc:
            self.messages.put(("cancelled", exc))
        except Exception as exc:
            self.messages.put(("failed", exc))
        finally:
            self.messages.put(("finished", None))

    def _run(self, request, config, analyze, transcribe, state_store):
        return run_automation(
            request,
            config,
            self.settings,
            analyze=analyze,
            transcribe=transcribe,
            cancel=self.cancel_event,
            state_store=state_store,
            event_handler=lambda event: self.messages.put(("event", event)),
        )
