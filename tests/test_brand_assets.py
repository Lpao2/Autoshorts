from __future__ import annotations

import json
from dataclasses import replace

import pytest

from autoshorts.brand_asset_import import BrandAssetImportService
from autoshorts.brand_assets import (
    BrandAssetService,
    BrandAssetStatus,
    BrandAssetType,
    FakeBrandImageProvider,
    brand_asset_prompt,
    brand_asset_version_directory,
    write_test_png,
)
from autoshorts.brand_identity import HandleAvailability
from autoshorts.brand_planning import (
    PlannedBrandStatus,
    PlannedChannelBrand,
    branding_directory,
    stable_brand_plan_id,
)
from autoshorts.branding_migration import migrate_branding_directories
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
        parent = branding_directory(tmp_path, plan)
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


def test_imports_external_logo_and_detects_current_checksum_duplicate(tmp_path):
    source = tmp_path / "external" / "logo.png"
    write_test_png(source, 1024, 1024)
    with StateStore(tmp_path / "state.db") as store:
        _theme, _archetype, plan = setup_brand(store)
        importer = BrandAssetImportService(tmp_path, store)
        first = importer.import_brand_asset(plan, BrandAssetType.LOGO, source)
        duplicate = importer.import_brand_asset(plan, BrandAssetType.LOGO, source)
        assert not first.duplicate
        assert duplicate.duplicate
        assert duplicate.asset == first.asset
        assert first.asset.provider == "external-import"
        assert first.asset.model == "external"
        assert first.asset.status == BrandAssetStatus.VALIDATED
        assert first.asset.version == 1
        assert first.asset.resolved_visual_direction is not None
        assert len(store.brand_assets(plan.brand_plan_id)) == 1


def test_imports_external_banner_with_preview_and_delivery(tmp_path):
    source = tmp_path / "external" / "banner.png"
    write_test_png(source, 2560, 1440)
    with StateStore(tmp_path / "state.db") as store:
        _theme, _archetype, plan = setup_brand(store)
        result = BrandAssetImportService(tmp_path, store).import_brand_asset(
            plan, BrandAssetType.BANNER, source
        )
        assert result.asset.width == 2560
        assert result.asset.height == 1440
        assert result.asset.preview_path.is_file()
        assert result.asset.delivery_path.is_file()
        assert result.asset.delivery_path.stat().st_size <= 6 * 1024 * 1024


@pytest.mark.parametrize(
    ("size", "message"),
    (((1000, 1000), "Invalid logo dimensions"), ((1024, 1024), "decodable")),
)
def test_rejects_invalid_external_asset_atomically(tmp_path, size, message):
    source = tmp_path / "external" / "logo.png"
    write_test_png(source, *size)
    if message == "decodable":
        source.write_bytes(source.read_bytes()[:32])
    with StateStore(tmp_path / "state.db") as store:
        _theme, _archetype, plan = setup_brand(store)
        importer = BrandAssetImportService(tmp_path, store)
        with pytest.raises(ValueError, match=message):
            importer.import_brand_asset(plan, BrandAssetType.LOGO, source)
        assert store.brand_assets(plan.brand_plan_id) == ()
        parent = branding_directory(tmp_path, plan)
        assert not tuple(parent.glob("logo/.tmp-*"))
        assert not tuple(parent.glob("logo/v*"))


def test_migrates_legacy_brand_folder_paths_and_manifests(tmp_path):
    with StateStore(tmp_path / "state.db") as store:
        theme, archetype, plan = setup_brand(store)
        asset = BrandAssetService(
            tmp_path, store, FakeBrandImageProvider()
        ).generate(plan, theme, archetype, BrandAssetType.LOGO)
        human_root = branding_directory(tmp_path, plan)
        legacy_root = (
            tmp_path / "data" / "branding" / plan.theme_key / plan.brand_plan_id
        )
        human_root.rename(legacy_root)
        store.connection.execute(
            """UPDATE brand_asset_metadata SET
               file_path=replace(file_path, ?, ?),
               preview_path=replace(preview_path, ?, ?),
               delivery_path=replace(delivery_path, ?, ?)""",
            (str(human_root), str(legacy_root)) * 3,
        )
        manifest = legacy_root / "logo" / "v1" / "manifest.json"
        manifest.write_text(
            manifest.read_text(encoding="utf-8").replace(
                str(human_root), str(legacy_root)
            ),
            encoding="utf-8",
        )
        store.connection.commit()

        report = migrate_branding_directories(tmp_path, store)
        migrated = store.brand_asset(plan.brand_plan_id, BrandAssetType.LOGO, 1)

        assert report.folders_migrated == 1
        assert report.sqlite_paths_updated == 3
        assert report.manifests_updated == 1
        assert migrated.brand_plan_id == plan.brand_plan_id
        assert migrated.file_path == human_root / "logo" / "v1" / "master.png"
        assert migrated.checksum == asset.checksum
        assert migrated.file_path.is_file()
        assert not legacy_root.exists()


