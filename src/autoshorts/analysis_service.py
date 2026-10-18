"""Restart-safe orchestration for transcript analysis."""

from __future__ import annotations

from pathlib import Path
from threading import Event

from autoshorts.analysis_cost import monthly_budget_usd
from autoshorts.automation_events import AutomationEvent, check_automation_cancelled
from autoshorts.state_store import StateStore, VideoStatus
from autoshorts.transcript_analysis import (
    ResolvedAnalysis,
    TranscriptAnalysisProvider,
    make_resolved_analysis,
    write_analysis_atomic,
)
from autoshorts.transcription import Transcript


def analyze_transcript(
    *,
    youtube_id: str,
    transcript: Transcript,
    artifact_path: Path,
    provider: TranscriptAnalysisProvider,
    state_store: StateStore,
    title: str,
    creator: str,
    theme: str,
    theme_prompt: str,
    cancel: Event | None = None,
    event_handler=None,
) -> ResolvedAnalysis:
    emit = event_handler or (lambda _event: None)
    check_automation_cancelled(cancel)
    if provider.provider_name == "gemini":
        spent = state_store.monthly_ai_cost("gemini")
        if spent >= monthly_budget_usd():
            raise RuntimeError("Limite mensal de IA do AutoShorts atingido.")
    state_store.transition_video(youtube_id, VideoStatus.ANALYZING)
    emit(AutomationEvent("analysis_started", creator=creator, title=title))
    try:
        result = provider.analyze(
            transcript,
            title=title,
            creator=creator,
            theme=theme,
            theme_prompt=theme_prompt,
            cancel=cancel,
        )
    except Exception as exc:
        usage = getattr(exc, "usage", None)
        if usage is not None:
            state_store.record_ai_usage(
                youtube_id,
                provider=provider.provider_name,
                model=provider.model,
                usage=usage,
            )
        raise
    resolved = make_resolved_analysis(
        youtube_id=youtube_id,
        theme=theme,
        theme_prompt=theme_prompt,
        transcript=transcript,
        result=result,
    )
    write_analysis_atomic(artifact_path, resolved)
    state_store.record_analysis(
        youtube_id,
        file_path=artifact_path,
        provider=result.provider,
        model=result.model,
        prompt_hash=resolved.prompt_hash,
        usage=result.usage,
    )
    emit(
        AutomationEvent(
            "analysis_finished",
            creator=creator,
            title=title,
            path=artifact_path,
            data={
                "model": result.model,
                "tokens": result.usage.total_tokens,
                "cost_usd": result.usage.estimated_cost_usd,
                "month_cost_usd": state_store.monthly_ai_cost(result.provider),
                "budget_usd": monthly_budget_usd(),
            },
        )
    )
    return resolved
