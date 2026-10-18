"""Persistence, budget, and recovery boundary for editorial reranking."""

from pathlib import Path
from threading import Event

from autoshorts.analysis_cost import monthly_budget_usd
from autoshorts.automation_events import AutomationEvent, check_automation_cancelled
from autoshorts.reranking import (
    CandidateReranker,
    build_rerank_candidates,
    load_rerank,
    validate_rerank,
    write_rerank_atomic,
)
from autoshorts.state_store import StateStore
from autoshorts.transcript_analysis import load_analysis
from autoshorts.transcription import Transcript


def rerank_analysis(
    *,
    youtube_id: str,
    transcript: Transcript,
    analysis_path: Path,
    reranker: CandidateReranker,
    state_store: StateStore,
    title: str,
    creator: str,
    theme: str,
    theme_prompt: str,
    cancel: Event | None = None,
    event_handler=None,
):
    emit = event_handler or (lambda _event: None)
    analysis = load_analysis(analysis_path, transcript, youtube_id=youtube_id)
    candidates = build_rerank_candidates(analysis, transcript)
    existing = load_rerank(analysis_path, candidates)
    expected_hash = reranker.prompt_hash(theme_prompt)
    if existing is not None and existing.prompt_hash == expected_hash:
        emit(
            AutomationEvent(
                "rerank_reused", creator=creator, title=title, path=analysis_path
            )
        )
        return existing
    check_automation_cancelled(cancel)
    if state_store.monthly_ai_cost("gemini") >= monthly_budget_usd():
        raise RuntimeError("Limite mensal de IA do AutoShorts atingido.")
    emit(AutomationEvent("rerank_started", creator=creator, title=title))
    try:
        result = reranker.rerank(
            candidates,
            theme=theme,
            theme_prompt=theme_prompt,
            title=title,
            creator=creator,
            youtube_id=youtube_id,
            cancel=cancel,
        )
        validate_rerank(result, candidates)
    except Exception as exc:
        usage = getattr(exc, "usage", None)
        if usage is not None:
            state_store.record_ai_usage(
                youtube_id,
                provider="gemini",
                model=reranker.model,
                usage=usage,
                operation="rerank",
            )
        raise
    write_rerank_atomic(analysis_path, analysis, transcript, result)
    state_store.record_ai_usage(
        youtube_id,
        provider="gemini",
        model=result.model,
        usage=result.usage,
        operation="rerank",
    )
    emit(
        AutomationEvent(
            "rerank_finished",
            creator=creator,
            title=title,
            path=analysis_path,
            data={
                "tokens": result.usage.total_tokens,
                "cost_usd": result.usage.estimated_cost_usd,
            },
        )
    )
    return result
