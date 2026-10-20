from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from test_youtube_publishing import configured_service

from autoshorts.editorial_scheduling import ResolvedPostingSchedule
from autoshorts.editorial_seed import import_editorial_seed, import_posting_seed
from autoshorts.editorial_sources import (
    PostingConfidence,
    PostingStrategy,
    PostingWindow,
    ThemePostingProfile,
    TimingPolicy,
)
from autoshorts.scheduler import (
    SchedulerExecutionError,
    SchedulerExecutionResult,
    SchedulerService,
    next_publication_time,
)
from autoshorts.scheduler_models import SchedulerJobState, SchedulerUrgency
from autoshorts.state_store import (
    ChannelCredentialBinding,
    PublicationStatus,
    StateStore,
)
from autoshorts.youtube_publishing import PublicationMetadata


def schedule(policy=TimingPolicy.SCHEDULED, *, crossing=False):
    profile = ThemePostingProfile(
        1,
        1,
        "America/Sao_Paulo",
        policy,
        PostingStrategy.SEEDED,
        PostingConfidence.MEDIUM,
        True,
        "",
        None,
        "now",
        "now",
    )
    primary = PostingWindow(
        1,
        1,
        1,
        "23:00" if crossing else "20:00",
        "01:00" if crossing else "23:00",
        ("Fri", "Sat"),
        False,
        True,
        "now",
        "now",
    )
    experiment = PostingWindow(
        2, 1, 2, "06:00", "08:00", ("Fri", "Sat"), True, True, "now", "now"
    )
    return ResolvedPostingSchedule(
        "lol", profile, (primary, experiment), "channel", True
    )


def test_next_window_timezone_preferred_days_and_experimental():
    now = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)  # Thursday, 09:00 São Paulo
    planned = next_publication_time(schedule(), now)
    experimental = next_publication_time(schedule(), now, experimental=True)
    assert planned.isoformat() == "2026-10-23T23:00:00+00:00"
    assert experimental.isoformat() == "2026-10-23T09:00:00+00:00"


def test_cross_midnight_window_is_supported():
    now = datetime(2026, 10, 24, 2, 30, tzinfo=UTC)  # Friday 23:30 local
    planned = next_publication_time(schedule(crossing=True), now)
    assert planned == datetime(2026, 10, 24, 2, 30, tzinfo=UTC)
    after_midnight = datetime(2026, 10, 24, 3, 30, tzinfo=UTC)
    assert (
        next_publication_time(schedule(crossing=True), after_midnight) == after_midnight
    )


def test_daily_limit_and_conflict_move_to_next_slot():
    now = datetime(2026, 10, 23, 22, 0, tzinfo=UTC)
    first = next_publication_time(schedule(), now)
    second = next_publication_time(
        schedule(), now, (first,), max_publications_per_day=1
    )
    assert second.date() > first.date()


def test_fresh_bypasses_window_only_for_freshness_first():
    now = datetime(2026, 10, 22, 12, 1, 20, tzinfo=UTC)
    fresh = next_publication_time(
        schedule(TimingPolicy.FRESHNESS_FIRST), now, urgency=SchedulerUrgency.FRESH
    )
    normal_policy = next_publication_time(
        schedule(TimingPolicy.SCHEDULED), now, urgency=SchedulerUrgency.FRESH
    )
    assert fresh == datetime(2026, 10, 22, 12, 2, tzinfo=UTC)
    assert normal_policy > fresh


class SequenceExecutor:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def execute(self, publication_id):
        self.calls += 1
        outcome = self.outcomes.pop(0) if self.outcomes else SchedulerExecutionResult()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def setup_scheduler(tmp_path, executor=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    publishing, store, channel, _plan, video, _uploader, _connection = (
        configured_service(tmp_path)
    )
    theme_key = publishing.config.themes[0].output_subdirectory
    market = store.theme_market_profile(theme_key, "youtube", "BR", "pt-BR")
    posting = store.save_theme_posting_profile(
        theme_market_profile_id=market.profile_id,
        timezone="America/Sao_Paulo",
        timing_policy=TimingPolicy.SCHEDULED,
    )
    store.replace_posting_windows(
        posting.posting_profile_id,
        (
            (
                1,
                "00:00",
                "23:59",
                ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"),
                False,
                True,
            ),
            (
                2,
                "06:00",
                "08:00",
                ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"),
                True,
                True,
            ),
        ),
    )
    publication = publishing.create_for_review(
        channel.channel_id, video, PublicationMetadata("Scheduled private")
    )
    publishing.approve(publication.publication_id)
    executor = executor or SequenceExecutor()
    scheduler = SchedulerService(publishing.config, store, executor)
    return scheduler, store, channel, publication, executor


def due_job(scheduler, store, publication, now):
    job = scheduler.enqueue(publication.publication_id, now - timedelta(days=1))
    return store.update_scheduler_job(
        job.job_id,
        state=SchedulerJobState.SCHEDULED,
        expected=(SchedulerJobState.SCHEDULED,),
        resolved_publish_at=(now - timedelta(hours=1)).isoformat(),
        event_type="TEST_DUE",
    )


def test_global_and_channel_pause(tmp_path):
    now = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)
    scheduler, store, channel, publication, executor = setup_scheduler(tmp_path)
    due_job(scheduler, store, publication, now)
    store.update_scheduler_settings(global_paused=True)
    assert scheduler.run_once(now) == ()
    store.update_scheduler_settings(global_paused=False)
    store.set_scheduler_channel_paused(channel.channel_id, True)
    assert scheduler.run_once(now) == ()
    assert executor.calls == 0
    store.set_scheduler_channel_paused(channel.channel_id, False)
    assert scheduler.run_once(now)[0].state is SchedulerJobState.COMPLETED
    assert executor.calls == 1


