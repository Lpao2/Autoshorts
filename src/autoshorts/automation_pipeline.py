"""Orchestration for the legacy end-to-end production workflow.

The workflow depends on injected analysis and publishing adapters. This keeps
the deterministic media engine usable without browser sessions or credentials.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Event

from autoshorts.ai_analysis import ClipAnalysis
from autoshorts.automation_config import AutomationConfig, ThemeProfile
from autoshorts.automation_events import (
    AutomationCancelled,
    AutomationEvent,
    check_automation_cancelled,
)
from autoshorts.collection import (
    VideoCandidate,
    discover_creator_videos,
    download_video,
)
from autoshorts.config import Settings
from autoshorts.pipeline import PipelineResult, run_pipeline
from autoshorts.render_events import RenderCancelled

AnalysisProvider = Callable[[Path, VideoCandidate, ThemeProfile], ClipAnalysis]
EventHandler = Callable[[AutomationEvent], None]


@dataclass(frozen=True, slots=True)
class AutomationRequest:
    theme: str
    videos_per_creator: int
    maximum_age_days: int
    workspace: Path
    download_only: bool = False
    selected_creators: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class AutomationItemResult:
    candidate: VideoCandidate
    source_path: Path | None
    analysis: ClipAnalysis | None
    render: PipelineResult | None
    error: str | None = None


def run_automation(
    request: AutomationRequest,
    config: AutomationConfig,
    settings: Settings,
    *,
    analyze: AnalysisProvider | None = None,
    event_handler: EventHandler | None = None,
    cancel: Event | None = None,
) -> tuple[AutomationItemResult, ...]:
    """Run collection and optional analysis/rendering for one configured theme."""
    theme = config.theme(request.theme)
    if request.videos_per_creator < 1:
        raise ValueError("videos_per_creator must be positive.")
    if request.maximum_age_days < 0 or request.maximum_age_days > 3650:
        raise ValueError("maximum_age_days must be between 0 and 3650.")
    workspace = request.workspace.expanduser().resolve()
    if not request.download_only and analyze is None:
        raise ValueError("An analysis provider is required before rendering.")
    theme_root = workspace / theme.output_subdirectory
    results: list[AutomationItemResult] = []
    creators = tuple(
        creator
        for creator in theme.creators
        if request.selected_creators is None
        or creator.name in request.selected_creators
    )
    if not creators:
        raise ValueError("Select at least one creator.")
    emit = event_handler or (lambda _event: None)
    emit(AutomationEvent("automation_started", total=len(creators), message=theme.name))
    try:
        for creator in creators:
            check_automation_cancelled(cancel)
            emit(AutomationEvent("creator_started", creator=creator.name))
            emit(AutomationEvent("discovery_started", creator=creator.name))
            candidates = discover_creator_videos(
                creator.name,
                creator.videos_url,
                quantity=request.videos_per_creator,
                age_days=request.maximum_age_days,
            )
            check_automation_cancelled(cancel)
            emit(
                AutomationEvent(
                    "discovery_finished",
                    creator=creator.name,
                    total=len(candidates),
                )
            )
            for index, candidate in enumerate(candidates, start=1):
                stage = "download"
                check_automation_cancelled(cancel)
                emit(
                    _candidate_event(
                        "candidate_found", candidate, index, len(candidates)
                    )
                )
                emit(
                    _candidate_event(
                        "download_started", candidate, index, len(candidates)
                    )
                )
                try:
                    source_path = download_video(
                        candidate,
                        theme_root / "downloads",
                        cancel=cancel,
                        progress_handler=lambda progress, item=candidate: emit(
                            AutomationEvent(
                                "download_progress",
                                creator=item.creator,
                                title=item.title,
                                url=item.url,
                                path=Path(progress.filename)
                                if progress.filename
                                else None,
                                progress=progress.percent,
                                downloaded_bytes=progress.downloaded_bytes,
                                total_bytes=progress.total_bytes,
                                total_is_estimate=progress.total_is_estimate,
                                speed=progress.speed,
                                eta=progress.eta,
                                message=progress.status,
                            )
                        ),
                    )
                    emit(
                        AutomationEvent(
                            "download_finished",
                            creator=candidate.creator,
                            title=candidate.title,
                            path=source_path,
                            progress=1.0,
                        )
                    )
                    if request.download_only:
                        results.append(
                            AutomationItemResult(candidate, source_path, None, None)
                        )
                        emit(
                            _candidate_event(
                                "item_finished", candidate, index, len(candidates)
                            )
                        )
                        continue
                    assert analyze is not None
                    stage = "analysis"
                    check_automation_cancelled(cancel)
                    emit(
                        _candidate_event(
                            "analysis_started", candidate, index, len(candidates)
                        )
                    )
                    analysis = analyze(source_path, candidate, theme)
                    emit(
                        _candidate_event(
                            "analysis_finished", candidate, index, len(candidates)
                        )
                    )
                    check_automation_cancelled(cancel)
                    stage = "render"
                    emit(
                        _candidate_event(
                            "render_started", candidate, index, len(candidates)
                        )
                    )
                    output = (
                        theme_root
                        / "outputs"
                        / _safe_creator_directory(candidate.creator)
                    )
                    render_settings = replace(settings, output_dir=output)
                    render = run_pipeline(
                        source_path,
                        list(analysis.clips),
                        render_settings,
                        cancel=cancel,
                    )
                    emit(
                        _candidate_event(
                            "render_finished", candidate, index, len(candidates)
                        )
                    )
                    results.append(
                        AutomationItemResult(candidate, source_path, analysis, render)
                    )
                    emit(
                        _candidate_event(
                            "item_finished", candidate, index, len(candidates)
                        )
                    )
                except (AutomationCancelled, RenderCancelled) as exc:
                    if isinstance(exc, RenderCancelled):
                        raise AutomationCancelled(str(exc)) from exc
                    raise
                except Exception as exc:
                    kind = f"{stage}_failed"
                    emit(
                        AutomationEvent(
                            kind,
                            creator=candidate.creator,
                            title=candidate.title,
                            url=candidate.url,
                            message=str(exc),
                        )
                    )
                    results.append(
                        AutomationItemResult(candidate, None, None, None, str(exc))
                    )
        emit(AutomationEvent("automation_completed", total=len(results)))
    except AutomationCancelled:
        emit(AutomationEvent("automation_cancelled", total=len(results)))
        raise
    except Exception as exc:
        emit(AutomationEvent("automation_failed", message=str(exc)))
        raise
    return tuple(results)


def _candidate_event(
    kind: str, candidate: VideoCandidate, index: int, total: int
) -> AutomationEvent:
    return AutomationEvent(
        kind,
        creator=candidate.creator,
        title=candidate.title,
        url=candidate.url,
        index=index,
        total=total,
    )


def _safe_creator_directory(name: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in "-_" else "_" for char in name)
    return cleaned.strip("._") or "creator"
