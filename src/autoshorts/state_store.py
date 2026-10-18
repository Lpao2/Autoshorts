"""SQLite operational state for restart-safe, headless automation workers."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

STATE_DB_ENV = "AUTOSHORTS_STATE_DB"
SCHEMA_VERSION = 2


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


@dataclass(frozen=True, slots=True)
class QueuedShort:
    clip_index: int
    account_slot: int
    account_identifier: str
    queue_path: Path


@dataclass(frozen=True, slots=True)
class ShortRecord:
    id: int
    youtube_id: str
    clip_index: int
    account_slot: int | None
    account_identifier: str | None
    status: ShortStatus
    file_path: Path
    queue_path: Path | None
    created_at: str
    updated_at: str
    posted_at: str | None
    error: str | None


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
        with self.connection:
            cursor = self.connection.execute(
                """
                INSERT OR IGNORE INTO videos (
                    youtube_id, source_url, creator_name, theme_name, title,
                    status, discovered_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
        return cursor.rowcount == 1

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
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        youtube_id,
                        item.clip_index,
                        ShortStatus.RENDERED,
                        str(item.file_path),
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
                    SET account_slot = ?, account_identifier = ?, status = ?,
                        queue_path = ?, updated_at = ?
                    WHERE youtube_id = ? AND clip_index = ? AND status = ?
                    """,
                    (
                        item.account_slot,
                        item.account_identifier,
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

                CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status);
                CREATE INDEX IF NOT EXISTS idx_shorts_status ON shorts(status);
                """
                )
                self.connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            return
        if version == 1:
            self._migrate_v1_to_v2()

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
        status=ShortStatus(row["status"]),
        file_path=Path(row["file_path"]),
        queue_path=Path(row["queue_path"]) if row["queue_path"] else None,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        posted_at=row["posted_at"],
        error=row["error"],
    )
