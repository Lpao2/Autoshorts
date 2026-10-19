from autoshorts.automation_config import PublishingChannel
from autoshorts.brand_identity import (
    BrandCandidate,
    BrandGenerationResult,
    HandleAvailability,
)
from autoshorts.brand_planning import (
    BrandRerankResult,
    PlannedBrandStatus,
    PlannedChannelBrand,
    PlannedIdentityAssignment,
    branding_directory,
    global_brand_collisions,
    materialize_brand_plan,
    planned_identity_for,
    plans_from_winners,
    stable_brand_plan_id,
)
from autoshorts.state_store import StateStore
from autoshorts.transcript_analysis import AnalysisUsage


def plan(theme="theme", slot=1, name="Marca", handle="@marca"):
    return PlannedChannelBrand(
        stable_brand_plan_id(theme, slot),
        theme,
        slot,
        name,
        handle,
        (f"{handle}br", f"{handle}_tv"),
        "Descrição",
        "Tagline",
        ("nicho",),
        "Direção",
        ("#000", "#111", "#fff", "#222"),
        "Logo",
        "Banner",
        PlannedBrandStatus.PROPOSED_FOR_REVIEW,
        HandleAvailability.UNKNOWN,
        "gemini",
        "gemini-3.5-flash-lite",
    )


def test_stable_plan_id_materialization_and_asset_path(tmp_path):
    planned = plan()
    assert planned.brand_plan_id == stable_brand_plan_id("theme", 1)
    channel = PublishingChannel("real", "Real", 1)
    kit = materialize_brand_plan(planned, channel)
    assert kit.publishing_channel_id == "real"
    assert kit.source_plan_id == planned.brand_plan_id
    assert branding_directory(tmp_path, planned).parts[-2:] == (
        "theme",
        planned.brand_plan_id,
    )


def test_four_way_distribution_is_balanced_and_distinct_per_theme():
    assignments = [
        planned_identity_for(theme, slot) for theme in range(20) for slot in (1, 2, 3)
    ]
    assert {key: assignments.count(key) for key in set(assignments)} == {
        "google-plan-1": 15,
        "google-plan-2": 15,
        "google-plan-3": 15,
        "google-plan-4": 15,
    }
    for theme in range(20):
        assert len({planned_identity_for(theme, slot) for slot in (1, 2, 3)}) == 3


def test_global_collision_detection_marks_both_plans():
    first = plan("a", 1, "Meta Lab", "@metalab")
    second = plan("b", 2, "Meta-Lab", "@meta.lab")
    assert set(global_brand_collisions((first, second))) == {
        first.brand_plan_id,
        second.brand_plan_id,
    }


def test_persists_sixty_plans_with_recovery_and_no_fake_channels(tmp_path):
    with StateStore(tmp_path / "state.db") as store:
        themes = store.list_theme_templates()
        assert len(themes) == 20
        for theme_index, theme in enumerate(themes):
            candidates = tuple(
                BrandCandidate(
                    f"catalog:{theme.theme_key}:{slot}",
                    f"Marca {theme_index} {slot} {candidate}",
                    (
                        f"@m{theme_index}s{slot}c{candidate}a",
                        f"@m{theme_index}s{slot}c{candidate}b",
                        f"@m{theme_index}s{slot}c{candidate}c",
                    ),
                    "Descrição",
                    "Tagline",
                    ("nicho",),
                    "Logo",
                    "Banner",
                    "Razão",
                )
                for slot in (1, 2, 3)
                for candidate in range(5)
            )
            generation_id = store.save_catalog_brand_generation(
                theme.theme_key,
                BrandGenerationResult(
                    candidates,
                    "test",
                    "test",
                    AnalysisUsage(1, 1, 0, 2, 0.1, 0.0),
                ),
            )
            ranking = BrandRerankResult(
                tuple(
                    (slot, f"Marca {theme_index} {slot} 0")
                    for slot in (1, 2, 3)
                ),
                tuple((slot, "Razão") for slot in (1, 2, 3)),
                "test",
                "test",
                AnalysisUsage(),
            )
            plans = plans_from_winners(
                theme, candidates, ranking, generation_id=generation_id
            )
            assignments = tuple(
                PlannedIdentityAssignment(
                    item.brand_plan_id,
                    planned_identity_for(theme_index, item.slot),
                )
                for item in plans
            )
            store.save_planned_channel_brands(
                generation_id,
                plans,
                assignments,
                rerank_usage=ranking.usage,
            )
        assert len(store.planned_channel_brands()) == 60
        assert store.connection.execute(
            "SELECT COUNT(*) FROM catalog_brand_candidates"
        ).fetchone()[0] == 300
        assert (
            store.catalog_brand_generation(themes[0].theme_key)["status"]
            == "RERANKED"
        )
