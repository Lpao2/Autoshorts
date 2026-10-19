from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_brand_assets import setup_brand

from autoshorts.brand_asset_package import (
    BrandAssetPackageService,
    CanonicalTransform,
    PackageAssetStatus,
    extract_zip_secure,
    normalize_asset_name,
)
from autoshorts.brand_assets import BrandAssetStatus, BrandAssetType, write_test_png
from autoshorts.state_store import StateStore


def _checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_normalizes_accents_case_and_separators():
    assert normalize_asset_name("Prisma Científico") == "prismacientifico"
    assert normalize_asset_name("prisma-cientifico") == "prismacientifico"
    assert normalize_asset_name("PRISMA_CIENTIFICO") == "prismacientifico"


def test_secure_zip_extraction_and_path_traversal_protection(tmp_path):
    safe = tmp_path / "safe.zip"
    with zipfile.ZipFile(safe, "w") as package:
        package.writestr("tema/canal/logo.png", b"png")
    destination = extract_zip_secure(safe, tmp_path / "safe")
    assert (destination / "tema" / "canal" / "logo.png").read_bytes() == b"png"

    unsafe = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(unsafe, "w") as package:
        package.writestr("../escape.png", b"bad")
    with pytest.raises(ValueError, match="Unsafe ZIP member"):
        extract_zip_secure(unsafe, tmp_path / "unsafe")
    assert not (tmp_path / "escape.png").exists()


def test_dry_run_maps_exact_normalized_name_and_reports_missing(tmp_path):
    package = tmp_path / "package"
    source = package / "MARCA-TESTE" / "logo.png"
    write_test_png(source, 1024, 1024)
    (package / "README.txt").write_text("metadata", encoding="utf-8")
    with StateStore(tmp_path / "state.db") as store:
        _theme, _archetype, plan = setup_brand(store)
        dry_run = BrandAssetPackageService(tmp_path, store).dry_run(package)
        assert len(dry_run.importable) == 1
        candidate = dry_run.importable[0]
        assert candidate.brand_plan_id == plan.brand_plan_id
        assert candidate.asset_type == BrandAssetType.LOGO
        assert candidate.transform == CanonicalTransform.NONE
        assert dry_run.missing == ((plan.brand_plan_id, BrandAssetType.BANNER),)
        assert dry_run.ignored_files == ("README.txt",)
        report = BrandAssetPackageService(tmp_path, store).write_dry_run_report(
            dry_run, tmp_path / "dry-run.json"
        )
        assert '"public_name": "Marca Teste"' in report.read_text(encoding="utf-8")


def test_dry_run_prefers_exact_brand_plan_id_directory(tmp_path):
    with StateStore(tmp_path / "state.db") as store:
        _theme, _archetype, plan = setup_brand(store)
        source = tmp_path / "package" / plan.brand_plan_id / "logo.png"
        write_test_png(source, 1024, 1024)
        dry_run = BrandAssetPackageService(tmp_path, store).dry_run(
            tmp_path / "package"
        )
        assert len(dry_run.importable) == 1
        assert dry_run.importable[0].brand_plan_id == plan.brand_plan_id


def test_ambiguous_normalized_mapping_is_not_importable(tmp_path):
    package = tmp_path / "package"
    write_test_png(package / "marca-teste" / "logo.png", 1024, 1024)
    plans = (
        SimpleNamespace(
            brand_plan_id="one",
            public_name="Marca Teste",
            theme_key="theme-one",
            slot=1,
        ),
        SimpleNamespace(
            brand_plan_id="two",
            public_name="Marca-Teste",
            theme_key="theme-two",
            slot=2,
        ),
    )
    store = SimpleNamespace(planned_channel_brands=lambda: plans)
    dry_run = BrandAssetPackageService(tmp_path, store).dry_run(package)
    assert dry_run.importable == ()
    assert dry_run.candidates[0].status == PackageAssetStatus.UNKNOWN
    assert "Ambiguous" in dry_run.candidates[0].issue


def test_prepares_preserves_and_imports_logo_and_banner_with_provenance(tmp_path):
    package = tmp_path / "final-package"
    logo = package / "marca_teste" / "logo.png"
    banner = package / "marca_teste" / "banner.png"
    write_test_png(logo, 1254, 1254)
    write_test_png(banner, 1672, 941)
    original_checksums = {_checksum(logo), _checksum(banner)}
    with StateStore(tmp_path / "state.db") as store:
        _theme, _archetype, plan = setup_brand(store)
        service = BrandAssetPackageService(tmp_path, store)
        dry_run = service.dry_run(package)
        transforms = {item.asset_type: item.transform for item in dry_run.importable}
        assert transforms == {
            BrandAssetType.LOGO: CanonicalTransform.RESIZE_LANCZOS,
            BrandAssetType.BANNER: CanonicalTransform.RESIZE_CENTER_CROP_LANCZOS,
        }
        result = service.import_safe(dry_run)
        assert len(result.completed) == 2
        assert result.failed == ()
        assert result.needs_attention == ()
        assets = store.brand_assets(plan.brand_plan_id)
        assert {item.asset_type for item in assets} == set(BrandAssetType)
        assert all(item.status == BrandAssetStatus.VALIDATED for item in assets)
        assert all(item.provider == "external-import" for item in assets)
        assert all(item.source_package == "final-package" for item in assets)
        assert {item.source_checksum for item in assets} == original_checksums
        assert all(item.source_preserved_path.is_file() for item in assets)
        assert all(
            item.source_preserved_path.stat().st_mode & 0o222 == 0
            for item in assets
        )
        assert [(item.width, item.height) for item in assets] == [
            (2560, 1440),
            (1024, 1024),
        ]
        assert _checksum(logo) in original_checksums
        assert _checksum(banner) in original_checksums


def test_non_square_logo_and_far_banner_need_attention(tmp_path):
    package = tmp_path / "package"
    write_test_png(package / "marca-teste" / "logo.png", 1200, 1000)
    write_test_png(package / "marca-teste" / "banner.png", 1400, 1000)
    with StateStore(tmp_path / "state.db") as store:
        setup_brand(store)
        dry_run = BrandAssetPackageService(tmp_path, store).dry_run(package)
        assert dry_run.importable == ()
        assert all(
            item.status == PackageAssetStatus.NEEDS_ATTENTION
            for item in dry_run.candidates
        )


def test_consolidated_manifest_contains_import_provenance(tmp_path):
    package = tmp_path / "package"
    logo = package / "marca-teste" / "logo.png"
    write_test_png(logo, 1024, 1024)
    with StateStore(tmp_path / "state.db") as store:
        setup_brand(store)
        service = BrandAssetPackageService(tmp_path, store)
        service.import_safe(service.dry_run(package))
        manifest = service.write_consolidated_manifest(tmp_path / "manifest.json")
        text = manifest.read_text(encoding="utf-8")
        assert '"source_filename": "marca-teste/logo.png"' in text
        assert '"canonical_dimensions": [' in text
        assert '"transform": "none"' in text
