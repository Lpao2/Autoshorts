from types import SimpleNamespace

import pytest

from autoshorts.automation_config import PublishingChannel
from autoshorts.brand_identity import (
    BrandCandidate,
    BrandGenerationResult,
    BrandKitStatus,
    ChannelBrandKit,
    GeminiBrandIdentityProvider,
    generate_brand_identity_with_retry,
)
from autoshorts.state_store import StateStore
from autoshorts.transcript_analysis import AnalysisUsage


def candidate(channel_id, index=1):
    return BrandCandidate(
        channel_id,
        f"Marca {index}",
        (f"@marca{index}a", f"@marca{index}b", f"@marca{index}c"),
        "Descrição editorial",
        f"Tagline {index}",
        ("gaming", "autoral"),
        "Logo original e legível",
        "Banner original e responsivo",
        "Boa diferenciação",
    )


def test_candidate_validation():
    with pytest.raises(ValueError, match="three unique"):
        BrandCandidate(
            "id",
            "Nome",
            ("@igual", "@igual", "@igual"),
            "d",
            "t",
            ("k",),
            "logo",
            "banner",
            "reason",
        )


def test_generation_persistence_and_separate_usage(tmp_path):
    result = BrandGenerationResult(
        tuple(
            candidate(f"channel-{slot}", i) for slot in range(1, 4) for i in range(1, 6)
        ),
        "gemini",
        "model",
        AnalysisUsage(100, 200, 30, 330, 1.2, 0.01),
    )
    with StateStore(tmp_path / "state.db") as store:
        generation_id = store.save_brand_generation(result)
        assert len(store.brand_candidates(generation_id)) == 15
        row = store.connection.execute(
            "SELECT operation FROM brand_generations WHERE id = ?", (generation_id,)
        ).fetchone()
        assert row[0] == "brand_generation"
        assert (
            store.connection.execute("SELECT COUNT(*) FROM ai_usage").fetchone()[0] == 0
        )


def test_approval_versions_and_one_active(tmp_path):
    base = ChannelBrandKit(
        "channel",
        "Nome",
        "@nome",
        "Descrição",
        "Tagline",
        ("gaming",),
        "visual",
        ("#000", "#fff", "#f00", "#111"),
        "logo",
        "banner",
        BrandKitStatus.APPROVED,
        0,
    )
    with StateStore(tmp_path / "state.db") as store:
        assert store.approve_brand_kit(base).version == 1
        assert store.approve_brand_kit(base).version == 2
        statuses = [
            row[0]
            for row in store.connection.execute(
                "SELECT status FROM channel_brand_kits ORDER BY version"
            )
        ]
        assert statuses == ["RETIRED", "APPROVED"]


class Models:
    def generate_content(self, **kwargs):
        channels = []
        for channel in ("a", "b", "c"):
            channels.append(
                {
                    "publishing_channel_id": channel,
                    "candidates": [
                        {
                            "public_name": f"{channel.upper()} Marca {i}",
                            "handle_candidates": [
                                f"@{channel}marca{i}a",
                                f"@{channel}marca{i}b",
                                f"@{channel}marca{i}c",
                            ],
                            "description": "Descrição",
                            "tagline": f"Tagline {channel} {i}",
                            "brand_keywords": [channel, "gaming", "autoral"],
                            "logo_brief": "Logo",
                            "banner_brief": "Banner",
                            "reason": "Razão",
                        }
                        for i in range(5)
                    ],
                }
            )
        usage = SimpleNamespace(
            prompt_token_count=10,
            candidates_token_count=20,
            thoughts_token_count=5,
            total_token_count=35,
        )
        return SimpleNamespace(parsed={"channels": channels}, usage_metadata=usage)


def test_provider_returns_fifteen_distinct_candidates():
    from pathlib import Path

    from autoshorts.theme_catalog import load_theme_seed

    theme = load_theme_seed(
        Path(__file__).parents[1] / "config/theme-catalog.legacy.json"
    )[0]
    channels = tuple(
        (theme.channel_archetypes[i], PublishingChannel(x, f"LoL {x.upper()}", i + 1))
        for i, x in enumerate(("a", "b", "c"))
    )
    provider = GeminiBrandIdentityProvider(client=SimpleNamespace(models=Models()))
    result = provider.generate(theme, channels)
    assert len(result.candidates) == 15
    assert len({x.public_name for x in result.candidates}) == 15


class FailingProvider:
    provider_name = "test"
    model = "test"

    def __init__(self, failures):
        self.failures = list(failures)
        self.calls = 0

    def generate(self, _theme, _channels):
        self.calls += 1
        error = self.failures.pop(0)
        if error is not None:
            raise RuntimeError(error)
        return BrandGenerationResult((), "test", "test", AnalysisUsage())


@pytest.mark.parametrize("error", ["503 UNAVAILABLE", "429 rate limited"])
def test_transient_brand_error_retries(error):
    provider = FailingProvider([error, None])
    result, attempts = generate_brand_identity_with_retry(
        provider, object(), (), sleep=lambda _seconds: None
    )
    assert result.provider == "test"
    assert attempts == 2
    assert provider.calls == 2


def test_forbidden_brand_error_does_not_retry():
    provider = FailingProvider(["403 forbidden"])
    with pytest.raises(RuntimeError, match="403"):
        generate_brand_identity_with_retry(
            provider, object(), (), sleep=lambda _seconds: None
        )
    assert provider.calls == 1


def test_three_transient_failures_stop_without_result():
    provider = FailingProvider(["503", "timeout", "504"])
    waits = []
    with pytest.raises(RuntimeError, match="504"):
        generate_brand_identity_with_retry(provider, object(), (), sleep=waits.append)
    assert provider.calls == 3
    assert waits == [2.0, 6.0]


def test_manual_brand_kit_uses_shared_validation():
    with pytest.raises(ValueError, match="handle"):
        ChannelBrandKit(
            "channel",
            "Nome",
            "handle inválido",
            "Descrição",
            "Tagline",
            ("gaming",),
            "visual",
            ("#000", "#fff", "#f00", "#111"),
            "logo",
            "banner",
            BrandKitStatus.APPROVED,
            0,
        )