def test_external_import_creates_v2_without_overwriting_v1(tmp_path):
    first_source = tmp_path / "external" / "logo-1.png"
    second_source = tmp_path / "external" / "logo-2.png"
    write_test_png(first_source, 1024, 1024)
    second_source.write_bytes(first_source.read_bytes() + b"external-version-two")
    with StateStore(tmp_path / "state.db") as store:
        _theme, _archetype, plan = setup_brand(store)
        importer = BrandAssetImportService(tmp_path, store)
        first = importer.import_brand_asset(
            plan, BrandAssetType.LOGO, first_source
        ).asset
        second = importer.import_brand_asset(
            plan, BrandAssetType.LOGO, second_source
        ).asset
        assert (first.version, second.version) == (1, 2)
        assert first.file_path.is_file()
        assert second.file_path.is_file()
        assert first.checksum != second.checksum


def test_batch_manifest_maps_brand_plan_and_continues_after_failure(tmp_path):
    logo = tmp_path / "incoming" / "logo.png"
    banner = tmp_path / "incoming" / "banner.png"
    invalid = tmp_path / "incoming" / "invalid-logo.png"
    write_test_png(logo, 1024, 1024)
    write_test_png(banner, 2560, 1440)
    write_test_png(invalid, 1000, 1000)
    with StateStore(tmp_path / "state.db") as store:
        _theme, _archetype, plan = setup_brand(store)
        manifest = tmp_path / "incoming" / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "assets": [
                        {
                            "brand_plan_id": plan.brand_plan_id,
                            "logo_path": "logo.png",
                            "banner_path": "banner.png",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        importer = BrandAssetImportService(tmp_path, store)
        manifest_requests = importer.read_manifest(manifest)
        assert [(item.public_name, item.asset_type) for item in manifest_requests] == [
            (plan.public_name, BrandAssetType.LOGO),
            (plan.public_name, BrandAssetType.BANNER),
        ]
        requests = (
            *manifest_requests,
            importer.request(plan, BrandAssetType.LOGO, invalid),
        )
        result = importer.import_requests(requests)
        assert len(result.completed) == 2
        assert len(result.failed) == 1
        assert "Invalid logo dimensions" in result.failed[0].error
        assert len(store.brand_assets(plan.brand_plan_id)) == 2


def test_discovers_strict_staging_layout(tmp_path):
    with StateStore(tmp_path / "state.db") as store:
        _theme, _archetype, plan = setup_brand(store)
        folder = (
            tmp_path
            / "data"
            / "branding-import"
            / plan.theme_key
            / plan.brand_plan_id
        )
        write_test_png(folder / "logo.png", 1024, 1024)
        write_test_png(folder / "banner.png", 2560, 1440)
        requests = BrandAssetImportService(tmp_path, store).discover_staging()
        assert {item.asset_type for item in requests} == set(BrandAssetType)
        assert all(item.brand_plan_id == plan.brand_plan_id for item in requests)


def test_import_is_scoped_and_preserves_existing_other_asset(tmp_path):
    source = tmp_path / "external" / "logo.png"
    write_test_png(source, 1024, 1024)
    with StateStore(tmp_path / "state.db") as store:
        theme, archetype, plan = setup_brand(store)
        existing = BrandAssetService(
            tmp_path, store, FakeBrandImageProvider()
        ).generate(plan, theme, archetype, BrandAssetType.BANNER)
        BrandAssetImportService(tmp_path, store).import_brand_asset(
            plan, BrandAssetType.LOGO, source
        )
        preserved = store.brand_asset(
            plan.brand_plan_id, BrandAssetType.BANNER, existing.version
        )
        assert preserved == existing
