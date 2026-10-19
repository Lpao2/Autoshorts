"""Inspection-first recovery for interrupted local production jobs."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path

from autoshorts.automation_config import ThemeProfile
from autoshorts.config import Settings
from autoshorts.distribution import (
    ShortArtifact,
    assign_shorts_to_channels,
    distribute_short_files,
)
from autoshorts.media_probe import MediaProbeError, probe_media
from autoshorts.pipeline import PipelineResult, run_pipeline
from autoshorts.production_metadata import rendered_short_records
from autoshorts.state_store import QueuedShort, StateStore, VideoStatus
from autoshorts.transcript_analysis import load_analysis
from autoshorts.transcription import load_transcript


class RecoveryAction(StrEnum):
    DOWNLOAD = "DOWNLOAD"
    REDOWNLOAD = "REDOWNLOAD"
    REUSE_SOURCE = "REUSE_SOURCE"
    RETRANSCRIBE = "RETRANSCRIBE"
    RECONCILE_TRANSCRIPT = "RECONCILE_TRANSCRIPT"
    REUSE_TRANSCRIPT = "REUSE_TRANSCRIPT"
    REANALYZE = "REANALYZE"
    RECONCILE_ANALYSIS = "RECONCILE_ANALYSIS"
    REUSE_ANALYSIS = "REUSE_ANALYSIS"
    RERENDER = "RERENDER"
    DISTRIBUTE = "DISTRIBUTE"
    NOOP = "NOOP"
    UNSUPPORTED = "UNSUPPORTED"
    MANUAL_RETRY = "MANUAL_RETRY"
    INCONSISTENT = "INCONSISTENT"


@dataclass(frozen=True, slots=True)
class RecoveryPlan:
    youtube_id: str
    current_state: VideoStatus
    next_action: RecoveryAction
    reason: str
    reusable_artifacts: tuple[Path, ...] = ()
    cleanup_actions: tuple[Path, ...] = ()


@dataclass(frozen=True, slots=True)
class ProductionResumeResult:
    render: PipelineResult
    queue_paths: tuple[Path, ...]
    link_methods: tuple[str, ...]


def inspect_video_state(
    store: StateStore,
    youtube_id: str,
    *,
    theme_root: Path,
    creator_directory: str,
    ffprobe_path: str = "ffprobe",
    expected_width: int | None = None,
    expected_height: int | None = None,
    expected_analysis_prompt_hash: str | None = None,
) -> RecoveryPlan:
    """Inspect SQLite and filesystem without changing either one."""
    record = store.get_video(youtube_id)
    if record is None:
        raise KeyError(f"Unknown YouTube ID: {youtube_id}")
    source = theme_root / "downloads" / creator_directory / f"{youtube_id}.mp4"
    master_root = theme_root / "creators" / creator_directory / youtube_id
    expected_masters = tuple(master_root / f"{index:02d}.mp4" for index in range(1, 4))
    partial_downloads = _download_residues(source.parent, youtube_id)
    partial_renders = tuple(sorted(master_root.glob("*.part*")))
    transcript_path = theme_root / ".work" / youtube_id / "transcript.json"
    analysis_path = theme_root / ".work" / youtube_id / "analysis.json"

    if record.status in {VideoStatus.QUEUED, VideoStatus.COMPLETED}:
        return RecoveryPlan(
            youtube_id,
            record.status,
            RecoveryAction.NOOP,
            "Production already finished for this video.",
        )
    if record.status == VideoStatus.FAILED:
        return RecoveryPlan(
            youtube_id,
            record.status,
            RecoveryAction.MANUAL_RETRY,
            "FAILED requires an explicit retry decision.",
        )
    if record.status == VideoStatus.TRANSCRIBING:
        transcript = _valid_transcript(transcript_path, youtube_id)
        if transcript is not None:
            return RecoveryPlan(
                youtube_id,
                record.status,
                RecoveryAction.RECONCILE_TRANSCRIPT,
                "Transcript artifact is valid but SQLite was not promoted.",
                reusable_artifacts=(transcript_path,),
            )
        if _valid_media(source, ffprobe_path):
            return RecoveryPlan(
                youtube_id,
                record.status,
                RecoveryAction.RETRANSCRIBE,
                "Interrupted transcription will restart from the valid source.",
                reusable_artifacts=(source,),
                cleanup_actions=(
                    (transcript_path,) if transcript_path.exists() else ()
                ),
            )
        return RecoveryPlan(
            youtube_id,
            record.status,
            RecoveryAction.REDOWNLOAD,
            "Interrupted transcription has no valid source.",
            cleanup_actions=((transcript_path,) if transcript_path.exists() else ()),
        )
    if record.status == VideoStatus.TRANSCRIBED:
        transcript_record = store.get_transcript(youtube_id)
        path = transcript_record.file_path if transcript_record else transcript_path
        if _valid_transcript(path, youtube_id) is not None:
            return RecoveryPlan(
                youtube_id,
                record.status,
                RecoveryAction.REUSE_TRANSCRIPT,
                "Transcript is valid and ready for the future analysis stage.",
                reusable_artifacts=(path,),
            )
        if _valid_media(source, ffprobe_path):
            return RecoveryPlan(
                youtube_id,
                record.status,
                RecoveryAction.RETRANSCRIBE,
                "Transcript is absent or invalid; retranscription is required.",
                reusable_artifacts=(source,),
                cleanup_actions=((path,) if path.exists() else ()),
            )
        return RecoveryPlan(
            youtube_id,
            record.status,
            RecoveryAction.REDOWNLOAD,
            "Transcript and source are absent or invalid.",
            cleanup_actions=((path,) if path.exists() else ()),
        )
    if record.status in {VideoStatus.ANALYZING, VideoStatus.ANALYZED}:
        transcript_record = store.get_transcript(youtube_id)
        transcript_file = (
            transcript_record.file_path if transcript_record else transcript_path
        )
        transcript = _valid_transcript(transcript_file, youtube_id)
        analysis = (
            _valid_analysis(analysis_path, transcript, youtube_id)
            if transcript is not None
            else None
        )
        prompt_matches = bool(
            analysis
            and (
                expected_analysis_prompt_hash is None
                or analysis.prompt_hash == expected_analysis_prompt_hash
            )
        )
        if transcript is not None and prompt_matches:
            action = (
                RecoveryAction.RECONCILE_ANALYSIS
                if record.status == VideoStatus.ANALYZING
                else RecoveryAction.REUSE_ANALYSIS
            )
            return RecoveryPlan(
                youtube_id,
                record.status,
                action,
                "Analysis artifact is valid and can be reused.",
                reusable_artifacts=(analysis_path, transcript_file),
            )
        if transcript is not None:
            return RecoveryPlan(
                youtube_id,
                record.status,
                RecoveryAction.REANALYZE,
                "Analysis is absent or invalid; transcript can be reused.",
                reusable_artifacts=(transcript_file,),
                cleanup_actions=((analysis_path,) if analysis_path.exists() else ()),
            )
        if _valid_media(source, ffprobe_path):
            return RecoveryPlan(
                youtube_id,
                record.status,
                RecoveryAction.RETRANSCRIBE,
                "Analysis recovery requires a valid transcript first.",
                reusable_artifacts=(source,),
                cleanup_actions=tuple(
                    path for path in (analysis_path, transcript_file) if path.exists()
                ),
            )
        return RecoveryPlan(
            youtube_id,
            record.status,
            RecoveryAction.REDOWNLOAD,
            "Analysis, transcript and source are unavailable.",
            cleanup_actions=tuple(
                path for path in (analysis_path, transcript_file) if path.exists()
            ),
        )
    if record.status == VideoStatus.DISCOVERED:
        return RecoveryPlan(
            youtube_id, record.status, RecoveryAction.DOWNLOAD, "Download not started."
        )
    if record.status == VideoStatus.DOWNLOADING:
        return RecoveryPlan(
            youtube_id,
            record.status,
            RecoveryAction.REDOWNLOAD,
            "Interrupted download will restart from a clean job-specific target.",
            cleanup_actions=partial_downloads,
        )
    if record.status == VideoStatus.DOWNLOADED:
        if _valid_media(source, ffprobe_path):
            return RecoveryPlan(
                youtube_id,
                record.status,
                RecoveryAction.REUSE_SOURCE,
                "Downloaded source passed FFprobe validation.",
                reusable_artifacts=(source,),
            )
        return RecoveryPlan(
            youtube_id,
            record.status,
            RecoveryAction.REDOWNLOAD,
            "Downloaded source is absent or invalid.",
            cleanup_actions=(
                *partial_downloads,
                *((source,) if source.exists() else ()),
            ),
        )
    if record.status == VideoStatus.RENDERING:
        valid_masters, invalid_masters = _inspect_masters(
            expected_masters, ffprobe_path, expected_width, expected_height
        )
        cleanup = (*partial_renders, *invalid_masters)
        if not _valid_media(source, ffprobe_path):
            return RecoveryPlan(
                youtube_id,
                record.status,
                RecoveryAction.REDOWNLOAD,
                "Render was interrupted and its source is absent or invalid.",
                reusable_artifacts=valid_masters,
                cleanup_actions=cleanup,
            )
        return RecoveryPlan(
            youtube_id,
            record.status,
            RecoveryAction.RERENDER,
            "Render was interrupted; valid masters may be reused and remaining "
            "outputs rerendered.",
            reusable_artifacts=(source, *valid_masters),
            cleanup_actions=cleanup,
        )
    if record.status == VideoStatus.RENDERED:
        rows = store.shorts_for_video(youtube_id)
        recorded = tuple(row.file_path for row in rows)
        valid, invalid = _inspect_masters(
            recorded, ffprobe_path, expected_width, expected_height
        )
        partial_queues = tuple(
            sorted((theme_root / "accounts").glob(f"*/queue/{youtube_id}_*.mp4"))
        )
        if len(rows) == 3 and len(valid) == 3 and not invalid:
            return RecoveryPlan(
                youtube_id,
                record.status,
                RecoveryAction.DISTRIBUTE,
                "Three recorded masters are valid; resume at distribution.",
                reusable_artifacts=valid,
                cleanup_actions=partial_queues,
            )
        return RecoveryPlan(
            youtube_id,
            record.status,
            RecoveryAction.INCONSISTENT,
            "RENDERED requires exactly three valid recorded masters.",
            reusable_artifacts=valid,
            cleanup_actions=partial_queues,
        )
    raise ValueError(f"Unhandled video state: {record.status}")


def prepare_recovery(store: StateStore, plan: RecoveryPlan) -> None:
    """Execute only cleanup/reset actions selected by an inspection plan."""
    if plan.next_action not in {
        RecoveryAction.REDOWNLOAD,
        RecoveryAction.RERENDER,
        RecoveryAction.RETRANSCRIBE,
        RecoveryAction.REANALYZE,
    }:
        return
    for path in plan.cleanup_actions:
        path.unlink(missing_ok=True)
    target = (
        VideoStatus.TRANSCRIBED
        if plan.next_action == RecoveryAction.REANALYZE
        else VideoStatus.DOWNLOADED
        if plan.next_action in {RecoveryAction.RERENDER, RecoveryAction.RETRANSCRIBE}
        else VideoStatus.DISCOVERED
    )
    store.reset_for_recovery(plan.youtube_id, target)


def reconcile_transcript(store: StateStore, plan: RecoveryPlan):
    if plan.next_action != RecoveryAction.RECONCILE_TRANSCRIPT:
        raise ValueError(f"Recovery plan cannot reconcile: {plan.next_action}")
    path = plan.reusable_artifacts[0]
    transcript = load_transcript(path, youtube_id=plan.youtube_id)
    store.record_transcript(
        plan.youtube_id,
        file_path=path,
        provider=transcript.provider,
        model=transcript.model,
        language=transcript.language,
    )
    return transcript


def reconcile_analysis(store: StateStore, plan: RecoveryPlan):
    if plan.next_action != RecoveryAction.RECONCILE_ANALYSIS:
        raise ValueError(f"Recovery plan cannot reconcile: {plan.next_action}")
    analysis_path, transcript_path = plan.reusable_artifacts
    transcript = load_transcript(transcript_path, youtube_id=plan.youtube_id)
    analysis = load_analysis(analysis_path, transcript, youtube_id=plan.youtube_id)
    store.record_analysis(
        plan.youtube_id,
        file_path=analysis_path,
        provider=analysis.result.provider,
        model=analysis.result.model,
        prompt_hash=analysis.prompt_hash,
        usage=analysis.result.usage,
    )
    return analysis


def resume_rendered_distribution(
    store: StateStore,
    plan: RecoveryPlan,
    theme: ThemeProfile,
    accounts_root: Path,
) -> tuple[Path, ...]:
    """Idempotently reconcile partial queues and commit RENDERED -> QUEUED."""
    if plan.next_action == RecoveryAction.NOOP:
        return tuple(
            row.queue_path
            for row in store.shorts_for_video(plan.youtube_id)
            if row.queue_path is not None
        )
    if plan.next_action != RecoveryAction.DISTRIBUTE:
        raise ValueError(f"Recovery plan cannot distribute: {plan.next_action}")
    for path in plan.cleanup_actions:
        path.unlink(missing_ok=True)
    artifacts = tuple(
        ShortArtifact(index, path)
        for index, path in enumerate(plan.reusable_artifacts, start=1)
    )
    assignments = assign_shorts_to_channels(artifacts, theme.channels)
    distributed = distribute_short_files(assignments, accounts_root, plan.youtube_id)
    try:
        store.record_queued_shorts(
            plan.youtube_id,
            tuple(
                QueuedShort(
                    item.assignment.clip_index,
                    item.assignment.account_slot,
                    item.assignment.channel.channel_id,
                    item.queue_path,
                )
                for item in distributed
            ),
        )
    except Exception:
        for item in distributed:
            item.queue_path.unlink(missing_ok=True)
        raise
    return tuple(item.queue_path for item in distributed)


def resume_analyzed_production(
    store: StateStore,
    youtube_id: str,
    *,
    theme_root: Path,
    creator_directory: str,
    theme: ThemeProfile,
    settings: Settings,
    remove_source_after_queue: bool = True,
) -> ProductionResumeResult:
    """Resume one known ANALYZED video locally, without discovery or providers."""
    plan = inspect_video_state(
        store,
        youtube_id,
        theme_root=theme_root,
        creator_directory=creator_directory,
        ffprobe_path=settings.ffprobe_path,
        expected_width=settings.output_width,
        expected_height=settings.output_height,
    )
    if plan.next_action not in {RecoveryAction.REUSE_ANALYSIS, RecoveryAction.RERENDER}:
        raise ValueError(f"Expected reusable analyzed job, got {plan.next_action}.")
    if plan.next_action == RecoveryAction.RERENDER:
        prepare_recovery(store, plan)
        analysis_path = theme_root / ".work" / youtube_id / "analysis.json"
        transcript_path = theme_root / ".work" / youtube_id / "transcript.json"
    else:
        analysis_path, transcript_path = plan.reusable_artifacts
    transcript = load_transcript(transcript_path, youtube_id=youtube_id)
    analysis = load_analysis(analysis_path, transcript, youtube_id=youtube_id)
    source_path = theme_root / "downloads" / creator_directory / f"{youtube_id}.mp4"
    if not _valid_media(source_path, settings.ffprobe_path):
        raise ValueError("The persisted source is absent or invalid.")
    output = theme_root / "creators" / creator_directory / youtube_id
    render_settings = replace(
        settings,
        output_dir=output,
        exact_index_filenames=True,
        overwrite_policy="error",
    )
    store.transition_video(youtube_id, VideoStatus.RENDERING)
    render = run_pipeline(source_path, list(analysis.clips.clips), render_settings)
    paths = tuple(item.output_path for item in render.renders)
    store.record_rendered_shorts(
        youtube_id,
        rendered_short_records(paths, analysis_path, analysis, transcript),
    )
    artifacts = tuple(ShortArtifact(index, path) for index, path in enumerate(paths, 1))
    assignments = assign_shorts_to_channels(artifacts, theme.channels)
    distributed = distribute_short_files(
        assignments, theme_root / "accounts", youtube_id
    )
    try:
        store.record_queued_shorts(
            youtube_id,
            tuple(
                QueuedShort(
                    item.assignment.clip_index,
                    item.assignment.account_slot,
                    item.assignment.channel.channel_id,
                    item.queue_path,
                )
                for item in distributed
            ),
        )
    except Exception:
        for item in distributed:
            item.queue_path.unlink(missing_ok=True)
        raise
    if remove_source_after_queue:
        source_path.unlink(missing_ok=True)
    return ProductionResumeResult(
        render,
        tuple(item.queue_path for item in distributed),
        tuple(item.method.value for item in distributed),
    )


def _download_residues(directory: Path, youtube_id: str) -> tuple[Path, ...]:
    if not directory.is_dir():
        return ()
    return tuple(
        sorted(
            path
            for path in directory.glob(f"{youtube_id}*")
            if path.is_file() and (".part" in path.name or path.suffix == ".ytdl")
        )
    )


def _valid_media(path: Path, ffprobe_path: str) -> bool:
    try:
        info = probe_media(path, ffprobe_path)
    except (FileNotFoundError, MediaProbeError, OSError):
        return False
    return info.duration_seconds > 0


def _valid_transcript(path: Path, youtube_id: str):
    try:
        return load_transcript(path, youtube_id=youtube_id)
    except ValueError:
        return None


def _valid_analysis(path: Path, transcript, youtube_id: str):
    try:
        return load_analysis(path, transcript, youtube_id=youtube_id)
    except (ValueError, OSError):
        return None


def _inspect_masters(
    paths: tuple[Path, ...],
    ffprobe_path: str,
    expected_width: int | None,
    expected_height: int | None,
) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    valid: list[Path] = []
    invalid: list[Path] = []
    for path in paths:
        try:
            info = probe_media(path, ffprobe_path)
            dimensions_match = (
                expected_width is None
                or expected_height is None
                or (info.width, info.height) == (expected_width, expected_height)
            )
            (
                valid if info.duration_seconds > 0 and dimensions_match else invalid
            ).append(path)
        except (FileNotFoundError, MediaProbeError, OSError):
            invalid.append(path)
    return tuple(valid), tuple(invalid)
