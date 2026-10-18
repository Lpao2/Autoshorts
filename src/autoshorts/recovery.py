"""Inspection-first recovery for interrupted local production jobs."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from autoshorts.automation_config import ThemeProfile
from autoshorts.distribution import (
    ShortArtifact,
    assign_shorts_to_accounts,
    distribute_short_files,
)
from autoshorts.media_probe import MediaProbeError, probe_media
from autoshorts.state_store import QueuedShort, StateStore, VideoStatus


class RecoveryAction(StrEnum):
    DOWNLOAD = "DOWNLOAD"
    REDOWNLOAD = "REDOWNLOAD"
    REUSE_SOURCE = "REUSE_SOURCE"
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


def inspect_video_state(
    store: StateStore,
    youtube_id: str,
    *,
    theme_root: Path,
    creator_directory: str,
    ffprobe_path: str = "ffprobe",
    expected_width: int | None = None,
    expected_height: int | None = None,
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
    if record.status in {
        VideoStatus.TRANSCRIBING,
        VideoStatus.TRANSCRIBED,
        VideoStatus.ANALYZING,
        VideoStatus.ANALYZED,
    }:
        return RecoveryPlan(
            youtube_id,
            record.status,
            RecoveryAction.UNSUPPORTED,
            "Recovery for this future stage is not implemented yet.",
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
    if plan.next_action not in {RecoveryAction.REDOWNLOAD, RecoveryAction.RERENDER}:
        return
    for path in plan.cleanup_actions:
        path.unlink(missing_ok=True)
    target = (
        VideoStatus.DOWNLOADED
        if plan.next_action == RecoveryAction.RERENDER
        else VideoStatus.DISCOVERED
    )
    store.reset_for_recovery(plan.youtube_id, target)


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
    assignments = assign_shorts_to_accounts(artifacts, theme.accounts)
    distributed = distribute_short_files(assignments, accounts_root, plan.youtube_id)
    try:
        store.record_queued_shorts(
            plan.youtube_id,
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
    return tuple(item.queue_path for item in distributed)


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
