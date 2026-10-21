from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import autoshorts.orchestrator as orchestrator_module
from autoshorts.automation_config import (
    AutomationConfig,
    GoogleIdentity,
    PublishingChannel,
    ThemeProfile,
)
from autoshorts.orchestrator import (
    ChannelContext,
    ChannelInventory,
    InventoryItem,
    Orchestrator,
    PipelineChannelProducer,
    ProductionOutcome,
    get_channel_inventory,
)
from autoshorts.state_store import MetadataDraftStatus, ReviewPolicy, StateStore


def setup_orchestrator(tmp_path, *, channels=1, producer=None):
    configured = tuple(
        PublishingChannel(f"channel-{index}", f"Channel {index}", index)
        for index in range(1, channels + 1)
    )
    theme = ThemeProfile("Theme", "Prompt", (), "theme", channels=configured)
    config = AutomationConfig((theme,), (GoogleIdentity("google", "G", "g@x"),))
    state = StateStore(tmp_path / "state.db")
    state.get_channel_binding = lambda _id: SimpleNamespace(status="CONNECTED")
    state.get_approved_brand_kit = lambda _id: SimpleNamespace(source_plan_id="p")
    worker = producer or FakeProducer()
    return state, Orchestrator(config, state, worker), worker


class FakeProducer:
    def __init__(self, failures=()):
        self.calls = []
        self.failures = set(failures)

    def produce(self, context):
        self.calls.append(context.channel.channel_id)
        if context.channel.channel_id in self.failures:
            raise RuntimeError("recoverable failure")
        return ProductionOutcome(
            f"source-{context.channel.channel_id}", 1, "REVIEW_REQUIRED"
        )


@pytest.mark.parametrize(
    ("inventory", "deficit", "action"),
    ((3, 0, "NONE"), (2, 0, "NONE"), (1, 2, "PRODUCE"), (0, 3, "PRODUCE")),
)
def test_inventory_policy_thresholds(tmp_path, monkeypatch, inventory, deficit, action):
    state, orchestrator, _ = setup_orchestrator(tmp_path)
    items = tuple(
        InventoryItem(str(index), "READY", str(index), Path("/tmp/item"))
        for index in range(inventory)
    )
    monkeypatch.setattr(
        orchestrator_module,
        "get_channel_inventory",
        lambda _state, channel_id: ChannelInventory(channel_id, items),
    )
    plan = orchestrator.inspect_channel(orchestrator.active_channels()[0])
    assert (plan.deficit, plan.action) == (deficit, action)
    state.close()


def test_scheduled_publication_counts_once_and_published_does_not(tmp_path):
    state, _orchestrator, _ = setup_orchestrator(tmp_path)
    media = tmp_path / "video.mp4"
    media.touch()
    state.connection.commit()
    state.connection.execute("PRAGMA foreign_keys=OFF")
    values = (
        "key-1", "plan", "channel-1", "google", "UC", str(media), "sha",
        "title", "description", "[]", "private", 0, 0, 0, "20", "Gaming",
        "pt-BR", "youtube", 1, 1, "APPROVED_FOR_PUBLISH", "now", "now",
    )
    state.connection.execute(
        """INSERT INTO publications
        (idempotency_key, brand_plan_id, publishing_channel_id,
         google_identity_id, youtube_channel_id, video_path, video_checksum,
         title, description, tags_json, privacy_status, made_for_kids,
         contains_synthetic_media, has_paid_product_placement, category_id,
         category_name, default_language, license, embeddable,
         public_stats_viewable, status, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        values,
    )
    state.connection.execute(
        """INSERT INTO publications
        (idempotency_key, brand_plan_id, publishing_channel_id,
         google_identity_id, youtube_channel_id, video_path, video_checksum,
         title, description, tags_json, privacy_status, made_for_kids,
         contains_synthetic_media, has_paid_product_placement, category_id,
         category_name, default_language, license, embeddable,
         public_stats_viewable, status, remote_video_id, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "key-2",
            *values[1:6],
            "old-sha",
            *values[7:20],
            "PUBLISHED",
            "remote",
            "now",
            "now",
        ),
    )
    state.connection.commit()
    inventory = get_channel_inventory(state, "channel-1")
    assert inventory.count == 1
    assert inventory.items[0].stage == "PUBLICATION_APPROVED_FOR_PUBLISH"
    state.close()


def test_disabled_and_paused_channels_are_respected(tmp_path):
    state, orchestrator, _ = setup_orchestrator(tmp_path, channels=2)
    disabled = orchestrator.config.themes[0].channels[1]
    object.__setattr__(disabled, "enabled", False)
    assert len(orchestrator.active_channels()) == 1
    channel = orchestrator.active_channels()[0].channel
    state.set_scheduler_channel_paused(channel.channel_id, True)
    plan = orchestrator.inspect_channel(orchestrator.active_channels()[0])
    assert plan.action == "PAUSED"
    state.close()


def test_failure_does_not_stop_next_channel_and_max_is_respected(tmp_path):
    worker = FakeProducer(("channel-1",))
    state, orchestrator, _ = setup_orchestrator(
        tmp_path, channels=2, producer=worker
    )
    result = orchestrator.run_cycle(max_productions=2)
    assert result.productions_started == 2
    assert len(result.errors) == 1
    assert worker.calls == ["channel-1", "channel-2"]
    state.close()


