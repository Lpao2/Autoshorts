"""Build durable production metadata from a persisted editorial analysis."""

from __future__ import annotations

from pathlib import Path

from autoshorts.reranking import build_rerank_candidates, load_rerank
from autoshorts.state_store import RenderedShort
from autoshorts.transcript_analysis import ResolvedAnalysis
from autoshorts.transcription import Transcript


def rendered_short_records(
    paths: tuple[Path, ...],
    analysis_path: Path,
    analysis: ResolvedAnalysis,
    transcript: Transcript,
) -> tuple[RenderedShort, ...]:
    """Map render positions to the persisted reranker order without re-sorting."""
    if len(paths) != 3:
        raise ValueError("Exactly 3 rendered paths are required.")
    candidates = build_rerank_candidates(analysis, transcript)
    rerank = load_rerank(analysis_path, candidates)
    if rerank is None:
        raise ValueError("A valid persisted editorial rerank is required.")
    candidate_by_selection = {
        item.selection: item.candidate_id for item in candidates
    }
    ranking_by_id = {item.candidate_id: item for item in rerank.ranking}
    records = []
    for rank, (path, selection, clip) in enumerate(
        zip(paths, analysis.result.selections, analysis.clips.clips, strict=True), 1
    ):
        candidate_id = candidate_by_selection[selection]
        editorial = ranking_by_id[candidate_id]
        if editorial.rank != rank:
            raise ValueError(
                "Persisted rerank order does not match analysis selections."
            )
        records.append(
            RenderedShort(
                clip_index=rank,
                file_path=path,
                title=selection.title,
                headline=selection.headline,
                generator_score=selection.score,
                editorial_score=editorial.editorial_score,
                candidate_id=candidate_id,
                duration_seconds=clip.end_seconds - clip.start_seconds,
            )
        )
    return tuple(records)
