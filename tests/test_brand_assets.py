from __future__ import annotations

import json
from dataclasses import replace

import pytest

from autoshorts.brand_assets import (
    BrandAssetService,
    BrandAssetStatus,
    BrandAssetType,
    FakeBrandImageProvider,
    brand_asset_prompt,
    brand_asset_version_directory,
)
from autoshorts.brand_identity import HandleAvailability
from autoshorts.brand_planning import (
    PlannedBrandStatus,
    PlannedChannelBrand,
    stable_brand_plan_id,
)
from autoshorts.state_store import StateStore


def setup_brand(store: StateStore):
    theme = store.list_theme_templates()[0]
    archetype = theme.channel_archetypes[0]
    plan = PlannedChannelBrand(
        stable_brand_plan_id(theme.theme_key, archetype.slot),
        theme.theme_key,
        archetype.slot,
        "Marca Teste",
        "@marcateste",
        ("@testemarca", "@canalmarcateste"),
        "Descrição editorial",
        "Tagline curta",
        ("teste", "original"),
        archetype.visual_direction,
        (
            archetype.palette.primary,
            archetype.palette.secondary,
            archetype.palette.accent,
            archetype.palette.background,
        ),
        "Símbolo geométrico simples",
        "Composição panorâmica complementar",
        PlannedBrandStatus.APPROVED,
        HandleAvailability.UNKNOWN,
        "human",
        "manual",
        2,
    )
    now = "2026-10-19T00:00:00+00:00"
    store.connection.execute(
        """INSERT INTO planned_channel_brands
        (brand_plan_id, theme_key, slot, public_name, preferred_handle,
         backup_handles_json, handle_availability, description, tagline,
         brand_keywords_json, visual_direction, palette_json, logo_brief,
         banner_brief, status, provider, model, version, source_generation_id,
         created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            plan.brand_plan_id,
            plan.theme_key,
            plan.slot,
            plan.public_name,
            plan.preferred_handle,
            json.dumps(plan.backup_handles),
            plan.handle_availability,
            plan.description,
            plan.tagline,
            json.dumps(plan.brand_keywords),
            plan.visual_direction,
            json.dumps(plan.palette),
            plan.logo_brief,
            plan.banner_brief,
            plan.status,
            plan.provider,
            plan.model,
            plan.version,
            None,
            now,
            now,
        ),
    )
    store.connection.commit()
    return theme, archetype, plan


def test_prompt_uses_catalog_context_without_handle(tmp_path):
    with StateStore(tmp_path / "state.db") as store:
        theme, archetype, plan = setup_brand(store)
        prompt = brand_asset_prompt(BrandAssetType.BANNER, theme, archetype, plan)
    assert theme.display_name in prompt
    assert archetype.name in prompt
    assert plan.public_name in prompt
    assert plan.banner_brief in prompt
    assert plan.preferred_handle not in prompt
    assert plan.tagline not in prompt
    assert "zona segura central de 1235x338" in prompt
    assert "logos existentes" in prompt.casefold()
    assert "minimal brand design" in prompt.casefold()
    assert "use generous negative space" in prompt.casefold()
    assert "limited number of visual elements" in prompt.casefold()
    assert "visual density" in prompt.casefold()
    assert "resolved visual mood" in prompt.casefold()
    assert "contrast" in prompt.casefold()
    assert "energy" in prompt.casefold()
    assert "ai-generated artwork" in prompt.casefold()
    assert "sem tagline" in prompt.casefold()
    assert "alto contraste" not in prompt.casefold()


def test_fake_provider_paths_validation_checksum_preview_and_resume(tmp_path):
    with StateStore(tmp_path / "state.db") as store:
        theme, archetype, plan = setup_brand(store)
        service = BrandAssetService(tmp_path, store, FakeBrandImageProvider())
        logo = service.generate(
            plan,
            theme,
            archetype,
            BrandAssetType.LOGO,
            prompt_override="Manual art direction without adaptive boilerplate",
        )
        assert (
            logo.file_path
            == brand_asset_version_directory(tmp_path, plan, BrandAssetType.LOGO, 1)
            / "master.png"
        )
        assert logo.file_path.is_file()
        assert logo.preview_path and logo.preview_path.is_file()
        assert (logo.width, logo.height) == (1024, 1024)
        assert len(logo.checksum) == 64
        assert logo.status == BrandAssetStatus.VALIDATED
        assert "Resolved visual mood" in logo.prompt
        assert "Manual art direction" in logo.prompt
        assert logo.resolved_visual_direction is not None
        persisted = store.brand_asset(plan.brand_plan_id, BrandAssetType.LOGO, 1)
        assert (
            persisted.resolved_visual_direction == logo.resolved_visual_direction
        )
        manifest = json.loads((logo.file_path.parent / "manifest.json").read_text())
        assert manifest["resolved_visual_direction"] == (
            logo.resolved_visual_direction.as_dict()
        )
        assert service.generate(plan, theme, archetype, BrandAssetType.LOGO) == logo
        assert len(store.brand_assets(plan.brand_plan_id)) == 1


def test_regeneration_versions_and_never_overwrites_approved(tmp_path):
    with StateStore(tmp_path / "state.db") as store:
        theme, archetype, plan = setup_brand(store)
        service = BrandAssetService(tmp_path, store, FakeBrandImageProvider())
        first = service.generate(plan, theme, archetype, BrandAssetType.LOGO)
        store.set_brand_asset_status(
            plan.brand_plan_id,
            BrandAssetType.LOGO,
            first.version,
            BrandAssetStatus.APPROVED,
        )
        second = service.generate(
            plan, theme, archetype, BrandAssetType.LOGO, regenerate=True
        )
        assert second.version == 2
        assert second.file_path != first.file_path
        assert first.file_path.is_file()
        assert (
            store.brand_asset(plan.brand_plan_id, BrandAssetType.LOGO, 1).status
            == BrandAssetStatus.APPROVED
        )
        store.set_brand_asset_status(
            plan.brand_plan_id,
            BrandAssetType.LOGO,
            second.version,
            BrandAssetStatus.APPROVED,
        )
        assert (
            store.brand_asset(plan.brand_plan_id, BrandAssetType.LOGO, 1).status
            == BrandAssetStatus.RETIRED
        )


def test_failure_cleans_partial_files(tmp_path):
    class BrokenProvider(FakeBrandImageProvider):
        def generate_logo(self, planned_brand, *, output_path, prompt):
            output_path.write_bytes(b"truncated")
            return super().generate_logo(
                planned_brand, output_path=output_path, prompt=prompt
            )

    with StateStore(tmp_path / "state.db") as store:
        theme, archetype, plan = setup_brand(store)
        provider = BrokenProvider()

        def broken(_brand, *, output_path, prompt):
            output_path.write_bytes(b"truncated")
            raise RuntimeError("provider failed")

        provider.generate_logo = broken
        service = BrandAssetService(tmp_path, store, provider)
        with pytest.raises(RuntimeError, match="provider failed"):
            service.generate(plan, theme, archetype, BrandAssetType.LOGO)
        parent = tmp_path / "data" / "branding" / plan.theme_key / plan.brand_plan_id
        assert not tuple(parent.glob("logo/.tmp-*"))
        assert store.brand_assets() == ()


def test_reconcile_recovers_published_manifest_and_batch_skips_valid(tmp_path):
    with StateStore(tmp_path / "state.db") as store:
        theme, archetype, plan = setup_brand(store)
        service = BrandAssetService(tmp_path, store, FakeBrandImageProvider())
        logo = service.generate(plan, theme, archetype, BrandAssetType.LOGO)
        store.connection.execute("DELETE FROM brand_asset_metadata")
        store.connection.commit()
        assert service.reconcile() == (logo,)
        requests = (
            (plan, theme, archetype, BrandAssetType.LOGO),
            (plan, theme, archetype, BrandAssetType.BANNER),
        )
        generated = service.generate_pending(requests, max_workers=2)
        assert [asset.asset_type for asset in generated] == [BrandAssetType.BANNER]
        assert len(store.brand_assets(plan.brand_plan_id)) == 2


def test_rejects_unapproved_plan_and_invalid_concurrency(tmp_path):
    with StateStore(tmp_path / "state.db") as store:
        theme, archetype, plan = setup_brand(store)
        service = BrandAssetService(tmp_path, store, FakeBrandImageProvider())
        with pytest.raises(ValueError, match="approved"):
            service.generate(
                replace(plan, status=PlannedBrandStatus.RETIRED),
                theme,
                archetype,
                BrandAssetType.LOGO,
            )
        with pytest.raises(ValueError, match="between 1 and 4"):
            service.generate_pending((), max_workers=120)
