"""Lifecycle orchestration for local transcription artifacts."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from threading import Event

from autoshorts.automation_events import AutomationEvent
from autoshorts.state_store import StateStore, VideoStatus
from autoshorts.transcription import (
    Transcript,
    TranscriptionProvider,
    write_transcript_atomic,
)

EventHandler = Callable[[AutomationEvent], None]


def transcript_video(
    *,
    youtube_id: str,
    source_path: Path,
    artifact_path: Path,
    provider: TranscriptionProvider,
    state_store: StateStore,
    creator: str,
    title: str,
    url: str,
    cancel: Event | None = None,
    event_handler: EventHandler | None = None,
) -> Transcript:
    """Transcribe and atomically commit artifact metadata plus TRANSCRIBED."""
    emit = event_handler or (lambda _event: None)
    state_store.transition_video(youtube_id, VideoStatus.TRANSCRIBING)
    emit(
        AutomationEvent("transcription_started", creator=creator, title=title, url=url)
    )
    try:
        transcript = provider.transcribe(
            source_path,
            cancel=cancel,
            progress_handler=lambda progress: emit(
                AutomationEvent(
                    "transcription_progress",
                    creator=creator,
                    title=title,
                    url=url,
                    progress=progress,
                )
            ),
        )
        write_transcript_atomic(artifact_path, transcript, youtube_id)
        state_store.record_transcript(
            youtube_id,
            file_path=artifact_path,
            provider=transcript.provider,
            model=transcript.model,
            language=transcript.language,
        )
    except Exception as exc:
        artifact_path.unlink(missing_ok=True)
        emit(
            AutomationEvent(
                "transcription_failed",
                creator=creator,
                title=title,
                url=url,
                message=str(exc),
            )
        )
        raise
    emit(
        AutomationEvent(
            "transcription_finished",
            creator=creator,
            title=title,
            url=url,
            path=artifact_path,
            progress=1.0,
            data={"language": transcript.language},
        )
    )
    return transcript