def test_crash_recovery_is_conservative(tmp_path):
    now = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)
    scheduler, store, _channel, publication, _executor = setup_scheduler(tmp_path)
    job = due_job(scheduler, store, publication, now)
    running = store.update_scheduler_job(
        job.job_id,
        state=SchedulerJobState.RUNNING,
        expected=(SchedulerJobState.SCHEDULED,),
    )
    assert scheduler.recover()[0].state is SchedulerJobState.SCHEDULED
    running = store.update_scheduler_job(
        running.job_id,
        state=SchedulerJobState.RUNNING,
        expected=(SchedulerJobState.SCHEDULED,),
        upload_started=True,
    )
    assert scheduler.recover()[0].state is SchedulerJobState.UPLOAD_UNCERTAIN


def test_missed_schedule_threshold(tmp_path):
    now = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)
    scheduler, store, _channel, publication, executor = setup_scheduler(tmp_path)
    due_job(scheduler, store, publication, now)
    completed = scheduler.run_once(now)[0]
    assert completed.state is SchedulerJobState.COMPLETED
    assert executor.calls == 1
    assert "MISSED_EXECUTED" in {
        event.event_type for event in store.scheduler_job_events(completed.job_id)
    }

    # A second isolated fixture proves a miss over six hours is replanned, not run.
    other = tmp_path / "other"
    scheduler2, store2, _channel2, publication2, executor2 = setup_scheduler(other)
    job2 = scheduler2.enqueue(publication2.publication_id, now - timedelta(days=1))
    store2.update_scheduler_job(
        job2.job_id,
        state=SchedulerJobState.SCHEDULED,
        expected=(SchedulerJobState.SCHEDULED,),
        resolved_publish_at=(now - timedelta(hours=7)).isoformat(),
    )
    replanned = scheduler2.run_once(now)[0]
    assert replanned.state is SchedulerJobState.SCHEDULED
    assert datetime.fromisoformat(replanned.resolved_publish_at) > now
    assert executor2.calls == 0


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            SchedulerExecutionError("temporary", recoverable=True),
            SchedulerJobState.FAILED_RECOVERABLE,
        ),
        (
            SchedulerExecutionError("invalid", recoverable=False),
            SchedulerJobState.FAILED_PERMANENT,
        ),
        (
            SchedulerExecutionError("ambiguous", recoverable=False, uncertain=True),
            SchedulerJobState.UPLOAD_UNCERTAIN,
        ),
    ],
)
def test_failure_classification(tmp_path, error, expected):
    now = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)
    executor = SequenceExecutor(error)
    scheduler, store, _channel, publication, _ = setup_scheduler(tmp_path, executor)
    due_job(scheduler, store, publication, now)
    result = scheduler.run_once(now)[0]
    assert result.state is expected
    if expected in {
        SchedulerJobState.FAILED_PERMANENT,
        SchedulerJobState.UPLOAD_UNCERTAIN,
    }:
        assert scheduler.run_once(now) == ()
        assert executor.calls == 1


def test_recoverable_retry_is_limited_and_enqueue_is_idempotent(tmp_path):
    now = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)
    executor = SequenceExecutor(
        SchedulerExecutionError("temporary", recoverable=True),
        SchedulerExecutionResult(),
    )
    scheduler, store, _channel, publication, _ = setup_scheduler(tmp_path, executor)
    first = due_job(scheduler, store, publication, now)
    assert scheduler.enqueue(publication.publication_id, now).job_id == first.job_id
    assert scheduler.run_once(now)[0].state is SchedulerJobState.FAILED_RECOVERABLE
    assert scheduler.run_once(now)[0].state is SchedulerJobState.COMPLETED
    assert store.scheduler_job(first.job_id).attempts == 2


