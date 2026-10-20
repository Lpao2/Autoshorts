"""Persistent scheduler domain records."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class SchedulerJobState(StrEnum):
    QUEUED = "QUEUED"
    SCHEDULED = "SCHEDULED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED_RECOVERABLE = "FAILED_RECOVERABLE"
    FAILED_PERMANENT = "FAILED_PERMANENT"
    CANCELLED = "CANCELLED"
    PAUSED = "PAUSED"
    UPLOAD_UNCERTAIN = "UPLOAD_UNCERTAIN"


class SchedulerUrgency(StrEnum):
    NORMAL = "NORMAL"
    FRESH = "FRESH"


@dataclass(frozen=True, slots=True)
class SchedulerSettings:
    global_paused: bool
    poll_interval_seconds: int
    max_attempts: int
    missed_threshold_hours: int
    max_publications_per_channel_per_day: int
    min_spacing_minutes: int
    updated_at: str


@dataclass(frozen=True, slots=True)
class PublicationJob:
    job_id: int
    publishing_channel_id: str
    publication_id: int | None
    media_path: Path
    requested_publish_at: str | None
    resolved_publish_at: str | None
    timezone: str
    priority: int
    urgency: SchedulerUrgency
    use_experimental_window: bool
    state: SchedulerJobState
    attempts: int
    max_attempts: int
    upload_started_at: str | None
    last_error: str | None
    created_at: str
    updated_at: str
    started_at: str | None
    finished_at: str | None


@dataclass(frozen=True, slots=True)
class SchedulerJobEvent:
    event_id: int
    job_id: int
    event_type: str
    from_state: str | None
    to_state: str | None
    details: str
    created_at: str
