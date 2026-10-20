"""SQLite operational state for restart-safe, headless automation workers."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import asdict, dataclass
from dataclasses import replace as dataclass_replace
from datetime import UTC, datetime
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
SCHEMA_VERSION = 19


class VideoStatus(StrEnum):
    DISCOVERED = "DISCOVERED"
    DOWNLOADING = "DOWNLOADING"
    DOWNLOADED = "DOWNLOADED"
    TRANSCRIBING = "TRANSCRIBING"
    TRANSCRIBED = "TRANSCRIBED"
    ANALYZING = "ANALYZING"
    ANALYZED = "ANALYZED"
    RENDERING = "RENDERING"
    RENDERED = "RENDERED"
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
    VideoStatus.ANALYZED: frozenset({VideoStatus.RENDERING, VideoStatus.FAILED}),
    VideoStatus.RENDERING: frozenset({VideoStatus.RENDERED, VideoStatus.FAILED}),
    VideoStatus.RENDERED: frozenset(
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
                    status, discovered_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(youtube_id) DO UPDATE SET
                    source_url = excluded.source_url,
                    creator_name = excluded.creator_name,
                    title = excluded.title
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
                ),
            )
        return not existed

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
            in {VideoStatus.RENDERED, VideoStatus.QUEUED, VideoStatus.COMPLETED}
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
        terminal = (VideoStatus.QUEUED, VideoStatus.COMPLETED, VideoStatus.FAILED)
        rows = self.connection.execute(
            "SELECT * FROM videos WHERE status NOT IN (?, ?, ?) ORDER BY updated_at",
            terminal,
        ).fetchall()
        return tuple(_video_record(row) for row in rows)

    def list_incomplete_videos(
        self, *, include_failed: bool = False
    ) -> tuple[VideoRecord, ...]:
        """List production jobs that may require recovery or manual retry."""
        excluded = [VideoStatus.QUEUED, VideoStatus.COMPLETED]
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
    ) -> PublicationRecord:
        now = _now()
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO publications (
                    idempotency_key, brand_plan_id, publishing_channel_id,
                    google_identity_id, youtube_channel_id, video_path,
                    source_short_id, video_checksum, title, description, tags_json,
                    privacy_status,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                WHERE id=? AND status IN ({','.join('?' for _ in allowed)})""",
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
                    status=?, approved_at=NULL, updated_at=?, last_error=NULL,
                    error_category=NULL
                WHERE id=? AND status IN ({','.join('?' for _ in allowed)})""",
                (
                    title,
                    description,
                    json.dumps(tags, ensure_ascii=False),
                    privacy_status,
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
                    error TEXT
                );

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
                """
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


def _now() -> str:
    return datetime.now(UTC).isoformat()


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
        Path(row["source_preserved_path"])
        if row["source_preserved_path"]
        else None,
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
        status=PublicationStatus(row["status"]),
        remote_video_id=row["remote_video_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        approved_at=row["approved_at"],
        published_at=row["published_at"],
        last_error=row["last_error"],
        error_category=row["error_category"],
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
