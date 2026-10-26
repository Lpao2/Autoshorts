"""Market-aware source discovery and one-at-a-time ingestion."""

from __future__ import annotations

import hashlib
import logging
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from autoshorts.collection import (
    CandidateSkip,
    VideoCandidate,
    YouTubeAccess,
    YouTubeCollectionError,
    YouTubeErrorCategory,
    candidate_youtube_id,
    discover_creator_videos,
    download_video,
)
from autoshorts.editorial_sources import (
    ContentSource,
    SourceRightsStatus,
    SourceValidationStatus,
    ThemeContentSource,
    ThemeMarketProfile,
)
from autoshorts.media_probe import MediaInfo, probe_media
from autoshorts.naming import sanitize_filename
from autoshorts.state_store import StateStore, VideoStatus

Discover = Callable[..., list[VideoCandidate]]
Download = Callable[..., Path]
Probe = Callable[[Path, str], MediaInfo]
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SourceCandidate:
    source: ContentSource
    association: ThemeContentSource
    video: VideoCandidate
    external_video_id: str


@dataclass(frozen=True, slots=True)
class IngestionSkip:
    source: str
    title: str
    external_video_id: str | None
    reason: str
    detail: str


@dataclass(frozen=True, slots=True)
class DiscoveryReport:
    profile: ThemeMarketProfile
    sources: tuple[tuple[ThemeContentSource, ContentSource], ...]
    examined: int
    eligible: tuple[SourceCandidate, ...]
    skipped: tuple[IngestionSkip, ...]
    selected: SourceCandidate | None


@dataclass(frozen=True, slots=True)
class IngestionResult:
    discovery: DiscoveryReport
    local_path: Path
    checksum: str
    media: MediaInfo
    decode_passed: bool


def discover_next_source_video(
    profile: ThemeMarketProfile,
    state: StateStore,
    *,
    per_source: int = 5,
    maximum_age_days: int = 3650,
    access: YouTubeAccess | None = None,
    discover: Discover = discover_creator_videos,
) -> DiscoveryReport:
    """Discover candidates; persist metadata cache and bounded failure cooldowns."""
    if per_source < 1:
        raise ValueError("per_source must be positive.")
    linked = tuple(
        (association, source)
        for association, source in state.theme_content_sources(profile.profile_id)
        if association.enabled
        and source.enabled
        and source.validation_status is SourceValidationStatus.VALIDATED
        and source.rights_status is not SourceRightsStatus.RESTRICTED
    )
    eligible: list[SourceCandidate] = []
    skipped: list[IngestionSkip] = []
    examined = 0
    for association, source in linked:
        remote_skips: list[CandidateSkip] = []
        try:
            videos = discover(
                source.display_name,
                _videos_url(source),
                quantity=per_source,
                age_days=maximum_age_days,
                min_duration_seconds=association.target_duration_min_seconds,
                max_duration_seconds=association.target_duration_max_seconds,
                access=access or YouTubeAccess(),
                skip_handler=remote_skips.append,
                candidate_allowed=lambda video_id: state.source_candidate_available(
                    profile.profile_id, video_id
                ),
                failure_handler=lambda video_id, category, detail: (
                    state.record_source_failure(
                        profile.profile_id,
                        video_id,
                        category=category.value,
                        detail=detail,
                    )
                ),
                metadata_lookup=lambda video_id: _cached_candidate(state, video_id),
                metadata_save=lambda video: _save_candidate_metadata(state, video),
            )
        except YouTubeCollectionError as exc:
            if exc.category not in {
                YouTubeErrorCategory.AUTH_REQUIRED,
                YouTubeErrorCategory.VIDEO_UNAVAILABLE,
            }:
                raise
            skipped.append(
                IngestionSkip(
                    source.display_name, "", None, exc.category.value, str(exc)
                )
            )
            continue
        examined += len(videos) + len(remote_skips)
        skipped.extend(
            IngestionSkip(
                source.display_name,
                item.title,
                candidate_youtube_id(
                    VideoCandidate(item.creator, item.url, item.title, None, None)
                ),
                item.reason,
                item.detail,
            )
            for item in remote_skips
        )
        for video in videos:
            external_id = candidate_youtube_id(video)
            reason = _rejection_reason(
                video, external_id, association, state, profile.profile_id
            )
            if reason is not None:
                skipped.append(
                    IngestionSkip(
                        source.display_name,
                        video.title,
                        external_id,
                        reason[0],
                        reason[1],
                    )
                )
                continue
            assert external_id is not None
            eligible.append(SourceCandidate(source, association, video, external_id))
    eligible.sort(key=_candidate_sort_key)
    return DiscoveryReport(
        profile,
        linked,
        examined,
        tuple(eligible),
        tuple(skipped),
        eligible[0] if eligible else None,
    )


