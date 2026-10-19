"""SQLite operational state for restart-safe, headless automation workers."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from autoshorts.automation_config import ColorPalette
from autoshorts.theme_catalog import (
    BrandProfile,
    CatalogProvenance,
    CatalogStatus,
    ChannelArchetype,
    PromptVersion,
    ThemeTemplate,
    load_theme_seed,
)
from autoshorts.transcript_analysis import AnalysisUsage

STATE_DB_ENV = "AUTOSHORTS_STATE_DB"
SCHEMA_VERSION = 9


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
                    template.theme_key, template.display_name, template.description,
                    json.dumps(template.tags, ensure_ascii=False),
                    json.dumps(template.source_strategy, ensure_ascii=False),
                    json.dumps(template.production_defaults, ensure_ascii=False),
                    template.status, template.provenance, now, now,
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
                        tone, visual_direction, palette_json, brand_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        template.theme_key, item.slot, item.name,
                        item.editorial_prompt, item.headline_style, item.tone,
                        item.visual_direction,
                        json.dumps([
                            item.palette.primary, item.palette.secondary,
                            item.palette.accent, item.palette.background,
                        ]),
                        json.dumps({
                            "public_name": item.brand.public_name,
                            "handle": item.brand.handle,
                            "description": item.brand.description,
                            "tagline": item.brand.tagline,
                            "logo_path": item.brand.logo_path,
                            "banner_path": item.brand.banner_path,
                        }, ensure_ascii=False),
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
            if self.get_theme_template(template.theme_key) is None:
                self.save_theme_template(template)
                inserted += 1
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
                version=item["version"], prompt=item["prompt"],
                provenance=CatalogProvenance(item["provenance"]),
                active=bool(item["active"]), created_at=item["created_at"],
            )
            for item in prompt_rows
        )
        active = next((item.prompt for item in prompts if item.active), "")
        archetypes = []
        for item in archetype_rows:
            colors = json.loads(item["palette_json"])
            brand = json.loads(item["brand_json"])
            archetypes.append(ChannelArchetype(
                slot=item["slot"], name=item["name"],
                editorial_prompt=item["editorial_prompt"],
                headline_style=item["headline_style"], tone=item["tone"],
                visual_direction=item["visual_direction"],
                palette=ColorPalette(*colors), brand=BrandProfile(**brand),
            ))
        return ThemeTemplate(
            theme_key=key, display_name=row["display_name"],
            description=row["description"], base_prompt=active,
            tags=tuple(json.loads(row["tags_json"])),
            source_strategy=json.loads(row["source_strategy_json"]),
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
                CREATE TABLE IF NOT EXISTS theme_catalog (
                    theme_key TEXT PRIMARY KEY, display_name TEXT NOT NULL,
                    description TEXT NOT NULL, tags_json TEXT NOT NULL,
                    source_strategy_json TEXT NOT NULL,
                    production_defaults_json TEXT NOT NULL,
                    status TEXT NOT NULL, provenance TEXT NOT NULL,
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
                    brand_json TEXT NOT NULL, PRIMARY KEY (theme_key, slot),
                    FOREIGN KEY (theme_key) REFERENCES theme_catalog(theme_key)
                        ON DELETE CASCADE
                );
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


def _now() -> str:
    return datetime.now(UTC).isoformat()


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
