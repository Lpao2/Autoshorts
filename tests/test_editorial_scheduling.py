from __future__ import annotations

from pathlib import Path

import pytest

from autoshorts.automation_config import (
    AutomationConfig,
    PublishingChannel,
    ThemeProfile,
)
from autoshorts.editorial_scheduling import resolve_channel_posting_schedule
from autoshorts.editorial_seed import import_editorial_seed, import_posting_seed
from autoshorts.editorial_sources import (
    PostingConfidence,
    PostingStrategy,
    TimingPolicy,
)
from autoshorts.state_store import SCHEMA_VERSION, StateStore

ROOT = Path(__file__).resolve().parents[1]
EDITORIAL_SEED = ROOT / "config" / "editorial-sources.br-pt-BR.seed.json"
POSTING_SEED = ROOT / "config" / "posting-schedules.br-pt-BR.seed.json"


def seeded_store(tmp_path):
    store = StateStore(tmp_path / "state.db")
    import_editorial_seed(EDITORIAL_SEED, store)
    import_posting_seed(POSTING_SEED, store)
    return store


def test_seed_has_twenty_profiles_and_two_windows_each(tmp_path):
    with seeded_store(tmp_path) as store:
        profiles = store.theme_posting_profiles()
        assert len(profiles) == 20
        assert all(item.timezone == "America/Sao_Paulo" for item in profiles)
        assert all(item.strategy is PostingStrategy.SEEDED for item in profiles)
        assert all(item.confidence is PostingConfidence.MEDIUM for item in profiles)
        assert all(
            [
                window.priority
                for window in store.posting_windows(item.posting_profile_id)
            ]
            == [1, 2]
            for item in profiles
        )
        assert (
            sum(
                len(store.posting_windows(item.posting_profile_id)) for item in profiles
            )
            == 40
        )


def test_future_us_market_can_use_another_timezone_and_cross_midnight(tmp_path):
    with StateStore(tmp_path / "state.db") as store:
        market = store.save_theme_market_profile(
            theme_key="lol",
            platform="youtube",
            region_code="US",
            language_code="en-US",
            youtube_category_id="20",
            youtube_category_name="Gaming",
        )
        posting = store.save_theme_posting_profile(
            theme_market_profile_id=market.profile_id,
            timezone="America/New_York",
            timing_policy=TimingPolicy.SCHEDULED,
        )
        windows = store.replace_posting_windows(
            posting.posting_profile_id,
            ((1, "23:00", "01:30", ("Fri", "Sat"), False, True),),
        )
        assert windows[0].start_local_time == "23:00"
        assert windows[0].end_local_time == "01:30"


def test_channel_inherits_and_override_does_not_duplicate_profile(tmp_path):
    with seeded_store(tmp_path) as store:
        channel = PublishingChannel("channel-a", "Channel A", 1)
        config = AutomationConfig(
            (ThemeProfile("LoL", "", (), "lol", channels=(channel,)),)
        )
        inherited = resolve_channel_posting_schedule(config, store, channel.channel_id)
        assert inherited.inherited is True
        assert inherited.posting_profile.timezone == "America/Sao_Paulo"
        count = len(store.theme_posting_profiles())
        store.save_channel_posting_override(
            publishing_channel_id=channel.channel_id,
            posting_profile_id=inherited.posting_profile.posting_profile_id,
            timezone="America/Manaus",
            timing_policy=TimingPolicy.HYBRID,
        )
        overridden = resolve_channel_posting_schedule(config, store, channel.channel_id)
        assert overridden.inherited is False
        assert overridden.posting_profile.timezone == "America/Manaus"
        assert overridden.posting_profile.timing_policy is TimingPolicy.HYBRID
        assert len(store.theme_posting_profiles()) == count


def test_timing_policy_values_are_closed():
    assert {item.value for item in TimingPolicy} == {
        "SCHEDULED",
        "HYBRID",
        "FRESHNESS_FIRST",
    }
    with pytest.raises(ValueError):
        TimingPolicy("INVALID")


def test_posting_migration_and_seed_are_idempotent(tmp_path):
    path = tmp_path / "state.db"
    with seeded_store(tmp_path) as store:
        store.connection.execute("PRAGMA user_version = 22")
    with StateStore(path) as store:
        assert store.schema_version == SCHEMA_VERSION
        assert import_posting_seed(POSTING_SEED, store) == (20, 40)
        assert len(store.theme_posting_profiles()) == 20
        assert (
            store.connection.execute("SELECT COUNT(*) FROM posting_windows").fetchone()[
                0
            ]
            == 40
        )
