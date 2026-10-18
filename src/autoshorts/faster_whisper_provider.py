"""Optional faster-whisper adapter; imported safely without the extra installed."""

from __future__ import annotations

import os
from pathlib import Path
from threading import Event

from autoshorts.automation_events import AutomationCancelled
from autoshorts.transcription import (
    Transcript,
    TranscriptionProgressHandler,
    TranscriptSegment,
    TranscriptWord,
)


class FasterWhisperProvider:
    def __init__(
        self,
        model: str | None = None,
        *,
        device: str | None = None,
        compute_type: str | None = None,
        language: str | None = None,
    ) -> None:
        self.model = model or os.getenv("AUTOSHORTS_WHISPER_MODEL", "small")
        self.device = device or os.getenv("AUTOSHORTS_WHISPER_DEVICE", "cpu")
        self.compute_type = compute_type or os.getenv(
            "AUTOSHORTS_WHISPER_COMPUTE_TYPE", "int8"
        )
        self.language = language
        self._engine = None

    def transcribe(
        self,
        source_path: Path,
        *,
        cancel: Event | None = None,
        progress_handler: TranscriptionProgressHandler | None = None,
    ) -> Transcript:
        if cancel is not None and cancel.is_set():
            raise AutomationCancelled("Transcrição cancelada pelo usuário.")
        engine = self._load_engine()
        segments_iterator, info = engine.transcribe(
            str(source_path),
            language=self.language,
            word_timestamps=True,
            vad_filter=True,
        )
        duration = float(getattr(info, "duration", 0) or 0)
        segments: list[TranscriptSegment] = []
        for raw in segments_iterator:
            if cancel is not None and cancel.is_set():
                raise AutomationCancelled("Transcrição cancelada pelo usuário.")
            words = tuple(
                TranscriptWord(float(word.start), float(word.end), str(word.word))
                for word in (raw.words or ())
                if _valid_word(word, raw)
            )
            segments.append(
                TranscriptSegment(
                    float(raw.start), float(raw.end), str(raw.text).strip(), words
                )
            )
            if progress_handler is not None:
                progress_handler(
                    min(max(float(raw.end) / duration, 0.0), 1.0)
                    if duration > 0
                    else None
                )
        if duration <= 0 and segments:
            duration = segments[-1].end
        transcript = Transcript(
            language=str(getattr(info, "language", "unknown")),
            language_probability=_optional_float(
                getattr(info, "language_probability", None)
            ),
            duration_seconds=duration,
            text=" ".join(segment.text for segment in segments).strip(),
            segments=tuple(segments),
            provider="faster-whisper",
            model=self.model,
        )
        if progress_handler is not None:
            progress_handler(1.0)
        return transcript

    def _load_engine(self):
        if self._engine is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:
                raise RuntimeError(
                    "Transcrição local requer o extra: "
                    "pip install -e '.[transcription]'"
                ) from exc
            self._engine = WhisperModel(
                self.model, device=self.device, compute_type=self.compute_type
            )
        return self._engine


def _optional_float(value) -> float | None:
    return float(value) if value is not None else None


def _valid_word(word, segment) -> bool:
    return bool(
        word.start is not None
        and word.end is not None
        and float(word.start) >= float(segment.start)
        and float(word.end) <= float(segment.end)
        and float(word.end) > float(word.start)
        and str(word.word).strip()
    )