def ingest(
    candidate: SourceCandidate,
    profile: ThemeMarketProfile,
    state: StateStore,
    *,
    workspace: Path,
    ffprobe_path: str = "ffprobe",
    ffmpeg_path: str = "ffmpeg",
    access: YouTubeAccess | None = None,
    download: Download = download_video,
    probe: Probe = probe_media,
) -> IngestionResult:
    """Download and validate one selected source video using legacy components."""
    existing = state.get_video(candidate.external_video_id)
    if existing is not None and existing.status is not VideoStatus.FAILED:
        raise ValueError("Source video is already registered and cannot be ingested.")
    if existing is not None:
        state.prepare_failed_video_retry(candidate.external_video_id)
    published_at = (
        candidate.video.upload_date.isoformat()
        if candidate.video.upload_date is not None
        else None
    )
    state.register_video(
        youtube_id=candidate.external_video_id,
        source_url=candidate.video.url,
        creator_name=candidate.source.display_name,
        theme_name=profile.theme_key,
        title=candidate.video.title,
        platform=profile.platform,
        content_source_id=candidate.source.source_id,
        duration_seconds=candidate.video.duration_seconds,
        published_at=published_at,
    )
    state.transition_video(candidate.external_video_id, VideoStatus.DOWNLOADING)
    destination = (
        Path(workspace).expanduser().resolve()
        / profile.theme_key
        / "downloads"
        / sanitize_filename(candidate.source.display_name)
    )
    try:
        local_path = download(
            candidate.video, destination, access=access or YouTubeAccess()
        )
        media = probe(local_path, ffprobe_path)
        if media.duration_seconds <= 0:
            raise ValueError("Downloaded source has no positive duration.")
        if not media.has_audio:
            raise ValueError("Downloaded source has no audio stream.")
        _decode(local_path, ffmpeg_path, duration_seconds=media.duration_seconds)
        checksum = _checksum(local_path)
        state.record_downloaded_video(
            candidate.external_video_id,
            local_path=local_path,
            checksum=checksum,
            duration_seconds=round(media.duration_seconds),
        )
    except Exception as exc:
        _cleanup_partials(destination, candidate.external_video_id)
        current = state.get_video(candidate.external_video_id)
        if current and current.status in {
            VideoStatus.DISCOVERED,
            VideoStatus.DOWNLOADING,
        }:
            state.fail_video(candidate.external_video_id, str(exc))
        raise
    return IngestionResult(
        DiscoveryReport(profile, (), 1, (candidate,), (), candidate),
        local_path.resolve(),
        checksum,
        media,
        True,
    )


def _rejection_reason(
    video: VideoCandidate,
    external_id: str | None,
    association: ThemeContentSource,
    state: StateStore,
    profile_id: int,
) -> tuple[str, str] | None:
    if external_id is None:
        return "missing_video_id", "YouTube video ID ausente"
    if not state.source_candidate_available(profile_id, external_id):
        failure = state.source_failure(profile_id, external_id)
        return "cooldown", str(failure["retry_after"] if failure else "blocked")
    existing = state.get_video(external_id)
    if existing is not None and existing.status is not VideoStatus.FAILED:
        return "already_processed", "YouTube video ID já registrado"
    duration = video.duration_seconds
    if duration is None:
        return "missing_duration", "duração ausente"
    if duration <= 180:
        return "shorts", f"{duration}s"
    if not (
        association.target_duration_min_seconds
        <= duration
        <= association.target_duration_max_seconds
    ):
        return "duration", f"{duration}s"
    return None


def record_ingestion_failure(
    state: StateStore,
    profile: ThemeMarketProfile,
    youtube_id: str,
    error: Exception,
) -> str | None:
    category = (
        error.category
        if isinstance(error, YouTubeCollectionError)
        else YouTubeErrorCategory.DOWNLOAD_ERROR
    )
    return state.record_source_failure(
        profile.profile_id,
        youtube_id,
        category=category.value,
        detail=str(error),
    )


