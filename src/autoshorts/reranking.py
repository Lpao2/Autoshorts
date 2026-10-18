"""Provider-neutral editorial reranking of an existing candidate pool."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Protocol
from uuid import uuid4

from autoshorts.transcript_analysis import (
    AnalysisUsage,
    ClipSelection,
    ResolvedAnalysis,
)
from autoshorts.transcription import Transcript

RERANK_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class RerankCandidate:
    candidate_id: str
    selection: ClipSelection
    duration_seconds: float
    transcript_text: str


@dataclass(frozen=True, slots=True)
class RankedSelection:
    candidate_id: str
    rank: int
    reason: str
    editorial_score: float


@dataclass(frozen=True, slots=True)
class RerankResult:
    ranking: tuple[RankedSelection, ...]
    provider: str
    model: str
    prompt_hash: str
    usage: AnalysisUsage = AnalysisUsage()


class CandidateReranker(Protocol):
    provider_name: str
    model: str

    def prompt_hash(self, theme_prompt: str) -> str: ...

    def rerank(
        self,
        candidates: tuple[RerankCandidate, ...],
        *,
        theme: str,
        theme_prompt: str,
        title: str,
        creator: str,
        youtube_id: str,
        cancel: Event | None = None,
    ) -> RerankResult: ...


def build_rerank_candidates(
    analysis: ResolvedAnalysis, transcript: Transcript
) -> tuple[RerankCandidate, ...]:
    if len(analysis.result.candidates) != 5:
        raise ValueError("Rerank exige exatamente 5 candidatos persistidos.")
    items = []
    for index, selection in enumerate(analysis.result.candidates, 1):
        segments = transcript.segments[
            selection.start_segment : selection.end_segment + 1
        ]
        items.append(
            RerankCandidate(
                f"candidate-{index}",
                selection,
                segments[-1].end - segments[0].start,
                " ".join(item.text.strip() for item in segments if item.text.strip()),
            )
        )
    return tuple(items)


def validate_rerank(
    result: RerankResult, candidates: tuple[RerankCandidate, ...]
) -> tuple[ClipSelection, ...]:
    if len(result.ranking) != 3:
        raise ValueError("O reranker deve escolher exatamente 3 candidatos.")
    by_id = {item.candidate_id: item.selection for item in candidates}
    ids = [item.candidate_id for item in result.ranking]
    if len(set(ids)) != 3:
        raise ValueError("O reranker não pode repetir candidate_id.")
    if set(item.rank for item in result.ranking) != {1, 2, 3}:
        raise ValueError("Ranks do reranker devem ser 1, 2 e 3.")
    for item in result.ranking:
        if item.candidate_id not in by_id:
            raise ValueError(f"Candidate ID inválido: {item.candidate_id}")
        if not item.reason.strip():
            raise ValueError("Motivo editorial não pode ficar vazio.")
        if not 0 <= item.editorial_score <= 1:
            raise ValueError("Editorial score deve estar entre 0 e 1.")
    return tuple(
        by_id[item.candidate_id]
        for item in sorted(result.ranking, key=lambda x: x.rank)
    )


def rerank_prompt_fingerprint(base_prompt: str, theme_prompt: str) -> str:
    return hashlib.sha256(
        f"rerank-v1\n{base_prompt}\n{theme_prompt.strip()}".encode()
    ).hexdigest()


def write_rerank_atomic(
    path: Path,
    analysis: ResolvedAnalysis,
    transcript: Transcript,
    result: RerankResult,
) -> None:
    candidates = build_rerank_candidates(analysis, transcript)
    selected = validate_rerank(result, candidates)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["schema_version"] = 3
    data["selections"] = []
    for rank, selection in enumerate(selected, 1):
        start = transcript.segments[selection.start_segment].start
        end = transcript.segments[selection.end_segment].end
        data["selections"].append(
            {
                "rank": rank,
                "start_segment": selection.start_segment,
                "end_segment": selection.end_segment,
                "title": selection.title,
                "headline": selection.headline,
                "reason": selection.reason,
                "score": selection.score,
                "start_seconds": start,
                "end_seconds": end,
            }
        )
    data["rerank"] = {
        "schema_version": RERANK_SCHEMA_VERSION,
        "provider": result.provider,
        "model": result.model,
        "prompt_hash": result.prompt_hash,
        "ranking": [
            {
                "candidate_id": item.candidate_id,
                "rank": item.rank,
                "reason": item.reason,
                "editorial_score": item.editorial_score,
            }
            for item in sorted(result.ranking, key=lambda x: x.rank)
        ],
        "usage": _usage_dict(result.usage),
    }
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def load_rerank(
    path: Path, candidates: tuple[RerankCandidate, ...]
) -> RerankResult | None:
    data = json.loads(path.read_text(encoding="utf-8"))
    raw = data.get("rerank")
    if not isinstance(raw, dict) or raw.get("schema_version") != RERANK_SCHEMA_VERSION:
        return None
    usage = raw.get("usage", {})
    result = RerankResult(
        tuple(
            RankedSelection(
                str(x["candidate_id"]),
                int(x["rank"]),
                str(x["reason"]),
                float(x["editorial_score"]),
            )
            for x in raw["ranking"]
        ),
        str(raw["provider"]),
        str(raw["model"]),
        str(raw["prompt_hash"]),
        AnalysisUsage(
            int(usage.get("input_tokens", 0)),
            int(usage.get("output_tokens", 0)),
            int(usage.get("thinking_tokens", 0)),
            int(usage.get("total_tokens", 0)),
            float(usage.get("duration_seconds", 0)),
            float(usage.get("estimated_cost_usd", 0)),
        ),
    )
    validate_rerank(result, candidates)
    return result


def _usage_dict(usage: AnalysisUsage) -> dict[str, object]:
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "thinking_tokens": usage.thinking_tokens,
        "total_tokens": usage.total_tokens,
        "duration_seconds": usage.duration_seconds,
        "estimated_cost_usd": usage.estimated_cost_usd,
    }
