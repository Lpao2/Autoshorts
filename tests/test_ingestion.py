from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from autoshorts.collection import VideoCandidate
from autoshorts.editorial_sources import (
    SourceRightsStatus,
    SourceValidationStatus,
)
from autoshorts.ingestion import discover_next_source_video, ingest
from autoshorts.media_probe import MediaInfo, MediaProbeError
from autoshorts.state_store import StateStore, VideoStatus


def configured_profile(store, *, region="BR", language="pt-BR"):
    profile = store.save_theme_market_profile(
        theme_key="lol",
        platform="youtube",
        region_code=region,
        language_code=language,
        youtube_category_id="20",
        youtube_category_name="Gaming",
        is_default_for_theme=region == "BR",
    )
    for priority in range(1, 6):
        source = store.save_content_source(
            platform="youtube",
            external_channel_id=f"UC{region}{priority}",
            display_name=f"Creator {priority}",
            handle=f"@creator{priority}",
            channel_url=f"https://youtube.test/UC{region}{priority}",
            region_code=region,
            language_code=language,
            validation_status=SourceValidationStatus.VALIDATED,
            rights_status=SourceRightsStatus.UNKNOWN,
            enabled=priority != 5,
        )
        store.link_theme_content_source(
            profile_id=profile.profile_id,
            source_id=source.source_id,
            priority=priority,
            enabled=priority != 4,
        )
    return profile


def candidate(creator, video_id, duration, published=date(2026, 10, 1)):
    return VideoCandidate(
        creator,
        f"https://youtube.com/watch?v={video_id}",
        f"Title {video_id}",
        published,
        duration,
        video_id,
    )


def test_discovery_reads_only_enabled_validated_sources_and_filters(tmp_path):
    with StateStore(tmp_path / "state.db") as store:
        profile = configured_profile(store)
        store.register_video(
            youtube_id="seen",
            source_url="https://youtube.com/watch?v=seen",
            creator_name="Old",
            theme_name="lol",
            title="Seen",
        )
        responses = {
            "Creator 1": [candidate("Creator 1", "short", 120)],
            "Creator 2": [
                candidate("Creator 2", "seen", 1200),
                candidate("Creator 2", "too-long", 2500),
                candidate("Creator 2", "valid", 1800),
            ],
            "Creator 3": [candidate("Creator 3", "lower-priority", 1800)],
        }
        called = []

        urls = []

        def discover(name, url, **_kwargs):
            called.append(name)
            urls.append(url)
            return responses[name]

        report = discover_next_source_video(profile, store, discover=discover)

        assert called == ["Creator 1", "Creator 2", "Creator 3"]
        assert all(url.endswith("/videos") for url in urls)
        assert report.selected.external_video_id == "valid"
        assert {item.reason for item in report.skipped} == {
            "shorts",
            "already_processed",
            "duration",
        }


def test_selection_is_priority_then_recency_and_market_is_not_hardcoded(tmp_path):
    with StateStore(tmp_path / "state.db") as store:
        profile = configured_profile(store, region="US", language="en-US")

        def discover(name, _url, **_kwargs):
            number = int(name.rsplit(" ", 1)[1])
            return [
                candidate(
                    name,
                    f"video-{number}",
                    1500,
                    date(2026, 10, 20 - number),
                )
            ]

        report = discover_next_source_video(profile, store, discover=discover)
        assert report.profile.region_code == "US"
        assert report.profile.language_code == "en-US"
        assert report.selected.source.display_name == "Creator 1"