def ingest_with_replacement(
    profile: ThemeMarketProfile,
    state: StateStore,
    *,
    workspace: Path,
    theme_prompt: str,
    maximum_age_days: int,
    access: YouTubeAccess,
    ffprobe_path: str = "ffprobe",
    ffmpeg_path: str = "ffmpeg",
    max_attempts: int = 5,
    discover=None,
    ingest_candidate=None,
    fallback=None,
) -> str:
    """Try approved candidates first, then one bounded Gemini source extension."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive.")
    discover = discover or discover_next_source_video
    ingest_candidate = ingest_candidate or ingest
    if fallback is None:
        from autoshorts.source_fallback import extend_sources

        fallback = extend_sources
    attempts = 0
    attempted: set[str] = set()
    for phase in range(2):
        report = discover(
            profile, state, maximum_age_days=maximum_age_days, access=access
        )
        for candidate in report.eligible:
            video_id = candidate.external_video_id
            if video_id in attempted or not state.source_candidate_available(
                profile.profile_id, video_id
            ):
                continue
            if attempts >= max_attempts:
                raise RuntimeError("Source replacement download budget exhausted.")
            attempted.add(video_id)
            attempts += 1
            try:
                ingest_candidate(
                    candidate,
                    profile,
                    state,
                    workspace=workspace,
                    ffprobe_path=ffprobe_path,
                    ffmpeg_path=ffmpeg_path,
                    access=access,
                )
                LOGGER.info(
                    "Source selected profile=%s video=%s attempt=%s",
                    profile.profile_id,
                    video_id,
                    attempts,
                )
                return video_id
            except YouTubeCollectionError as exc:
                retry_after = record_ingestion_failure(state, profile, video_id, exc)
                LOGGER.warning(
                    "Source rejected profile=%s video=%s category=%s retry_after=%s",
                    profile.profile_id,
                    video_id,
                    exc.category,
                    retry_after,
                )
                if exc.category not in {
                    YouTubeErrorCategory.AUTH_REQUIRED,
                    YouTubeErrorCategory.VIDEO_UNAVAILABLE,
                }:
                    raise
            except Exception as exc:
                record_ingestion_failure(state, profile, video_id, exc)
                raise
        if phase == 1 or attempts >= max_attempts:
            break
        if not fallback(profile, state, theme_prompt=theme_prompt, access=access):
            break
    raise RuntimeError("No eligible source after bounded editorial replacement.")


def _cached_candidate(state: StateStore, youtube_id: str) -> VideoCandidate | None:
    row = state.cached_source_metadata(youtube_id)
    if row is None:
        return None
    return VideoCandidate(
        row["creator"],
        row["url"],
        row["title"],
        date.fromisoformat(row["upload_date"]) if row["upload_date"] else None,
        row["duration_seconds"],
        youtube_id,
    )


def _save_candidate_metadata(state: StateStore, candidate: VideoCandidate) -> None:
    youtube_id = candidate_youtube_id(candidate)
    if youtube_id is None:
        return
    state.save_source_metadata(
        youtube_id,
        creator=candidate.creator,
        url=candidate.url,
        title=candidate.title,
        upload_date=(
            candidate.upload_date.isoformat() if candidate.upload_date else None
        ),
        duration_seconds=candidate.duration_seconds,
    )


def _candidate_sort_key(candidate: SourceCandidate) -> tuple[int, int, str]:
    published = candidate.video.upload_date or date.min
    return (
        candidate.association.priority,
        -published.toordinal(),
        candidate.external_video_id,
    )


def _videos_url(source: ContentSource) -> str:
    base = source.channel_url.rstrip("/")
    if base.endswith("/videos"):
        return base
    return f"{base}/videos"


def _decode(
    path: Path,
    ffmpeg_path: str,
    *,
    duration_seconds: float,
    sample_seconds: float = 8.0,
    timeout_seconds: int = 120,
) -> None:
    """Decode representative samples without rereading the entire source.

    ``ffprobe`` has already validated the container and stream inventory. Sampling
    the beginning, midpoint, and tail catches the common truncated/corrupt media
    cases while keeping ingestion bounded for long source videos.
    """
    if duration_seconds <= 0:
        raise ValueError("FFmpeg decode requires a positive media duration.")
    last_start = max(0.0, duration_seconds - sample_seconds)
    offsets = tuple(dict.fromkeys((0.0, max(0.0, duration_seconds / 2), last_start)))
    for offset in offsets:
        command = [ffmpeg_path, "-nostdin", "-v", "error", "-xerror"]
        if offset:
            command.extend(("-ss", f"{offset:.3f}"))
        command.extend(
            (
                "-i",
                str(path),
                "-t",
                f"{min(sample_seconds, duration_seconds):.3f}",
                "-map",
                "0:v:0?",
                "-map",
                "0:a:0?",
                "-sn",
                "-f",
                "null",
                "-",
            )
        )
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"FFmpeg sample decode timed out at {offset:.3f}s."
            ) from exc
        if result.returncode != 0:
            raise RuntimeError(
                f"FFmpeg sample decode failed at {offset:.3f}s: {result.stderr[-2000:]}"
            )


def _checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _cleanup_partials(directory: Path, external_video_id: str) -> None:
    if not directory.exists():
        return
    for path in directory.glob(f"{external_video_id}*.part*"):
        path.unlink(missing_ok=True)
