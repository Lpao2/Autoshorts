import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from test_ingestion import candidate, configured_profile
from test_youtube_collection import install_fake_ytdlp

from autoshorts.collection import (
    YouTubeAccess,
    YouTubeCollectionError,
    YouTubeErrorCategory,
)
from autoshorts.ingestion import discover_next_source_video, ingest_with_replacement
from autoshorts.source_fallback import (
    GeminiSourceAdvisor,
    channel_url,
    extend_sources,
    inspect_channel,
)
from autoshorts.state_store import SCHEMA_VERSION, StateStore


@pytest.mark.parametrize("category", ["AUTH_REQUIRED", "VIDEO_UNAVAILABLE"])
def test_discovery_skips_blocked_channel_without_losing_other_sources(
    tmp_path, category
):
    with StateStore(tmp_path / "db") as state:
        profile = configured_profile(state)

        def discover(name, *_args, **_kwargs):
            if name == "Creator 1":
                raise YouTubeCollectionError(YouTubeErrorCategory(category), "blocked")
            return [candidate(name, name[-1], 1200)]

        report = discover_next_source_video(profile, state, discover=discover)
        assert report.selected.source.display_name == "Creator 2"
        assert report.skipped[0].reason == category


@pytest.mark.parametrize("category", ["AUTH_REQUIRED", "VIDEO_UNAVAILABLE"])
def test_download_restriction_selects_next_candidate_same_theme(tmp_path, category):
    with StateStore(tmp_path / "db") as state:
        profile = configured_profile(state)
        items = [SimpleNamespace(external_video_id=x) for x in ["blocked", "ok"]]
        calls = []

        def download(item, *_args, **_kwargs):
            calls.append(item.external_video_id)
            if item.external_video_id == "blocked":
                raise YouTubeCollectionError(YouTubeErrorCategory(category), "blocked")

        def fallback(*_args, **_kwargs):
            pytest.fail("Gemini should not run while approved sources remain")

        kwargs = dict(
            workspace=tmp_path,
            theme_prompt="LoL",
            maximum_age_days=3650,
            access=YouTubeAccess(),
            ingest_candidate=download,
            discover=lambda *a, **k: SimpleNamespace(eligible=items),
            fallback=fallback,
        )
        assert ingest_with_replacement(profile, state, **kwargs) == "ok"
        assert calls == ["blocked", "ok"]
        assert not state.source_candidate_available(profile.profile_id, "blocked")
        calls.clear()
        assert ingest_with_replacement(profile, state, **kwargs) == "ok"
        assert calls == ["ok"]


@pytest.mark.parametrize(
    "category", ["ANTI_BOT_CHALLENGE", "RATE_LIMITED", "NETWORK_ERROR"]
)
def test_global_download_failures_do_not_rotate_or_call_gemini(tmp_path, category):
    with StateStore(tmp_path / "db") as state:
        profile = configured_profile(state)
        calls = []

        def download(item, *_args, **_kwargs):
            calls.append(item.external_video_id)
            raise YouTubeCollectionError(YouTubeErrorCategory(category), category)

        with pytest.raises(YouTubeCollectionError):
            ingest_with_replacement(
                profile,
                state,
                workspace=tmp_path,
                theme_prompt="LoL",
                maximum_age_days=3650,
                access=YouTubeAccess(),
                ingest_candidate=download,
                discover=lambda *a, **k: SimpleNamespace(
                    eligible=[
                        SimpleNamespace(external_video_id="one"),
                        SimpleNamespace(external_video_id="two"),
                    ]
                ),
                fallback=lambda *a, **k: pytest.fail("Unexpected Gemini"),
            )
        assert calls == ["one"]


