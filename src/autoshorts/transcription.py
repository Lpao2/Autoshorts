"""Provider-neutral transcription domain model and JSON artifacts."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Protocol
from uuid import uuid4

TRANSCRIPT_SCHEMA_VERSION = 1
TranscriptionProgressHandler = Callable[[float | None], None]


@dataclass(frozen=True, slots=True)
class TranscriptWord:
    start: float
    end: float
    text: str

    def __post_init__(self) -> None:
        _validate_interval(self.start, self.end, "word")
        if not self.text.strip():
            raise ValueError("Transcript word text cannot be empty.")


@dataclass(frozen=True, slots=True)
class TranscriptSegment:
    start: float
    end: float
    text: str
    words: tuple[TranscriptWord, ...] = ()

    def __post_init__(self) -> None:
        _validate_interval(self.start, self.end, "segment")
        previous = self.start
        for word in self.words:
            if word.start < self.start or word.end > self.end:
                raise ValueError("Transcript word must be inside its segment.")
            if word.start < previous:
                raise ValueError("Transcript words must be ordered.")
            previous = word.start


@dataclass(frozen=True, slots=True)
class Transcript:
    language: str
    language_probability: float | None
    duration_seconds: float
    text: str
    segments: tuple[TranscriptSegment, ...]
    provider: str
    model: str

    def __post_init__(self) -> None:
        if not self.language.strip():
            raise ValueError("Transcript language cannot be empty.")
        if self.language_probability is not None and not (
            0 <= self.language_probability <= 1
        ):
            raise ValueError("language_probability must be between 0 and 1.")
        if self.duration_seconds <= 0:
            raise ValueError("Transcript duration_seconds must be positive.")
        if not self.provider.strip() or not self.model.strip():
            raise ValueError("Transcript provider and model cannot be empty.")
        previous = 0.0
        for segment in self.segments:
            if segment.start < previous:
                raise ValueError(
                    "Transcript segments must be ordered and non-overlapping."
                )
            if segment.end > self.duration_seconds + 0.5:
                raise ValueError("Transcript segment exceeds media duration.")
            previous = segment.end


class TranscriptionProvider(Protocol):
    def transcribe(
        self,
        source_path: Path,
        *,
        cancel: Event | None = None,
        progress_handler: TranscriptionProgressHandler | None = None,
    ) -> Transcript: ...


def transcript_to_dict(transcript: Transcript, youtube_id: str) -> dict[str, object]:
    return {
        "schema_version": TRANSCRIPT_SCHEMA_VERSION,
        "youtube_id": youtube_id,
        "provider": transcript.provider,
        "model": transcript.model,
        "language": transcript.language,
        "language_probability": transcript.language_probability,
        "duration_seconds": transcript.duration_seconds,
        "text": transcript.text,
        "segments": [
            {
                "start": segment.start,
                "end": segment.end,
                "text": segment.text,
                "words": [
                    {"start": word.start, "end": word.end, "text": word.text}
                    for word in segment.words
                ],
            }
            for segment in transcript.segments
        ],
    }


def transcript_from_dict(data: object, *, youtube_id: str | None = None) -> Transcript:
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("Unsupported or invalid transcript schema.")
    if youtube_id is not None and data.get("youtube_id") != youtube_id:
        raise ValueError("Transcript youtube_id does not match the requested video.")
    try:
        segments = tuple(
            TranscriptSegment(
                float(item["start"]),
                float(item["end"]),
                str(item.get("text", "")),
                tuple(
                    TranscriptWord(
                        float(word["start"]),
                        float(word["end"]),
                        str(word.get("text", "")),
                    )
                    for word in item.get("words", [])
                ),
            )
            for item in data["segments"]
        )
        probability = data.get("language_probability")
        return Transcript(
            language=str(data["language"]),
            language_probability=float(probability)
            if probability is not None
            else None,
            duration_seconds=float(data["duration_seconds"]),
            text=str(data.get("text", "")),
            segments=segments,
            provider=str(data["provider"]),
            model=str(data["model"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Invalid transcript artifact: {exc}") from exc


def write_transcript_atomic(
    path: str | Path, transcript: Transcript, youtube_id: str
) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    payload = transcript_to_dict(transcript, youtube_id)
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def load_transcript(path: str | Path, *, youtube_id: str | None = None) -> Transcript:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid transcript JSON: {exc}") from exc
    return transcript_from_dict(data, youtube_id=youtube_id)


def _validate_interval(start: float, end: float, label: str) -> None:
    if start < 0 or end <= start:
        raise ValueError(f"Transcript {label} requires 0 <= start < end.")
