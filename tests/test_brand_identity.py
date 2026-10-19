from types import SimpleNamespace

import pytest

from autoshorts.analysis_cost import estimate_cost_usd
from autoshorts.automation_config import PublishingChannel
from autoshorts.brand_identity import (
    DEFAULT_GEMINI_BRAND_MODEL,
    BrandCandidate,
    BrandGenerationResult,
    BrandGenerationValidationError,
    BrandKitStatus,
    ChannelBrandKit,
    GeminiBrandIdentityProvider,
    generate_brand_identity_with_retry,
    normalize_handle_candidate,
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


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("riftsemfreio", "@riftsemfreio"),
        ("@RiftSemFreio", "@riftsemfreio"),
        ("  @riftsemfreio  ", "@riftsemfreio"),
        ("@@riftsemfreio", "@riftsemfreio"),
        ("rift sem freio", "@rift-sem-freio"),
        ("youtube.com/@riftsemfreio", "@riftsemfreio"),
        ("https://youtube.com/@riftsemfreio", "@riftsemfreio"),
        ("Ríft do Dragão", "@rift-do-dragao"),
        ("-riftsemfreio-", "@riftsemfreio"),
    ],
)
def test_normalize_handle_candidate(raw, expected):
    assert normalize_handle_candidate(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "ab",
        "a" * 31,
        "https://example.com/rift",
        "+55 11 99999-9999",
        "rift🔥freio",
        "🔥🔥",
        "",
    ],
)
def test_normalize_handle_rejects_unsafe_values(raw):
    with pytest.raises(ValueError):
        normalize_handle_candidate(raw)


def test_candidate_rejects_duplicates_after_normalization():
    normalized = tuple(
        normalize_handle_candidate(value)
        for value in ("Rift Sem Freio", "@rift-sem-freio", "rift.ativo")
    )
    with pytest.raises(ValueError, match="three unique"):
        candidate("channel", 1).__class__(
            "channel",
            "Marca",
            normalized,
            "Descrição",
            "Tagline",
            ("gaming",),
            "Logo",
            "Banner",
            "Razão",
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
            "SELECT operation, model FROM brand_generations WHERE id = ?",
            (generation_id,),
        ).fetchone()
        assert row[0] == "brand_generation"
        assert row[1] == "model"
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
        self.kwargs = kwargs
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
        channels[0]["candidates"][0]["handle_candidates"][0] = "A Marca 0 A"
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
    models = Models()
    provider = GeminiBrandIdentityProvider(client=SimpleNamespace(models=models))
    result = provider.generate(theme, channels)
    assert len(result.candidates) == 15
    assert len({x.public_name for x in result.candidates}) == 15
    assert provider.model == DEFAULT_GEMINI_BRAND_MODEL
    assert models.kwargs["model"] == "gemini-3.5-flash-lite"
    assert models.kwargs["config"]["thinking_config"] == {
        "thinking_level": "minimal"
    }
    assert result.model == "gemini-3.5-flash-lite"
    assert result.candidates[0].handle_candidates[0] == "@a-marca-0-a"
    assert result.normalizations[0].original == "A Marca 0 A"
    assert result.usage.estimated_cost_usd == estimate_cost_usd(
        "gemini-3.5-flash-lite", 10, 25
    )


def test_brand_model_env_override(monkeypatch):
    monkeypatch.setenv("AUTOSHORTS_GEMINI_BRAND_MODEL", "brand-override")
    provider = GeminiBrandIdentityProvider(client=SimpleNamespace(models=Models()))
    assert provider.model == "brand-override"


class InvalidModels(Models):
    def generate_content(self, **kwargs):
        response = super().generate_content(**kwargs)
        response.parsed["channels"][2]["candidates"][4]["handle_candidates"][2] = (
            "rift🔥freio"
        )
        return response


def test_validation_failure_preserves_usage_without_candidates(tmp_path):
    from pathlib import Path

    from autoshorts.theme_catalog import load_theme_seed

    theme = load_theme_seed(
        Path(__file__).parents[1] / "config/theme-catalog.legacy.json"
    )[0]
    channels = tuple(
        (theme.channel_archetypes[i], PublishingChannel(x, f"LoL {x.upper()}", i + 1))
        for i, x in enumerate(("a", "b", "c"))
    )
    provider = GeminiBrandIdentityProvider(
        client=SimpleNamespace(models=InvalidModels())
    )
    with pytest.raises(BrandGenerationValidationError) as caught:
        provider.generate(theme, channels)
    error = caught.value
    assert error.usage.total_tokens == 35
    with StateStore(tmp_path / "state.db") as store:
        generation_id = store.save_brand_generation_failure(
            provider=error.provider,
            model=error.model,
            usage=error.usage,
            status="VALIDATION_FAILED",
            error=str(error),
        )
        row = store.connection.execute(
            "SELECT status, total_tokens, model FROM brand_generations WHERE id = ?",
            (generation_id,),
        ).fetchone()
        assert tuple(row) == ("VALIDATION_FAILED", 35, "gemini-3.5-flash-lite")
        assert store.brand_candidates(generation_id) == ()


def test_normalized_response_persists_all_fifteen_candidates(tmp_path):
    from pathlib import Path

    from autoshorts.theme_catalog import load_theme_seed

    theme = load_theme_seed(
        Path(__file__).parents[1] / "config/theme-catalog.legacy.json"
    )[0]
    channels = tuple(
        (theme.channel_archetypes[i], PublishingChannel(x, f"LoL {x.upper()}", i + 1))
        for i, x in enumerate(("a", "b", "c"))
    )
    result = GeminiBrandIdentityProvider(
        client=SimpleNamespace(models=Models())
    ).generate(theme, channels)
    with StateStore(tmp_path / "state.db") as store:
        generation_id = store.save_brand_generation(result)
        assert len(store.brand_candidates(generation_id)) == 15
        status = store.connection.execute(
            "SELECT status FROM brand_generations WHERE id = ?", (generation_id,)
        ).fetchone()[0]
        assert status == "SUCCEEDED"


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
