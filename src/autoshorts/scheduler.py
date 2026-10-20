"""Persistent, local publication scheduler with conservative recovery."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from autoshorts.automation_config import AutomationConfig
from autoshorts.editorial_scheduling import (
    ResolvedPostingSchedule,
    resolve_channel_posting_schedule,
)
from autoshorts.editorial_sources import PostingWindow, TimingPolicy
from autoshorts.scheduler_models import (
    PublicationJob,
    SchedulerJobState,
    SchedulerUrgency,
)
from autoshorts.state_store import PublicationStatus, StateStore


@dataclass(frozen=True, slots=True)
class SchedulerExecutionResult:
    confirmed: bool = True


class SchedulerExecutionError(RuntimeError):
    def __init__(self, message: str, *, recoverable: bool, uncertain: bool = False):
        super().__init__(message)
        self.recoverable = recoverable
        self.uncertain = uncertain


class SchedulerExecutor(Protocol):
    def execute(self, publication_id: int) -> SchedulerExecutionResult: ...


def next_publication_time(
    schedule: ResolvedPostingSchedule,
    now: datetime,
    occupied: tuple[datetime, ...] = (),
    *,
    urgency: SchedulerUrgency = SchedulerUrgency.NORMAL,
    experimental: bool = False,
    max_publications_per_day: int = 1,
    min_spacing_minutes: int = 30,
    max_preference_delay_days: int = 3,
) -> datetime:
    """Return an aware UTC instant; preferred weekdays are soft constraints."""
    if now.tzinfo is None:
        raise ValueError("Scheduler time must be timezone-aware.")
    zone = ZoneInfo(schedule.posting_profile.timezone)
    local_now = now.astimezone(zone)
    local_occupied = tuple(item.astimezone(zone) for item in occupied)
    if (
        urgency is SchedulerUrgency.FRESH
        and schedule.posting_profile.timing_policy is TimingPolicy.FRESHNESS_FIRST
    ):
        fresh = local_now.replace(second=0, microsecond=0)
        if fresh < local_now:
            fresh += timedelta(minutes=1)
        candidate = _avoid_conflict(
            fresh,
            local_occupied,
            max_publications_per_day,
            min_spacing_minutes,
        )
        return candidate.astimezone(UTC)
    priority = 2 if experimental else 1
    window = next(
        (item for item in schedule.windows if item.priority == priority), None
    )
    if window is None or not window.enabled:
        raise ValueError(f"Active posting window priority {priority} not found.")
    candidates = _window_candidates(
        window,
        local_now,
        local_occupied,
        max_publications_per_day,
        min_spacing_minutes,
    )
    preferred = [
        item for item in candidates if item.strftime("%a") in window.days_of_week
    ]
    fallback = candidates[0]
    chosen = preferred[0] if preferred else fallback
    if (
        preferred
        and (preferred[0].date() - fallback.date()).days > max_preference_delay_days
    ):
        chosen = fallback
    return chosen.astimezone(UTC)


def _window_candidates(
    window: PostingWindow,
    now: datetime,
    occupied: tuple[datetime, ...],
    max_per_day: int,
    spacing_minutes: int,
) -> list[datetime]:
    result = []
    # Start one day earlier so a 23:00-01:00 window remains eligible at 00:30.
    for offset in range(-1, 15):
        day = now.date() + timedelta(days=offset)
        start, end = _window_bounds(day, window, now.tzinfo)
        candidate = max(now, start)
        if candidate > start:
            elapsed = (candidate - start).total_seconds() / 60
            candidate = start + timedelta(
                minutes=math.ceil(elapsed / spacing_minutes) * spacing_minutes
            )
        while candidate < end:
            if _available(candidate, occupied, max_per_day, spacing_minutes):
                result.append(candidate)
                break
            candidate += timedelta(minutes=spacing_minutes)
    if not result:
        raise ValueError("No publication slot is available in the planning horizon.")
    return result


def _window_bounds(
    day: date, window: PostingWindow, tzinfo
) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.fromisoformat(window.start_local_time), tzinfo)
    end = datetime.combine(day, time.fromisoformat(window.end_local_time), tzinfo)
    if end <= start:
        end += timedelta(days=1)
    return start, end


def _available(
    candidate: datetime,
    occupied: tuple[datetime, ...],
    max_per_day: int,
    spacing_minutes: int,
) -> bool:
    same_day = [item for item in occupied if item.date() == candidate.date()]
    if len(same_day) >= max_per_day:
        return False
    spacing = timedelta(minutes=spacing_minutes)
    return all(abs(item - candidate) >= spacing for item in occupied)


def _avoid_conflict(
    candidate: datetime,
    occupied: tuple[datetime, ...],
    max_per_day: int,
    spacing_minutes: int,
) -> datetime:
    while not _available(candidate, occupied, max_per_day, spacing_minutes):
        candidate += timedelta(days=1)
    return candidate


class SchedulerService:
    def __init__(
        self,
        config: AutomationConfig,
        state: StateStore,
        executor: SchedulerExecutor,
    ) -> None:
        self.config = config
        self.state = state
        self.executor = executor

    def plan_channel(
        self,
        publishing_channel_id: str,
        now: datetime,
        *,
        urgency: SchedulerUrgency = SchedulerUrgency.NORMAL,
        experimental: bool = False,
    ) -> tuple[ResolvedPostingSchedule, datetime]:
        schedule = resolve_channel_posting_schedule(
            self.config, self.state, publishing_channel_id
        )
        settings = self.state.scheduler_settings()
        occupied = tuple(
            datetime.fromisoformat(item.resolved_publish_at)
            for item in self.state.scheduler_jobs()
            if item.publishing_channel_id == publishing_channel_id
            and item.resolved_publish_at
            and item.state
            not in {SchedulerJobState.CANCELLED, SchedulerJobState.FAILED_PERMANENT}
        )
        proposed = next_publication_time(
            schedule,
            now,
            occupied,
            urgency=urgency,
            experimental=experimental,
            max_publications_per_day=settings.max_publications_per_channel_per_day,
            min_spacing_minutes=settings.min_spacing_minutes,
        )
        return schedule, proposed

    def enqueue(self, publication_id: int, now: datetime, **options) -> PublicationJob:
        publication = self.state.publication(publication_id)
        if publication is None:
            raise KeyError(f"Unknown publication: {publication_id}")
        if publication.privacy_status != "private":
            raise ValueError("Pilot scheduler accepts private publications only.")
        if publication.publish_at is not None:
            raise ValueError(
                "Pilot scheduler does not accept YouTube publish_at; uploads must "
                "remain private."
            )
        if (
            publication.status is PublicationStatus.PUBLISHED
            or publication.remote_video_id
        ):
            raise ValueError("Published publications cannot be enqueued.")
        urgency = options.get("urgency", SchedulerUrgency.NORMAL)
        experimental = bool(options.get("experimental", False))
        schedule, proposed = self.plan_channel(
            publication.publishing_channel_id,
            now,
            urgency=urgency,
            experimental=experimental,
        )
        job = self.state.enqueue_publication_job(
            publishing_channel_id=publication.publishing_channel_id,
            publication_id=publication_id,
            media_path=publication.video_path,
            timezone=schedule.posting_profile.timezone,
            urgency=urgency,
            use_experimental_window=experimental,
            requested_publish_at=options.get("requested_publish_at"),
        )
        if job.state is not SchedulerJobState.QUEUED:
            return job
        return self.state.update_scheduler_job(
            job.job_id,
            state=SchedulerJobState.SCHEDULED,
            expected=(SchedulerJobState.QUEUED,),
            resolved_publish_at=proposed.isoformat(),
            event_type="SCHEDULED",
        )

    def recover(self) -> tuple[PublicationJob, ...]:
        return self.state.recover_running_scheduler_jobs()

    def run_once(self, now: datetime) -> tuple[PublicationJob, ...]:
        settings = self.state.scheduler_settings()
        if settings.global_paused:
            return ()
        results = []
        for job in self.state.scheduler_jobs():
            if job.state not in {
                SchedulerJobState.SCHEDULED,
                SchedulerJobState.FAILED_RECOVERABLE,
            }:
                continue
            if self.state.scheduler_channel_paused(job.publishing_channel_id):
                continue
            if not job.resolved_publish_at:
                continue
            due = datetime.fromisoformat(job.resolved_publish_at)
            if due > now.astimezone(UTC):
                continue
            lateness = now.astimezone(UTC) - due
            if lateness > timedelta(hours=settings.missed_threshold_hours):
                _, proposed = self.plan_channel(
                    job.publishing_channel_id,
                    now,
                    urgency=job.urgency,
                    experimental=job.use_experimental_window,
                )
                results.append(
                    self.state.update_scheduler_job(
                        job.job_id,
                        state=SchedulerJobState.SCHEDULED,
                        expected=(job.state,),
                        resolved_publish_at=proposed.isoformat(),
                        event_type="MISSED_RESCHEDULED",
                        event_details=f"Missed by {lateness}.",
                    )
                )
                continue
            if lateness > timedelta(0):
                job = self.state.update_scheduler_job(
                    job.job_id,
                    state=job.state,
                    expected=(job.state,),
                    event_type="MISSED_EXECUTED",
                    event_details=f"Executed after a delay of {lateness}.",
                )
            results.append(self._execute(job))
        return tuple(results)

    def _execute(self, job: PublicationJob) -> PublicationJob:
        if job.publication_id is None:
            return self._permanent(job, "Job has no reviewed Publication.")
        publication = self.state.publication(job.publication_id)
        if publication is None:
            return self._permanent(job, "Publication no longer exists.")
        if (
            publication.privacy_status != "private"
            or publication.publish_at is not None
        ):
            return self._permanent(
                job, "Pilot scheduler requires a permanently private Publication."
            )
        if (
            publication.remote_video_id
            or publication.status is PublicationStatus.PUBLISHED
        ):
            return self.state.update_scheduler_job(
                job.job_id,
                state=SchedulerJobState.COMPLETED,
                expected=(job.state,),
                event_type="IDEMPOTENT_REMOTE_FOUND",
            )
        if job.attempts >= job.max_attempts:
            return self._permanent(job, "Maximum scheduler attempts reached.")
        if publication.status is PublicationStatus.UPLOAD_UNCERTAIN:
            return self._uncertain(job, "Publication upload outcome is uncertain.")
        if publication.status is PublicationStatus.FAILED_RETRYABLE:
            self.state.approve_publication(publication.publication_id)
        elif publication.status is not PublicationStatus.APPROVED_FOR_PUBLISH:
            return self._permanent(job, "Publication is not approved for upload.")
        running = self.state.update_scheduler_job(
            job.job_id,
            state=SchedulerJobState.RUNNING,
            expected=(job.state,),
            increment_attempts=True,
            event_type="EXECUTION_STARTED",
        )
        running = self.state.update_scheduler_job(
            running.job_id,
            state=SchedulerJobState.RUNNING,
            expected=(SchedulerJobState.RUNNING,),
            upload_started=True,
            event_type="UPLOAD_EXECUTION_ENTERED",
        )
        try:
            result = self.executor.execute(publication.publication_id)
            if not result.confirmed:
                return self._uncertain(running, "Executor did not confirm upload.")
        except SchedulerExecutionError as exc:
            if exc.uncertain:
                return self._uncertain(running, str(exc))
            if exc.recoverable and running.attempts < running.max_attempts:
                return self.state.update_scheduler_job(
                    running.job_id,
                    state=SchedulerJobState.FAILED_RECOVERABLE,
                    expected=(SchedulerJobState.RUNNING,),
                    last_error=str(exc),
                    event_type="RECOVERABLE_FAILURE",
                )
            return self._permanent(running, str(exc))
        return self.state.update_scheduler_job(
            running.job_id,
            state=SchedulerJobState.COMPLETED,
            expected=(SchedulerJobState.RUNNING,),
            event_type="UPLOAD_CONFIRMED",
        )

    def _permanent(self, job: PublicationJob, message: str) -> PublicationJob:
        return self.state.update_scheduler_job(
            job.job_id,
            state=SchedulerJobState.FAILED_PERMANENT,
            expected=(job.state,),
            last_error=message,
            event_type="PERMANENT_FAILURE",
        )

    def _uncertain(self, job: PublicationJob, message: str) -> PublicationJob:
        return self.state.update_scheduler_job(
            job.job_id,
            state=SchedulerJobState.UPLOAD_UNCERTAIN,
            expected=(job.state,),
            last_error=message,
            event_type="UPLOAD_UNCERTAIN",
        )