def test_failed_channel_enters_persistent_backoff(tmp_path):
    worker = FakeProducer(("channel-1",))
    state, orchestrator, _ = setup_orchestrator(tmp_path, producer=worker)
    orchestrator.run_cycle(max_productions=1)
    repeated = orchestrator.run_cycle(max_productions=1)
    assert repeated.productions_started == 0
    assert worker.calls == ["channel-1"]
    result = state.connection.execute(
        "SELECT result FROM orchestrator_decisions ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]
    assert result == "BACKOFF"
    state.close()


def test_never_attempted_channel_precedes_recently_attempted_channel(tmp_path):
    state, orchestrator, worker = setup_orchestrator(tmp_path, channels=3)
    first = orchestrator.run_cycle(max_productions=1)
    second = orchestrator.run_cycle(max_productions=1)
    third = orchestrator.run_cycle(max_productions=1)

    assert first.productions_started == 1
    assert second.productions_started == 1
    assert third.productions_started == 1
    assert worker.calls == ["channel-1", "channel-2", "channel-3"]
    state.close()


def test_oldest_attempt_wins_after_every_channel_had_an_opportunity(tmp_path):
    state, orchestrator, worker = setup_orchestrator(tmp_path, channels=2)
    orchestrator.run_cycle(max_productions=1)
    orchestrator.run_cycle(max_productions=1)
    orchestrator.run_cycle(max_productions=1)

    assert worker.calls == ["channel-1", "channel-2", "channel-1"]
    state.close()


def test_dry_run_has_no_side_effects_and_limit_is_zero(tmp_path):
    state, orchestrator, worker = setup_orchestrator(tmp_path)
    before = state.connection.total_changes
    result = orchestrator.run_cycle(dry_run=True, max_productions=1)
    assert result.productions_started == 0
    assert worker.calls == []
    assert state.connection.total_changes == before
    assert state.connection.execute(
        "SELECT count(*) FROM orchestrator_cycles"
    ).fetchone()[0] == 0
    state.close()


def test_lease_prevents_duplicate_work_and_expires(tmp_path):
    state, _orchestrator, _ = setup_orchestrator(tmp_path)
    now = datetime.now(UTC)
    assert state.acquire_orchestrator_lease("channel-1", "one", now, 30)
    assert not state.acquire_orchestrator_lease("channel-1", "two", now, 30)
    assert state.acquire_orchestrator_lease(
        "channel-1", "two", now + timedelta(seconds=31), 30
    )
    state.close()


def test_inventory_policy_is_configurable_per_channel(tmp_path):
    state, _orchestrator, _ = setup_orchestrator(tmp_path)
    state.update_inventory_policy(min_ready_videos=4, target_ready_videos=6)
    state.update_inventory_policy(
        min_ready_videos=1,
        target_ready_videos=2,
        publishing_channel_id="channel-1",
    )
    assert state.inventory_policy("channel-1").target_ready_videos == 2
    assert state.inventory_policy("channel-2").target_ready_videos == 6
    state.close()


@pytest.mark.parametrize(
    ("policy", "expected", "approvals"),
    (
        (ReviewPolicy.MANUAL_REVIEW, "REVIEW_REQUIRED", 0),
        (ReviewPolicy.AUTO_APPROVE, "APPROVED", 1),
    ),
)
def test_pipeline_producer_respects_review_policy(
    tmp_path, monkeypatch, policy, expected, approvals
):
    channel = PublishingChannel("channel-1", "Channel", 1)
    theme = ThemeProfile("Theme", "Prompt", (), "theme", channels=(channel,))
    config = AutomationConfig((theme,))
    state = SimpleNamespace(
        theme_market_profiles=lambda: (
            SimpleNamespace(
                theme_key="theme",
                platform="youtube",
                enabled=True,
                is_default_for_theme=True,
            ),
        ),
        assign_source_to_channel=lambda *_args: None,
    )
    selected = SimpleNamespace(external_video_id="source-1")
    monkeypatch.setattr(
        orchestrator_module,
        "discover_next_source_video",
        lambda *_args, **_kwargs: SimpleNamespace(selected=selected),
    )
    monkeypatch.setattr(orchestrator_module, "ingest", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        orchestrator_module, "produce_source_video", lambda **kwargs: None
    )
    draft = SimpleNamespace(
        draft_id=8,
        status=MetadataDraftStatus.REVIEW_REQUIRED,
        review_policy=policy,
    )
    monkeypatch.setattr(
        orchestrator_module, "generate_metadata_draft", lambda **kwargs: draft
    )
    approval_calls = []

    def approve(*args, **kwargs):
        approval_calls.append(1)
        return (
            SimpleNamespace(draft_id=8, status=MetadataDraftStatus.APPROVED),
            object(),
        )

    monkeypatch.setattr(orchestrator_module, "approve_and_enqueue_metadata", approve)
    monkeypatch.setattr(
        orchestrator_module, "YouTubePublicationService", lambda *args: object()
    )
    monkeypatch.setattr(orchestrator_module, "SchedulerService", lambda *args: object())
    producer = PipelineChannelProducer(
        config,
        state,
        SimpleNamespace(ffprobe_path="ffprobe", ffmpeg_path="ffmpeg"),
        tmp_path,
    )
    result = producer.produce(ChannelContext(theme, channel))
    assert result.result == expected
    assert len(approval_calls) == approvals
