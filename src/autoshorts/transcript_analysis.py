"""Provider-neutral transcript analysis, validation, and durable artifacts."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import Protocol
from uuid import uuid4

from autoshorts.ai_analysis import ClipAnalysis
from autoshorts.timestamp_parser import Clip
from autoshorts.transcription import Transcript

ANALYSIS_SCHEMA_VERSION = 1
MAX_HEADLINE_LENGTH = 100


@dataclass(frozen=True, slots=True)
class ClipSelection:
    start_segment: int
    end_segment: int
    title: str
    headline: str
    reason: str
    score: float


@dataclass(frozen=True, slots=True)
class AnalysisUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    total_tokens: int = 0
    duration_seconds: float = 0.0
    estimated_cost_usd: float = 0.0


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    selections: tuple[ClipSelection, ...]
    provider: str
    model: str
    usage: AnalysisUsage = AnalysisUsage()


@dataclass(frozen=True, slots=True)
class ClipPolicy:
    min_duration_seconds: float = 20
    max_duration_seconds: float = 60


DEFAULT_CLIP_POLICY = ClipPolicy()


@dataclass(frozen=True, slots=True)
class ResolvedAnalysis:
    result: AnalysisResult
    clips: ClipAnalysis
    youtube_id: str
    theme: str
    prompt_hash: str
    created_at: str


class TranscriptAnalysisProvider(Protocol):
    provider_name: str
    model: str

    def analyze(
        self,
        transcript: Transcript,
        *,
        title: str,
        creator: str,
        theme: str,
        theme_prompt: str,
        cancel: Event | None = None,
    ) -> AnalysisResult: ...


def prompt_fingerprint(theme_prompt: str) -> str:
    return hashlib.sha256(theme_prompt.encode("utf-8")).hexdigest()


def resolve_clip_selections(
    transcript: Transcript,
    result: AnalysisResult,
    policy: ClipPolicy = DEFAULT_CLIP_POLICY,
) -> ClipAnalysis:
    if len(result.selections) != 3:
        raise ValueError("A análise deve conter exatamente 3 seleções.")
    ranked = tuple(sorted(result.selections, key=lambda item: item.score, reverse=True))
    ranges: list[tuple[float, float]] = []
    clips: list[Clip] = []
    seen: set[tuple[int, int]] = set()
    for selection in ranked:
        if (
            not isinstance(selection.start_segment, int)
            or isinstance(selection.start_segment, bool)
            or not isinstance(selection.end_segment, int)
            or isinstance(selection.end_segment, bool)
        ):
            raise ValueError("Índices de segmento devem ser inteiros.")
        if not (
            0
            <= selection.start_segment
            <= selection.end_segment
            < len(transcript.segments)
        ):
            raise ValueError("Seleção contém índice de segmento inválido.")
        key = (selection.start_segment, selection.end_segment)
        if key in seen:
            raise ValueError("As três seleções devem usar ranges diferentes.")
        seen.add(key)
        if not selection.title.strip():
            raise ValueError("Título da seleção não pode ficar vazio.")
        if not selection.reason.strip():
            raise ValueError("Motivo da seleção não pode ficar vazio.")
        headline = selection.headline.strip()
        if not headline:
            raise ValueError("Headline da seleção não pode ficar vazia.")
        if len(headline) > MAX_HEADLINE_LENGTH:
            raise ValueError(f"Headline excede {MAX_HEADLINE_LENGTH} caracteres.")
        if not 0 <= selection.score <= 1:
            raise ValueError("Score da seleção deve estar entre 0 e 1.")
        start = transcript.segments[selection.start_segment].start
        end = transcript.segments[selection.end_segment].end
        duration = end - start
        if not policy.min_duration_seconds <= duration <= policy.max_duration_seconds:
            raise ValueError(
                f"Seleção '{selection.title}' dura {duration:.2f}s; esperado entre "
                f"{policy.min_duration_seconds:.0f}s e "
                f"{policy.max_duration_seconds:.0f}s."
            )
        if any(
            start < other_end and end > other_start for other_start, other_end in ranges
        ):
            raise ValueError("As seleções não podem se sobrepor.")
        ranges.append((start, end))
        clips.append(Clip(start, end, selection.title.strip()))
    return ClipAnalysis(tuple(clips), "structured transcript analysis")


def make_resolved_analysis(
    *,
    youtube_id: str,
    theme: str,
    theme_prompt: str,
    transcript: Transcript,
    result: AnalysisResult,
    policy: ClipPolicy = DEFAULT_CLIP_POLICY,
) -> ResolvedAnalysis:
    return ResolvedAnalysis(
        result=result,
        clips=resolve_clip_selections(transcript, result, policy),
        youtube_id=youtube_id,
        theme=theme,
        prompt_hash=prompt_fingerprint(theme_prompt),
        created_at=datetime.now(UTC).isoformat(),
    )


def analysis_to_dict(analysis: ResolvedAnalysis) -> dict[str, object]:
    selections = []
    for rank, (selection, clip) in enumerate(
        zip(
            sorted(
                analysis.result.selections, key=lambda item: item.score, reverse=True
            ),
            analysis.clips.clips,
            strict=True,
        ),
        1,
    ):
        selections.append(
            {
                "rank": rank,
                "start_segment": selection.start_segment,
                "end_segment": selection.end_segment,
                "title": selection.title,
                "headline": selection.headline,
                "reason": selection.reason,
                "score": selection.score,
                "start_seconds": clip.start_seconds,
                "end_seconds": clip.end_seconds,
            }
        )
    usage = analysis.result.usage
    return {
        "schema_version": ANALYSIS_SCHEMA_VERSION,
        "youtube_id": analysis.youtube_id,
        "provider": analysis.result.provider,
        "model": analysis.result.model,
        "theme": analysis.theme,
        "prompt_hash": analysis.prompt_hash,
        "created_at": analysis.created_at,
        "selections": selections,
        "usage": {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "thinking_tokens": usage.thinking_tokens,
            "total_tokens": usage.total_tokens,
            "duration_seconds": usage.duration_seconds,
            "estimated_cost_usd": usage.estimated_cost_usd,
        },
    }


def analysis_from_dict(
    data: object, transcript: Transcript, *, youtube_id: str | None = None
) -> ResolvedAnalysis:
    if (
        not isinstance(data, dict)
        or data.get("schema_version") != ANALYSIS_SCHEMA_VERSION
    ):
        raise ValueError("Unsupported or invalid analysis schema.")
    if youtube_id is not None and data.get("youtube_id") != youtube_id:
        raise ValueError("Analysis youtube_id does not match the requested video.")
    try:
        selections = tuple(
            ClipSelection(
                int(item["start_segment"]),
                int(item["end_segment"]),
                str(item["title"]),
                str(item["headline"]),
                str(item["reason"]),
                float(item["score"]),
            )
            for item in data["selections"]
        )
        raw_usage = data.get("usage", {})
        usage = AnalysisUsage(
            int(raw_usage.get("input_tokens", 0)),
            int(raw_usage.get("output_tokens", 0)),
            int(raw_usage.get("thinking_tokens", 0)),
            int(raw_usage.get("total_tokens", 0)),
            float(raw_usage.get("duration_seconds", 0)),
            float(raw_usage.get("estimated_cost_usd", 0)),
        )
        result = AnalysisResult(
            selections, str(data["provider"]), str(data["model"]), usage
        )
        resolved = ResolvedAnalysis(
            result,
            resolve_clip_selections(transcript, result),
            str(data["youtube_id"]),
            str(data["theme"]),
            str(data["prompt_hash"]),
            str(data["created_at"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Invalid analysis artifact: {exc}") from exc
    return resolved


def write_analysis_atomic(path: str | Path, analysis: ResolvedAnalysis) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(analysis_to_dict(analysis), stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def load_analysis(
    path: str | Path, transcript: Transcript, *, youtube_id: str | None = None
) -> ResolvedAnalysis:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid analysis JSON: {exc}") from exc
    return analysis_from_dict(data, transcript, youtube_id=youtube_id)
