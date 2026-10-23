"""SQLite operational state for restart-safe, headless automation workers."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import asdict, dataclass
from dataclasses import replace as dataclass_replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from autoshorts.automation_config import ColorPalette
from autoshorts.brand_identity import (
    BrandCandidate,
    BrandGenerationResult,
    BrandKitStatus,
    ChannelBrandKit,
    HandleAvailability,
    normalize_handle_candidate,
)
from autoshorts.brand_planning import (
    PlannedBrandStatus,
    PlannedChannelBrand,
    PlannedIdentityAssignment,
    global_brand_collisions,
)
from autoshorts.editorial_sources import (
    ChannelPostingOverride,
    ContentSource,
    PostingConfidence,
    PostingStrategy,
    PostingWindow,
    SourceRightsStatus,
    SourceValidationStatus,
    ThemeContentSource,
    ThemeMarketProfile,
    ThemePostingProfile,
    TimingPolicy,
)
from autoshorts.scheduler_models import (
    PublicationJob,
    SchedulerJobEvent,
    SchedulerJobState,
    SchedulerSettings,
    SchedulerUrgency,
)
from autoshorts.theme_catalog import (
    AuthorizationStatus,
    BrandBlueprint,
    BrandProfile,
    CatalogProvenance,
    CatalogStatus,
    ChannelArchetype,
    PromptVersion,
    ReuseRisk,
    RightsProfile,
    SourceStrategy,
    ThemeReadiness,
    ThemeTemplate,
    load_theme_seed,
)
from autoshorts.transcript_analysis import AnalysisUsage

STATE_DB_ENV = "AUTOSHORTS_STATE_DB"
SCHEMA_VERSION = 30


class ReviewPolicy(StrEnum):
    MANUAL_REVIEW = "MANUAL_REVIEW"
    AUTO_APPROVE = "AUTO_APPROVE"


class MetadataDraftStatus(StrEnum):
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    APPROVED = "APPROVED"
    FAILED_RECOVERABLE = "FAILED_RECOVERABLE"
    FAILED_CONFIGURATION = "FAILED_CONFIGURATION"


class VideoStatus(StrEnum):
    DISCOVERED = "DISCOVERED"
    DOWNLOADING = "DOWNLOADING"
    DOWNLOADED = "DOWNLOADED"
    TRANSCRIBING = "TRANSCRIBING"
    TRANSCRIBED = "TRANSCRIBED"
    ANALYZING = "ANALYZING"
    ANALYZED = "ANALYZED"
    SEGMENT_SELECTED = "SEGMENT_SELECTED"
    RENDERING = "RENDERING"
    RENDERED = "RENDERED"
    CAPTIONED = "CAPTIONED"
    READY = "READY"
    QUEUED = "QUEUED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class ShortStatus(StrEnum):
    RENDERED = "RENDERED"
    QUEUED = "QUEUED"
    POSTED = "POSTED"
    FAILED = "FAILED"


class PublicationStatus(StrEnum):
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    APPROVED_FOR_PUBLISH = "APPROVED_FOR_PUBLISH"
    UPLOADING = "UPLOADING"
    PUBLISHED = "PUBLISHED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_PERMANENT = "FAILED_PERMANENT"
    UPLOAD_UNCERTAIN = "UPLOAD_UNCERTAIN"


VIDEO_TRANSITIONS: dict[VideoStatus, frozenset[VideoStatus]] = {
    VideoStatus.DISCOVERED: frozenset({VideoStatus.DOWNLOADING, VideoStatus.FAILED}),
    VideoStatus.DOWNLOADING: frozenset({VideoStatus.DOWNLOADED, VideoStatus.FAILED}),
    VideoStatus.DOWNLOADED: frozenset(
        {
            VideoStatus.TRANSCRIBING,
            VideoStatus.ANALYZING,
            VideoStatus.RENDERING,
            VideoStatus.FAILED,
        }
    ),
    VideoStatus.TRANSCRIBING: frozenset({VideoStatus.TRANSCRIBED, VideoStatus.FAILED}),
    VideoStatus.TRANSCRIBED: frozenset({VideoStatus.ANALYZING, VideoStatus.FAILED}),
    VideoStatus.ANALYZING: frozenset({VideoStatus.ANALYZED, VideoStatus.FAILED}),
    VideoStatus.ANALYZED: frozenset(
        {VideoStatus.SEGMENT_SELECTED, VideoStatus.RENDERING, VideoStatus.FAILED}
    ),
    VideoStatus.SEGMENT_SELECTED: frozenset(
        {VideoStatus.RENDERING, VideoStatus.FAILED}
    ),
    VideoStatus.RENDERING: frozenset({VideoStatus.RENDERED, VideoStatus.FAILED}),
    VideoStatus.RENDERED: frozenset(
        {
            VideoStatus.CAPTIONED,
            VideoStatus.QUEUED,
            VideoStatus.COMPLETED,
            VideoStatus.FAILED,
        }
    ),
    VideoStatus.CAPTIONED: frozenset({VideoStatus.READY, VideoStatus.FAILED}),
    VideoStatus.READY: frozenset(
        {VideoStatus.QUEUED, VideoStatus.COMPLETED, VideoStatus.FAILED}
    ),
    VideoStatus.QUEUED: frozenset({VideoStatus.COMPLETED, VideoStatus.FAILED}),
    VideoStatus.COMPLETED: frozenset(),
    VideoStatus.FAILED: frozenset({VideoStatus.DISCOVERED}),
}


@dataclass(frozen=True, slots=True)
class VideoRecord:
    youtube_id: str
    source_url: str
    creator_name: str
    theme_name: str
    title: str
    status: VideoStatus
    discovered_at: str
    updated_at: str
    processed_at: str | None
    error: str | None
    platform: str
    content_source_id: int | None
    duration_seconds: int | None
    published_at: str | None
    local_path: Path | None
    checksum: str | None
    validated_at: str | None


@dataclass(frozen=True, slots=True)
class RenderedShort:
    clip_index: int
    file_path: Path
    title: str = ""
    headline: str = ""
    generator_score: float | None = None
    editorial_score: float | None = None
    candidate_id: str | None = None
    duration_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class QueuedShort:
    clip_index: int
    account_slot: int
    publishing_channel_id: str
    queue_path: Path


@dataclass(frozen=True, slots=True)
class ShortRecord:
    id: int
    youtube_id: str
    clip_index: int
    account_slot: int | None
    account_identifier: str | None
    publishing_channel_id: str | None
    status: ShortStatus
    file_path: Path
    queue_path: Path | None
    created_at: str
    updated_at: str
    posted_at: str | None
    error: str | None
    title: str
    headline: str
    generator_score: float | None
    editorial_score: float | None
    candidate_id: str | None
    duration_seconds: float | None


@dataclass(frozen=True, slots=True)
class QueueMetadata:
    youtube_id: str
    creator_name: str
    theme_name: str
    rank: int
    account_slot: int
    account_identifier: str
    publishing_channel_id: str
    title: str
    headline: str
    generator_score: float | None
    editorial_score: float | None
    candidate_id: str | None
    duration_seconds: float | None
    master_path: Path
    queue_path: Path


@dataclass(frozen=True, slots=True)
class TranscriptRecord:
    youtube_id: str
    file_path: Path
    provider: str
    model: str
    language: str
    created_at: str


@dataclass(frozen=True, slots=True)
class AnalysisRecord:
    youtube_id: str
    file_path: Path
    provider: str
    model: str
    prompt_hash: str
    created_at: str


@dataclass(frozen=True, slots=True)
class ReadyArtifact:
    youtube_id: str
    file_path: Path
    checksum: str
    manifest_path: Path
    subtitle_path: Path
    caption_fingerprint: str
    created_at: str
    updated_at: str
    validated_at: str


@dataclass(frozen=True, slots=True)
class MetadataDraft:
    draft_id: int
    youtube_id: str
    ready_checksum: str
    publishing_channel_id: str
    brand_plan_id: str
    fingerprint: str
    status: MetadataDraftStatus
    review_policy: ReviewPolicy
    title: str
    description: str
    tags: tuple[str, ...]
    hashtags: tuple[str, ...]
    category_id: str
    category_name: str
    default_language: str
    privacy_status: str
    made_for_kids: bool
    contains_synthetic_media: bool
    has_paid_product_placement: bool
    license: str
    embeddable: bool
    public_stats_viewable: bool
    provider: str
    model: str
    prompt_hash: str
    generation_version: str
    title_candidates: tuple[str, ...]
    selection_reason: str
    error: str | None
    publication_id: int | None
    created_at: str
    updated_at: str
    approved_at: str | None


@dataclass(frozen=True, slots=True)
class ChannelCredentialBinding:
    publishing_channel_id: str
    google_identity_id: str
    provider: str
    credential_ref: str
    youtube_channel_id: str
    youtube_channel_title: str
    connected_at: str
    last_verified_at: str
    status: str


@dataclass(frozen=True, slots=True)
class YouTubeProvisioningRecord:
    brand_plan_id: str
    google_identity_id: str
    expected_name: str
    expected_handle: str
    actual_handle: str | None
    youtube_channel_id: str | None
    status: str
    logo_path: Path | None
    last_error: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class PublicationRecord:
    publication_id: int
    idempotency_key: str
    brand_plan_id: str
    publishing_channel_id: str
    google_identity_id: str
    youtube_channel_id: str
    source_short_id: int | None
    video_path: Path
    video_checksum: str
    title: str
    description: str
    tags: tuple[str, ...]
    privacy_status: str
    made_for_kids: bool | None
    contains_synthetic_media: bool | None
    has_paid_product_placement: bool | None
    category_id: str | None
    category_name: str | None
    default_language: str | None
    license: str | None
    embeddable: bool | None
    public_stats_viewable: bool | None
    publish_at: str | None
    recording_date: str | None
    status: PublicationStatus
    remote_video_id: str | None
    created_at: str
    updated_at: str
    approved_at: str | None
    published_at: str | None
    last_error: str | None
    error_category: str | None


@dataclass(frozen=True, slots=True)
class PublicationAttempt:
    attempt_id: int
    publication_id: int
    attempt_number: int
    started_at: str
    finished_at: str | None
    status: str
    remote_video_id: str | None
    error_category: str | None
    error_message: str | None


@dataclass(frozen=True, slots=True)
class InventoryPolicy:
    min_ready_videos: int = 2
    target_ready_videos: int = 3

    def __post_init__(self) -> None:
        if self.min_ready_videos < 0:
            raise ValueError("min_ready_videos cannot be negative.")
        if self.target_ready_videos < self.min_ready_videos:
            raise ValueError("target_ready_videos must be at least the minimum.")


def default_state_db_path(project_root: Path | None = None) -> Path:
    configured = os.environ.get(STATE_DB_ENV)
    if configured:
        return Path(configured).expanduser().resolve()
    root = project_root or Path(__file__).resolve().parents[2]
    return (root / "data" / "autoshorts.db").resolve()


class StateStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self._initialize()
        seed = Path(__file__).resolve().parents[2] / "config/theme-catalog.legacy.json"
        if seed.exists():
            self.seed_theme_templates(load_theme_seed(seed))

    def __enter__(self) -> StateStore:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    @property
    def schema_version(self) -> int:
        row = self.connection.execute("PRAGMA user_version").fetchone()
        return int(row[0])

    def register_video(
        self,
        *,
        youtube_id: str,
        source_url: str,
        creator_name: str,
        theme_name: str,
        title: str,
        platform: str = "youtube",
        content_source_id: int | None = None,
        duration_seconds: int | None = None,
        published_at: str | None = None,
    ) -> bool:
        """Register globally; return False when this YouTube ID already exists."""
        youtube_id = youtube_id.strip()
        if not youtube_id:
            raise ValueError("youtube_id cannot be empty")
        now = _now()
        existed = self.get_video(youtube_id) is not None
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO videos (
                    youtube_id, source_url, creator_name, theme_name, title,
                    status, discovered_at, updated_at, platform,
                    content_source_id, duration_seconds, published_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(youtube_id) DO UPDATE SET
                    source_url = excluded.source_url,
                    creator_name = excluded.creator_name,
                    title = excluded.title,
                    content_source_id = COALESCE(
                        excluded.content_source_id, videos.content_source_id
                    ),
                    duration_seconds = COALESCE(
                        excluded.duration_seconds, videos.duration_seconds
                    ),
                    published_at = COALESCE(
                        excluded.published_at, videos.published_at
                    )
                """,
                (
                    youtube_id,
                    source_url,
                    creator_name,
                    theme_name,
                    title,
                    VideoStatus.DISCOVERED,
                    now,
                    now,
                    platform,
                    content_source_id,
                    duration_seconds,
                    published_at,
                ),
            )
        return not existed

    def prepare_failed_video_retry(self, youtube_id: str) -> None:
        with self.connection:
            cursor = self.connection.execute(
                """UPDATE videos SET status=?, error=NULL, updated_at=?
                WHERE youtube_id=? AND status=?""",
                (VideoStatus.DISCOVERED, _now(), youtube_id, VideoStatus.FAILED),
            )
        if cursor.rowcount != 1:
            raise ValueError("Only a failed source video can be retried.")

    def record_downloaded_video(
        self,
        youtube_id: str,
        *,
        local_path: Path,
        checksum: str,
        duration_seconds: int,
    ) -> VideoRecord:
        """Atomically promote a validated download and persist its provenance."""
        current = self.get_video(youtube_id)
        if current is None:
            raise KeyError(f"Unknown YouTube ID: {youtube_id}")
        if current.status != VideoStatus.DOWNLOADING:
            raise ValueError(
                f"Invalid video transition: {current.status} -> DOWNLOADED"
            )
        now = _now()
        with self.connection:
            self.connection.execute(
                """UPDATE videos SET status=?, local_path=?, checksum=?,
                    duration_seconds=?, validated_at=?, updated_at=?, error=NULL
                WHERE youtube_id=?""",
                (
                    VideoStatus.DOWNLOADED,
                    str(Path(local_path).resolve()),
                    checksum,
                    duration_seconds,
                    now,
                    now,
                    youtube_id,
                ),
            )
        record = self.get_video(youtube_id)
        assert record is not None
        return record

    def get_video(self, youtube_id: str) -> VideoRecord | None:
        row = self.connection.execute(
            "SELECT * FROM videos WHERE youtube_id = ?", (youtube_id,)
        ).fetchone()
        return _video_record(row) if row else None

    def is_completed(self, youtube_id: str) -> bool:
        record = self.get_video(youtube_id)
        return bool(record and record.status == VideoStatus.COMPLETED)

    def is_produced(self, youtube_id: str) -> bool:
        """Return whether production reached a state that must not be duplicated."""
        record = self.get_video(youtube_id)
        return bool(
            record
            and record.status
            in {
                VideoStatus.RENDERED,
                VideoStatus.CAPTIONED,
                VideoStatus.READY,
                VideoStatus.QUEUED,
                VideoStatus.COMPLETED,
            }
        )

    def transition_video(
        self,
        youtube_id: str,
        status: VideoStatus,
        *,
        error: str | None = None,
    ) -> VideoRecord:
        with self.connection:
            self._transition_video(youtube_id, status, error=error)
        record = self.get_video(youtube_id)
        assert record is not None
        return record

    def fail_video(self, youtube_id: str, error: str) -> VideoRecord:
        return self.transition_video(youtube_id, VideoStatus.FAILED, error=error)

    def interrupted_videos(self) -> tuple[VideoRecord, ...]:
        terminal = (
            VideoStatus.READY,
            VideoStatus.QUEUED,
            VideoStatus.COMPLETED,
            VideoStatus.FAILED,
        )
        placeholders = ", ".join("?" for _ in terminal)
        rows = self.connection.execute(
            f"SELECT * FROM videos WHERE status NOT IN ({placeholders}) "
            "ORDER BY updated_at",
            terminal,
        ).fetchall()
        return tuple(_video_record(row) for row in rows)

    def list_incomplete_videos(
        self, *, include_failed: bool = False
    ) -> tuple[VideoRecord, ...]:
        """List production jobs that may require recovery or manual retry."""
        excluded = [VideoStatus.READY, VideoStatus.QUEUED, VideoStatus.COMPLETED]
        if not include_failed:
            excluded.append(VideoStatus.FAILED)
        placeholders = ", ".join("?" for _ in excluded)
        rows = self.connection.execute(
            f"SELECT * FROM videos WHERE status NOT IN ({placeholders}) "
            "ORDER BY updated_at",
            tuple(excluded),
        ).fetchall()
        return tuple(_video_record(row) for row in rows)

    def reset_for_recovery(self, youtube_id: str, target: VideoStatus) -> VideoRecord:
        """Apply one explicit, audited rewind selected by the recovery service."""
        allowed = {
            (VideoStatus.DOWNLOADING, VideoStatus.DISCOVERED),
            (VideoStatus.DOWNLOADED, VideoStatus.DISCOVERED),
            (VideoStatus.RENDERING, VideoStatus.DISCOVERED),
            (VideoStatus.RENDERING, VideoStatus.DOWNLOADED),
            (VideoStatus.TRANSCRIBING, VideoStatus.DOWNLOADED),
            (VideoStatus.TRANSCRIBED, VideoStatus.DOWNLOADED),
            (VideoStatus.TRANSCRIBING, VideoStatus.DISCOVERED),
            (VideoStatus.TRANSCRIBED, VideoStatus.DISCOVERED),
            (VideoStatus.ANALYZING, VideoStatus.TRANSCRIBED),
            (VideoStatus.ANALYZED, VideoStatus.TRANSCRIBED),
            (VideoStatus.ANALYZING, VideoStatus.DOWNLOADED),
            (VideoStatus.ANALYZED, VideoStatus.DOWNLOADED),
            (VideoStatus.ANALYZING, VideoStatus.DISCOVERED),
            (VideoStatus.ANALYZED, VideoStatus.DISCOVERED),
            (VideoStatus.FAILED, VideoStatus.DISCOVERED),
        }
        with self.connection:
            record = self.get_video(youtube_id)
            if record is None:
                raise KeyError(f"Unknown YouTube ID: {youtube_id}")
            if (record.status, target) not in allowed:
                raise ValueError(f"Invalid recovery reset: {record.status} -> {target}")
            self.connection.execute(
                "UPDATE videos SET status = ?, updated_at = ?, error = NULL "
                "WHERE youtube_id = ?",
                (target, _now(), youtube_id),
            )
        updated = self.get_video(youtube_id)
        assert updated is not None
        return updated

    def get_transcript(self, youtube_id: str) -> TranscriptRecord | None:
        row = self.connection.execute(
            "SELECT * FROM transcripts WHERE youtube_id = ?", (youtube_id,)
        ).fetchone()
        return _transcript_record(row) if row else None

    def record_transcript(
        self,
        youtube_id: str,
        *,
        file_path: Path,
        provider: str,
        model: str,
        language: str,
    ) -> TranscriptRecord:
        if not file_path.is_file():
            raise ValueError(f"Transcript artifact does not exist: {file_path}")
        now = _now()
        with self.connection:
            current = self.get_video(youtube_id)
            if current is None or current.status != VideoStatus.TRANSCRIBING:
                raise ValueError("Video must be TRANSCRIBING before persistence.")
            self.connection.execute(
                """
                INSERT INTO transcripts (
                    youtube_id, file_path, provider, model, language, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(youtube_id) DO UPDATE SET
                    file_path = excluded.file_path,
                    provider = excluded.provider,
                    model = excluded.model,
                    language = excluded.language,
                    created_at = excluded.created_at
                """,
                (youtube_id, str(file_path), provider, model, language, now),
            )
            self._transition_video(youtube_id, VideoStatus.TRANSCRIBED)
        record = self.get_transcript(youtube_id)
        assert record is not None
        return record

    def clear_transcript(self, youtube_id: str) -> None:
        with self.connection:
            self.connection.execute(
                "DELETE FROM transcripts WHERE youtube_id = ?", (youtube_id,)
            )

    def get_analysis(self, youtube_id: str) -> AnalysisRecord | None:
        row = self.connection.execute(
            "SELECT * FROM analyses WHERE youtube_id = ?", (youtube_id,)
        ).fetchone()
        return _analysis_record(row) if row else None

    def record_analysis(
        self,
        youtube_id: str,
        *,
        file_path: Path,
        provider: str,
        model: str,
        prompt_hash: str,
        usage: AnalysisUsage,
    ) -> AnalysisRecord:
        if not file_path.is_file():
            raise ValueError(f"Analysis artifact does not exist: {file_path}")
        now = _now()
        with self.connection:
            current = self.get_video(youtube_id)
            if current is None or current.status != VideoStatus.ANALYZING:
                raise ValueError("Video must be ANALYZING before persistence.")
            self.connection.execute(
                """INSERT INTO analyses
                   (youtube_id, file_path, provider, model, prompt_hash, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(youtube_id) DO UPDATE SET
                     file_path=excluded.file_path, provider=excluded.provider,
                     model=excluded.model, prompt_hash=excluded.prompt_hash,
                     created_at=excluded.created_at""",
                (youtube_id, str(file_path), provider, model, prompt_hash, now),
            )
            self.connection.execute(
                """INSERT INTO ai_usage
                   (youtube_id, provider, model, operation, input_tokens, output_tokens,
                    thinking_tokens, total_tokens, estimated_cost_usd, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    youtube_id,
                    provider,
                    model,
                    "generation",
                    usage.input_tokens,
                    usage.output_tokens,
                    usage.thinking_tokens,
                    usage.total_tokens,
                    usage.estimated_cost_usd,
                    now,
                ),
            )
            self._transition_video(youtube_id, VideoStatus.ANALYZED)
        record = self.get_analysis(youtube_id)
        assert record is not None
        return record

    def monthly_ai_cost(self, provider: str, *, month: str | None = None) -> float:
        month = month or datetime.now(UTC).strftime("%Y-%m")
        row = self.connection.execute(
            "SELECT COALESCE(SUM(estimated_cost_usd), 0) FROM ai_usage "
            "WHERE provider = ? AND substr(created_at, 1, 7) = ?",
            (provider, month),
        ).fetchone()
        return float(row[0])

    def record_ai_usage(
        self,
        youtube_id: str,
        *,
        provider: str,
        model: str,
        usage: AnalysisUsage,
        operation: str = "generation",
    ) -> None:
        """Record a billed call that did not produce a reusable analysis."""
        with self.connection:
            self.connection.execute(
                """INSERT INTO ai_usage
                   (youtube_id, provider, model, operation, input_tokens, output_tokens,
                    thinking_tokens, total_tokens, estimated_cost_usd, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    youtube_id,
                    provider,
                    model,
                    operation,
                    usage.input_tokens,
                    usage.output_tokens,
                    usage.thinking_tokens,
                    usage.total_tokens,
                    usage.estimated_cost_usd,
                    _now(),
                ),
            )

    def record_rendered_shorts(
        self, youtube_id: str, shorts: tuple[RenderedShort, ...]
    ) -> tuple[ShortRecord, ...]:
        if len(shorts) != 3:
            raise ValueError("Exactly 3 rendered shorts are required.")
        if {item.clip_index for item in shorts} != {1, 2, 3}:
            raise ValueError("clip_index values must be 1, 2 and 3.")
        missing = [
            str(item.file_path) for item in shorts if not item.file_path.is_file()
        ]
        if missing:
            raise ValueError(f"Rendered short does not exist: {missing[0]}")
        now = _now()
        with self.connection:
            for item in shorts:
                self.connection.execute(
                    """
                    INSERT INTO shorts (
                        youtube_id, clip_index, status, file_path,
                        title, headline, generator_score, editorial_score,
                        candidate_id, duration_seconds, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        youtube_id,
                        item.clip_index,
                        ShortStatus.RENDERED,
                        str(item.file_path),
                        item.title,
                        item.headline,
                        item.generator_score,
                        item.editorial_score,
                        item.candidate_id,
                        item.duration_seconds,
                        now,
                        now,
                    ),
                )
            self._transition_video(youtube_id, VideoStatus.RENDERED)
        return self.shorts_for_video(youtube_id)

    def record_single_rendered_short(
        self, youtube_id: str, short: RenderedShort
    ) -> ShortRecord:
        """Atomically persist the controlled single-winner production path."""
        if short.clip_index != 1:
            raise ValueError("The single rendered short must use clip_index 1.")
        if not short.file_path.is_file():
            raise ValueError(f"Rendered short does not exist: {short.file_path}")
        now = _now()
        with self.connection:
            current = self.get_video(youtube_id)
            if current is None or current.status != VideoStatus.RENDERING:
                raise ValueError("Video must be RENDERING before persistence.")
            self.connection.execute(
                """
                INSERT INTO shorts (
                    youtube_id, clip_index, status, file_path, title, headline,
                    generator_score, editorial_score, candidate_id,
                    duration_seconds, created_at, updated_at
                ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    youtube_id,
                    ShortStatus.RENDERED,
                    str(short.file_path),
                    short.title,
                    short.headline,
                    short.generator_score,
                    short.editorial_score,
                    short.candidate_id,
                    short.duration_seconds,
                    now,
                    now,
                ),
            )
            self._transition_video(youtube_id, VideoStatus.RENDERED)
        rows = self.shorts_for_video(youtube_id)
        if len(rows) != 1:
            raise RuntimeError("Single-short persistence invariant failed.")
        return rows[0]

    def replace_ready_rendered_short(
        self, youtube_id: str, short: RenderedShort
    ) -> ShortRecord:
        """Atomically point an existing READY source at a recalibrated render."""
        if short.clip_index != 1 or not short.file_path.is_file():
            raise ValueError("A valid single rendered short is required.")
        current = self.get_video(youtube_id)
        rows = self.shorts_for_video(youtube_id)
        if current is None or current.status is not VideoStatus.READY or len(rows) != 1:
            raise ValueError("Visual recalibration requires one READY Short.")
        with self.connection:
            cursor = self.connection.execute(
                """UPDATE shorts SET file_path=?, title=?, headline=?,
                generator_score=?, editorial_score=?, candidate_id=?,
                duration_seconds=?, updated_at=? WHERE youtube_id=? AND clip_index=1""",
                (
                    str(short.file_path), short.title, short.headline,
                    short.generator_score, short.editorial_score,
                    short.candidate_id, short.duration_seconds, _now(), youtube_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("READY Short recalibration update failed.")
        return self.shorts_for_video(youtube_id)[0]

    def get_ready_artifact(self, youtube_id: str) -> ReadyArtifact | None:
        row = self.connection.execute(
            "SELECT * FROM ready_artifacts WHERE youtube_id = ?", (youtube_id,)
        ).fetchone()
        return _ready_artifact(row) if row else None

    def record_ready_artifact(
        self,
        youtube_id: str,
        *,
        file_path: Path,
        checksum: str,
        manifest_path: Path,
        subtitle_path: Path,
        caption_fingerprint: str,
    ) -> ReadyArtifact:
        """Persist the validated final derivative and promote the source to READY."""
        for path in (file_path, manifest_path, subtitle_path):
            if not path.is_file():
                raise ValueError(f"READY artifact does not exist: {path}")
        current = self.get_video(youtube_id)
        allowed = {VideoStatus.RENDERED, VideoStatus.CAPTIONED, VideoStatus.READY}
        if current is None or current.status not in allowed:
            raise ValueError("Video must be RENDERED or CAPTIONED before READY.")
        now = _now()
        existing = self.get_ready_artifact(youtube_id)
        created_at = existing.created_at if existing else now
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO ready_artifacts (
                    youtube_id, file_path, checksum, manifest_path, subtitle_path,
                    caption_fingerprint, created_at, updated_at, validated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(youtube_id) DO UPDATE SET
                    file_path=excluded.file_path, checksum=excluded.checksum,
                    manifest_path=excluded.manifest_path,
                    subtitle_path=excluded.subtitle_path,
                    caption_fingerprint=excluded.caption_fingerprint,
                    updated_at=excluded.updated_at,
                    validated_at=excluded.validated_at
                """,
                (
                    youtube_id,
                    str(file_path),
                    checksum,
                    str(manifest_path),
                    str(subtitle_path),
                    caption_fingerprint,
                    created_at,
                    now,
                    now,
                ),
            )
            if current.status == VideoStatus.RENDERED:
                self._transition_video(youtube_id, VideoStatus.CAPTIONED)
                self._transition_video(youtube_id, VideoStatus.READY)
            elif current.status == VideoStatus.CAPTIONED:
                self._transition_video(youtube_id, VideoStatus.READY)
        artifact = self.get_ready_artifact(youtube_id)
        assert artifact is not None
        return artifact

    def metadata_review_policy(self, publishing_channel_id: str) -> ReviewPolicy:
        row = self.connection.execute(
            "SELECT review_policy FROM metadata_channel_settings "
            "WHERE publishing_channel_id = ?",
            (publishing_channel_id,),
        ).fetchone()
        if row:
            return ReviewPolicy(row[0])
        row = self.connection.execute(
            "SELECT default_review_policy FROM metadata_settings WHERE singleton=1"
        ).fetchone()
        return ReviewPolicy(row[0])

    def set_metadata_review_policy(
        self, policy: ReviewPolicy, *, publishing_channel_id: str | None = None
    ) -> None:
        with self.connection:
            if publishing_channel_id is None:
                self.connection.execute(
                    "UPDATE metadata_settings SET default_review_policy=?, "
                    "updated_at=? WHERE singleton=1",
                    (policy, _now()),
                )
            else:
                self.connection.execute(
                    """INSERT INTO metadata_channel_settings
                    (publishing_channel_id, review_policy, updated_at)
                    VALUES (?, ?, ?) ON CONFLICT(publishing_channel_id) DO UPDATE SET
                    review_policy=excluded.review_policy,
                    updated_at=excluded.updated_at""",
                    (publishing_channel_id, policy, _now()),
                )

    def metadata_draft_by_fingerprint(self, fingerprint: str) -> MetadataDraft | None:
        row = self.connection.execute(
            "SELECT * FROM metadata_drafts WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        return _metadata_draft(row) if row else None

    def metadata_draft(self, draft_id: int) -> MetadataDraft | None:
        row = self.connection.execute(
            "SELECT * FROM metadata_drafts WHERE id = ?", (draft_id,)
        ).fetchone()
        return _metadata_draft(row) if row else None

    def save_metadata_draft(self, **values) -> MetadataDraft:
        existing = self.metadata_draft_by_fingerprint(values["fingerprint"])
        if existing and existing.status not in {
            MetadataDraftStatus.FAILED_RECOVERABLE,
            MetadataDraftStatus.FAILED_CONFIGURATION,
        }:
            return existing
        now = _now()
        status = values["status"]
        with self.connection:
            self.connection.execute(
                """INSERT INTO metadata_drafts (
                    youtube_id, ready_checksum, publishing_channel_id, brand_plan_id,
                    fingerprint, status, review_policy, title, description, tags_json,
                    hashtags_json, category_id, category_name, default_language,
                    privacy_status, made_for_kids, contains_synthetic_media,
                    has_paid_product_placement, license, embeddable,
                    public_stats_viewable, provider, model, prompt_hash,
                    generation_version, title_candidates_json, selection_reason,
                    error, created_at, updated_at
                ) VALUES (
                    :youtube_id, :ready_checksum, :publishing_channel_id,
                    :brand_plan_id, :fingerprint, :status, :review_policy, :title,
                    :description, :tags_json, :hashtags_json, :category_id,
                    :category_name, :default_language, :privacy_status,
                    :made_for_kids, :contains_synthetic_media,
                    :has_paid_product_placement, :license, :embeddable,
                    :public_stats_viewable, :provider, :model, :prompt_hash,
                    :generation_version, :title_candidates_json,
                    :selection_reason, :error, :created_at, :updated_at
                ) ON CONFLICT(fingerprint) DO UPDATE SET
                    status=excluded.status, title=excluded.title,
                    description=excluded.description, tags_json=excluded.tags_json,
                    hashtags_json=excluded.hashtags_json, error=excluded.error,
                    updated_at=excluded.updated_at""",
                {
                    **values,
                    "status": status.value,
                    "review_policy": values["review_policy"].value,
                    "tags_json": json.dumps(values.get("tags", ()), ensure_ascii=False),
                    "hashtags_json": json.dumps(
                        values.get("hashtags", ()), ensure_ascii=False
                    ),
                    "title_candidates_json": json.dumps(
                        values.get("title_candidates", ()), ensure_ascii=False
                    ),
                    "created_at": existing.created_at if existing else now,
                    "updated_at": now,
                },
            )
        draft = self.metadata_draft_by_fingerprint(values["fingerprint"])
        assert draft is not None
        return draft

    def approve_metadata_draft(
        self, draft_id: int, publication_id: int
    ) -> MetadataDraft:
        now = _now()
        with self.connection:
            cursor = self.connection.execute(
                """UPDATE metadata_drafts SET status=?, publication_id=?,
                approved_at=COALESCE(approved_at, ?), updated_at=?
                WHERE id=? AND status IN (?, ?)""",
                (
                    MetadataDraftStatus.APPROVED,
                    publication_id,
                    now,
                    now,
                    draft_id,
                    MetadataDraftStatus.REVIEW_REQUIRED,
                    MetadataDraftStatus.APPROVED,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("Metadata draft is not reviewable.")
        draft = self.metadata_draft(draft_id)
        assert draft is not None
        return draft

    def revise_metadata_draft(
        self,
        draft_id: int,
        *,
        title: str,
        description: str,
        tags: tuple[str, ...],
        hashtags: tuple[str, ...],
    ) -> MetadataDraft:
        title = title.strip()
        description = description.strip()
        tags = tuple(dict.fromkeys(item.strip() for item in tags if item.strip()))
        hashtags = tuple(
            dict.fromkeys(item.strip() for item in hashtags if item.strip())
        )
        if not title or not description or not tags:
            raise ValueError(
                "Approved metadata title, description and tags are required."
            )
        current = self.metadata_draft(draft_id)
        if current is None:
            raise ValueError(f"Metadata draft not found: {draft_id}")
        if current.status == MetadataDraftStatus.APPROVED:
            if (
                current.title == title
                and current.description == description
                and current.tags == tags
                and current.hashtags == hashtags
            ):
                return current
            raise ValueError("Approved metadata cannot be revised.")
        now = _now()
        with self.connection:
            cursor = self.connection.execute(
                """UPDATE metadata_drafts SET title=?, description=?, tags_json=?,
                hashtags_json=?, updated_at=? WHERE id=? AND status=?""",
                (
                    title,
                    description,
                    json.dumps(tags, ensure_ascii=False),
                    json.dumps(hashtags, ensure_ascii=False),
                    now,
                    draft_id,
                    MetadataDraftStatus.REVIEW_REQUIRED,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("Only review-required metadata can be revised.")
        draft = self.metadata_draft(draft_id)
        assert draft is not None
        return draft

    def record_queued_shorts(
        self, youtube_id: str, shorts: tuple[QueuedShort, ...]
    ) -> tuple[ShortRecord, ...]:
        if len(shorts) != 3 or {item.clip_index for item in shorts} != {1, 2, 3}:
            raise ValueError("Exactly clips 1, 2 and 3 are required for queueing.")
        if {item.account_slot for item in shorts} != {1, 2, 3}:
            raise ValueError("Account slots 1, 2 and 3 are required for queueing.")
        missing = [
            str(item.queue_path) for item in shorts if not item.queue_path.is_file()
        ]
        if missing:
            raise ValueError(f"Queued short does not exist: {missing[0]}")
        now = _now()
        with self.connection:
            current = self.get_video(youtube_id)
            if current is None or current.status != VideoStatus.RENDERED:
                raise ValueError("Video must be RENDERED before queueing.")
            for item in shorts:
                cursor = self.connection.execute(
                    """
                    UPDATE shorts
                    SET account_slot = ?, account_identifier = ?,
                        publishing_channel_id = ?, status = ?,
                        queue_path = ?, updated_at = ?
                    WHERE youtube_id = ? AND clip_index = ? AND status = ?
                    """,
                    (
                        item.account_slot,
                        item.publishing_channel_id,
                        item.publishing_channel_id,
                        ShortStatus.QUEUED,
                        str(item.queue_path),
                        now,
                        youtube_id,
                        item.clip_index,
                        ShortStatus.RENDERED,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError(f"Rendered Short {item.clip_index} was not found.")
            self._transition_video(youtube_id, VideoStatus.QUEUED)
        return self.shorts_for_video(youtube_id)

    def shorts_for_video(self, youtube_id: str) -> tuple[ShortRecord, ...]:
        rows = self.connection.execute(
            "SELECT * FROM shorts WHERE youtube_id = ? ORDER BY clip_index",
            (youtube_id,),
        ).fetchall()
        return tuple(_short_record(row) for row in rows)

    def get_channel_binding(
        self, publishing_channel_id: str
    ) -> ChannelCredentialBinding | None:
        row = self.connection.execute(
            "SELECT * FROM channel_bindings WHERE publishing_channel_id = ?",
            (publishing_channel_id,),
        ).fetchone()
        return _channel_binding(row) if row else None

    def get_channel_binding_by_youtube_id(
        self, youtube_channel_id: str
    ) -> ChannelCredentialBinding | None:
        row = self.connection.execute(
            "SELECT * FROM channel_bindings WHERE youtube_channel_id = ?",
            (youtube_channel_id,),
        ).fetchone()
        return _channel_binding(row) if row else None

    def save_channel_binding(self, binding: ChannelCredentialBinding) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO channel_bindings (
                    publishing_channel_id, google_identity_id, provider,
                    credential_ref, youtube_channel_id, youtube_channel_title,
                    connected_at, last_verified_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(publishing_channel_id) DO UPDATE SET
                    google_identity_id = excluded.google_identity_id,
                    provider = excluded.provider,
                    credential_ref = excluded.credential_ref,
                    youtube_channel_id = excluded.youtube_channel_id,
                    youtube_channel_title = excluded.youtube_channel_title,
                    connected_at = excluded.connected_at,
                    last_verified_at = excluded.last_verified_at,
                    status = excluded.status
                """,
                (
                    binding.publishing_channel_id,
                    binding.google_identity_id,
                    binding.provider,
                    binding.credential_ref,
                    binding.youtube_channel_id,
                    binding.youtube_channel_title,
                    binding.connected_at,
                    binding.last_verified_at,
                    binding.status,
                ),
            )

    def delete_channel_binding(self, publishing_channel_id: str) -> bool:
        with self.connection:
            cursor = self.connection.execute(
                "DELETE FROM channel_bindings WHERE publishing_channel_id = ?",
                (publishing_channel_id,),
            )
        return cursor.rowcount == 1

    def create_publication(
        self,
        *,
        idempotency_key: str,
        brand_plan_id: str,
        publishing_channel_id: str,
        google_identity_id: str,
        youtube_channel_id: str,
        source_short_id: int | None,
        video_path: Path,
        video_checksum: str,
        title: str,
        description: str,
        tags: tuple[str, ...],
        privacy_status: str,
        made_for_kids: bool | None,
        contains_synthetic_media: bool | None,
        has_paid_product_placement: bool | None,
        category_id: str | None,
        category_name: str | None,
        default_language: str | None,
        license: str | None,
        embeddable: bool | None,
        public_stats_viewable: bool | None,
        publish_at: str | None,
        recording_date: str | None,
    ) -> PublicationRecord:
        now = _now()
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO publications (
                    idempotency_key, brand_plan_id, publishing_channel_id,
                    google_identity_id, youtube_channel_id, video_path,
                    source_short_id, video_checksum, title, description, tags_json,
                    privacy_status, made_for_kids, contains_synthetic_media,
                    has_paid_product_placement, category_id, category_name,
                    default_language, license, embeddable, public_stats_viewable,
                    publish_at, recording_date,
                    status, created_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                ON CONFLICT(idempotency_key) DO NOTHING
                """,
                (
                    idempotency_key,
                    brand_plan_id,
                    publishing_channel_id,
                    google_identity_id,
                    youtube_channel_id,
                    str(video_path),
                    source_short_id,
                    video_checksum,
                    title,
                    description,
                    json.dumps(tags, ensure_ascii=False),
                    privacy_status,
                    made_for_kids,
                    contains_synthetic_media,
                    has_paid_product_placement,
                    category_id,
                    category_name,
                    default_language,
                    license,
                    embeddable,
                    public_stats_viewable,
                    publish_at,
                    recording_date,
                    PublicationStatus.READY_FOR_REVIEW,
                    now,
                    now,
                ),
            )
        record = self.publication_by_key(idempotency_key)
        if record is None:
            raise RuntimeError("Publication could not be persisted.")
        return record

    def publication(self, publication_id: int) -> PublicationRecord | None:
        row = self.connection.execute(
            "SELECT * FROM publications WHERE id = ?", (publication_id,)
        ).fetchone()
        return _publication_record(row) if row else None

    def publication_by_key(self, idempotency_key: str) -> PublicationRecord | None:
        row = self.connection.execute(
            "SELECT * FROM publications WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        return _publication_record(row) if row else None

    def publications(self) -> tuple[PublicationRecord, ...]:
        rows = self.connection.execute(
            "SELECT * FROM publications ORDER BY created_at DESC, id DESC"
        ).fetchall()
        return tuple(_publication_record(row) for row in rows)

    def approve_publication(self, publication_id: int) -> PublicationRecord:
        now = _now()
        allowed = (
            PublicationStatus.READY_FOR_REVIEW,
            PublicationStatus.FAILED_RETRYABLE,
            PublicationStatus.FAILED_PERMANENT,
        )
        with self.connection:
            cursor = self.connection.execute(
                f"""UPDATE publications
                SET status=?, approved_at=?, updated_at=?, last_error=NULL,
                    error_category=NULL
                WHERE id=? AND status IN ({",".join("?" for _ in allowed)})""",
                (
                    PublicationStatus.APPROVED_FOR_PUBLISH,
                    now,
                    now,
                    publication_id,
                    *allowed,
                ),
            )
        if cursor.rowcount != 1:
            raise ValueError(
                "A publicação precisa estar em revisão ou falha confirmada "
                "antes da aprovação."
            )
        return self._required_publication(publication_id)

    def revise_publication(
        self,
        publication_id: int,
        *,
        title: str,
        description: str,
        tags: tuple[str, ...],
        privacy_status: str,
        made_for_kids: bool | None,
        contains_synthetic_media: bool | None,
        has_paid_product_placement: bool | None,
        category_id: str | None,
        category_name: str | None,
        default_language: str | None,
        license: str | None,
        embeddable: bool | None,
        public_stats_viewable: bool | None,
        publish_at: str | None,
        recording_date: str | None,
    ) -> PublicationRecord:
        allowed = (
            PublicationStatus.READY_FOR_REVIEW,
            PublicationStatus.APPROVED_FOR_PUBLISH,
            PublicationStatus.FAILED_RETRYABLE,
            PublicationStatus.FAILED_PERMANENT,
        )
        now = _now()
        with self.connection:
            cursor = self.connection.execute(
                f"""UPDATE publications
                SET title=?, description=?, tags_json=?, privacy_status=?,
                    made_for_kids=?, contains_synthetic_media=?,
                    has_paid_product_placement=?, category_id=?, category_name=?,
                    default_language=?, license=?, embeddable=?,
                    public_stats_viewable=?, publish_at=?, recording_date=?,
                    status=?, approved_at=NULL, updated_at=?, last_error=NULL,
                    error_category=NULL
                WHERE id=? AND status IN ({",".join("?" for _ in allowed)})""",
                (
                    title,
                    description,
                    json.dumps(tags, ensure_ascii=False),
                    privacy_status,
                    made_for_kids,
                    contains_synthetic_media,
                    has_paid_product_placement,
                    category_id,
                    category_name,
                    default_language,
                    license,
                    embeddable,
                    public_stats_viewable,
                    publish_at,
                    recording_date,
                    PublicationStatus.READY_FOR_REVIEW,
                    now,
                    publication_id,
                    *allowed,
                ),
            )
        if cursor.rowcount != 1:
            raise ValueError("Published, active or uncertain publication is immutable.")
        return self._required_publication(publication_id)

    def start_publication_attempt(
        self, publication_id: int
    ) -> tuple[PublicationRecord, PublicationAttempt]:
        now = _now()
        with self.connection:
            cursor = self.connection.execute(
                """UPDATE publications SET status=?, updated_at=?
                WHERE id=? AND status=? AND remote_video_id IS NULL""",
                (
                    PublicationStatus.UPLOADING,
                    now,
                    publication_id,
                    PublicationStatus.APPROVED_FOR_PUBLISH,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError(
                    "A publicação não está aprovada ou já possui tentativa ativa."
                )
            number = self.connection.execute(
                """SELECT COALESCE(MAX(attempt_number), 0) + 1
                FROM publication_attempts WHERE publication_id=?""",
                (publication_id,),
            ).fetchone()[0]
            attempt_id = self.connection.execute(
                """INSERT INTO publication_attempts
                (publication_id, attempt_number, started_at, status)
                VALUES (?, ?, ?, 'STARTED')""",
                (publication_id, number, now),
            ).lastrowid
        return (
            self._required_publication(publication_id),
            self._required_publication_attempt(int(attempt_id)),
        )

    def finish_publication_attempt(
        self,
        publication_id: int,
        attempt_id: int,
        *,
        status: PublicationStatus,
        remote_video_id: str | None = None,
        error_category: str | None = None,
        error_message: str | None = None,
    ) -> PublicationRecord:
        allowed = {
            PublicationStatus.PUBLISHED,
            PublicationStatus.FAILED_RETRYABLE,
            PublicationStatus.FAILED_PERMANENT,
            PublicationStatus.UPLOAD_UNCERTAIN,
        }
        if status not in allowed:
            raise ValueError("Invalid terminal publication status.")
        if status == PublicationStatus.PUBLISHED and not remote_video_id:
            raise ValueError("Published upload requires a remote video ID.")
        now = _now()
        with self.connection:
            attempt = self.connection.execute(
                """UPDATE publication_attempts
                SET finished_at=?, status=?, remote_video_id=?, error_category=?,
                    error_message=?
                WHERE id=? AND publication_id=? AND status='STARTED'""",
                (
                    now,
                    status,
                    remote_video_id,
                    error_category,
                    error_message,
                    attempt_id,
                    publication_id,
                ),
            )
            if attempt.rowcount != 1:
                raise ValueError("Publication attempt is not active.")
            publication = self.connection.execute(
                """UPDATE publications
                SET status=?, remote_video_id=?, updated_at=?, published_at=?,
                    last_error=?, error_category=?
                WHERE id=? AND status=?""",
                (
                    status,
                    remote_video_id,
                    now,
                    now if status == PublicationStatus.PUBLISHED else None,
                    error_message,
                    error_category,
                    publication_id,
                    PublicationStatus.UPLOADING,
                ),
            )
            if publication.rowcount != 1:
                raise ValueError("Publication is not uploading.")
            if status == PublicationStatus.PUBLISHED:
                source = self.connection.execute(
                    "SELECT source_short_id FROM publications WHERE id=?",
                    (publication_id,),
                ).fetchone()
                if source and source["source_short_id"] is not None:
                    self.connection.execute(
                        """UPDATE shorts SET status=?, posted_at=?, updated_at=?,
                            error=NULL WHERE id=?""",
                        (
                            ShortStatus.POSTED,
                            now,
                            now,
                            source["source_short_id"],
                        ),
                    )
        return self._required_publication(publication_id)

    def publication_attempts(
        self, publication_id: int
    ) -> tuple[PublicationAttempt, ...]:
        rows = self.connection.execute(
            """SELECT * FROM publication_attempts WHERE publication_id=?
            ORDER BY attempt_number""",
            (publication_id,),
        ).fetchall()
        return tuple(_publication_attempt(row) for row in rows)

    def scheduler_settings(self) -> SchedulerSettings:
        row = self.connection.execute(
            "SELECT * FROM scheduler_settings WHERE singleton=1"
        ).fetchone()
        if row is None:
            raise RuntimeError("Scheduler settings are missing.")
        return _scheduler_settings(row)

    def update_scheduler_settings(self, **changes: object) -> SchedulerSettings:
        allowed = {
            "global_paused",
            "poll_interval_seconds",
            "max_attempts",
            "missed_threshold_hours",
            "max_publications_per_channel_per_day",
            "min_spacing_minutes",
        }
        if not changes or not set(changes) <= allowed:
            raise ValueError("Invalid scheduler settings update.")
        for key, value in changes.items():
            if key != "global_paused" and int(value) < 1:
                raise ValueError(f"Scheduler setting must be positive: {key}")
        assignments = ", ".join(f"{key}=?" for key in changes)
        values = [
            int(value) if isinstance(value, bool) else value
            for value in changes.values()
        ]
        with self.connection:
            self.connection.execute(
                f"UPDATE scheduler_settings SET {assignments}, "
                "updated_at=? WHERE singleton=1",
                (*values, _now()),
            )
        return self.scheduler_settings()

    def set_scheduler_channel_paused(
        self, publishing_channel_id: str, paused: bool
    ) -> None:
        now = _now()
        with self.connection:
            self.connection.execute(
                """INSERT INTO scheduler_channel_settings
                (publishing_channel_id, paused, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(publishing_channel_id) DO UPDATE SET
                    paused=excluded.paused, updated_at=excluded.updated_at""",
                (publishing_channel_id, paused, now),
            )

    def scheduler_channel_paused(self, publishing_channel_id: str) -> bool:
        row = self.connection.execute(
            """SELECT paused FROM scheduler_channel_settings
            WHERE publishing_channel_id=?""",
            (publishing_channel_id,),
        ).fetchone()
        return bool(row["paused"]) if row else False

    def inventory_policy(self, publishing_channel_id: str) -> InventoryPolicy:
        row = self.connection.execute(
            """SELECT min_ready_videos, target_ready_videos
            FROM orchestrator_channel_settings WHERE publishing_channel_id=?""",
            (publishing_channel_id,),
        ).fetchone()
        if row is None:
            row = self.connection.execute(
                """SELECT min_ready_videos, target_ready_videos
                FROM orchestrator_settings WHERE singleton=1"""
            ).fetchone()
        if row is None:
            return InventoryPolicy()
        return InventoryPolicy(row["min_ready_videos"], row["target_ready_videos"])

    def update_inventory_policy(
        self,
        *,
        min_ready_videos: int,
        target_ready_videos: int,
        publishing_channel_id: str | None = None,
    ) -> InventoryPolicy:
        policy = InventoryPolicy(min_ready_videos, target_ready_videos)
        now = _now()
        with self.connection:
            if publishing_channel_id is None:
                self.connection.execute(
                    """UPDATE orchestrator_settings SET min_ready_videos=?,
                    target_ready_videos=?, updated_at=? WHERE singleton=1""",
                    (policy.min_ready_videos, policy.target_ready_videos, now),
                )
            else:
                self.connection.execute(
                    """INSERT INTO orchestrator_channel_settings
                    (publishing_channel_id, min_ready_videos,
                     target_ready_videos, updated_at) VALUES (?, ?, ?, ?)
                    ON CONFLICT(publishing_channel_id) DO UPDATE SET
                    min_ready_videos=excluded.min_ready_videos,
                    target_ready_videos=excluded.target_ready_videos,
                    updated_at=excluded.updated_at""",
                    (
                        publishing_channel_id,
                        policy.min_ready_videos,
                        policy.target_ready_videos,
                        now,
                    ),
                )
        return policy

    def assign_source_to_channel(
        self, youtube_id: str, publishing_channel_id: str
    ) -> None:
        with self.connection:
            self.connection.execute(
                """INSERT INTO orchestrator_artifact_assignments
                (youtube_id, publishing_channel_id, created_at) VALUES (?, ?, ?)
                ON CONFLICT(youtube_id) DO NOTHING""",
                (youtube_id, publishing_channel_id, _now()),
            )
            row = self.connection.execute(
                """SELECT publishing_channel_id
                FROM orchestrator_artifact_assignments WHERE youtube_id=?""",
                (youtube_id,),
            ).fetchone()
            if row["publishing_channel_id"] != publishing_channel_id:
                raise ValueError("Source video is already assigned to another channel.")

    def acquire_orchestrator_lease(
        self,
        publishing_channel_id: str,
        owner: str,
        now: datetime,
        lease_seconds: int = 1800,
    ) -> bool:
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive.")
        acquired_at = now.astimezone(UTC).isoformat()
        expires_at = (
            now.astimezone(UTC) + timedelta(seconds=lease_seconds)
        ).isoformat()
        with self.connection:
            cursor = self.connection.execute(
                """INSERT INTO orchestrator_leases
                (publishing_channel_id, owner, acquired_at, expires_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(publishing_channel_id) DO UPDATE SET
                    owner=excluded.owner, acquired_at=excluded.acquired_at,
                    expires_at=excluded.expires_at
                WHERE orchestrator_leases.expires_at <= excluded.acquired_at
                   OR orchestrator_leases.owner = excluded.owner""",
                (publishing_channel_id, owner, acquired_at, expires_at),
            )
        return cursor.rowcount == 1

    def release_orchestrator_lease(
        self, publishing_channel_id: str, owner: str
    ) -> None:
        with self.connection:
            self.connection.execute(
                """DELETE FROM orchestrator_leases
                WHERE publishing_channel_id=? AND owner=?""",
                (publishing_channel_id, owner),
            )

    def start_orchestrator_cycle(self, *, max_productions: int) -> int:
        with self.connection:
            cursor = self.connection.execute(
                """INSERT INTO orchestrator_cycles
                (started_at, max_productions, result) VALUES (?, ?, 'RUNNING')""",
                (_now(), max_productions),
            )
        return int(cursor.lastrowid)

    def finish_orchestrator_cycle(
        self, cycle_id: int, *, result: str, error: str | None = None
    ) -> None:
        with self.connection:
            self.connection.execute(
                """UPDATE orchestrator_cycles SET finished_at=?, result=?, error=?
                WHERE id=?""",
                (_now(), result, error, cycle_id),
            )

    def record_orchestrator_decision(
        self,
        cycle_id: int,
        publishing_channel_id: str,
        *,
        inventory_before: int,
        minimum: int,
        target: int,
        planned_count: int,
        result: str,
        error: str | None = None,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """INSERT INTO orchestrator_decisions
                (cycle_id, publishing_channel_id, inventory_before, minimum,
                 target, planned_count, result, error, started_at, finished_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    cycle_id,
                    publishing_channel_id,
                    inventory_before,
                    minimum,
                    target,
                    planned_count,
                    result,
                    error,
                    _now(),
                    _now(),
                ),
            )

    def source_candidate_available(
        self,
        profile_id: int,
        youtube_id: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        row = self.connection.execute(
            """SELECT category, retry_after FROM youtube_source_failures
            WHERE profile_id=? AND youtube_id=?""",
            (profile_id, youtube_id),
        ).fetchone()
        if row is None:
            return True
        if row["retry_after"] is None:
            return False
        current = (now or datetime.now(UTC)).astimezone(UTC)
        return datetime.fromisoformat(row["retry_after"]) <= current

    def record_source_failure(
        self,
        profile_id: int,
        youtube_id: str,
        *,
        category: str,
        detail: str,
        now: datetime | None = None,
    ) -> str | None:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        row = self.connection.execute(
            """SELECT attempt_count FROM youtube_source_failures
            WHERE profile_id=? AND youtube_id=?""",
            (profile_id, youtube_id),
        ).fetchone()
        attempt = (int(row["attempt_count"]) if row else 0) + 1
        retry_after = _source_retry_after(youtube_id, category, attempt, current)
        with self.connection:
            self.connection.execute(
                """INSERT INTO youtube_source_failures
                (profile_id, youtube_id, category, attempt_count, retry_after,
                 detail, first_failed_at, last_failed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(profile_id, youtube_id) DO UPDATE SET
                    category=excluded.category,
                    attempt_count=excluded.attempt_count,
                    retry_after=excluded.retry_after, detail=excluded.detail,
                    last_failed_at=excluded.last_failed_at""",
                (
                    profile_id,
                    youtube_id,
                    category,
                    attempt,
                    retry_after,
                    detail[:500],
                    current.isoformat(),
                    current.isoformat(),
                ),
            )
        return retry_after

    def source_failure(self, profile_id: int, youtube_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            """SELECT * FROM youtube_source_failures
            WHERE profile_id=? AND youtube_id=?""",
            (profile_id, youtube_id),
        ).fetchone()

    def clear_source_failure(self, profile_id: int, youtube_id: str) -> None:
        with self.connection:
            self.connection.execute(
                """DELETE FROM youtube_source_failures
                WHERE profile_id=? AND youtube_id=?""",
                (profile_id, youtube_id),
            )

    def cached_source_metadata(
        self, youtube_id: str, *, max_age_hours: int = 24
    ) -> sqlite3.Row | None:
        cutoff = datetime.now(UTC) - timedelta(hours=max_age_hours)
        return self.connection.execute(
            """SELECT * FROM youtube_source_metadata
            WHERE youtube_id=? AND hydrated_at>=?""",
            (youtube_id, cutoff.isoformat()),
        ).fetchone()

    def save_source_metadata(
        self,
        youtube_id: str,
        *,
        creator: str,
        url: str,
        title: str,
        upload_date: str | None,
        duration_seconds: int | None,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """INSERT INTO youtube_source_metadata
                (youtube_id, creator, url, title, upload_date, duration_seconds,
                 hydrated_at) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(youtube_id) DO UPDATE SET creator=excluded.creator,
                    url=excluded.url, title=excluded.title,
                    upload_date=excluded.upload_date,
                    duration_seconds=excluded.duration_seconds,
                    hydrated_at=excluded.hydrated_at""",
                (
                    youtube_id,
                    creator,
                    url,
                    title,
                    upload_date,
                    duration_seconds,
                    _now(),
                ),
            )

    def enqueue_publication_job(
        self,
        *,
        publishing_channel_id: str,
        publication_id: int | None,
        media_path: Path,
        timezone: str,
        priority: int = 100,
        urgency: SchedulerUrgency = SchedulerUrgency.NORMAL,
        use_experimental_window: bool = False,
        requested_publish_at: str | None = None,
        max_attempts: int | None = None,
    ) -> PublicationJob:
        if priority < 1:
            raise ValueError("Scheduler job priority must be positive.")
        attempts_limit = max_attempts or self.scheduler_settings().max_attempts
        if attempts_limit < 1:
            raise ValueError("Scheduler max_attempts must be positive.")
        now = _now()
        with self.connection:
            cursor = self.connection.execute(
                """INSERT INTO publication_jobs (
                    publishing_channel_id, publication_id, media_path,
                    requested_publish_at, timezone, priority, urgency,
                    use_experimental_window, state, attempts, max_attempts,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
                ON CONFLICT(publication_id) DO NOTHING""",
                (
                    publishing_channel_id,
                    publication_id,
                    str(Path(media_path).resolve()),
                    requested_publish_at,
                    timezone,
                    priority,
                    urgency,
                    use_experimental_window,
                    SchedulerJobState.QUEUED,
                    attempts_limit,
                    now,
                    now,
                ),
            )
            if cursor.rowcount == 1:
                job_id = int(cursor.lastrowid)
                self._record_scheduler_event(
                    job_id, "ENQUEUED", None, SchedulerJobState.QUEUED, ""
                )
            elif publication_id is not None:
                row = self.connection.execute(
                    "SELECT id FROM publication_jobs WHERE publication_id=?",
                    (publication_id,),
                ).fetchone()
                job_id = int(row["id"])
            else:
                raise RuntimeError("Scheduler job could not be persisted.")
        return self.scheduler_job(job_id)

    def scheduler_job(self, job_id: int) -> PublicationJob:
        row = self.connection.execute(
            "SELECT * FROM publication_jobs WHERE id=?", (job_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown scheduler job: {job_id}")
        return _publication_job(row)

    def scheduler_jobs(self) -> tuple[PublicationJob, ...]:
        rows = self.connection.execute(
            "SELECT * FROM publication_jobs ORDER BY priority, created_at, id"
        ).fetchall()
        return tuple(_publication_job(row) for row in rows)

    def scheduler_job_events(self, job_id: int) -> tuple[SchedulerJobEvent, ...]:
        rows = self.connection.execute(
            """SELECT * FROM scheduler_job_events WHERE job_id=?
            ORDER BY id""",
            (job_id,),
        ).fetchall()
        return tuple(_scheduler_job_event(row) for row in rows)

    def update_scheduler_job(
        self,
        job_id: int,
        *,
        state: SchedulerJobState,
        expected: tuple[SchedulerJobState, ...],
        resolved_publish_at: str | None = None,
        last_error: str | None = None,
        increment_attempts: bool = False,
        upload_started: bool = False,
        event_type: str = "STATE_CHANGED",
        event_details: str = "",
    ) -> PublicationJob:
        current = self.scheduler_job(job_id)
        if current.state not in expected:
            raise ValueError(
                f"Invalid scheduler transition: {current.state} -> {state}"
            )
        now = _now()
        terminal = state in {
            SchedulerJobState.COMPLETED,
            SchedulerJobState.FAILED_PERMANENT,
            SchedulerJobState.CANCELLED,
            SchedulerJobState.UPLOAD_UNCERTAIN,
        }
        with self.connection:
            self.connection.execute(
                """UPDATE publication_jobs SET state=?, resolved_publish_at=?,
                    attempts=attempts+?, upload_started_at=?, last_error=?,
                    updated_at=?, started_at=CASE WHEN ?='RUNNING'
                        THEN ? ELSE started_at END,
                    finished_at=CASE WHEN ? THEN ? ELSE finished_at END
                WHERE id=?""",
                (
                    state,
                    resolved_publish_at
                    if resolved_publish_at is not None
                    else current.resolved_publish_at,
                    int(increment_attempts),
                    now if upload_started else current.upload_started_at,
                    last_error,
                    now,
                    state,
                    now,
                    terminal,
                    now,
                    job_id,
                ),
            )
            self._record_scheduler_event(
                job_id, event_type, current.state, state, event_details
            )
        return self.scheduler_job(job_id)

    def recover_running_scheduler_jobs(self) -> tuple[PublicationJob, ...]:
        recovered = []
        for job in self.scheduler_jobs():
            if job.state is not SchedulerJobState.RUNNING:
                continue
            publication = (
                self.publication(job.publication_id) if job.publication_id else None
            )
            if (
                publication
                and publication.status is PublicationStatus.PUBLISHED
                and publication.remote_video_id
            ):
                target = SchedulerJobState.COMPLETED
                detail = "Persisted remote video confirms completed upload."
            elif (
                publication and publication.status is PublicationStatus.UPLOAD_UNCERTAIN
            ):
                target = SchedulerJobState.UPLOAD_UNCERTAIN
                detail = "Publication already records an uncertain upload outcome."
            elif job.upload_started_at:
                target = SchedulerJobState.UPLOAD_UNCERTAIN
                detail = "Process restarted after upload execution became eligible."
            else:
                target = SchedulerJobState.SCHEDULED
                detail = "Process restarted before upload execution began."
            recovered.append(
                self.update_scheduler_job(
                    job.job_id,
                    state=target,
                    expected=(SchedulerJobState.RUNNING,),
                    event_type="CRASH_RECOVERY",
                    event_details=detail,
                )
            )
        return tuple(recovered)

    def _record_scheduler_event(
        self,
        job_id: int,
        event_type: str,
        from_state: str | None,
        to_state: str | None,
        details: str,
    ) -> None:
        self.connection.execute(
            """INSERT INTO scheduler_job_events
            (job_id, event_type, from_state, to_state, details, created_at)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (job_id, event_type, from_state, to_state, details, _now()),
        )

    def short_by_path(self, path: Path) -> ShortRecord | None:
        resolved = str(Path(path).resolve())
        row = self.connection.execute(
            """SELECT * FROM shorts
            WHERE file_path=? OR queue_path=? ORDER BY id LIMIT 1""",
            (resolved, resolved),
        ).fetchone()
        return _short_record(row) if row else None

    def _required_publication(self, publication_id: int) -> PublicationRecord:
        record = self.publication(publication_id)
        if record is None:
            raise KeyError(f"Unknown publication: {publication_id}")
        return record

    def _required_publication_attempt(self, attempt_id: int) -> PublicationAttempt:
        row = self.connection.execute(
            "SELECT * FROM publication_attempts WHERE id=?", (attempt_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown publication attempt: {attempt_id}")
        return _publication_attempt(row)

    def queued_metadata(self, youtube_id: str) -> tuple[QueueMetadata, ...]:
        """Return complete publication metadata in editorial rank order."""
        rows = self.connection.execute(
            """
            SELECT s.*, v.creator_name, v.theme_name
            FROM shorts AS s JOIN videos AS v USING (youtube_id)
            WHERE s.youtube_id = ? AND s.status = ?
            ORDER BY s.clip_index
            """,
            (youtube_id, ShortStatus.QUEUED),
        ).fetchall()
        return tuple(
            QueueMetadata(
                youtube_id=row["youtube_id"],
                creator_name=row["creator_name"],
                theme_name=row["theme_name"],
                rank=row["clip_index"],
                account_slot=row["account_slot"],
                account_identifier=row["account_identifier"],
                publishing_channel_id=row["publishing_channel_id"],
                title=row["title"],
                headline=row["headline"],
                generator_score=row["generator_score"],
                editorial_score=row["editorial_score"],
                candidate_id=row["candidate_id"],
                duration_seconds=row["duration_seconds"],
                master_path=Path(row["file_path"]),
                queue_path=Path(row["queue_path"]),
            )
            for row in rows
        )

    def list_theme_templates(self) -> tuple[ThemeTemplate, ...]:
        rows = self.connection.execute(
            "SELECT * FROM theme_catalog ORDER BY display_name COLLATE NOCASE"
        ).fetchall()
        return tuple(self._theme_template(row) for row in rows)

    def save_theme_market_profile(
        self,
        *,
        theme_key: str,
        platform: str,
        region_code: str,
        language_code: str,
        youtube_category_id: str,
        youtube_category_name: str,
        enabled: bool = True,
        is_default_for_theme: bool = False,
    ) -> ThemeMarketProfile:
        """Upsert one platform/market profile without overwriting other markets."""
        now = _now()
        with self.connection:
            if is_default_for_theme:
                self.connection.execute(
                    "UPDATE theme_market_profiles SET is_default_for_theme=0 "
                    "WHERE theme_key=? AND platform=?",
                    (theme_key, platform),
                )
            self.connection.execute(
                """INSERT INTO theme_market_profiles (
                    theme_key, platform, region_code, language_code,
                    youtube_category_id, youtube_category_name, enabled,
                    is_default_for_theme, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(theme_key, platform, region_code, language_code)
                DO UPDATE SET
                    youtube_category_id=excluded.youtube_category_id,
                    youtube_category_name=excluded.youtube_category_name,
                    enabled=excluded.enabled,
                    is_default_for_theme=excluded.is_default_for_theme,
                    updated_at=excluded.updated_at""",
                (
                    theme_key,
                    platform,
                    region_code,
                    language_code,
                    youtube_category_id,
                    youtube_category_name,
                    enabled,
                    is_default_for_theme,
                    now,
                    now,
                ),
            )
        profile = self.theme_market_profile(
            theme_key, platform, region_code, language_code
        )
        if profile is None:
            raise RuntimeError("Theme market profile could not be persisted.")
        return profile

    def theme_market_profile(
        self, theme_key: str, platform: str, region_code: str, language_code: str
    ) -> ThemeMarketProfile | None:
        row = self.connection.execute(
            """SELECT * FROM theme_market_profiles
            WHERE theme_key=? AND platform=? AND region_code=? AND language_code=?""",
            (theme_key, platform, region_code, language_code),
        ).fetchone()
        return _theme_market_profile(row) if row else None

    def theme_market_profiles(self) -> tuple[ThemeMarketProfile, ...]:
        rows = self.connection.execute(
            """SELECT * FROM theme_market_profiles
            ORDER BY theme_key, platform, region_code, language_code"""
        ).fetchall()
        return tuple(_theme_market_profile(row) for row in rows)

    def save_content_source(
        self,
        *,
        platform: str,
        external_channel_id: str,
        display_name: str,
        handle: str | None,
        channel_url: str,
        region_code: str,
        language_code: str,
        source_kind: str = "CREATOR_CHANNEL",
        validation_status: SourceValidationStatus = SourceValidationStatus.PENDING,
        last_validated_at: str | None = None,
        latest_upload_at: str | None = None,
        sampled_video_count: int = 0,
        sampled_longform_count: int = 0,
        sampled_target_duration_count: int = 0,
        observed_duration_min_seconds: int | None = None,
        observed_duration_max_seconds: int | None = None,
        enabled: bool = True,
        rights_status: SourceRightsStatus = SourceRightsStatus.UNKNOWN,
    ) -> ContentSource:
        now = _now()
        with self.connection:
            self.connection.execute(
                """INSERT INTO content_sources (
                    platform, external_channel_id, display_name, handle, channel_url,
                    region_code, language_code, source_kind, validation_status,
                    last_validated_at, latest_upload_at, sampled_video_count,
                    sampled_longform_count, sampled_target_duration_count,
                    observed_duration_min_seconds, observed_duration_max_seconds,
                    enabled, rights_status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(platform, external_channel_id) DO UPDATE SET
                    display_name=excluded.display_name, handle=excluded.handle,
                    channel_url=excluded.channel_url, region_code=excluded.region_code,
                    language_code=excluded.language_code,
                    source_kind=excluded.source_kind,
                    validation_status=excluded.validation_status,
                    last_validated_at=excluded.last_validated_at,
                    latest_upload_at=excluded.latest_upload_at,
                    sampled_video_count=excluded.sampled_video_count,
                    sampled_longform_count=excluded.sampled_longform_count,
                    sampled_target_duration_count=excluded.sampled_target_duration_count,
                    observed_duration_min_seconds=excluded.observed_duration_min_seconds,
                    observed_duration_max_seconds=excluded.observed_duration_max_seconds,
                    enabled=excluded.enabled, rights_status=excluded.rights_status,
                    updated_at=excluded.updated_at""",
                (
                    platform,
                    external_channel_id,
                    display_name,
                    handle,
                    channel_url,
                    region_code,
                    language_code,
                    source_kind,
                    validation_status,
                    last_validated_at,
                    latest_upload_at,
                    sampled_video_count,
                    sampled_longform_count,
                    sampled_target_duration_count,
                    observed_duration_min_seconds,
                    observed_duration_max_seconds,
                    enabled,
                    rights_status,
                    now,
                    now,
                ),
            )
        source = self.content_source(platform, external_channel_id)
        if source is None:
            raise RuntimeError("Content source could not be persisted.")
        return source

    def content_source(
        self, platform: str, external_channel_id: str
    ) -> ContentSource | None:
        row = self.connection.execute(
            "SELECT * FROM content_sources WHERE platform=? AND external_channel_id=?",
            (platform, external_channel_id),
        ).fetchone()
        return _content_source(row) if row else None

    def content_sources(self) -> tuple[ContentSource, ...]:
        rows = self.connection.execute(
            "SELECT * FROM content_sources ORDER BY display_name COLLATE NOCASE"
        ).fetchall()
        return tuple(_content_source(row) for row in rows)

    def link_theme_content_source(
        self,
        *,
        profile_id: int,
        source_id: int,
        priority: int,
        enabled: bool = True,
        target_duration_min_seconds: int = 900,
        target_duration_max_seconds: int = 2400,
    ) -> ThemeContentSource:
        if not 1 <= priority <= 5:
            raise ValueError("Source priority must be between 1 and 5.")
        if target_duration_min_seconds >= target_duration_max_seconds:
            raise ValueError("Source target duration range is invalid.")
        now = _now()
        with self.connection:
            self.connection.execute(
                """INSERT INTO theme_content_sources (
                    profile_id, source_id, priority, enabled,
                    target_duration_min_seconds, target_duration_max_seconds,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(profile_id, source_id) DO UPDATE SET
                    priority=excluded.priority, enabled=excluded.enabled,
                    target_duration_min_seconds=excluded.target_duration_min_seconds,
                    target_duration_max_seconds=excluded.target_duration_max_seconds,
                    updated_at=excluded.updated_at""",
                (
                    profile_id,
                    source_id,
                    priority,
                    enabled,
                    target_duration_min_seconds,
                    target_duration_max_seconds,
                    now,
                    now,
                ),
            )
        row = self.connection.execute(
            """SELECT * FROM theme_content_sources
            WHERE profile_id=? AND source_id=?""",
            (profile_id, source_id),
        ).fetchone()
        if row is None:
            raise RuntimeError("Theme content source could not be persisted.")
        return _theme_content_source(row)

    def theme_content_sources(
        self, profile_id: int
    ) -> tuple[tuple[ThemeContentSource, ContentSource], ...]:
        rows = self.connection.execute(
            """SELECT * FROM theme_content_sources
            WHERE profile_id=? ORDER BY priority""",
            (profile_id,),
        ).fetchall()
        result = []
        for row in rows:
            source_row = self.connection.execute(
                "SELECT * FROM content_sources WHERE id=?", (row["source_id"],)
            ).fetchone()
            if source_row is None:
                raise RuntimeError("Theme association references a missing source.")
            result.append((_theme_content_source(row), _content_source(source_row)))
        return tuple(result)

    def save_theme_posting_profile(
        self,
        *,
        theme_market_profile_id: int,
        timezone: str,
        timing_policy: TimingPolicy,
        strategy: PostingStrategy = PostingStrategy.SEEDED,
        confidence: PostingConfidence = PostingConfidence.MEDIUM,
        enabled: bool = True,
        source_note: str = "",
        last_reviewed_at: str | None = None,
    ) -> ThemePostingProfile:
        # Construction validates the IANA timezone before persistence.
        ThemePostingProfile(
            0,
            theme_market_profile_id,
            timezone,
            timing_policy,
            strategy,
            confidence,
            enabled,
            source_note,
            last_reviewed_at,
            "",
            "",
        )
        now = _now()
        with self.connection:
            self.connection.execute(
                """INSERT INTO theme_posting_profiles (
                    theme_market_profile_id, timezone, timing_policy, strategy,
                    confidence, enabled, source_note, last_reviewed_at,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(theme_market_profile_id) DO UPDATE SET
                    timezone=excluded.timezone,
                    timing_policy=excluded.timing_policy,
                    strategy=excluded.strategy,
                    confidence=excluded.confidence,
                    enabled=excluded.enabled,
                    source_note=excluded.source_note,
                    last_reviewed_at=excluded.last_reviewed_at,
                    updated_at=excluded.updated_at""",
                (
                    theme_market_profile_id,
                    timezone,
                    timing_policy,
                    strategy,
                    confidence,
                    enabled,
                    source_note,
                    last_reviewed_at,
                    now,
                    now,
                ),
            )
        profile = self.theme_posting_profile(theme_market_profile_id)
        if profile is None:
            raise RuntimeError("Theme posting profile could not be persisted.")
        return profile

    def theme_posting_profile(
        self, theme_market_profile_id: int
    ) -> ThemePostingProfile | None:
        row = self.connection.execute(
            """SELECT * FROM theme_posting_profiles
            WHERE theme_market_profile_id=?""",
            (theme_market_profile_id,),
        ).fetchone()
        return _theme_posting_profile(row) if row else None

    def theme_posting_profiles(self) -> tuple[ThemePostingProfile, ...]:
        rows = self.connection.execute(
            "SELECT * FROM theme_posting_profiles ORDER BY theme_market_profile_id"
        ).fetchall()
        return tuple(_theme_posting_profile(row) for row in rows)

    def replace_posting_windows(
        self,
        posting_profile_id: int,
        windows: tuple[tuple[int, str, str, tuple[str, ...], bool, bool], ...],
    ) -> tuple[PostingWindow, ...]:
        priorities = [item[0] for item in windows]
        if len(priorities) != len(set(priorities)):
            raise ValueError("Posting window priorities must be unique.")
        normalized = []
        for priority, start, end, days, experimental, enabled in windows:
            _validate_posting_window(priority, start, end, days)
            normalized.append(
                (priority, start, end, tuple(days), experimental, enabled)
            )
        now = _now()
        with self.connection:
            self.connection.execute(
                "DELETE FROM posting_windows WHERE posting_profile_id=?",
                (posting_profile_id,),
            )
            self.connection.executemany(
                """INSERT INTO posting_windows (
                    posting_profile_id, priority, start_local_time, end_local_time,
                    days_of_week_json, experimental, enabled, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    (
                        posting_profile_id,
                        priority,
                        start,
                        end,
                        json.dumps(days),
                        experimental,
                        enabled,
                        now,
                        now,
                    )
                    for priority, start, end, days, experimental, enabled in normalized
                ),
            )
        return self.posting_windows(posting_profile_id)

    def posting_windows(self, posting_profile_id: int) -> tuple[PostingWindow, ...]:
        rows = self.connection.execute(
            """SELECT * FROM posting_windows WHERE posting_profile_id=?
            ORDER BY priority""",
            (posting_profile_id,),
        ).fetchall()
        return tuple(_posting_window(row) for row in rows)

    def save_channel_posting_override(
        self,
        *,
        publishing_channel_id: str,
        posting_profile_id: int,
        timezone: str | None = None,
        timing_policy: TimingPolicy | None = None,
        strategy: PostingStrategy | None = None,
        confidence: PostingConfidence | None = None,
        enabled: bool = True,
        source_note: str = "",
    ) -> ChannelPostingOverride:
        if timezone:
            from zoneinfo import ZoneInfo

            ZoneInfo(timezone)
        now = _now()
        with self.connection:
            self.connection.execute(
                """INSERT INTO channel_posting_overrides (
                    publishing_channel_id, posting_profile_id, timezone,
                    timing_policy, strategy, confidence, enabled, source_note,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(publishing_channel_id) DO UPDATE SET
                    posting_profile_id=excluded.posting_profile_id,
                    timezone=excluded.timezone,
                    timing_policy=excluded.timing_policy,
                    strategy=excluded.strategy,
                    confidence=excluded.confidence,
                    enabled=excluded.enabled,
                    source_note=excluded.source_note,
                    updated_at=excluded.updated_at""",
                (
                    publishing_channel_id,
                    posting_profile_id,
                    timezone,
                    timing_policy,
                    strategy,
                    confidence,
                    enabled,
                    source_note,
                    now,
                    now,
                ),
            )
        override = self.channel_posting_override(publishing_channel_id)
        if override is None:
            raise RuntimeError("Channel posting override could not be persisted.")
        return override

    def channel_posting_override(
        self, publishing_channel_id: str
    ) -> ChannelPostingOverride | None:
        row = self.connection.execute(
            """SELECT * FROM channel_posting_overrides
            WHERE publishing_channel_id=?""",
            (publishing_channel_id,),
        ).fetchone()
        return _channel_posting_override(row) if row else None

    def save_brand_generation(self, result: BrandGenerationResult) -> int:
        now = _now()
        usage = result.usage
        with self.connection:
            cursor = self.connection.execute(
                """INSERT INTO brand_generations
                (provider, model, operation, status, error, input_tokens, output_tokens,
                 thinking_tokens, total_tokens, duration_seconds,
                 theoretical_cost_usd, created_at)
                VALUES (?, ?, 'brand_generation', 'SUCCEEDED', NULL,
                        ?, ?, ?, ?, ?, ?, ?)""",
                (
                    result.provider,
                    result.model,
                    usage.input_tokens,
                    usage.output_tokens,
                    usage.thinking_tokens,
                    usage.total_tokens,
                    usage.duration_seconds,
                    usage.estimated_cost_usd,
                    now,
                ),
            )
            generation_id = int(cursor.lastrowid)
            for candidate in result.candidates:
                self.connection.execute(
                    """INSERT INTO brand_candidates
                    (generation_id, publishing_channel_id, public_name,
                     handle_candidates_json, handle_availability, description,
                     tagline, brand_keywords_json, logo_brief, banner_brief,
                     reason, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        generation_id,
                        candidate.publishing_channel_id,
                        candidate.public_name,
                        json.dumps(candidate.handle_candidates, ensure_ascii=False),
                        HandleAvailability.UNKNOWN,
                        candidate.description,
                        candidate.tagline,
                        json.dumps(candidate.brand_keywords, ensure_ascii=False),
                        candidate.logo_brief,
                        candidate.banner_brief,
                        candidate.reason,
                        now,
                    ),
                )
        return generation_id

    def save_brand_generation_failure(
        self,
        *,
        provider: str,
        model: str,
        usage: AnalysisUsage,
        status: str,
        error: str,
    ) -> int:
        if status not in {"API_FAILED", "VALIDATION_FAILED"}:
            raise ValueError("Invalid brand generation failure status.")
        with self.connection:
            cursor = self.connection.execute(
                """INSERT INTO brand_generations
                (provider, model, operation, status, error, input_tokens,
                 output_tokens, thinking_tokens, total_tokens, duration_seconds,
                 theoretical_cost_usd, created_at)
                VALUES (?, ?, 'brand_generation', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    provider,
                    model,
                    status,
                    error,
                    usage.input_tokens,
                    usage.output_tokens,
                    usage.thinking_tokens,
                    usage.total_tokens,
                    usage.duration_seconds,
                    usage.estimated_cost_usd,
                    _now(),
                ),
            )
        return int(cursor.lastrowid)

    def brand_candidates(self, generation_id: int) -> tuple[BrandCandidate, ...]:
        rows = self.connection.execute(
            "SELECT * FROM brand_candidates WHERE generation_id = ? ORDER BY id",
            (generation_id,),
        ).fetchall()
        return tuple(
            BrandCandidate(
                publishing_channel_id=row["publishing_channel_id"],
                public_name=row["public_name"],
                handle_candidates=tuple(json.loads(row["handle_candidates_json"])),
                description=row["description"],
                tagline=row["tagline"],
                brand_keywords=tuple(json.loads(row["brand_keywords_json"])),
                logo_brief=row["logo_brief"],
                banner_brief=row["banner_brief"],
                reason=row["reason"],
            )
            for row in rows
        )

    def save_catalog_brand_generation(
        self, theme_key: str, result: BrandGenerationResult
    ) -> int:
        usage = result.usage
        now = _now()
        with self.connection:
            cursor = self.connection.execute(
                """INSERT INTO catalog_brand_generations
                (theme_key, provider, model, status, input_tokens, output_tokens,
                 thinking_tokens, total_tokens, duration_seconds,
                 theoretical_cost_usd, created_at, updated_at)
                VALUES (?, ?, ?, 'GENERATED', ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    theme_key,
                    result.provider,
                    result.model,
                    usage.input_tokens,
                    usage.output_tokens,
                    usage.thinking_tokens,
                    usage.total_tokens,
                    usage.duration_seconds,
                    usage.estimated_cost_usd,
                    now,
                    now,
                ),
            )
            generation_id = int(cursor.lastrowid)
            for candidate in result.candidates:
                self.connection.execute(
                    """INSERT INTO catalog_brand_candidates
                    (generation_id, target_id, public_name, handles_json,
                     description, tagline, keywords_json, logo_brief,
                     banner_brief, reason, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        generation_id,
                        candidate.publishing_channel_id,
                        candidate.public_name,
                        json.dumps(candidate.handle_candidates, ensure_ascii=False),
                        candidate.description,
                        candidate.tagline,
                        json.dumps(candidate.brand_keywords, ensure_ascii=False),
                        candidate.logo_brief,
                        candidate.banner_brief,
                        candidate.reason,
                        now,
                    ),
                )
        return generation_id

    def catalog_brand_generation(self, theme_key: str):
        return self.connection.execute(
            "SELECT * FROM catalog_brand_generations WHERE theme_key = ?",
            (theme_key,),
        ).fetchone()

    def catalog_brand_candidates(
        self, generation_id: int
    ) -> tuple[BrandCandidate, ...]:
        rows = self.connection.execute(
            "SELECT * FROM catalog_brand_candidates "
            "WHERE generation_id = ? ORDER BY id",
            (generation_id,),
        ).fetchall()
        return tuple(
            BrandCandidate(
                row["target_id"],
                row["public_name"],
                tuple(json.loads(row["handles_json"])),
                row["description"],
                row["tagline"],
                tuple(json.loads(row["keywords_json"])),
                row["logo_brief"],
                row["banner_brief"],
                row["reason"],
            )
            for row in rows
        )

    def save_planned_channel_brands(
        self,
        generation_id: int,
        plans: tuple[PlannedChannelBrand, ...],
        assignments: tuple[PlannedIdentityAssignment, ...],
        *,
        rerank_usage: AnalysisUsage,
    ) -> None:
        now = _now()
        with self.connection:
            for plan in plans:
                self.connection.execute(
                    """INSERT INTO planned_channel_brands
                    (brand_plan_id, theme_key, slot, public_name, preferred_handle,
                     backup_handles_json, handle_availability, description, tagline,
                     brand_keywords_json, visual_direction, palette_json, logo_brief,
                     banner_brief, status, provider, model, version,
                     source_generation_id, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?)
                    ON CONFLICT(brand_plan_id) DO UPDATE SET
                      public_name=excluded.public_name,
                      preferred_handle=excluded.preferred_handle,
                      backup_handles_json=excluded.backup_handles_json,
                      description=excluded.description, tagline=excluded.tagline,
                      brand_keywords_json=excluded.brand_keywords_json,
                      visual_direction=excluded.visual_direction,
                      palette_json=excluded.palette_json,
                      logo_brief=excluded.logo_brief,
                      banner_brief=excluded.banner_brief,
                      status=excluded.status, provider=excluded.provider,
                      model=excluded.model, version=planned_channel_brands.version + 1,
                      source_generation_id=excluded.source_generation_id,
                      updated_at=excluded.updated_at""",
                    (
                        plan.brand_plan_id,
                        plan.theme_key,
                        plan.slot,
                        plan.public_name,
                        plan.preferred_handle,
                        json.dumps(plan.backup_handles, ensure_ascii=False),
                        plan.handle_availability,
                        plan.description,
                        plan.tagline,
                        json.dumps(plan.brand_keywords, ensure_ascii=False),
                        plan.visual_direction,
                        json.dumps(plan.palette),
                        plan.logo_brief,
                        plan.banner_brief,
                        plan.status,
                        plan.provider,
                        plan.model,
                        plan.version,
                        generation_id,
                        now,
                        now,
                    ),
                )
            for assignment in assignments:
                self.connection.execute(
                    """INSERT INTO planned_identity_assignments
                    (brand_plan_id, planned_identity_id, created_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(brand_plan_id) DO UPDATE SET
                      planned_identity_id=excluded.planned_identity_id""",
                    (assignment.brand_plan_id, assignment.planned_identity_id, now),
                )
            self.connection.execute(
                """UPDATE catalog_brand_generations SET
                status='RERANKED', rerank_input_tokens=?, rerank_output_tokens=?,
                rerank_thinking_tokens=?, rerank_total_tokens=?,
                rerank_duration_seconds=?, rerank_theoretical_cost_usd=?, updated_at=?
                WHERE id=?""",
                (
                    rerank_usage.input_tokens,
                    rerank_usage.output_tokens,
                    rerank_usage.thinking_tokens,
                    rerank_usage.total_tokens,
                    rerank_usage.duration_seconds,
                    rerank_usage.estimated_cost_usd,
                    now,
                    generation_id,
                ),
            )

    def planned_channel_brands(
        self, theme_key: str | None = None
    ) -> tuple[PlannedChannelBrand, ...]:
        sql = "SELECT * FROM planned_channel_brands"
        params = ()
        if theme_key is not None:
            sql += " WHERE theme_key = ?"
            params = (theme_key,)
        sql += " ORDER BY theme_key, slot"
        rows = self.connection.execute(sql, params).fetchall()
        return tuple(
            PlannedChannelBrand(
                row["brand_plan_id"],
                row["theme_key"],
                row["slot"],
                row["public_name"],
                row["preferred_handle"],
                tuple(json.loads(row["backup_handles_json"])),
                row["description"],
                row["tagline"],
                tuple(json.loads(row["brand_keywords_json"])),
                row["visual_direction"],
                tuple(json.loads(row["palette_json"])),
                row["logo_brief"],
                row["banner_brief"],
                PlannedBrandStatus(row["status"]),
                HandleAvailability(row["handle_availability"]),
                row["provider"],
                row["model"],
                row["version"],
                row["source_generation_id"],
            )
            for row in rows
        )

    def planned_identity_assignments(self) -> dict[str, str]:
        rows = self.connection.execute(
            """SELECT brand_plan_id, planned_identity_id
            FROM planned_identity_assignments"""
        ).fetchall()
        return {row["brand_plan_id"]: row["planned_identity_id"] for row in rows}

    def assign_planned_identity(
        self, brand_plan_id: str, google_identity_id: str
    ) -> None:
        with self.connection:
            self.connection.execute(
                """INSERT INTO planned_identity_assignments
                (brand_plan_id, planned_identity_id, created_at) VALUES (?, ?, ?)
                ON CONFLICT(brand_plan_id) DO UPDATE SET
                  planned_identity_id=excluded.planned_identity_id""",
                (brand_plan_id, google_identity_id, _now()),
            )

    def youtube_provisioning_records(
        self, google_identity_id: str | None = None
    ) -> tuple[YouTubeProvisioningRecord, ...]:
        sql = "SELECT * FROM youtube_channel_provisioning"
        params: tuple[object, ...] = ()
        if google_identity_id is not None:
            sql += " WHERE google_identity_id=?"
            params = (google_identity_id,)
        sql += " ORDER BY expected_name"
        return tuple(
            _youtube_provisioning_record(row)
            for row in self.connection.execute(sql, params).fetchall()
        )

    def save_youtube_provisioning_record(
        self,
        *,
        brand_plan_id: str,
        google_identity_id: str,
        expected_name: str,
        expected_handle: str,
        status: str,
        actual_handle: str | None = None,
        youtube_channel_id: str | None = None,
        logo_path: Path | None = None,
        last_error: str | None = None,
    ) -> YouTubeProvisioningRecord:
        now = _now()
        with self.connection:
            self.connection.execute(
                """INSERT INTO youtube_channel_provisioning
                (brand_plan_id, google_identity_id, expected_name, expected_handle,
                 actual_handle, youtube_channel_id, status, logo_path, last_error,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(brand_plan_id) DO UPDATE SET
                  google_identity_id=excluded.google_identity_id,
                  expected_name=excluded.expected_name,
                  expected_handle=excluded.expected_handle,
                  actual_handle=COALESCE(excluded.actual_handle, actual_handle),
                  youtube_channel_id=COALESCE(
                    excluded.youtube_channel_id, youtube_channel_id
                  ), status=excluded.status,
                  logo_path=COALESCE(excluded.logo_path, logo_path),
                  last_error=excluded.last_error, updated_at=excluded.updated_at""",
                (
                    brand_plan_id,
                    google_identity_id,
                    expected_name,
                    expected_handle,
                    actual_handle,
                    youtube_channel_id,
                    status,
                    str(logo_path) if logo_path else None,
                    last_error,
                    now,
                    now,
                ),
            )
        row = self.connection.execute(
            "SELECT * FROM youtube_channel_provisioning WHERE brand_plan_id=?",
            (brand_plan_id,),
        ).fetchone()
        assert row is not None
        return _youtube_provisioning_record(row)

    def set_youtube_web_session_status(
        self, google_identity_id: str, status: str, error: str | None = None
    ) -> None:
        with self.connection:
            self.connection.execute(
                """INSERT INTO youtube_web_sessions
                (google_identity_id, status, last_checked_at, last_error)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(google_identity_id) DO UPDATE SET
                  status=excluded.status,
                  last_checked_at=excluded.last_checked_at,
                  last_error=excluded.last_error""",
                (google_identity_id, status, _now(), error),
            )

    def youtube_web_session_statuses(self) -> dict[str, tuple[str, str | None]]:
        rows = self.connection.execute(
            "SELECT google_identity_id, status, last_error FROM youtube_web_sessions"
        ).fetchall()
        return {
            row["google_identity_id"]: (row["status"], row["last_error"])
            for row in rows
        }

    def approve_planned_channel_brands(
        self,
        plans: tuple[PlannedChannelBrand, ...],
        *,
        provenance: str,
        review_note: str,
    ) -> None:
        """Atomically archive and approve a complete human-reviewed brand plan set."""
        current = self.planned_channel_brands()
        if not current or {item.brand_plan_id for item in plans} != {
            item.brand_plan_id for item in current
        }:
            raise ValueError(
                "Review must include the complete existing brand plan set."
            )
        if any(
            item.status != PlannedBrandStatus.PROPOSED_FOR_REVIEW for item in current
        ):
            raise ValueError("Only plans proposed for review can be approved.")
        if any(item.status != PlannedBrandStatus.APPROVED for item in plans):
            raise ValueError("Every reviewed plan must have APPROVED status.")
        if any(
            item.handle_availability != HandleAvailability.UNKNOWN for item in plans
        ):
            raise ValueError("Manual review must not infer handle availability.")

        def normalize(value: str) -> str:
            return "".join(
                character for character in value.casefold() if character.isalnum()
            )

        names = [normalize(item.public_name) for item in plans]
        preferred = [normalize(item.preferred_handle) for item in plans]
        if len(names) != len(set(names)):
            raise ValueError("Reviewed public names must be globally unique.")
        if len(preferred) != len(set(preferred)):
            raise ValueError("Reviewed preferred handles must be globally unique.")
        for item in plans:
            handles = (item.preferred_handle, *item.backup_handles)
            if any(normalize_handle_candidate(handle) != handle for handle in handles):
                raise ValueError("Every reviewed handle must already be normalized.")
            normalized = [normalize(handle) for handle in handles]
            if len(handles) != 3 or len(normalized) != len(set(normalized)):
                raise ValueError(
                    "Each plan must have three distinct normalized handles."
                )
        if global_brand_collisions(plans):
            raise ValueError("Reviewed plans must pass global collision validation.")

        now = _now()
        by_id = {item.brand_plan_id: item for item in plans}
        with self.connection:
            for previous in current:
                reviewed = by_id[previous.brand_plan_id]
                snapshot = json.dumps(asdict(previous), ensure_ascii=False)
                self.connection.execute(
                    """INSERT INTO planned_channel_brand_history
                    (brand_plan_id, version, snapshot_json, provenance, review_note,
                     archived_at)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        previous.brand_plan_id,
                        previous.version,
                        snapshot,
                        provenance,
                        review_note,
                        now,
                    ),
                )
                self.connection.execute(
                    """UPDATE planned_channel_brands SET
                    public_name=?, preferred_handle=?, backup_handles_json=?,
                    handle_availability=?, description=?, tagline=?,
                    brand_keywords_json=?, visual_direction=?, palette_json=?,
                    logo_brief=?, banner_brief=?, status=?, provider=?, model=?,
                    version=?, source_generation_id=?, updated_at=?
                    WHERE brand_plan_id=?""",
                    (
                        reviewed.public_name,
                        reviewed.preferred_handle,
                        json.dumps(reviewed.backup_handles, ensure_ascii=False),
                        reviewed.handle_availability,
                        reviewed.description,
                        reviewed.tagline,
                        json.dumps(reviewed.brand_keywords, ensure_ascii=False),
                        reviewed.visual_direction,
                        json.dumps(reviewed.palette),
                        reviewed.logo_brief,
                        reviewed.banner_brief,
                        reviewed.status,
                        reviewed.provider,
                        reviewed.model,
                        previous.version + 1,
                        previous.source_generation_id,
                        now,
                        previous.brand_plan_id,
                    ),
                )

    def save_brand_asset(self, asset) -> None:
        from autoshorts.brand_assets import GeneratedBrandAsset

        if not isinstance(asset, GeneratedBrandAsset):
            raise TypeError("asset must be GeneratedBrandAsset")
        with self.connection:
            self.connection.execute(
                """INSERT INTO brand_asset_metadata
                (brand_plan_id, asset_type, file_path, provider, model, prompt,
                 width, height, checksum, created_at, status, version, file_size,
                 preview_path, delivery_path, resolved_visual_direction_json,
                 source_package, source_file, source_preserved_path,
                 source_checksum, source_width, source_height, canonical_transform)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(brand_plan_id, asset_type, version) DO NOTHING""",
                (
                    asset.brand_plan_id,
                    asset.asset_type,
                    str(asset.file_path),
                    asset.provider,
                    asset.model,
                    asset.prompt,
                    asset.width,
                    asset.height,
                    asset.checksum,
                    asset.created_at,
                    asset.status,
                    asset.version,
                    asset.file_size,
                    str(asset.preview_path) if asset.preview_path else None,
                    str(asset.delivery_path) if asset.delivery_path else None,
                    json.dumps(asset.resolved_visual_direction.as_dict())
                    if asset.resolved_visual_direction
                    else None,
                    asset.source_package,
                    asset.source_file,
                    str(asset.source_preserved_path)
                    if asset.source_preserved_path
                    else None,
                    asset.source_checksum,
                    asset.source_width,
                    asset.source_height,
                    asset.canonical_transform,
                ),
            )

    def brand_assets(self, brand_plan_id: str | None = None):
        sql = "SELECT * FROM brand_asset_metadata"
        params = ()
        if brand_plan_id is not None:
            sql += " WHERE brand_plan_id = ?"
            params = (brand_plan_id,)
        sql += " ORDER BY brand_plan_id, asset_type, version"
        return tuple(
            _brand_asset_record(row) for row in self.connection.execute(sql, params)
        )

    def brand_asset(self, brand_plan_id: str, asset_type, version: int):
        row = self.connection.execute(
            """SELECT * FROM brand_asset_metadata
            WHERE brand_plan_id=? AND asset_type=? AND version=?""",
            (brand_plan_id, asset_type, version),
        ).fetchone()
        return _brand_asset_record(row) if row else None

    def latest_valid_brand_asset(self, brand_plan_id: str, asset_type):
        row = self.connection.execute(
            """SELECT * FROM brand_asset_metadata
            WHERE brand_plan_id=? AND asset_type=?
              AND status IN ('VALIDATED', 'APPROVED')
            ORDER BY version DESC LIMIT 1""",
            (brand_plan_id, asset_type),
        ).fetchone()
        return _brand_asset_record(row) if row else None

    def next_brand_asset_version(self, brand_plan_id: str, asset_type) -> int:
        return int(
            self.connection.execute(
                """SELECT COALESCE(MAX(version), 0) + 1
                FROM brand_asset_metadata WHERE brand_plan_id=? AND asset_type=?""",
                (brand_plan_id, asset_type),
            ).fetchone()[0]
        )

    def set_brand_asset_status(
        self, brand_plan_id: str, asset_type, version: int, status
    ) -> None:
        from autoshorts.brand_assets import BrandAssetStatus

        selected = BrandAssetStatus(status)
        with self.connection:
            current = self.connection.execute(
                """SELECT status FROM brand_asset_metadata
                WHERE brand_plan_id=? AND asset_type=? AND version=?""",
                (brand_plan_id, asset_type, version),
            ).fetchone()
            if current is None:
                raise KeyError("Unknown brand asset version.")
            if selected == BrandAssetStatus.APPROVED and current["status"] not in {
                BrandAssetStatus.VALIDATED,
                BrandAssetStatus.APPROVED,
            }:
                raise ValueError("Only a validated brand asset can be approved.")
            if selected == BrandAssetStatus.APPROVED:
                self.connection.execute(
                    """UPDATE brand_asset_metadata SET status='RETIRED'
                    WHERE brand_plan_id=? AND asset_type=? AND status='APPROVED'""",
                    (brand_plan_id, asset_type),
                )
            cursor = self.connection.execute(
                """UPDATE brand_asset_metadata SET status=?
                WHERE brand_plan_id=? AND asset_type=? AND version=?""",
                (selected, brand_plan_id, asset_type, version),
            )
            if cursor.rowcount != 1:
                raise KeyError("Unknown brand asset version.")

    def approve_brand_kit(self, kit: ChannelBrandKit) -> ChannelBrandKit:
        if kit.status != BrandKitStatus.APPROVED:
            raise ValueError("Approval requires APPROVED status.")
        now = _now()
        with self.connection:
            self.connection.execute(
                "UPDATE channel_brand_kits SET status = 'RETIRED' "
                "WHERE publishing_channel_id = ? AND status = 'APPROVED'",
                (kit.publishing_channel_id,),
            )
            version = self.connection.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM channel_brand_kits "
                "WHERE publishing_channel_id = ?",
                (kit.publishing_channel_id,),
            ).fetchone()[0]
            self.connection.execute(
                """INSERT INTO channel_brand_kits
                (publishing_channel_id, public_name, handle, description,
                 tagline, brand_keywords_json, visual_direction, palette_json,
                 logo_brief, banner_brief, status, version, source_plan_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'APPROVED', ?, ?, ?)""",
                (
                    kit.publishing_channel_id,
                    kit.public_name,
                    kit.handle,
                    kit.description,
                    kit.tagline,
                    json.dumps(kit.brand_keywords, ensure_ascii=False),
                    kit.visual_direction,
                    json.dumps(kit.palette),
                    kit.logo_brief,
                    kit.banner_brief,
                    version,
                    kit.source_plan_id,
                    now,
                ),
            )
        return dataclass_replace(kit, version=version)

    def get_approved_brand_kit(
        self, publishing_channel_id: str
    ) -> ChannelBrandKit | None:
        row = self.connection.execute(
            """SELECT * FROM channel_brand_kits
            WHERE publishing_channel_id = ? AND status = 'APPROVED'
            ORDER BY version DESC LIMIT 1""",
            (publishing_channel_id,),
        ).fetchone()
        if row is None:
            return None
        return ChannelBrandKit(
            publishing_channel_id=row["publishing_channel_id"],
            public_name=row["public_name"],
            handle=row["handle"],
            description=row["description"],
            tagline=row["tagline"],
            brand_keywords=tuple(json.loads(row["brand_keywords_json"])),
            visual_direction=row["visual_direction"],
            palette=tuple(json.loads(row["palette_json"])),
            logo_brief=row["logo_brief"],
            banner_brief=row["banner_brief"],
            status=BrandKitStatus(row["status"]),
            version=row["version"],
            source_plan_id=row["source_plan_id"],
        )

    def get_theme_template(self, theme_key: str) -> ThemeTemplate | None:
        row = self.connection.execute(
            "SELECT * FROM theme_catalog WHERE theme_key = ?", (theme_key,)
        ).fetchone()
        return self._theme_template(row) if row else None

    def save_theme_template(self, template: ThemeTemplate) -> None:
        """Persist an intentional edit and retain the prompt version history."""
        now = _now()
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO theme_catalog (
                    theme_key, display_name, description, tags_json,
                    source_strategy_json, production_defaults_json,
                    status, provenance, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(theme_key) DO UPDATE SET
                    display_name=excluded.display_name,
                    description=excluded.description,
                    tags_json=excluded.tags_json,
                    source_strategy_json=excluded.source_strategy_json,
                    production_defaults_json=excluded.production_defaults_json,
                    status=excluded.status,
                    provenance=excluded.provenance,
                    updated_at=excluded.updated_at
                """,
                (
                    template.theme_key,
                    template.display_name,
                    template.description,
                    json.dumps(template.tags, ensure_ascii=False),
                    json.dumps(asdict(template.source_strategy), ensure_ascii=False),
                    json.dumps(template.production_defaults, ensure_ascii=False),
                    template.status,
                    template.provenance,
                    now,
                    now,
                ),
            )
            self.connection.execute(
                "DELETE FROM theme_channel_archetypes WHERE theme_key = ?",
                (template.theme_key,),
            )
            for item in template.channel_archetypes:
                self.connection.execute(
                    """
                    INSERT INTO theme_channel_archetypes (
                        theme_key, slot, name, editorial_prompt, headline_style,
                        tone, visual_direction, palette_json, brand_json,
                        editorial_json, brand_blueprint_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        template.theme_key,
                        item.slot,
                        item.name,
                        item.editorial_prompt,
                        item.headline_style,
                        item.tone,
                        item.visual_direction,
                        json.dumps(
                            [
                                item.palette.primary,
                                item.palette.secondary,
                                item.palette.accent,
                                item.palette.background,
                            ]
                        ),
                        json.dumps(
                            {
                                "public_name": item.brand.public_name,
                                "handle": item.brand.handle,
                                "description": item.brand.description,
                                "tagline": item.brand.tagline,
                                "logo_path": item.brand.logo_path,
                                "banner_path": item.brand.banner_path,
                            },
                            ensure_ascii=False,
                        ),
                        json.dumps(
                            {
                                "editorial_focus": item.editorial_focus,
                                "audience": item.audience,
                                "content_preferences": item.content_preferences,
                                "content_avoid": item.content_avoid,
                            },
                            ensure_ascii=False,
                        ),
                        json.dumps(asdict(item.brand_blueprint), ensure_ascii=False),
                    ),
                )
            self.connection.execute(
                "UPDATE theme_catalog SET rights_json = ?, readiness_json = ? "
                "WHERE theme_key = ?",
                (
                    json.dumps(asdict(template.rights_profile), ensure_ascii=False),
                    json.dumps(asdict(template.readiness)),
                    template.theme_key,
                ),
            )
            active = next((x for x in template.prompt_versions if x.active), None)
            prompt = active.prompt if active else template.base_prompt
            current = self.connection.execute(
                "SELECT version, prompt FROM theme_prompt_versions "
                "WHERE theme_key = ? ORDER BY version DESC LIMIT 1",
                (template.theme_key,),
            ).fetchone()
            if current is None or current["prompt"] != prompt:
                version = 1 if current is None else int(current["version"]) + 1
                self.connection.execute(
                    "UPDATE theme_prompt_versions SET active = 0 WHERE theme_key = ?",
                    (template.theme_key,),
                )
                self.connection.execute(
                    """INSERT INTO theme_prompt_versions
                    (theme_key, version, prompt, provenance, active, created_at)
                    VALUES (?, ?, ?, ?, 1, ?)""",
                    (template.theme_key, version, prompt, template.provenance, now),
                )

    def seed_theme_templates(self, templates: tuple[ThemeTemplate, ...]) -> int:
        """Insert missing templates only, preserving all existing/manual edits."""
        inserted = 0
        for template in templates:
            current = self.get_theme_template(template.theme_key)
            if current is None:
                self.save_theme_template(template)
                inserted += 1
            elif current.provenance == CatalogProvenance.LEGACY:
                # Catalog planning upgrades untouched legacy rows, while keeping
                # the active v1 prompt and its complete history.
                self.save_theme_template(
                    dataclass_replace(
                        template,
                        base_prompt=current.base_prompt,
                        prompt_versions=current.prompt_versions,
                    )
                )
        return inserted

    def _theme_template(self, row: sqlite3.Row) -> ThemeTemplate:
        key = row["theme_key"]
        prompt_rows = self.connection.execute(
            "SELECT * FROM theme_prompt_versions WHERE theme_key = ? ORDER BY version",
            (key,),
        ).fetchall()
        archetype_rows = self.connection.execute(
            "SELECT * FROM theme_channel_archetypes WHERE theme_key = ? ORDER BY slot",
            (key,),
        ).fetchall()
        prompts = tuple(
            PromptVersion(
                version=item["version"],
                prompt=item["prompt"],
                provenance=CatalogProvenance(item["provenance"]),
                active=bool(item["active"]),
                created_at=item["created_at"],
            )
            for item in prompt_rows
        )
        active = next((item.prompt for item in prompts if item.active), "")
        archetypes = []
        for item in archetype_rows:
            colors = json.loads(item["palette_json"])
            brand = json.loads(item["brand_json"])
            editorial = json.loads(item["editorial_json"])
            blueprint = json.loads(item["brand_blueprint_json"])
            archetypes.append(
                ChannelArchetype(
                    slot=item["slot"],
                    name=item["name"],
                    editorial_focus=tuple(editorial["editorial_focus"]),
                    editorial_prompt=item["editorial_prompt"],
                    headline_style=item["headline_style"],
                    tone=item["tone"],
                    visual_direction=item["visual_direction"],
                    audience=editorial["audience"],
                    content_preferences=tuple(editorial["content_preferences"]),
                    content_avoid=tuple(editorial["content_avoid"]),
                    palette=ColorPalette(*colors),
                    brand=BrandProfile(**brand),
                    brand_blueprint=BrandBlueprint(
                        keywords=tuple(blueprint["keywords"]),
                        **{k: v for k, v in blueprint.items() if k != "keywords"},
                    ),
                )
            )
        source = json.loads(row["source_strategy_json"])
        if "source_types" not in source:
            source = {
                "source_types": ("creator_longform",),
                "description": "Legacy creator-channel collection strategy.",
                "requires_creator_authorization": True,
                "preferred_sources": (),
                "avoid_sources": (),
                "notes": "",
            }
        rights = json.loads(row["rights_json"])
        if not rights:
            rights = {
                "authorization_required": True,
                "authorization_status": "UNKNOWN",
                "reuse_risk": "MEDIUM",
                "notes": "Pending review",
            }
        readiness = json.loads(row["readiness_json"])
        if not readiness:
            readiness = {
                key: False
                for key in (
                    "editorial_ready",
                    "channel_profiles_ready",
                    "source_strategy_ready",
                    "rights_strategy_ready",
                    "brand_direction_ready",
                    "production_fit_ready",
                )
            }
        return ThemeTemplate(
            theme_key=key,
            display_name=row["display_name"],
            description=row["description"],
            base_prompt=active,
            tags=tuple(json.loads(row["tags_json"])),
            source_strategy=SourceStrategy(
                source_types=tuple(source["source_types"]),
                preferred_sources=tuple(source["preferred_sources"]),
                avoid_sources=tuple(source["avoid_sources"]),
                description=source["description"],
                requires_creator_authorization=source["requires_creator_authorization"],
                notes=source.get("notes", ""),
            ),
            rights_profile=RightsProfile(
                authorization_required=rights["authorization_required"],
                authorization_status=AuthorizationStatus(
                    rights["authorization_status"]
                ),
                reuse_risk=ReuseRisk(rights["reuse_risk"]),
                notes=rights["notes"],
            ),
            readiness=ThemeReadiness(**readiness),
            production_defaults=json.loads(row["production_defaults_json"]),
            channel_archetypes=tuple(archetypes),
            status=CatalogStatus(row["status"]),
            provenance=CatalogProvenance(row["provenance"]),
            prompt_versions=prompts,
        )

    def _transition_video(
        self, youtube_id: str, status: VideoStatus, *, error: str | None = None
    ) -> None:
        row = self.connection.execute(
            "SELECT status FROM videos WHERE youtube_id = ?", (youtube_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown YouTube ID: {youtube_id}")
        current = VideoStatus(row["status"])
        if status not in VIDEO_TRANSITIONS[current]:
            raise ValueError(f"Invalid video transition: {current} -> {status}")
        now = _now()
        processed_at = now if status == VideoStatus.COMPLETED else None
        self.connection.execute(
            """
            UPDATE videos
            SET status = ?, updated_at = ?, processed_at = COALESCE(?, processed_at),
                error = ?
            WHERE youtube_id = ?
            """,
            (status, now, processed_at, error, youtube_id),
        )

    def _initialize(self) -> None:
        version = self.schema_version
        if version > SCHEMA_VERSION:
            raise RuntimeError(f"Unsupported state schema version: {version}")
        if version == 0:
            with self.connection:
                self.connection.executescript(
                    """
                CREATE TABLE IF NOT EXISTS videos (
                    youtube_id TEXT PRIMARY KEY,
                    source_url TEXT NOT NULL,
                    creator_name TEXT NOT NULL,
                    theme_name TEXT NOT NULL,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    discovered_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    processed_at TEXT,
                    error TEXT,
                    platform TEXT NOT NULL DEFAULT 'youtube',
                    content_source_id INTEGER,
                    duration_seconds INTEGER,
                    published_at TEXT,
                    local_path TEXT,
                    checksum TEXT,
                    validated_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_videos_platform_external
                    ON videos(platform, youtube_id);

                CREATE TABLE IF NOT EXISTS shorts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    youtube_id TEXT NOT NULL,
                    clip_index INTEGER NOT NULL CHECK (clip_index BETWEEN 1 AND 3),
                    account_slot INTEGER CHECK (account_slot BETWEEN 1 AND 3),
                    account_identifier TEXT,
                    publishing_channel_id TEXT,
                    status TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    queue_path TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    posted_at TEXT,
                    error TEXT,
                    title TEXT NOT NULL DEFAULT '',
                    headline TEXT NOT NULL DEFAULT '',
                    generator_score REAL,
                    editorial_score REAL,
                    candidate_id TEXT,
                    duration_seconds REAL,
                    FOREIGN KEY (youtube_id) REFERENCES videos(youtube_id)
                        ON DELETE CASCADE,
                    UNIQUE (youtube_id, clip_index),
                    UNIQUE (youtube_id, account_slot)
                );

                CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status);
                CREATE INDEX IF NOT EXISTS idx_shorts_status ON shorts(status);

                CREATE TABLE IF NOT EXISTS transcripts (
                    youtube_id TEXT PRIMARY KEY,
                    file_path TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    language TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (youtube_id) REFERENCES videos(youtube_id)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS analyses (
                    youtube_id TEXT PRIMARY KEY, file_path TEXT NOT NULL,
                    provider TEXT NOT NULL, model TEXT NOT NULL,
                    prompt_hash TEXT NOT NULL, created_at TEXT NOT NULL,
                    FOREIGN KEY (youtube_id) REFERENCES videos(youtube_id)
                        ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS ready_artifacts (
                    youtube_id TEXT PRIMARY KEY,
                    file_path TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    manifest_path TEXT NOT NULL,
                    subtitle_path TEXT NOT NULL,
                    caption_fingerprint TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    validated_at TEXT NOT NULL,
                    FOREIGN KEY (youtube_id) REFERENCES videos(youtube_id)
                        ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS metadata_settings (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    default_review_policy TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS metadata_channel_settings (
                    publishing_channel_id TEXT PRIMARY KEY,
                    review_policy TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS metadata_drafts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    youtube_id TEXT NOT NULL, ready_checksum TEXT NOT NULL,
                    publishing_channel_id TEXT NOT NULL, brand_plan_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
                    review_policy TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '', tags_json TEXT NOT NULL,
                    hashtags_json TEXT NOT NULL, category_id TEXT NOT NULL,
                    category_name TEXT NOT NULL, default_language TEXT NOT NULL,
                    privacy_status TEXT NOT NULL, made_for_kids INTEGER NOT NULL,
                    contains_synthetic_media INTEGER NOT NULL,
                    has_paid_product_placement INTEGER NOT NULL,
                    license TEXT NOT NULL, embeddable INTEGER NOT NULL,
                    public_stats_viewable INTEGER NOT NULL, provider TEXT NOT NULL,
                    model TEXT NOT NULL, prompt_hash TEXT NOT NULL,
                    generation_version TEXT NOT NULL,
                    title_candidates_json TEXT NOT NULL,
                    selection_reason TEXT NOT NULL DEFAULT '', error TEXT,
                    publication_id INTEGER, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, approved_at TEXT,
                    FOREIGN KEY(youtube_id) REFERENCES videos(youtube_id),
                    FOREIGN KEY(publication_id) REFERENCES publications(id)
                );
                CREATE TABLE IF NOT EXISTS ai_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, youtube_id TEXT NOT NULL,
                    provider TEXT NOT NULL, model TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
                    thinking_tokens INTEGER NOT NULL, total_tokens INTEGER NOT NULL,
                    estimated_cost_usd REAL NOT NULL, created_at TEXT NOT NULL,
                    FOREIGN KEY (youtube_id) REFERENCES videos(youtube_id)
                        ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_ai_usage_created ON ai_usage(created_at);
                CREATE TABLE IF NOT EXISTS channel_bindings (
                    publishing_channel_id TEXT PRIMARY KEY,
                    google_identity_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    credential_ref TEXT NOT NULL,
                    youtube_channel_id TEXT NOT NULL,
                    youtube_channel_title TEXT NOT NULL,
                    connected_at TEXT NOT NULL,
                    last_verified_at TEXT NOT NULL,
                    status TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_youtube_channel_binding
                    ON channel_bindings(youtube_channel_id);
                CREATE TABLE IF NOT EXISTS publications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    brand_plan_id TEXT NOT NULL,
                    publishing_channel_id TEXT NOT NULL,
                    google_identity_id TEXT NOT NULL,
                    youtube_channel_id TEXT NOT NULL,
                    source_short_id INTEGER,
                    video_path TEXT NOT NULL,
                    video_checksum TEXT NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    tags_json TEXT NOT NULL,
                    privacy_status TEXT NOT NULL,
                    made_for_kids INTEGER,
                    contains_synthetic_media INTEGER,
                    has_paid_product_placement INTEGER,
                    category_id TEXT,
                    category_name TEXT,
                    default_language TEXT,
                    license TEXT,
                    embeddable INTEGER,
                    public_stats_viewable INTEGER,
                    publish_at TEXT,
                    recording_date TEXT,
                    status TEXT NOT NULL,
                    remote_video_id TEXT UNIQUE,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    approved_at TEXT,
                    published_at TEXT,
                    last_error TEXT,
                    error_category TEXT,
                    FOREIGN KEY (brand_plan_id)
                        REFERENCES planned_channel_brands(brand_plan_id),
                    FOREIGN KEY (source_short_id) REFERENCES shorts(id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_publication_video_destination
                    ON publications(publishing_channel_id, video_checksum);
                CREATE INDEX IF NOT EXISTS idx_publications_status
                    ON publications(status);
                CREATE TABLE IF NOT EXISTS publication_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    publication_id INTEGER NOT NULL,
                    attempt_number INTEGER NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    remote_video_id TEXT,
                    error_category TEXT,
                    error_message TEXT,
                    FOREIGN KEY (publication_id) REFERENCES publications(id)
                        ON DELETE CASCADE,
                    UNIQUE (publication_id, attempt_number)
                );
                CREATE TABLE IF NOT EXISTS theme_catalog (
                    theme_key TEXT PRIMARY KEY, display_name TEXT NOT NULL,
                    description TEXT NOT NULL, tags_json TEXT NOT NULL,
                    source_strategy_json TEXT NOT NULL,
                    production_defaults_json TEXT NOT NULL,
                    status TEXT NOT NULL, provenance TEXT NOT NULL,
                    rights_json TEXT NOT NULL DEFAULT '{}',
                    readiness_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS theme_prompt_versions (
                    theme_key TEXT NOT NULL, version INTEGER NOT NULL,
                    prompt TEXT NOT NULL, provenance TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
                    PRIMARY KEY (theme_key, version),
                    FOREIGN KEY (theme_key) REFERENCES theme_catalog(theme_key)
                        ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS theme_channel_archetypes (
                    theme_key TEXT NOT NULL, slot INTEGER NOT NULL,
                    name TEXT NOT NULL, editorial_prompt TEXT NOT NULL,
                    headline_style TEXT NOT NULL, tone TEXT NOT NULL,
                    visual_direction TEXT NOT NULL, palette_json TEXT NOT NULL,
                    brand_json TEXT NOT NULL,
                    editorial_json TEXT NOT NULL,
                    brand_blueprint_json TEXT NOT NULL,
                    PRIMARY KEY (theme_key, slot),
                    FOREIGN KEY (theme_key) REFERENCES theme_catalog(theme_key)
                        ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS brand_generations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT NOT NULL,
                    model TEXT NOT NULL, operation TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
                    thinking_tokens INTEGER NOT NULL, total_tokens INTEGER NOT NULL,
                    duration_seconds REAL NOT NULL, theoretical_cost_usd REAL NOT NULL,
                    status TEXT NOT NULL, error TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS brand_candidates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    generation_id INTEGER NOT NULL,
                    publishing_channel_id TEXT NOT NULL, public_name TEXT NOT NULL,
                    handle_candidates_json TEXT NOT NULL,
                    handle_availability TEXT NOT NULL, description TEXT NOT NULL,
                    tagline TEXT NOT NULL, brand_keywords_json TEXT NOT NULL,
                    logo_brief TEXT NOT NULL, banner_brief TEXT NOT NULL,
                    reason TEXT NOT NULL, created_at TEXT NOT NULL,
                    FOREIGN KEY (generation_id) REFERENCES brand_generations(id)
                        ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS channel_brand_kits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    publishing_channel_id TEXT NOT NULL, public_name TEXT NOT NULL,
                    handle TEXT NOT NULL, description TEXT NOT NULL,
                    tagline TEXT NOT NULL, brand_keywords_json TEXT NOT NULL,
                    visual_direction TEXT NOT NULL, palette_json TEXT NOT NULL,
                    logo_brief TEXT NOT NULL, banner_brief TEXT NOT NULL,
                    status TEXT NOT NULL, version INTEGER NOT NULL,
                    source_plan_id TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE (publishing_channel_id, version)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_one_approved_brand
                    ON channel_brand_kits(publishing_channel_id)
                    WHERE status = 'APPROVED';
                CREATE TABLE IF NOT EXISTS catalog_brand_generations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    theme_key TEXT NOT NULL UNIQUE,
                    provider TEXT NOT NULL, model TEXT NOT NULL, status TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
                    thinking_tokens INTEGER NOT NULL, total_tokens INTEGER NOT NULL,
                    duration_seconds REAL NOT NULL, theoretical_cost_usd REAL NOT NULL,
                    rerank_input_tokens INTEGER NOT NULL DEFAULT 0,
                    rerank_output_tokens INTEGER NOT NULL DEFAULT 0,
                    rerank_thinking_tokens INTEGER NOT NULL DEFAULT 0,
                    rerank_total_tokens INTEGER NOT NULL DEFAULT 0,
                    rerank_duration_seconds REAL NOT NULL DEFAULT 0,
                    rerank_theoretical_cost_usd REAL NOT NULL DEFAULT 0,
                    error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS catalog_brand_candidates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    generation_id INTEGER NOT NULL,
                    target_id TEXT NOT NULL, public_name TEXT NOT NULL,
                    handles_json TEXT NOT NULL, description TEXT NOT NULL,
                    tagline TEXT NOT NULL, keywords_json TEXT NOT NULL,
                    logo_brief TEXT NOT NULL, banner_brief TEXT NOT NULL,
                    reason TEXT NOT NULL, created_at TEXT NOT NULL,
                    FOREIGN KEY (generation_id) REFERENCES catalog_brand_generations(id)
                        ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS planned_channel_brands (
                    brand_plan_id TEXT PRIMARY KEY, theme_key TEXT NOT NULL,
                    slot INTEGER NOT NULL, public_name TEXT NOT NULL,
                    preferred_handle TEXT NOT NULL, backup_handles_json TEXT NOT NULL,
                    handle_availability TEXT NOT NULL, description TEXT NOT NULL,
                    tagline TEXT NOT NULL, brand_keywords_json TEXT NOT NULL,
                    visual_direction TEXT NOT NULL, palette_json TEXT NOT NULL,
                    logo_brief TEXT NOT NULL, banner_brief TEXT NOT NULL,
                    status TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
                    version INTEGER NOT NULL, source_generation_id INTEGER,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(theme_key, slot)
                );
                CREATE TABLE IF NOT EXISTS planned_identity_assignments (
                    brand_plan_id TEXT PRIMARY KEY, planned_identity_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (brand_plan_id)
                        REFERENCES planned_channel_brands(brand_plan_id)
                        ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS planned_channel_brand_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    brand_plan_id TEXT NOT NULL, version INTEGER NOT NULL,
                    snapshot_json TEXT NOT NULL, provenance TEXT NOT NULL,
                    review_note TEXT NOT NULL, archived_at TEXT NOT NULL,
                    UNIQUE(brand_plan_id, version),
                    FOREIGN KEY (brand_plan_id)
                        REFERENCES planned_channel_brands(brand_plan_id)
                        ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS brand_asset_metadata (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, brand_plan_id TEXT NOT NULL,
                    asset_type TEXT NOT NULL, file_path TEXT NOT NULL,
                    provider TEXT NOT NULL, model TEXT NOT NULL, prompt TEXT NOT NULL,
                    width INTEGER NOT NULL, height INTEGER NOT NULL,
                    checksum TEXT NOT NULL, created_at TEXT NOT NULL,
                    status TEXT NOT NULL, version INTEGER NOT NULL,
                    file_size INTEGER NOT NULL, preview_path TEXT,
                    delivery_path TEXT, resolved_visual_direction_json TEXT,
                    source_package TEXT, source_file TEXT,
                    source_preserved_path TEXT, source_checksum TEXT,
                    source_width INTEGER, source_height INTEGER,
                    canonical_transform TEXT,
                    UNIQUE(brand_plan_id, asset_type, version),
                    FOREIGN KEY (brand_plan_id)
                        REFERENCES planned_channel_brands(brand_plan_id)
                        ON DELETE CASCADE
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_one_approved_brand_asset
                    ON brand_asset_metadata(brand_plan_id, asset_type)
                    WHERE status = 'APPROVED';
                CREATE TABLE IF NOT EXISTS theme_market_profiles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    theme_key TEXT NOT NULL, platform TEXT NOT NULL,
                    region_code TEXT NOT NULL, language_code TEXT NOT NULL,
                    youtube_category_id TEXT NOT NULL,
                    youtube_category_name TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    is_default_for_theme INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    FOREIGN KEY (theme_key) REFERENCES theme_catalog(theme_key)
                        ON DELETE CASCADE,
                    UNIQUE(theme_key, platform, region_code, language_code)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_default_theme_market_profile
                    ON theme_market_profiles(theme_key, platform)
                    WHERE is_default_for_theme = 1;
                CREATE TABLE IF NOT EXISTS content_sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform TEXT NOT NULL, external_channel_id TEXT NOT NULL,
                    display_name TEXT NOT NULL, handle TEXT, channel_url TEXT NOT NULL,
                    region_code TEXT NOT NULL, language_code TEXT NOT NULL,
                    source_kind TEXT NOT NULL, validation_status TEXT NOT NULL,
                    last_validated_at TEXT, latest_upload_at TEXT,
                    sampled_video_count INTEGER NOT NULL DEFAULT 0,
                    sampled_longform_count INTEGER NOT NULL DEFAULT 0,
                    sampled_target_duration_count INTEGER NOT NULL DEFAULT 0,
                    observed_duration_min_seconds INTEGER,
                    observed_duration_max_seconds INTEGER,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    rights_status TEXT NOT NULL, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(platform, external_channel_id)
                );
                CREATE TABLE IF NOT EXISTS theme_content_sources (
                    profile_id INTEGER NOT NULL, source_id INTEGER NOT NULL,
                    priority INTEGER NOT NULL CHECK(priority BETWEEN 1 AND 5),
                    enabled INTEGER NOT NULL DEFAULT 1,
                    target_duration_min_seconds INTEGER NOT NULL DEFAULT 900,
                    target_duration_max_seconds INTEGER NOT NULL DEFAULT 2400,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(profile_id, source_id),
                    UNIQUE(profile_id, priority),
                    FOREIGN KEY(profile_id) REFERENCES theme_market_profiles(id)
                        ON DELETE CASCADE,
                    FOREIGN KEY(source_id) REFERENCES content_sources(id)
                        ON DELETE CASCADE,
                    CHECK(target_duration_min_seconds < target_duration_max_seconds)
                );
                CREATE TABLE IF NOT EXISTS theme_posting_profiles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    theme_market_profile_id INTEGER NOT NULL UNIQUE,
                    timezone TEXT NOT NULL, timing_policy TEXT NOT NULL,
                    strategy TEXT NOT NULL, confidence TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    source_note TEXT NOT NULL DEFAULT '', last_reviewed_at TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    FOREIGN KEY(theme_market_profile_id)
                        REFERENCES theme_market_profiles(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS posting_windows (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    posting_profile_id INTEGER NOT NULL,
                    priority INTEGER NOT NULL CHECK(priority > 0),
                    start_local_time TEXT NOT NULL, end_local_time TEXT NOT NULL,
                    days_of_week_json TEXT NOT NULL,
                    experimental INTEGER NOT NULL DEFAULT 0,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(posting_profile_id, priority),
                    FOREIGN KEY(posting_profile_id)
                        REFERENCES theme_posting_profiles(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS channel_posting_overrides (
                    publishing_channel_id TEXT PRIMARY KEY,
                    posting_profile_id INTEGER NOT NULL,
                    timezone TEXT, timing_policy TEXT, strategy TEXT, confidence TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    source_note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    FOREIGN KEY(posting_profile_id)
                        REFERENCES theme_posting_profiles(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS scheduler_settings (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    global_paused INTEGER NOT NULL DEFAULT 0,
                    poll_interval_seconds INTEGER NOT NULL DEFAULT 45,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    missed_threshold_hours INTEGER NOT NULL DEFAULT 6,
                    max_publications_per_channel_per_day INTEGER NOT NULL DEFAULT 1,
                    min_spacing_minutes INTEGER NOT NULL DEFAULT 30,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS scheduler_channel_settings (
                    publishing_channel_id TEXT PRIMARY KEY,
                    paused INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS publication_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    publishing_channel_id TEXT NOT NULL,
                    publication_id INTEGER UNIQUE,
                    media_path TEXT NOT NULL, requested_publish_at TEXT,
                    resolved_publish_at TEXT, timezone TEXT NOT NULL,
                    priority INTEGER NOT NULL, urgency TEXT NOT NULL,
                    use_experimental_window INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    upload_started_at TEXT, last_error TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    started_at TEXT, finished_at TEXT,
                    FOREIGN KEY(publication_id) REFERENCES publications(id)
                );
                CREATE INDEX IF NOT EXISTS idx_publication_jobs_due
                    ON publication_jobs(state, resolved_publish_at, priority);
                CREATE TABLE IF NOT EXISTS scheduler_job_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER NOT NULL,
                    event_type TEXT NOT NULL, from_state TEXT, to_state TEXT,
                    details TEXT NOT NULL, created_at TEXT NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES publication_jobs(id)
                        ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS orchestrator_settings (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    min_ready_videos INTEGER NOT NULL DEFAULT 2,
                    target_ready_videos INTEGER NOT NULL DEFAULT 3,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS orchestrator_channel_settings (
                    publishing_channel_id TEXT PRIMARY KEY,
                    min_ready_videos INTEGER NOT NULL,
                    target_ready_videos INTEGER NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS orchestrator_artifact_assignments (
                    youtube_id TEXT PRIMARY KEY, publishing_channel_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(youtube_id) REFERENCES videos(youtube_id)
                        ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_orchestrator_assignments_channel
                    ON orchestrator_artifact_assignments(publishing_channel_id);
                CREATE TABLE IF NOT EXISTS orchestrator_leases (
                    publishing_channel_id TEXT PRIMARY KEY, owner TEXT NOT NULL,
                    acquired_at TEXT NOT NULL, expires_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS orchestrator_cycles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL,
                    finished_at TEXT, max_productions INTEGER NOT NULL,
                    result TEXT NOT NULL, error TEXT
                );
                CREATE TABLE IF NOT EXISTS orchestrator_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id INTEGER NOT NULL,
                    publishing_channel_id TEXT NOT NULL,
                    inventory_before INTEGER NOT NULL, minimum INTEGER NOT NULL,
                    target INTEGER NOT NULL, planned_count INTEGER NOT NULL,
                    result TEXT NOT NULL, error TEXT, started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    FOREIGN KEY(cycle_id) REFERENCES orchestrator_cycles(id)
                        ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS youtube_source_failures (
                    profile_id INTEGER NOT NULL, youtube_id TEXT NOT NULL,
                    category TEXT NOT NULL, attempt_count INTEGER NOT NULL,
                    retry_after TEXT, detail TEXT NOT NULL,
                    first_failed_at TEXT NOT NULL, last_failed_at TEXT NOT NULL,
                    PRIMARY KEY(profile_id, youtube_id),
                    FOREIGN KEY(profile_id) REFERENCES theme_market_profiles(id)
                        ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_youtube_source_retry
                    ON youtube_source_failures(profile_id, retry_after);
                CREATE TABLE IF NOT EXISTS youtube_source_metadata (
                    youtube_id TEXT PRIMARY KEY, creator TEXT NOT NULL,
                    url TEXT NOT NULL, title TEXT NOT NULL, upload_date TEXT,
                    duration_seconds INTEGER, hydrated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS youtube_channel_provisioning (
                    brand_plan_id TEXT PRIMARY KEY,
                    google_identity_id TEXT NOT NULL,
                    expected_name TEXT NOT NULL,
                    expected_handle TEXT NOT NULL,
                    actual_handle TEXT,
                    youtube_channel_id TEXT,
                    status TEXT NOT NULL,
                    logo_path TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(brand_plan_id)
                        REFERENCES planned_channel_brands(brand_plan_id)
                        ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_youtube_provisioning_identity
                    ON youtube_channel_provisioning(google_identity_id, status);
                CREATE TABLE IF NOT EXISTS youtube_web_sessions (
                    google_identity_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    last_checked_at TEXT,
                    last_error TEXT
                );
                """
                )
                self.connection.execute(
                    """INSERT OR IGNORE INTO scheduler_settings
                    (singleton, updated_at) VALUES (1, ?)""",
                    (_now(),),
                )
                self.connection.execute(
                    """INSERT OR IGNORE INTO metadata_settings
                    (singleton, default_review_policy, updated_at)
                    VALUES (1, ?, ?)""",
                    (ReviewPolicy.MANUAL_REVIEW, _now()),
                )
                self.connection.execute(
                    """INSERT OR IGNORE INTO orchestrator_settings
                    (singleton, min_ready_videos, target_ready_videos, updated_at)
                    VALUES (1, 2, 3, ?)""",
                    (_now(),),
                )
                self.connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            return
        if version == 1:
            self._migrate_v1_to_v2()
            version = 2
        if version == 2:
            self._migrate_v2_to_v3()
            version = 3
        if version == 3:
            self._migrate_v3_to_v4()
            version = 4
        if version == 4:
            self._migrate_v4_to_v5()
            version = 5
        if version == 5:
            self._migrate_v5_to_v6()
            version = 6
        if version == 6:
            self._migrate_v6_to_v7()
            version = 7
        if version == 7:
            self._migrate_v7_to_v8()
            version = 8
        if version == 8:
            self._migrate_v8_to_v9()
            version = 9
        if version == 9:
            self._migrate_v9_to_v10()
            version = 10
        if version == 10:
            self._migrate_v10_to_v11()
            version = 11
        if version == 11:
            self._migrate_v11_to_v12()
            version = 12
        if version == 12:
            self._migrate_v12_to_v13()
            version = 13
        if version == 13:
            self._migrate_v13_to_v14()
            version = 14
        if version == 14:
            self._migrate_v14_to_v15()
            version = 15
        if version == 15:
            self._migrate_v15_to_v16()
            version = 16
        if version == 16:
            self._migrate_v16_to_v17()
            version = 17
        if version == 17:
            self._migrate_v17_to_v18()
            version = 18
        if version == 18:
            self._migrate_v18_to_v19()
            version = 19
        if version == 19:
            self._migrate_v19_to_v20()
            version = 20
        if version == 20:
            self._migrate_v20_to_v21()
            version = 21
        if version == 21:
            self._migrate_v21_to_v22()
            version = 22
        if version == 22:
            self._migrate_v22_to_v23()
            version = 23
        if version == 23:
            self._migrate_v23_to_v24()
            version = 24
        if version == 24:
            self._migrate_v24_to_v25()
            version = 25
        if version == 25:
            self._migrate_v25_to_v26()
            version = 26
        if version == 26:
            self._migrate_v26_to_v27()
            version = 27
        if version == 27:
            self._migrate_v27_to_v28()
            version = 28
        if version == 28:
            self._migrate_v28_to_v29()
            version = 29
        if version == 29:
            self._migrate_v29_to_v30()
            version = 30

    def _migrate_v1_to_v2(self) -> None:
        """Separate rendered masters from queue assignment while preserving rows."""
        with self.connection:
            self.connection.executescript(
                """
                ALTER TABLE shorts RENAME TO shorts_v1;
                CREATE TABLE shorts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    youtube_id TEXT NOT NULL,
                    clip_index INTEGER NOT NULL CHECK (clip_index BETWEEN 1 AND 3),
                    account_slot INTEGER CHECK (account_slot BETWEEN 1 AND 3),
                    account_identifier TEXT,
                    status TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    queue_path TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    posted_at TEXT,
                    error TEXT,
                    FOREIGN KEY (youtube_id) REFERENCES videos(youtube_id)
                        ON DELETE CASCADE,
                    UNIQUE (youtube_id, clip_index),
                    UNIQUE (youtube_id, account_slot)
                );
                INSERT INTO shorts (
                    id, youtube_id, clip_index, account_slot, account_identifier,
                    status, file_path, created_at, updated_at, posted_at, error
                ) SELECT id, youtube_id, clip_index, account_slot, account_identifier,
                    status, file_path, created_at, updated_at, posted_at, error
                  FROM shorts_v1;
                DROP TABLE shorts_v1;
                CREATE INDEX idx_shorts_status ON shorts(status);
                PRAGMA user_version = 2;
                """
            )

    def _migrate_v2_to_v3(self) -> None:
        with self.connection:
            self.connection.executescript(
                """
                CREATE TABLE transcripts (
                    youtube_id TEXT PRIMARY KEY,
                    file_path TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    language TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (youtube_id) REFERENCES videos(youtube_id)
                        ON DELETE CASCADE
                );
                PRAGMA user_version = 3;
                """
            )

    def _migrate_v3_to_v4(self) -> None:
        with self.connection:
            self.connection.executescript(
                """
                CREATE TABLE analyses (
                    youtube_id TEXT PRIMARY KEY, file_path TEXT NOT NULL,
                    provider TEXT NOT NULL, model TEXT NOT NULL,
                    prompt_hash TEXT NOT NULL, created_at TEXT NOT NULL,
                    FOREIGN KEY (youtube_id) REFERENCES videos(youtube_id)
                        ON DELETE CASCADE
                );
                CREATE TABLE ai_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, youtube_id TEXT NOT NULL,
                    provider TEXT NOT NULL, model TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
                    thinking_tokens INTEGER NOT NULL, total_tokens INTEGER NOT NULL,
                    estimated_cost_usd REAL NOT NULL, created_at TEXT NOT NULL,
                    FOREIGN KEY (youtube_id) REFERENCES videos(youtube_id)
                        ON DELETE CASCADE
                );
                CREATE INDEX idx_ai_usage_created ON ai_usage(created_at);
                PRAGMA user_version = 4;
                """
            )

    def _migrate_v4_to_v5(self) -> None:
        with self.connection:
            self.connection.executescript(
                """
                ALTER TABLE ai_usage ADD COLUMN operation TEXT NOT NULL
                    DEFAULT 'generation';
                PRAGMA user_version = 5;
                """
            )

    def _migrate_v5_to_v6(self) -> None:
        with self.connection:
            self.connection.executescript(
                """
                ALTER TABLE shorts ADD COLUMN title TEXT NOT NULL DEFAULT '';
                ALTER TABLE shorts ADD COLUMN headline TEXT NOT NULL DEFAULT '';
                ALTER TABLE shorts ADD COLUMN generator_score REAL;
                ALTER TABLE shorts ADD COLUMN editorial_score REAL;
                ALTER TABLE shorts ADD COLUMN candidate_id TEXT;
                ALTER TABLE shorts ADD COLUMN duration_seconds REAL;
                PRAGMA user_version = 6;
                """
            )

    def _migrate_v6_to_v7(self) -> None:
        with self.connection:
            self.connection.executescript(
                """
                ALTER TABLE shorts ADD COLUMN publishing_channel_id TEXT;
                UPDATE shorts
                SET publishing_channel_id = account_identifier
                WHERE publishing_channel_id IS NULL;
                PRAGMA user_version = 7;
                """
            )

    def _migrate_v7_to_v8(self) -> None:
        with self.connection:
            self.connection.executescript(
                """
                CREATE TABLE channel_bindings (
                    publishing_channel_id TEXT PRIMARY KEY,
                    google_identity_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    credential_ref TEXT NOT NULL,
                    youtube_channel_id TEXT NOT NULL,
                    youtube_channel_title TEXT NOT NULL,
                    connected_at TEXT NOT NULL,
                    last_verified_at TEXT NOT NULL,
                    status TEXT NOT NULL
                );
                PRAGMA user_version = 8;
                """
            )

    def _migrate_v8_to_v9(self) -> None:
        with self.connection:
            self.connection.executescript(
                """
                CREATE TABLE theme_catalog (
                    theme_key TEXT PRIMARY KEY, display_name TEXT NOT NULL,
                    description TEXT NOT NULL, tags_json TEXT NOT NULL,
                    source_strategy_json TEXT NOT NULL,
                    production_defaults_json TEXT NOT NULL,
                    status TEXT NOT NULL, provenance TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE theme_prompt_versions (
                    theme_key TEXT NOT NULL, version INTEGER NOT NULL,
                    prompt TEXT NOT NULL, provenance TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
                    PRIMARY KEY (theme_key, version),
                    FOREIGN KEY (theme_key) REFERENCES theme_catalog(theme_key)
                        ON DELETE CASCADE
                );
                CREATE TABLE theme_channel_archetypes (
                    theme_key TEXT NOT NULL, slot INTEGER NOT NULL,
                    name TEXT NOT NULL, editorial_prompt TEXT NOT NULL,
                    headline_style TEXT NOT NULL, tone TEXT NOT NULL,
                    visual_direction TEXT NOT NULL, palette_json TEXT NOT NULL,
                    brand_json TEXT NOT NULL, PRIMARY KEY (theme_key, slot),
                    FOREIGN KEY (theme_key) REFERENCES theme_catalog(theme_key)
                        ON DELETE CASCADE
                );
                PRAGMA user_version = 9;
                """
            )

    def _migrate_v9_to_v10(self) -> None:
        with self.connection:
            self.connection.executescript(
                """
                ALTER TABLE theme_catalog ADD COLUMN rights_json TEXT NOT NULL
                    DEFAULT '{}';
                ALTER TABLE theme_catalog ADD COLUMN readiness_json TEXT NOT NULL
                    DEFAULT '{}';
                ALTER TABLE theme_channel_archetypes
                    ADD COLUMN editorial_json TEXT NOT NULL DEFAULT
                    '{"editorial_focus":[],"audience":"","content_preferences":[],"content_avoid":[]}';
                ALTER TABLE theme_channel_archetypes
                    ADD COLUMN brand_blueprint_json TEXT NOT NULL DEFAULT
                    '{"keywords":[],"name_direction":"","handle_direction":"","description_direction":"","tagline_direction":"","logo_brief":"","banner_brief":""}';
                PRAGMA user_version = 10;
                """
            )

    def _migrate_v10_to_v11(self) -> None:
        with self.connection:
            self.connection.executescript(
                """
                CREATE TABLE brand_generations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT NOT NULL,
                    model TEXT NOT NULL, operation TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
                    thinking_tokens INTEGER NOT NULL, total_tokens INTEGER NOT NULL,
                    duration_seconds REAL NOT NULL, theoretical_cost_usd REAL NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE brand_candidates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    generation_id INTEGER NOT NULL,
                    publishing_channel_id TEXT NOT NULL, public_name TEXT NOT NULL,
                    handle_candidates_json TEXT NOT NULL,
                    handle_availability TEXT NOT NULL, description TEXT NOT NULL,
                    tagline TEXT NOT NULL, brand_keywords_json TEXT NOT NULL,
                    logo_brief TEXT NOT NULL, banner_brief TEXT NOT NULL,
                    reason TEXT NOT NULL, created_at TEXT NOT NULL,
                    FOREIGN KEY (generation_id) REFERENCES brand_generations(id)
                        ON DELETE CASCADE
                );
                CREATE TABLE channel_brand_kits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    publishing_channel_id TEXT NOT NULL, public_name TEXT NOT NULL,
                    handle TEXT NOT NULL, description TEXT NOT NULL,
                    tagline TEXT NOT NULL, brand_keywords_json TEXT NOT NULL,
                    visual_direction TEXT NOT NULL, palette_json TEXT NOT NULL,
                    logo_brief TEXT NOT NULL, banner_brief TEXT NOT NULL,
                    status TEXT NOT NULL, version INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (publishing_channel_id, version)
                );
                CREATE UNIQUE INDEX idx_one_approved_brand
                    ON channel_brand_kits(publishing_channel_id)
                    WHERE status = 'APPROVED';
                PRAGMA user_version = 11;
                """
            )

    def _migrate_v11_to_v12(self) -> None:
        with self.connection:
            self.connection.executescript(
                """
                ALTER TABLE brand_generations
                    ADD COLUMN status TEXT NOT NULL DEFAULT 'SUCCEEDED';
                ALTER TABLE brand_generations ADD COLUMN error TEXT;
                PRAGMA user_version = 12;
                """
            )

    def _migrate_v12_to_v13(self) -> None:
        with self.connection:
            self.connection.executescript(
                """
                ALTER TABLE channel_brand_kits ADD COLUMN source_plan_id TEXT;
                CREATE TABLE catalog_brand_generations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    theme_key TEXT NOT NULL UNIQUE,
                    provider TEXT NOT NULL, model TEXT NOT NULL, status TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
                    thinking_tokens INTEGER NOT NULL, total_tokens INTEGER NOT NULL,
                    duration_seconds REAL NOT NULL, theoretical_cost_usd REAL NOT NULL,
                    rerank_input_tokens INTEGER NOT NULL DEFAULT 0,
                    rerank_output_tokens INTEGER NOT NULL DEFAULT 0,
                    rerank_thinking_tokens INTEGER NOT NULL DEFAULT 0,
                    rerank_total_tokens INTEGER NOT NULL DEFAULT 0,
                    rerank_duration_seconds REAL NOT NULL DEFAULT 0,
                    rerank_theoretical_cost_usd REAL NOT NULL DEFAULT 0,
                    error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE catalog_brand_candidates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    generation_id INTEGER NOT NULL,
                    target_id TEXT NOT NULL, public_name TEXT NOT NULL,
                    handles_json TEXT NOT NULL, description TEXT NOT NULL,
                    tagline TEXT NOT NULL, keywords_json TEXT NOT NULL,
                    logo_brief TEXT NOT NULL, banner_brief TEXT NOT NULL,
                    reason TEXT NOT NULL, created_at TEXT NOT NULL,
                    FOREIGN KEY (generation_id) REFERENCES catalog_brand_generations(id)
                        ON DELETE CASCADE
                );
                CREATE TABLE planned_channel_brands (
                    brand_plan_id TEXT PRIMARY KEY, theme_key TEXT NOT NULL,
                    slot INTEGER NOT NULL, public_name TEXT NOT NULL,
                    preferred_handle TEXT NOT NULL, backup_handles_json TEXT NOT NULL,
                    handle_availability TEXT NOT NULL, description TEXT NOT NULL,
                    tagline TEXT NOT NULL, brand_keywords_json TEXT NOT NULL,
                    visual_direction TEXT NOT NULL, palette_json TEXT NOT NULL,
                    logo_brief TEXT NOT NULL, banner_brief TEXT NOT NULL,
                    status TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
                    version INTEGER NOT NULL, source_generation_id INTEGER,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(theme_key, slot)
                );
                CREATE TABLE planned_identity_assignments (
                    brand_plan_id TEXT PRIMARY KEY, planned_identity_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (brand_plan_id)
                        REFERENCES planned_channel_brands(brand_plan_id)
                        ON DELETE CASCADE
                );
                CREATE TABLE brand_asset_metadata (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, brand_plan_id TEXT NOT NULL,
                    asset_type TEXT NOT NULL, file_path TEXT NOT NULL,
                    provider TEXT NOT NULL, model TEXT NOT NULL, prompt TEXT NOT NULL,
                    width INTEGER NOT NULL, height INTEGER NOT NULL,
                    checksum TEXT NOT NULL, created_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    FOREIGN KEY (brand_plan_id)
                        REFERENCES planned_channel_brands(brand_plan_id)
                        ON DELETE CASCADE
                );
                PRAGMA user_version = 13;
                """
            )

    def _migrate_v13_to_v14(self) -> None:
        with self.connection:
            self.connection.executescript(
                """
                CREATE TABLE planned_channel_brand_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    brand_plan_id TEXT NOT NULL, version INTEGER NOT NULL,
                    snapshot_json TEXT NOT NULL, provenance TEXT NOT NULL,
                    review_note TEXT NOT NULL, archived_at TEXT NOT NULL,
                    UNIQUE(brand_plan_id, version),
                    FOREIGN KEY (brand_plan_id)
                        REFERENCES planned_channel_brands(brand_plan_id)
                        ON DELETE CASCADE
                );
                PRAGMA user_version = 14;
                """
            )

    def _migrate_v14_to_v15(self) -> None:
        with self.connection:
            self.connection.executescript(
                """
                ALTER TABLE brand_asset_metadata
                    ADD COLUMN version INTEGER NOT NULL DEFAULT 1;
                ALTER TABLE brand_asset_metadata
                    ADD COLUMN file_size INTEGER NOT NULL DEFAULT 0;
                ALTER TABLE brand_asset_metadata ADD COLUMN preview_path TEXT;
                ALTER TABLE brand_asset_metadata ADD COLUMN delivery_path TEXT;
                CREATE UNIQUE INDEX idx_brand_asset_version
                    ON brand_asset_metadata(brand_plan_id, asset_type, version);
                CREATE UNIQUE INDEX idx_one_approved_brand_asset
                    ON brand_asset_metadata(brand_plan_id, asset_type)
                    WHERE status = 'APPROVED';
                PRAGMA user_version = 15;
                """
            )

    def _migrate_v15_to_v16(self) -> None:
        with self.connection:
            self.connection.executescript(
                """
                ALTER TABLE brand_asset_metadata
                    ADD COLUMN resolved_visual_direction_json TEXT;
                PRAGMA user_version = 16;
                """
            )

    def _migrate_v16_to_v17(self) -> None:
        with self.connection:
            self.connection.executescript(
                """
                ALTER TABLE brand_asset_metadata ADD COLUMN source_package TEXT;
                ALTER TABLE brand_asset_metadata ADD COLUMN source_file TEXT;
                ALTER TABLE brand_asset_metadata
                    ADD COLUMN source_preserved_path TEXT;
                ALTER TABLE brand_asset_metadata ADD COLUMN source_checksum TEXT;
                ALTER TABLE brand_asset_metadata ADD COLUMN source_width INTEGER;
                ALTER TABLE brand_asset_metadata ADD COLUMN source_height INTEGER;
                ALTER TABLE brand_asset_metadata
                    ADD COLUMN canonical_transform TEXT;
                PRAGMA user_version = 17;
                """
            )

    def _migrate_v17_to_v18(self) -> None:
        with self.connection:
            self.connection.executescript(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_youtube_channel_binding
                    ON channel_bindings(youtube_channel_id);
                CREATE TABLE IF NOT EXISTS publications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    brand_plan_id TEXT NOT NULL,
                    publishing_channel_id TEXT NOT NULL,
                    google_identity_id TEXT NOT NULL,
                    youtube_channel_id TEXT NOT NULL,
                    source_short_id INTEGER,
                    video_path TEXT NOT NULL,
                    video_checksum TEXT NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    tags_json TEXT NOT NULL,
                    privacy_status TEXT NOT NULL,
                    status TEXT NOT NULL,
                    remote_video_id TEXT UNIQUE,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    approved_at TEXT,
                    published_at TEXT,
                    last_error TEXT,
                    error_category TEXT,
                    FOREIGN KEY (brand_plan_id)
                        REFERENCES planned_channel_brands(brand_plan_id),
                    FOREIGN KEY (source_short_id) REFERENCES shorts(id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_publication_video_destination
                    ON publications(publishing_channel_id, video_checksum);
                CREATE INDEX IF NOT EXISTS idx_publications_status
                    ON publications(status);
                CREATE TABLE IF NOT EXISTS publication_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    publication_id INTEGER NOT NULL,
                    attempt_number INTEGER NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    remote_video_id TEXT,
                    error_category TEXT,
                    error_message TEXT,
                    FOREIGN KEY (publication_id) REFERENCES publications(id)
                        ON DELETE CASCADE,
                    UNIQUE (publication_id, attempt_number)
                );
                PRAGMA user_version = 18;
                """
            )

    def _migrate_v18_to_v19(self) -> None:
        """Repair early v18 databases created without the Short linkage."""
        columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(publications)")
        }
        with self.connection:
            if "source_short_id" not in columns:
                self.connection.execute(
                    "ALTER TABLE publications ADD COLUMN source_short_id INTEGER "
                    "REFERENCES shorts(id)"
                )
            self.connection.execute("PRAGMA user_version = 19")

    def _migrate_v19_to_v20(self) -> None:
        columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(publications)")
        }
        with self.connection:
            for definition in (
                "made_for_kids INTEGER",
                "contains_synthetic_media INTEGER",
                "has_paid_product_placement INTEGER",
                "category_id TEXT",
                "category_name TEXT",
                "default_language TEXT",
                "license TEXT",
                "embeddable INTEGER",
                "public_stats_viewable INTEGER",
                "publish_at TEXT",
                "recording_date TEXT",
            ):
                if definition.split()[0] not in columns:
                    self.connection.execute(
                        f"ALTER TABLE publications ADD COLUMN {definition}"
                    )
            self.connection.execute("PRAGMA user_version = 20")

    def _migrate_v20_to_v21(self) -> None:
        """Add market-aware editorial sources without changing legacy records."""
        with self.connection:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS theme_market_profiles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    theme_key TEXT NOT NULL, platform TEXT NOT NULL,
                    region_code TEXT NOT NULL, language_code TEXT NOT NULL,
                    youtube_category_id TEXT NOT NULL,
                    youtube_category_name TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    is_default_for_theme INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    FOREIGN KEY (theme_key) REFERENCES theme_catalog(theme_key)
                        ON DELETE CASCADE,
                    UNIQUE(theme_key, platform, region_code, language_code)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_default_theme_market_profile
                    ON theme_market_profiles(theme_key, platform)
                    WHERE is_default_for_theme = 1;
                CREATE TABLE IF NOT EXISTS content_sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform TEXT NOT NULL, external_channel_id TEXT NOT NULL,
                    display_name TEXT NOT NULL, handle TEXT, channel_url TEXT NOT NULL,
                    region_code TEXT NOT NULL, language_code TEXT NOT NULL,
                    source_kind TEXT NOT NULL, validation_status TEXT NOT NULL,
                    last_validated_at TEXT, latest_upload_at TEXT,
                    sampled_video_count INTEGER NOT NULL DEFAULT 0,
                    sampled_longform_count INTEGER NOT NULL DEFAULT 0,
                    sampled_target_duration_count INTEGER NOT NULL DEFAULT 0,
                    observed_duration_min_seconds INTEGER,
                    observed_duration_max_seconds INTEGER,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    rights_status TEXT NOT NULL, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(platform, external_channel_id)
                );
                CREATE TABLE IF NOT EXISTS theme_content_sources (
                    profile_id INTEGER NOT NULL, source_id INTEGER NOT NULL,
                    priority INTEGER NOT NULL CHECK(priority BETWEEN 1 AND 5),
                    enabled INTEGER NOT NULL DEFAULT 1,
                    target_duration_min_seconds INTEGER NOT NULL DEFAULT 900,
                    target_duration_max_seconds INTEGER NOT NULL DEFAULT 2400,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(profile_id, source_id),
                    UNIQUE(profile_id, priority),
                    FOREIGN KEY(profile_id) REFERENCES theme_market_profiles(id)
                        ON DELETE CASCADE,
                    FOREIGN KEY(source_id) REFERENCES content_sources(id)
                        ON DELETE CASCADE,
                    CHECK(target_duration_min_seconds < target_duration_max_seconds)
                );
                PRAGMA user_version = 21;
                """
            )

    def _migrate_v21_to_v22(self) -> None:
        """Persist evidence from the recent-upload validation sample."""
        columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(content_sources)")
        }
        with self.connection:
            for definition in (
                "latest_upload_at TEXT",
                "sampled_video_count INTEGER NOT NULL DEFAULT 0",
                "sampled_longform_count INTEGER NOT NULL DEFAULT 0",
                "sampled_target_duration_count INTEGER NOT NULL DEFAULT 0",
                "observed_duration_min_seconds INTEGER",
                "observed_duration_max_seconds INTEGER",
            ):
                if definition.split()[0] not in columns:
                    self.connection.execute(
                        f"ALTER TABLE content_sources ADD COLUMN {definition}"
                    )
            self.connection.execute("PRAGMA user_version = 22")

    def _migrate_v22_to_v23(self) -> None:
        """Add market posting policies and optional channel overrides."""
        with self.connection:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS theme_posting_profiles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    theme_market_profile_id INTEGER NOT NULL UNIQUE,
                    timezone TEXT NOT NULL, timing_policy TEXT NOT NULL,
                    strategy TEXT NOT NULL, confidence TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    source_note TEXT NOT NULL DEFAULT '', last_reviewed_at TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    FOREIGN KEY(theme_market_profile_id)
                        REFERENCES theme_market_profiles(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS posting_windows (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    posting_profile_id INTEGER NOT NULL,
                    priority INTEGER NOT NULL CHECK(priority > 0),
                    start_local_time TEXT NOT NULL, end_local_time TEXT NOT NULL,
                    days_of_week_json TEXT NOT NULL,
                    experimental INTEGER NOT NULL DEFAULT 0,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(posting_profile_id, priority),
                    FOREIGN KEY(posting_profile_id)
                        REFERENCES theme_posting_profiles(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS channel_posting_overrides (
                    publishing_channel_id TEXT PRIMARY KEY,
                    posting_profile_id INTEGER NOT NULL,
                    timezone TEXT, timing_policy TEXT, strategy TEXT, confidence TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    source_note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    FOREIGN KEY(posting_profile_id)
                        REFERENCES theme_posting_profiles(id) ON DELETE CASCADE
                );
                PRAGMA user_version = 23;
                """
            )

    def _migrate_v23_to_v24(self) -> None:
        """Add persistent publication scheduling and recovery history."""
        now = _now()
        with self.connection:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS scheduler_settings (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    global_paused INTEGER NOT NULL DEFAULT 0,
                    poll_interval_seconds INTEGER NOT NULL DEFAULT 45,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    missed_threshold_hours INTEGER NOT NULL DEFAULT 6,
                    max_publications_per_channel_per_day INTEGER NOT NULL DEFAULT 1,
                    min_spacing_minutes INTEGER NOT NULL DEFAULT 30,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS scheduler_channel_settings (
                    publishing_channel_id TEXT PRIMARY KEY,
                    paused INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS publication_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    publishing_channel_id TEXT NOT NULL,
                    publication_id INTEGER UNIQUE,
                    media_path TEXT NOT NULL, requested_publish_at TEXT,
                    resolved_publish_at TEXT, timezone TEXT NOT NULL,
                    priority INTEGER NOT NULL, urgency TEXT NOT NULL,
                    use_experimental_window INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    upload_started_at TEXT, last_error TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    started_at TEXT, finished_at TEXT,
                    FOREIGN KEY(publication_id) REFERENCES publications(id)
                );
                CREATE INDEX IF NOT EXISTS idx_publication_jobs_due
                    ON publication_jobs(state, resolved_publish_at, priority);
                CREATE TABLE IF NOT EXISTS scheduler_job_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER NOT NULL,
                    event_type TEXT NOT NULL, from_state TEXT, to_state TEXT,
                    details TEXT NOT NULL, created_at TEXT NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES publication_jobs(id)
                        ON DELETE CASCADE
                );
                PRAGMA user_version = 24;
                """
            )
            self.connection.execute(
                """INSERT OR IGNORE INTO scheduler_settings
                (singleton, updated_at) VALUES (1, ?)""",
                (now,),
            )

    def _migrate_v24_to_v25(self) -> None:
        """Persist source-video discovery and validated download provenance."""
        with self.connection:
            table = self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='videos'"
            ).fetchone()
            if table:
                existing = {
                    row["name"]
                    for row in self.connection.execute("PRAGMA table_info(videos)")
                }
                additions = {
                    "platform": "TEXT NOT NULL DEFAULT 'youtube'",
                    "content_source_id": "INTEGER",
                    "duration_seconds": "INTEGER",
                    "published_at": "TEXT",
                    "local_path": "TEXT",
                    "checksum": "TEXT",
                    "validated_at": "TEXT",
                }
                for name, declaration in additions.items():
                    if name not in existing:
                        self.connection.execute(
                            f"ALTER TABLE videos ADD COLUMN {name} {declaration}"
                        )
                self.connection.execute(
                    """CREATE UNIQUE INDEX IF NOT EXISTS
                    idx_videos_platform_external ON videos(platform, youtube_id)"""
                )
            self.connection.execute("PRAGMA user_version = 25")

    def _migrate_v25_to_v26(self) -> None:
        """Persist the validated final artifact independently from publication."""
        with self.connection:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS ready_artifacts (
                    youtube_id TEXT PRIMARY KEY,
                    file_path TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    manifest_path TEXT NOT NULL,
                    subtitle_path TEXT NOT NULL,
                    caption_fingerprint TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    validated_at TEXT NOT NULL,
                    FOREIGN KEY (youtube_id) REFERENCES videos(youtube_id)
                        ON DELETE CASCADE
                );
                PRAGMA user_version = 26;
                """
            )

    def _migrate_v26_to_v27(self) -> None:
        """Add durable AI metadata drafts and configurable review policy."""
        with self.connection:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata_settings (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    default_review_policy TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS metadata_channel_settings (
                    publishing_channel_id TEXT PRIMARY KEY,
                    review_policy TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS metadata_drafts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    youtube_id TEXT NOT NULL, ready_checksum TEXT NOT NULL,
                    publishing_channel_id TEXT NOT NULL, brand_plan_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
                    review_policy TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '', tags_json TEXT NOT NULL,
                    hashtags_json TEXT NOT NULL, category_id TEXT NOT NULL,
                    category_name TEXT NOT NULL, default_language TEXT NOT NULL,
                    privacy_status TEXT NOT NULL, made_for_kids INTEGER NOT NULL,
                    contains_synthetic_media INTEGER NOT NULL,
                    has_paid_product_placement INTEGER NOT NULL,
                    license TEXT NOT NULL, embeddable INTEGER NOT NULL,
                    public_stats_viewable INTEGER NOT NULL, provider TEXT NOT NULL,
                    model TEXT NOT NULL, prompt_hash TEXT NOT NULL,
                    generation_version TEXT NOT NULL,
                    title_candidates_json TEXT NOT NULL,
                    selection_reason TEXT NOT NULL DEFAULT '', error TEXT,
                    publication_id INTEGER, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, approved_at TEXT,
                    FOREIGN KEY(youtube_id) REFERENCES videos(youtube_id),
                    FOREIGN KEY(publication_id) REFERENCES publications(id)
                );
                PRAGMA user_version = 27;
                """
            )
            self.connection.execute(
                """INSERT OR IGNORE INTO metadata_settings
                (singleton, default_review_policy, updated_at) VALUES (1, ?, ?)""",
                (ReviewPolicy.MANUAL_REVIEW, _now()),
            )

    def _migrate_v27_to_v28(self) -> None:
        """Add durable inventory policy, decisions, assignments and leases."""
        with self.connection:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS orchestrator_settings (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    min_ready_videos INTEGER NOT NULL DEFAULT 2,
                    target_ready_videos INTEGER NOT NULL DEFAULT 3,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS orchestrator_channel_settings (
                    publishing_channel_id TEXT PRIMARY KEY,
                    min_ready_videos INTEGER NOT NULL,
                    target_ready_videos INTEGER NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS orchestrator_artifact_assignments (
                    youtube_id TEXT PRIMARY KEY, publishing_channel_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(youtube_id) REFERENCES videos(youtube_id)
                        ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_orchestrator_assignments_channel
                    ON orchestrator_artifact_assignments(publishing_channel_id);
                CREATE TABLE IF NOT EXISTS orchestrator_leases (
                    publishing_channel_id TEXT PRIMARY KEY, owner TEXT NOT NULL,
                    acquired_at TEXT NOT NULL, expires_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS orchestrator_cycles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL,
                    finished_at TEXT, max_productions INTEGER NOT NULL,
                    result TEXT NOT NULL, error TEXT
                );
                CREATE TABLE IF NOT EXISTS orchestrator_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id INTEGER NOT NULL,
                    publishing_channel_id TEXT NOT NULL,
                    inventory_before INTEGER NOT NULL, minimum INTEGER NOT NULL,
                    target INTEGER NOT NULL, planned_count INTEGER NOT NULL,
                    result TEXT NOT NULL, error TEXT, started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    FOREIGN KEY(cycle_id) REFERENCES orchestrator_cycles(id)
                        ON DELETE CASCADE
                );
                PRAGMA user_version = 28;
                """
            )
            self.connection.execute(
                """INSERT OR IGNORE INTO orchestrator_settings
                (singleton, min_ready_videos, target_ready_videos, updated_at)
                VALUES (1, 2, 3, ?)""",
                (_now(),),
            )

    def _migrate_v28_to_v29(self) -> None:
        """Persist YouTube candidate cooldowns and hydrated metadata cache."""
        with self.connection:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS youtube_source_failures (
                    profile_id INTEGER NOT NULL, youtube_id TEXT NOT NULL,
                    category TEXT NOT NULL, attempt_count INTEGER NOT NULL,
                    retry_after TEXT, detail TEXT NOT NULL,
                    first_failed_at TEXT NOT NULL, last_failed_at TEXT NOT NULL,
                    PRIMARY KEY(profile_id, youtube_id),
                    FOREIGN KEY(profile_id) REFERENCES theme_market_profiles(id)
                        ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_youtube_source_retry
                    ON youtube_source_failures(profile_id, retry_after);
                CREATE TABLE IF NOT EXISTS youtube_source_metadata (
                    youtube_id TEXT PRIMARY KEY, creator TEXT NOT NULL,
                    url TEXT NOT NULL, title TEXT NOT NULL, upload_date TEXT,
                    duration_seconds INTEGER, hydrated_at TEXT NOT NULL
                );
                PRAGMA user_version = 29;
                """
            )

    def _migrate_v29_to_v30(self) -> None:
        """Persist administrative YouTube provisioning and web-session state."""
        with self.connection:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS youtube_channel_provisioning (
                    brand_plan_id TEXT PRIMARY KEY,
                    google_identity_id TEXT NOT NULL,
                    expected_name TEXT NOT NULL,
                    expected_handle TEXT NOT NULL,
                    actual_handle TEXT,
                    youtube_channel_id TEXT,
                    status TEXT NOT NULL,
                    logo_path TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(brand_plan_id)
                        REFERENCES planned_channel_brands(brand_plan_id)
                        ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_youtube_provisioning_identity
                    ON youtube_channel_provisioning(google_identity_id, status);
                CREATE TABLE IF NOT EXISTS youtube_web_sessions (
                    google_identity_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    last_checked_at TEXT,
                    last_error TEXT
                );
                PRAGMA user_version = 30;
                """
            )


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _source_retry_after(
    youtube_id: str, category: str, attempt: int, now: datetime
) -> str | None:
    if category == "VIDEO_UNAVAILABLE":
        return None
    if category in {"RATE_LIMITED", "ANTI_BOT_CHALLENGE"}:
        base = (1800, 3600, 14400)[min(attempt - 1, 2)]
    elif category == "AUTH_REQUIRED":
        base = 3600
    elif category == "NETWORK_ERROR":
        base = min(600 * (2 ** (attempt - 1)), 3600)
    else:
        base = min(900 * (2 ** (attempt - 1)), 7200)
    digest = hashlib.sha256(f"{youtube_id}:{attempt}".encode()).digest()[0]
    jitter = 0.9 + (digest / 255) * 0.2
    return (now + timedelta(seconds=round(base * jitter))).isoformat()


def _theme_market_profile(row: sqlite3.Row) -> ThemeMarketProfile:
    return ThemeMarketProfile(
        row["id"],
        row["theme_key"],
        row["platform"],
        row["region_code"],
        row["language_code"],
        row["youtube_category_id"],
        row["youtube_category_name"],
        bool(row["enabled"]),
        bool(row["is_default_for_theme"]),
        row["created_at"],
        row["updated_at"],
    )


def _content_source(row: sqlite3.Row) -> ContentSource:
    return ContentSource(
        row["id"],
        row["platform"],
        row["external_channel_id"],
        row["display_name"],
        row["handle"],
        row["channel_url"],
        row["region_code"],
        row["language_code"],
        row["source_kind"],
        SourceValidationStatus(row["validation_status"]),
        row["last_validated_at"],
        row["latest_upload_at"],
        row["sampled_video_count"],
        row["sampled_longform_count"],
        row["sampled_target_duration_count"],
        row["observed_duration_min_seconds"],
        row["observed_duration_max_seconds"],
        bool(row["enabled"]),
        SourceRightsStatus(row["rights_status"]),
        row["created_at"],
        row["updated_at"],
    )


def _theme_content_source(row: sqlite3.Row) -> ThemeContentSource:
    return ThemeContentSource(
        row["profile_id"],
        row["source_id"],
        row["priority"],
        bool(row["enabled"]),
        row["target_duration_min_seconds"],
        row["target_duration_max_seconds"],
        row["created_at"],
        row["updated_at"],
    )


def _theme_posting_profile(row: sqlite3.Row) -> ThemePostingProfile:
    return ThemePostingProfile(
        row["id"],
        row["theme_market_profile_id"],
        row["timezone"],
        TimingPolicy(row["timing_policy"]),
        PostingStrategy(row["strategy"]),
        PostingConfidence(row["confidence"]),
        bool(row["enabled"]),
        row["source_note"],
        row["last_reviewed_at"],
        row["created_at"],
        row["updated_at"],
    )


def _posting_window(row: sqlite3.Row) -> PostingWindow:
    return PostingWindow(
        row["id"],
        row["posting_profile_id"],
        row["priority"],
        row["start_local_time"],
        row["end_local_time"],
        tuple(json.loads(row["days_of_week_json"])),
        bool(row["experimental"]),
        bool(row["enabled"]),
        row["created_at"],
        row["updated_at"],
    )


def _channel_posting_override(row: sqlite3.Row) -> ChannelPostingOverride:
    return ChannelPostingOverride(
        row["publishing_channel_id"],
        row["posting_profile_id"],
        row["timezone"],
        TimingPolicy(row["timing_policy"]) if row["timing_policy"] else None,
        PostingStrategy(row["strategy"]) if row["strategy"] else None,
        PostingConfidence(row["confidence"]) if row["confidence"] else None,
        bool(row["enabled"]),
        row["source_note"],
        row["created_at"],
        row["updated_at"],
    )


def _validate_posting_window(
    priority: int, start: str, end: str, days: tuple[str, ...]
) -> None:
    if priority < 1:
        raise ValueError("Posting window priority must be positive.")
    for value in (start, end):
        try:
            datetime.strptime(value, "%H:%M")
        except ValueError as exc:
            raise ValueError(f"Invalid local posting time: {value}") from exc
    if start == end:
        raise ValueError("Posting window start and end cannot be equal.")
    allowed = {"Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"}
    if not days or len(days) != len(set(days)) or not set(days) <= allowed:
        raise ValueError("Posting window days must be unique weekday abbreviations.")


def _brand_asset_record(row):
    from autoshorts.asset_visual_direction import ResolvedAssetVisualDirection
    from autoshorts.brand_assets import (
        BrandAssetStatus,
        BrandAssetType,
        GeneratedBrandAsset,
    )

    return GeneratedBrandAsset(
        row["brand_plan_id"],
        BrandAssetType(row["asset_type"]),
        Path(row["file_path"]),
        row["provider"],
        row["model"],
        row["prompt"],
        row["width"],
        row["height"],
        row["file_size"],
        row["checksum"],
        BrandAssetStatus(row["status"]),
        row["created_at"],
        row["version"],
        Path(row["preview_path"]) if row["preview_path"] else None,
        Path(row["delivery_path"]) if row["delivery_path"] else None,
        ResolvedAssetVisualDirection.from_dict(
            json.loads(row["resolved_visual_direction_json"])
        )
        if row["resolved_visual_direction_json"]
        else None,
        row["source_package"],
        row["source_file"],
        Path(row["source_preserved_path"]) if row["source_preserved_path"] else None,
        row["source_checksum"],
        row["source_width"],
        row["source_height"],
        row["canonical_transform"],
    )


def _video_record(row: sqlite3.Row) -> VideoRecord:
    return VideoRecord(
        youtube_id=row["youtube_id"],
        source_url=row["source_url"],
        creator_name=row["creator_name"],
        theme_name=row["theme_name"],
        title=row["title"],
        status=VideoStatus(row["status"]),
        discovered_at=row["discovered_at"],
        updated_at=row["updated_at"],
        processed_at=row["processed_at"],
        error=row["error"],
        platform=row["platform"],
        content_source_id=row["content_source_id"],
        duration_seconds=row["duration_seconds"],
        published_at=row["published_at"],
        local_path=Path(row["local_path"]) if row["local_path"] else None,
        checksum=row["checksum"],
        validated_at=row["validated_at"],
    )


def _short_record(row: sqlite3.Row) -> ShortRecord:
    return ShortRecord(
        id=row["id"],
        youtube_id=row["youtube_id"],
        clip_index=row["clip_index"],
        account_slot=row["account_slot"],
        account_identifier=row["account_identifier"],
        publishing_channel_id=row["publishing_channel_id"],
        status=ShortStatus(row["status"]),
        file_path=Path(row["file_path"]),
        queue_path=Path(row["queue_path"]) if row["queue_path"] else None,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        posted_at=row["posted_at"],
        error=row["error"],
        title=row["title"],
        headline=row["headline"],
        generator_score=row["generator_score"],
        editorial_score=row["editorial_score"],
        candidate_id=row["candidate_id"],
        duration_seconds=row["duration_seconds"],
    )


def _transcript_record(row: sqlite3.Row) -> TranscriptRecord:
    return TranscriptRecord(
        youtube_id=row["youtube_id"],
        file_path=Path(row["file_path"]),
        provider=row["provider"],
        model=row["model"],
        language=row["language"],
        created_at=row["created_at"],
    )


def _analysis_record(row: sqlite3.Row) -> AnalysisRecord:
    return AnalysisRecord(
        youtube_id=row["youtube_id"],
        file_path=Path(row["file_path"]),
        provider=row["provider"],
        model=row["model"],
        prompt_hash=row["prompt_hash"],
        created_at=row["created_at"],
    )


def _ready_artifact(row: sqlite3.Row) -> ReadyArtifact:
    return ReadyArtifact(
        row["youtube_id"],
        Path(row["file_path"]),
        row["checksum"],
        Path(row["manifest_path"]),
        Path(row["subtitle_path"]),
        row["caption_fingerprint"],
        row["created_at"],
        row["updated_at"],
        row["validated_at"],
    )


def _metadata_draft(row: sqlite3.Row) -> MetadataDraft:
    return MetadataDraft(
        row["id"],
        row["youtube_id"],
        row["ready_checksum"],
        row["publishing_channel_id"],
        row["brand_plan_id"],
        row["fingerprint"],
        MetadataDraftStatus(row["status"]),
        ReviewPolicy(row["review_policy"]),
        row["title"],
        row["description"],
        tuple(json.loads(row["tags_json"])),
        tuple(json.loads(row["hashtags_json"])),
        row["category_id"],
        row["category_name"],
        row["default_language"],
        row["privacy_status"],
        bool(row["made_for_kids"]),
        bool(row["contains_synthetic_media"]),
        bool(row["has_paid_product_placement"]),
        row["license"],
        bool(row["embeddable"]),
        bool(row["public_stats_viewable"]),
        row["provider"],
        row["model"],
        row["prompt_hash"],
        row["generation_version"],
        tuple(json.loads(row["title_candidates_json"])),
        row["selection_reason"],
        row["error"],
        row["publication_id"],
        row["created_at"],
        row["updated_at"],
        row["approved_at"],
    )


def _channel_binding(row: sqlite3.Row) -> ChannelCredentialBinding:
    return ChannelCredentialBinding(
        publishing_channel_id=row["publishing_channel_id"],
        google_identity_id=row["google_identity_id"],
        provider=row["provider"],
        credential_ref=row["credential_ref"],
        youtube_channel_id=row["youtube_channel_id"],
        youtube_channel_title=row["youtube_channel_title"],
        connected_at=row["connected_at"],
        last_verified_at=row["last_verified_at"],
        status=row["status"],
    )


def _publication_record(row: sqlite3.Row) -> PublicationRecord:
    return PublicationRecord(
        publication_id=row["id"],
        idempotency_key=row["idempotency_key"],
        brand_plan_id=row["brand_plan_id"],
        publishing_channel_id=row["publishing_channel_id"],
        google_identity_id=row["google_identity_id"],
        youtube_channel_id=row["youtube_channel_id"],
        source_short_id=row["source_short_id"],
        video_path=Path(row["video_path"]),
        video_checksum=row["video_checksum"],
        title=row["title"],
        description=row["description"],
        tags=tuple(json.loads(row["tags_json"])),
        privacy_status=row["privacy_status"],
        made_for_kids=_optional_bool(row["made_for_kids"]),
        contains_synthetic_media=_optional_bool(row["contains_synthetic_media"]),
        has_paid_product_placement=_optional_bool(row["has_paid_product_placement"]),
        category_id=row["category_id"],
        category_name=row["category_name"],
        default_language=row["default_language"],
        license=row["license"],
        embeddable=_optional_bool(row["embeddable"]),
        public_stats_viewable=_optional_bool(row["public_stats_viewable"]),
        publish_at=row["publish_at"],
        recording_date=row["recording_date"],
        status=PublicationStatus(row["status"]),
        remote_video_id=row["remote_video_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        approved_at=row["approved_at"],
        published_at=row["published_at"],
        last_error=row["last_error"],
        error_category=row["error_category"],
    )


def _optional_bool(value: int | None) -> bool | None:
    return None if value is None else bool(value)


def _youtube_provisioning_record(row: sqlite3.Row) -> YouTubeProvisioningRecord:
    return YouTubeProvisioningRecord(
        row["brand_plan_id"],
        row["google_identity_id"],
        row["expected_name"],
        row["expected_handle"],
        row["actual_handle"],
        row["youtube_channel_id"],
        row["status"],
        Path(row["logo_path"]) if row["logo_path"] else None,
        row["last_error"],
        row["created_at"],
        row["updated_at"],
    )


def _publication_attempt(row: sqlite3.Row) -> PublicationAttempt:
    return PublicationAttempt(
        attempt_id=row["id"],
        publication_id=row["publication_id"],
        attempt_number=row["attempt_number"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        status=row["status"],
        remote_video_id=row["remote_video_id"],
        error_category=row["error_category"],
        error_message=row["error_message"],
    )


def _scheduler_settings(row: sqlite3.Row) -> SchedulerSettings:
    return SchedulerSettings(
        bool(row["global_paused"]),
        row["poll_interval_seconds"],
        row["max_attempts"],
        row["missed_threshold_hours"],
        row["max_publications_per_channel_per_day"],
        row["min_spacing_minutes"],
        row["updated_at"],
    )


def _publication_job(row: sqlite3.Row) -> PublicationJob:
    return PublicationJob(
        row["id"],
        row["publishing_channel_id"],
        row["publication_id"],
        Path(row["media_path"]),
        row["requested_publish_at"],
        row["resolved_publish_at"],
        row["timezone"],
        row["priority"],
        SchedulerUrgency(row["urgency"]),
        bool(row["use_experimental_window"]),
        SchedulerJobState(row["state"]),
        row["attempts"],
        row["max_attempts"],
        row["upload_started_at"],
        row["last_error"],
        row["created_at"],
        row["updated_at"],
        row["started_at"],
        row["finished_at"],
    )


def _scheduler_job_event(row: sqlite3.Row) -> SchedulerJobEvent:
    return SchedulerJobEvent(
        row["id"],
        row["job_id"],
        row["event_type"],
        row["from_state"],
        row["to_state"],
        row["details"],
        row["created_at"],
    )