def test_fallback_runs_only_after_exhaustion_and_is_not_recursive(tmp_path):
    with StateStore(tmp_path / "db") as state:
        profile = configured_profile(state)
        phases = []

        def discover(*_a, **_k):
            phases.append("discover")
            return SimpleNamespace(eligible=[])

        def fallback(*_a, **kwargs):
            phases.append("gemini")
            assert kwargs["theme_prompt"] == "LoL gameplay"
            return True

        with pytest.raises(RuntimeError, match="No eligible source"):
            ingest_with_replacement(
                profile,
                state,
                workspace=tmp_path,
                theme_prompt="LoL gameplay",
                maximum_age_days=3650,
                access=YouTubeAccess(),
                discover=discover,
                fallback=fallback,
            )
        assert phases == ["discover", "gemini", "discover"]


def test_download_attempt_budget_is_bounded(tmp_path):
    with StateStore(tmp_path / "db") as state:
        profile = configured_profile(state)
        calls = []

        def download(item, *_a, **_k):
            calls.append(item.external_video_id)
            raise YouTubeCollectionError(YouTubeErrorCategory.AUTH_REQUIRED, "age")

        with pytest.raises(RuntimeError, match="budget exhausted"):
            ingest_with_replacement(
                profile,
                state,
                workspace=tmp_path,
                theme_prompt="LoL",
                maximum_age_days=3650,
                access=YouTubeAccess(),
                max_attempts=2,
                ingest_candidate=download,
                discover=lambda *a, **k: SimpleNamespace(
                    eligible=[
                        SimpleNamespace(external_video_id=str(i)) for i in range(8)
                    ]
                ),
            )
        assert calls == ["0", "1"]


class Advisor:
    usage = []

    def __init__(self, urls=None, relevant=True):
        self.urls = urls or ["https://www.youtube.com/@NewCreator"]
        self.relevant = relevant
        self.calls = 0

    def suggest(self, context):
        self.calls += 1
        assert context["theme"] == "lol"
        return self.urls

    def assess(self, context, evidence):
        assert context["language"] == "pt-BR"
        assert evidence["samples"]
        return self.relevant


def evidence(_url, _access):
    return {
        "channel_id": "UC" + "x" * 22,
        "name": "New Creator",
        "samples": [
            {"id": "1", "title": "LoL", "duration": 1200},
            {"id": "2", "title": "LoL", "duration": 1300},
        ],
    }


def test_new_source_validated_audited_and_cooldown_survives_restart(tmp_path):
    path = tmp_path / "db"
    now = datetime.now(UTC)
    advisor = Advisor()
    with StateStore(path) as state:
        profile = configured_profile(state)
        original = state.theme_content_sources(profile.profile_id)
        assert extend_sources(
            profile,
            state,
            theme_prompt="LoL",
            access=YouTubeAccess(),
            advisor=advisor,
            inspect=evidence,
            now=now,
        )
        sources = state.theme_content_sources(profile.profile_id)
        assert len(sources) == len(original)
        assert sources[:4] == original[:4]
        assert state.content_source("youtube", original[-1][1].external_channel_id)
        assert sources[-1][1].source_kind == "GEMINI_VALIDATED_FALLBACK"
        assert sources[-1][1].rights_status == "UNKNOWN"
        row = state.connection.execute(
            "select * from editorial_source_fallbacks"
        ).fetchone()
        assert row["status"] == "ACCEPTED"
        assert json.loads(row["audit_json"])["proposals"][0]["evidence"]
    with StateStore(path) as state:
        assert not extend_sources(
            profile,
            state,
            theme_prompt="LoL",
            access=YouTubeAccess(),
            advisor=advisor,
            inspect=evidence,
            now=now + timedelta(hours=23),
        )
        assert advisor.calls == 1
        assert state.schema_version == SCHEMA_VERSION


@pytest.mark.parametrize("relevant", [True, False])
def test_rejected_or_existing_source_does_not_modify_approved_sources(
    tmp_path, relevant
):
    with StateStore(tmp_path / "db") as state:
        profile = configured_profile(state)
        before = state.content_sources()

        def inspect(url, access):
            result = evidence(url, access)
            if relevant:
                result["channel_id"] = before[0].external_channel_id
            return result

        assert not extend_sources(
            profile,
            state,
            theme_prompt="LoL",
            access=YouTubeAccess(),
            advisor=Advisor(relevant=relevant),
            inspect=inspect,
        )
        assert state.content_sources() == before


