from __future__ import annotations

from pathlib import Path

from autoshorts.editorial_seed import import_editorial_seed
from autoshorts.editorial_sources import (
    SourceRightsStatus,
    SourceValidationStatus,
)
from autoshorts.state_store import SCHEMA_VERSION, StateStore


def test_market_profiles_allow_two_markets_for_same_theme(tmp_path):
    with StateStore(tmp_path / "state.db") as store:
        br = store.save_theme_market_profile(
            theme_key="lol",
            platform="youtube",
            region_code="BR",
            language_code="pt-BR",
            youtube_category_id="20",
            youtube_category_name="Gaming",
            is_default_for_theme=True,
        )
        us = store.save_theme_market_profile(
            theme_key="lol",
            platform="youtube",
            region_code="US",
            language_code="en-US",
            youtube_category_id="20",
            youtube_category_name="Gaming",
        )

        assert br.profile_id != us.profile_id
        assert len(store.theme_market_profiles()) == 2


def test_five_validated_sources_and_reuse_across_profiles(tmp_path):
    with StateStore(tmp_path / "state.db") as store:
        first = store.save_theme_market_profile(
            theme_key="lol",
            platform="youtube",
            region_code="BR",
            language_code="pt-BR",
            youtube_category_id="20",
            youtube_category_name="Gaming",
            is_default_for_theme=True,
        )
        second = store.save_theme_market_profile(
            theme_key="mine",
            platform="youtube",
            region_code="BR",
            language_code="pt-BR",
            youtube_category_id="20",
            youtube_category_name="Gaming",
            is_default_for_theme=True,
        )
        sources = []
        for priority in range(1, 6):
            source = store.save_content_source(
                platform="youtube",
                external_channel_id=f"UC{priority:022d}",
                display_name=f"Creator {priority}",
                handle=f"@creator{priority}",
                channel_url=f"https://youtube.com/channel/UC{priority:022d}",
                region_code="BR",
                language_code="pt-BR",
                validation_status=SourceValidationStatus.VALIDATED,
                rights_status=SourceRightsStatus.UNKNOWN,
            )
            sources.append(source)
            association = store.link_theme_content_source(
                profile_id=first.profile_id,
                source_id=source.source_id,
                priority=priority,
            )
            assert association.target_duration_min_seconds == 900
            assert association.target_duration_max_seconds == 2400
        store.link_theme_content_source(
            profile_id=second.profile_id, source_id=sources[0].source_id, priority=1
        )

        assert len(store.content_sources()) == 5
        assert len(store.theme_content_sources(first.profile_id)) == 5
        assert (
            store.theme_content_sources(second.profile_id)[0][1].source_id
            == sources[0].source_id
        )
        assert all(
            source.validation_status is SourceValidationStatus.VALIDATED
            and source.rights_status is SourceRightsStatus.UNKNOWN
            for _, source in store.theme_content_sources(first.profile_id)
        )


def test_channel_id_is_unique_and_migration_preserves_existing_data(tmp_path):
    path = tmp_path / "state.db"
    with StateStore(path) as store:
        store.register_video(
            youtube_id="existing",
            source_url="https://youtube.test/existing",
            creator_name="Creator",
            theme_name="LoL",
            title="Existing",
        )
        store.connection.execute("PRAGMA user_version = 20")
    with StateStore(path) as store:
        assert store.schema_version == SCHEMA_VERSION
        assert store.get_video("existing") is not None
        one = store.save_content_source(
            platform="youtube",
            external_channel_id="UCunique",
            display_name="First",
            handle=None,
            channel_url="https://youtube.com/channel/UCunique",
            region_code="BR",
            language_code="pt-BR",
        )
        two = store.save_content_source(
            platform="youtube",
            external_channel_id="UCunique",
            display_name="Updated",
            handle="@updated",
            channel_url="https://youtube.com/channel/UCunique",
            region_code="BR",
            language_code="pt-BR",
        )
        assert one.source_id == two.source_id
        assert len(store.content_sources()) == 1


def test_reviewed_br_seed_has_twenty_profiles_and_one_hundred_links(tmp_path):
    seed = (
        Path(__file__).resolve().parents[1]
        / "config"
        / "editorial-sources.br-pt-BR.seed.json"
    )
    with StateStore(tmp_path / "state.db") as store:
        assert import_editorial_seed(seed, store) == (20, 100)
        profiles = store.theme_market_profiles()
        sources = store.content_sources()
        assert len(profiles) == 20
        assert len(sources) == 100
        assert all(
            len(store.theme_content_sources(item.profile_id)) == 5 for item in profiles
        )
        assert all(
            item.validation_status is SourceValidationStatus.VALIDATED
            for item in sources
        )
        assert all(item.rights_status is SourceRightsStatus.UNKNOWN for item in sources)