def test_recoverable_retry_stops_at_configured_maximum(tmp_path):
    now = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)
    executor = SequenceExecutor(
        *(SchedulerExecutionError("temporary", recoverable=True) for _ in range(3))
    )
    scheduler, store, _channel, publication, _ = setup_scheduler(tmp_path, executor)
    job = due_job(scheduler, store, publication, now)
    assert scheduler.run_once(now)[0].state is SchedulerJobState.FAILED_RECOVERABLE
    assert scheduler.run_once(now)[0].state is SchedulerJobState.FAILED_RECOVERABLE
    assert scheduler.run_once(now)[0].state is SchedulerJobState.FAILED_PERMANENT
    assert scheduler.run_once(now) == ()
    assert store.scheduler_job(job.job_id).attempts == 3
    assert executor.calls == 3


def test_published_publication_is_never_uploaded_again(tmp_path):
    now = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)
    scheduler, store, _channel, publication, executor = setup_scheduler(tmp_path)
    due_job(scheduler, store, publication, now)
    store.connection.execute(
        "UPDATE publications SET status=?, remote_video_id=? WHERE id=?",
        (PublicationStatus.PUBLISHED, "existing-remote", publication.publication_id),
    )
    assert scheduler.run_once(now)[0].state is SchedulerJobState.COMPLETED
    assert executor.calls == 0


def test_pilot_refuses_public_or_scheduled_publication(tmp_path):
    now = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)
    scheduler, store, _channel, publication, executor = setup_scheduler(tmp_path)
    due_job(scheduler, store, publication, now)
    store.connection.execute(
        "UPDATE publications SET privacy_status='public' WHERE id=?",
        (publication.publication_id,),
    )
    assert scheduler.run_once(now)[0].state is SchedulerJobState.FAILED_PERMANENT
    assert executor.calls == 0


def test_schema_migration_is_idempotent(tmp_path):
    path = tmp_path / "state.db"
    with StateStore(path) as store:
        store.connection.execute("PRAGMA user_version=23")
    with StateStore(path) as store:
        assert store.schema_version == 24
        assert store.scheduler_settings().max_attempts == 3
        assert store.scheduler_jobs() == ()


def test_four_pilot_channels_resolve_distinct_bindings_and_profiles(tmp_path):
    from pathlib import Path

    from autoshorts.automation_config import (
        AutomationConfig,
        GoogleIdentity,
        PublishingChannel,
        ThemeProfile,
    )

    root = Path(__file__).resolve().parents[1]
    rows = (
        ("lol", "Tiltado", "google-a", "UC-A"),
        ("fe", "Sopro Leve", "google-b", "UC-B"),
        ("technology-ai", "PromptLab", "google-c", "UC-C"),
        ("finance-business", "Grana Clara", "google-d", "UC-D"),
    )
    with StateStore(tmp_path / "pilots.db") as store:
        import_editorial_seed(
            root / "config" / "editorial-sources.br-pt-BR.seed.json", store
        )
        import_posting_seed(
            root / "config" / "posting-schedules.br-pt-BR.seed.json", store
        )
        identities = tuple(
            GoogleIdentity(identity, identity, f"{identity}@test")
            for _, _, identity, _ in rows
        )
        themes = []
        for slot, (theme_key, name, identity, remote_id) in enumerate(rows, 1):
            channel = PublishingChannel(
                f"channel-{slot}", name, 1, google_identity_id=identity
            )
            themes.append(ThemeProfile(name, "", (), theme_key, channels=(channel,)))
            store.save_channel_binding(
                ChannelCredentialBinding(
                    channel.channel_id,
                    identity,
                    "youtube",
                    f"{channel.channel_id}.json",
                    remote_id,
                    name,
                    "created",
                    "verified",
                    "CONNECTED",
                )
            )
        config = AutomationConfig(tuple(themes), identities)
        scheduler = SchedulerService(config, store, SequenceExecutor())
        now = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)
        resolved = [
            scheduler.plan_channel(theme.channels[0].channel_id, now)[0]
            for theme in config.themes
        ]
        assert {item.theme_key for item in resolved} == {
            "lol",
            "fe",
            "technology-ai",
            "finance-business",
        }
        assert all(
            store.get_channel_binding(theme.channels[0].channel_id).status
            == "CONNECTED"
            for theme in config.themes
        )