@pytest.mark.parametrize(
    "url",
    [
        "http://youtube.com/@name",
        "https://evil.test/@name",
        "https://youtube.com/watch?v=123",
        "file:///tmp/a",
        "https://youtube.com/@name?x=1",
    ],
)
def test_untrusted_urls_never_reach_network(url):
    with pytest.raises(ValueError):
        channel_url(url)


def test_gemini_failure_is_audited_and_cools_down(tmp_path):
    with StateStore(tmp_path / "db") as state:
        profile = configured_profile(state)
        advisor = Advisor()

        def fail(_context):
            raise RuntimeError("Gemini unavailable")

        advisor.suggest = fail
        with pytest.raises(RuntimeError):
            extend_sources(
                profile,
                state,
                theme_prompt="LoL",
                access=YouTubeAccess(),
                advisor=advisor,
            )
        assert (
            state.connection.execute(
                "select status from editorial_source_fallbacks"
            ).fetchone()[0]
            == "FAILED_RECOVERABLE"
        )
        assert not extend_sources(
            profile, state, theme_prompt="LoL", access=YouTubeAccess(), advisor=advisor
        )


def test_migration_from_schema_30_preserves_existing_records(tmp_path):
    with StateStore(tmp_path / "db") as state:
        profile = configured_profile(state)
        state.connection.execute("DROP TABLE editorial_source_fallbacks")
        state.connection.execute("PRAGMA user_version=30")
    with StateStore(tmp_path / "db") as state:
        assert state.schema_version == 31
        assert len(state.theme_content_sources(profile.profile_id)) == 5


def test_remote_validation_uses_real_id_and_accessible_samples(monkeypatch):
    channel_id = "UC" + "x" * 22
    flat = {
        "channel_id": channel_id,
        "channel": "Real Creator",
        "entries": [
            {"id": "a" * 11},
            {"id": "b" * 11},
            {"id": "c" * 11},
        ],
    }
    install_fake_ytdlp(
        monkeypatch,
        [
            flat,
            RuntimeError("Sign in to confirm your age"),
            {"channel_id": channel_id, "duration": 1200, "title": "LoL 1"},
            {"channel_id": channel_id, "duration": 1300, "title": "LoL 2"},
        ],
    )
    result = inspect_channel("https://youtube.com/@creator", YouTubeAccess())
    assert result["channel_id"] == channel_id
    assert len(result["samples"]) == 2
    assert result["name"] == "Real Creator"


@pytest.mark.parametrize(
    "metadata",
    [
        {"age_limit": 18},
        {"availability": "subscriber_only"},
        {"channel_id": "another-channel"},
        {"duration": 60},
    ],
)
def test_remote_validation_rejects_restricted_or_wrong_channel(monkeypatch, metadata):
    channel_id = "UC" + "x" * 22
    install_fake_ytdlp(
        monkeypatch,
        [
            {"channel_id": channel_id, "entries": [{"id": "a" * 11}]},
            {"channel_id": channel_id, "duration": 1200, **metadata},
        ],
    )
    with pytest.raises(ValueError, match="validated"):
        inspect_channel("https://youtube.com/@creator", YouTubeAccess())


@pytest.mark.parametrize(
    "response,expected",
    [
        ({"relevant": True, "language_matches": True, "score": 0.9}, True),
        ({"relevant": True, "language_matches": False, "score": 0.99}, False),
        ({"relevant": True, "language_matches": True, "score": 0.7}, False),
        ({"relevant": False, "language_matches": True, "score": 0.99}, False),
    ],
)
def test_editorial_gate_requires_theme_language_and_confidence(
    monkeypatch, response, expected
):
    advisor = GeminiSourceAdvisor()
    monkeypatch.setattr(advisor, "_generate", lambda *args: response)
    assert advisor.assess({"theme": "lol"}, {}) is expected
