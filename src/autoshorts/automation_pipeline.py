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
    CandidateSkip,
    VideoCandidate,
    YouTubeAccess,
    candidate_youtube_id,
    discover_creator_videos,
    download_video,
)
from autoshorts.config import Settings
from autoshorts.distribution import (
    ShortArtifact,
    assign_shorts_to_accounts,
    distribute_short_files,
)
from autoshorts.pipeline import PipelineResult, run_pipeline
from autoshorts.render_events import RenderCancelled
from autoshorts.state_store import (
    QueuedShort,
    RenderedShort,
    StateStore,
    VideoStatus,
)

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
    youtube_access: YouTubeAccess = YouTubeAccess()


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
    state_store: StateStore | None = None,
) -> tuple[AutomationItemResult, ...]:
    """Run collection and optional analysis/rendering for one configured theme."""
    if request.videos_per_creator < 1:
        raise ValueError("videos_per_creator must be positive.")
    if request.maximum_age_days < 0 or request.maximum_age_days > 3650:
        raise ValueError("maximum_age_days must be between 0 and 3650.")
    theme = config.theme(request.theme)
    if not theme.enabled:
        raise ValueError(f"Theme '{theme.name}' is disabled.")
    workspace = request.workspace.expanduser().resolve()
    if not request.download_only and analyze is None:
        raise ValueError("An analysis provider is required before rendering.")
    theme_root = workspace / theme.output_subdirectory
    results: list[AutomationItemResult] = []
    creators = tuple(
        creator
        for creator in theme.creators
        if creator.enabled
        and (
            request.selected_creators is None
            or creator.name in request.selected_creators
        )
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
                access=request.youtube_access,
                cancel=cancel,
                skip_handler=lambda skipped: emit(_candidate_skipped_event(skipped)),
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
                youtube_id = candidate_youtube_id(candidate)
                if state_store is not None and youtube_id:
                    if state_store.is_produced(youtube_id):
                        emit(
                            AutomationEvent(
                                "candidate_skipped",
                                creator=candidate.creator,
                                title=candidate.title,
                                url=candidate.url,
                                message="already_produced",
                            )
                        )
                        continue
                    state_store.register_video(
                        youtube_id=youtube_id,
                        source_url=candidate.url,
                        creator_name=candidate.creator,
                        theme_name=theme.name,
                        title=candidate.title,
                    )
                    record = state_store.get_video(youtube_id)
                    if record and record.status == VideoStatus.FAILED:
                        state_store.transition_video(
                            youtube_id, VideoStatus.DISCOVERED
                        )
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
                    if state_store is not None and youtube_id:
                        record = state_store.get_video(youtube_id)
                        if record and record.status == VideoStatus.DISCOVERED:
                            state_store.transition_video(
                                youtube_id, VideoStatus.DOWNLOADING
                            )
                    source_path = download_video(
                        candidate,
                        theme_root
                        / "downloads"
                        / _safe_creator_directory(candidate.creator),
                        access=request.youtube_access,
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
                    if state_store is not None and youtube_id:
                        record = state_store.get_video(youtube_id)
                        if record and record.status == VideoStatus.DOWNLOADING:
                            state_store.transition_video(
                                youtube_id, VideoStatus.DOWNLOADED
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
                    if len(analysis.clips) != 3:
                        raise ValueError(
                            "Produção automática exige exatamente 3 cortes; "
                            f"o provider retornou {len(analysis.clips)}."
                        )
                    if state_store is None or not youtube_id:
                        raise ValueError(
                            "Produção automática requer o SQLite e um youtube_id."
                        )
                    state_store.transition_video(youtube_id, VideoStatus.ANALYZING)
                    state_store.transition_video(youtube_id, VideoStatus.ANALYZED)
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
                        / "creators"
                        / _safe_creator_directory(candidate.creator)
                        / youtube_id
                    )
                    render_settings = replace(
                        settings, output_dir=output, exact_index_filenames=True
                    )
                    state_store.transition_video(youtube_id, VideoStatus.RENDERING)
                    render = run_pipeline(
                        source_path,
                        list(analysis.clips),
                        render_settings,
                        cancel=cancel,
                        on_progress=lambda progress, item=candidate: emit(
                            AutomationEvent(
                                "render_progress",
                                creator=item.creator,
                                title=item.title,
                                url=item.url,
                                index=progress.index,
                                total=3,
                                progress=progress.progress,
                                message=progress.status,
                            )
                        ),
                    )
                    artifacts = tuple(
                        ShortArtifact(item.index, item.output_path)
                        for item in render.renders
                    )
                    if len(artifacts) != 3:
                        raise ValueError(
                            "A renderização não produziu exatamente 3 Shorts."
                        )
                    state_store.record_rendered_shorts(
                        youtube_id,
                        tuple(
                            RenderedShort(item.clip_index, item.file_path)
                            for item in artifacts
                        ),
                    )
                    emit(
                        _candidate_event(
                            "render_finished", candidate, index, len(candidates)
                        )
                    )
                    stage = "queue"
                    assignments = assign_shorts_to_accounts(artifacts, theme.accounts)
                    distributed = ()
                    try:
                        distributed = distribute_short_files(
                            assignments, theme_root / "accounts", youtube_id
                        )
                        state_store.record_queued_shorts(
                            youtube_id,
                            tuple(
                                QueuedShort(
                                    item.assignment.clip_index,
                                    item.assignment.account_slot,
                                    item.assignment.account.email,
                                    item.queue_path,
                                )
                                for item in distributed
                            ),
                        )
                    except Exception:
                        for item in distributed:
                            item.queue_path.unlink(missing_ok=True)
                        raise
                    emit(
                        _candidate_event(
                            "queue_finished", candidate, index, len(candidates)
                        )
                    )
                    try:
                        source_path.unlink()
                        emit(
                            AutomationEvent(
                                "source_removed",
                                creator=candidate.creator,
                                title=candidate.title,
                                path=source_path,
                            )
                        )
                    except OSError as exc:
                        emit(
                            AutomationEvent(
                                "cleanup_warning",
                                creator=candidate.creator,
                                title=candidate.title,
                                path=source_path,
                                message=str(exc),
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
                    if state_store is not None and youtube_id:
                        record = state_store.get_video(youtube_id)
                        if record and record.status not in {
                            VideoStatus.RENDERED,
                            VideoStatus.QUEUED,
                            VideoStatus.COMPLETED,
                            VideoStatus.FAILED,
                        }:
                            state_store.fail_video(youtube_id, str(exc))
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


def _candidate_skipped_event(skipped: CandidateSkip) -> AutomationEvent:
    return AutomationEvent(
        "candidate_skipped",
        creator=skipped.creator,
        title=skipped.title,
        url=skipped.url,
        message=skipped.reason,
        data={"technical_detail": skipped.detail},
    )


def _safe_creator_directory(name: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in "-_" else "_" for char in name)
    return cleaned.strip("._") or "creator"