def test_ingest_persists_validated_download_and_second_run_excludes_it(
    tmp_path, monkeypatch
):
    with StateStore(tmp_path / "state.db") as store:
        profile = configured_profile(store)
        source = store.theme_content_sources(profile.profile_id)[0][1]
        selected = candidate(source.display_name, "downloaded", 1200)

        def discover(_name, _url, **_kwargs):
            return [selected]

        report = discover_next_source_video(profile, store, discover=discover)

        def download(_candidate, destination, **_kwargs):
            path = Path(destination) / "downloaded.mp4"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"valid-media")
            return path

        def probe(path, _ffprobe):
            return MediaInfo(path.resolve(), 1200.4, 1920, 1080, 30.0, True, 0)

        monkeypatch.setattr("autoshorts.ingestion._decode", lambda *_args: None)
        result = ingest(
            report.selected,
            profile,
            store,
            workspace=tmp_path / "automation",
            download=download,
            probe=probe,
        )
        record = store.get_video("downloaded")
        assert result.local_path.is_file()
        assert record.status is VideoStatus.DOWNLOADED
        assert record.local_path == result.local_path
        assert record.checksum == result.checksum
        assert record.content_source_id == source.source_id
        assert record.validated_at

        second = discover_next_source_video(profile, store, discover=discover)
        assert second.selected is None
        assert second.skipped[0].reason == "already_processed"


@pytest.mark.parametrize(
    "failure", [RuntimeError("download"), MediaProbeError("probe")]
)
def test_ingest_failure_is_persisted_and_partials_are_removed(
    tmp_path, monkeypatch, failure
):
    with StateStore(tmp_path / "state.db") as store:
        profile = configured_profile(store)
        source = store.theme_content_sources(profile.profile_id)[0][1]
        video = candidate(source.display_name, "broken", 1200)
        report = discover_next_source_video(
            profile, store, discover=lambda *_args, **_kwargs: [video]
        )

        def download(_candidate, destination, **_kwargs):
            directory = Path(destination)
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "broken.mp4.part").write_bytes(b"partial")
            if str(failure) == "download":
                raise failure
            path = directory / "broken.mp4"
            path.write_bytes(b"bad")
            return path

        def probe(_path, _ffprobe):
            raise failure

        monkeypatch.setattr("autoshorts.ingestion._decode", lambda *_args: None)
        with pytest.raises(type(failure)):
            ingest(
                report.selected,
                profile,
                store,
                workspace=tmp_path / "automation",
                download=download,
                probe=probe,
            )
        assert store.get_video("broken").status is VideoStatus.FAILED
        assert not list((tmp_path / "automation").rglob("*.part"))


def test_unique_external_video_id_is_global(tmp_path):
    with StateStore(tmp_path / "state.db") as store:
        first = store.register_video(
            youtube_id="same",
            source_url="https://youtube.com/watch?v=same",
            creator_name="One",
            theme_name="lol",
            title="One",
        )
        second = store.register_video(
            youtube_id="same",
            source_url="https://youtube.com/watch?v=same",
            creator_name="Two",
            theme_name="lol",
            title="Two",
        )
        assert first is True
        assert second is False
        assert store.connection.execute(
            "SELECT COUNT(*) FROM videos WHERE youtube_id='same'"
        ).fetchone()[0] == 1


def test_candidate_cooldown_skips_blocked_and_selects_fallback(tmp_path):
    with StateStore(tmp_path / "state.db") as store:
        profile = configured_profile(store)
        store.record_source_failure(
            profile.profile_id,
            "blocked",
            category="RATE_LIMITED",
            detail="limited",
            now=datetime.now(UTC),
        )

        def discover(name, _url, **_kwargs):
            if name == "Creator 1":
                return [
                    candidate(name, "blocked", 1200),
                    candidate(name, "fallback", 1200),
                ]
            return []

        report = discover_next_source_video(profile, store, discover=discover)
        assert report.selected.external_video_id == "fallback"
        assert any(item.reason == "cooldown" for item in report.skipped)


def test_persistent_rate_limit_backoff_increases(tmp_path):
    with StateStore(tmp_path / "state.db") as store:
        profile = configured_profile(store)
        now = datetime(2026, 10, 21, tzinfo=UTC)
        first = store.record_source_failure(
            profile.profile_id,
            "video",
            category="RATE_LIMITED",
            detail="limited",
            now=now,
        )
        assert not store.source_candidate_available(
            profile.profile_id, "video", now=now
        )
        second = store.record_source_failure(
            profile.profile_id,
            "video",
            category="RATE_LIMITED",
            detail="limited",
            now=now,
        )
        assert datetime.fromisoformat(second) > datetime.fromisoformat(first)
