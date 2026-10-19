"""Strict external asset import through the existing brand asset pipeline."""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from autoshorts.brand_assets import (
    BrandAssetService,
    BrandAssetStatus,
    BrandAssetType,
    GeneratedBrandAsset,
    validate_brand_asset,
)
from autoshorts.brand_planning import PlannedChannelBrand


@dataclass(frozen=True, slots=True)
class ExternalAssetImport:
    brand_plan_id: str
    public_name: str
    theme_key: str
    asset_type: BrandAssetType
    source_file: Path


@dataclass(frozen=True, slots=True)
class ExternalAssetImportResult:
    request: ExternalAssetImport
    asset: GeneratedBrandAsset
    duplicate: bool


@dataclass(frozen=True, slots=True)
class ExternalAssetImportFailure:
    request: ExternalAssetImport
    error: str


@dataclass(frozen=True, slots=True)
class ExternalAssetBatchResult:
    completed: tuple[ExternalAssetImportResult, ...]
    failed: tuple[ExternalAssetImportFailure, ...]


class _StrictExternalFileProvider:
    provider_name = "external-import"
    model = "external"

    def __init__(self, source_file: Path) -> None:
        self.source_file = source_file

    def _generate(self, brand, asset_type, output_path, prompt):
        shutil.copyfile(self.source_file, output_path)
        width, height = validate_brand_asset(output_path, asset_type)
        return GeneratedBrandAsset(
            brand_plan_id=brand.brand_plan_id,
            asset_type=asset_type,
            file_path=output_path,
            provider=self.provider_name,
            model=self.model,
            prompt=prompt,
            width=width,
            height=height,
            file_size=output_path.stat().st_size,
            checksum=_checksum(output_path),
            status=BrandAssetStatus.GENERATED,
            created_at=datetime.now(UTC).isoformat(),
        )

    def generate_logo(self, planned_brand, *, output_path, prompt):
        return self._generate(
            planned_brand, BrandAssetType.LOGO, output_path, prompt
        )

    def generate_banner(self, planned_brand, *, output_path, prompt):
        return self._generate(
            planned_brand, BrandAssetType.BANNER, output_path, prompt
        )


class BrandAssetImportService:
    def __init__(self, project_root: Path, store) -> None:
        self.project_root = project_root
        self.store = store

    def _catalog_plan(self, brand_plan_id: str) -> PlannedChannelBrand:
        plan = next(
            (
                item
                for item in self.store.planned_channel_brands()
                if item.brand_plan_id == brand_plan_id
            ),
            None,
        )
        if plan is None:
            raise KeyError(f"Unknown brand_plan_id: {brand_plan_id}")
        return plan

    def request(
        self,
        brand_plan: PlannedChannelBrand,
        asset_type: BrandAssetType,
        source_file: Path,
    ) -> ExternalAssetImport:
        catalog_plan = self._catalog_plan(brand_plan.brand_plan_id)
        return ExternalAssetImport(
            catalog_plan.brand_plan_id,
            catalog_plan.public_name,
            catalog_plan.theme_key,
            BrandAssetType(asset_type),
            Path(source_file).expanduser().resolve(),
        )

    def import_brand_asset(
        self,
        brand_plan: PlannedChannelBrand,
        asset_type: BrandAssetType,
        source_file: Path,
    ) -> ExternalAssetImportResult:
        request = self.request(brand_plan, asset_type, source_file)
        brand_plan = self._catalog_plan(request.brand_plan_id)
        validate_brand_asset(request.source_file, request.asset_type)
        checksum = _checksum(request.source_file)
        versions = [
            asset
            for asset in self.store.brand_assets(brand_plan.brand_plan_id)
            if asset.asset_type == request.asset_type
        ]
        current = max(versions, key=lambda item: item.version) if versions else None
        if current and current.checksum == checksum:
            return ExternalAssetImportResult(request, current, True)

        theme = self.store.get_theme_template(brand_plan.theme_key)
        if theme is None:
            raise KeyError(f"Unknown theme for brand plan: {brand_plan.theme_key}")
        archetype = next(
            item for item in theme.channel_archetypes if item.slot == brand_plan.slot
        )
        provider = _StrictExternalFileProvider(request.source_file)
        asset = BrandAssetService(self.project_root, self.store, provider).generate(
            brand_plan,
            theme,
            archetype,
            request.asset_type,
            regenerate=True,
            prompt_override=(
                "External asset supplied for local validation and import. "
                "No image generation was performed by AutoShorts."
            ),
        )
        return ExternalAssetImportResult(request, asset, False)

    def discover_staging(
        self, root: Path | None = None
    ) -> tuple[ExternalAssetImport, ...]:
        staging = root or self.project_root / "data" / "branding-import"
        staging = Path(staging).expanduser().resolve()
        plans = {
            plan.brand_plan_id: plan for plan in self.store.planned_channel_brands()
        }
        requests = []
        if not staging.exists():
            return ()
        for source in sorted(staging.glob("*/*/*.png")):
            asset_type = {
                "logo.png": BrandAssetType.LOGO,
                "banner.png": BrandAssetType.BANNER,
            }.get(source.name.casefold())
            if asset_type is None:
                continue
            brand_plan_id = source.parent.name
            plan = plans.get(brand_plan_id)
            if plan is None:
                raise ValueError(f"Unknown brand_plan_id in staging: {brand_plan_id}")
            if source.parent.parent.name != plan.theme_key:
                raise ValueError(
                    f"Theme directory does not match {plan.public_name}: "
                    f"{source.parent.parent.name} != {plan.theme_key}"
                )
            requests.append(self.request(plan, asset_type, source))
        return tuple(requests)

    def read_manifest(self, manifest_path: Path) -> tuple[ExternalAssetImport, ...]:
        manifest_path = Path(manifest_path).expanduser().resolve()
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        rows = payload.get("assets") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            raise ValueError(
                "Import manifest must be a list or contain an assets list."
            )
        plans = {
            plan.brand_plan_id: plan for plan in self.store.planned_channel_brands()
        }
        requests = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(
                row.get("brand_plan_id"), str
            ):
                raise ValueError("Each manifest item requires brand_plan_id.")
            plan = plans.get(row["brand_plan_id"])
            if plan is None:
                raise ValueError(
                    f"Unknown brand_plan_id in manifest: {row['brand_plan_id']}"
                )
            item_count = 0
            for field, asset_type in (
                ("logo_path", BrandAssetType.LOGO),
                ("banner_path", BrandAssetType.BANNER),
            ):
                value = row.get(field)
                if value is None:
                    continue
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"{field} must be a non-empty path.")
                source = Path(value).expanduser()
                if not source.is_absolute():
                    source = manifest_path.parent / source
                requests.append(self.request(plan, asset_type, source))
                item_count += 1
            if not item_count:
                raise ValueError(
                    f"Manifest item for {plan.public_name} has no asset paths."
                )
        return tuple(requests)

    def import_requests(
        self, requests: tuple[ExternalAssetImport, ...]
    ) -> ExternalAssetBatchResult:
        plans = {
            plan.brand_plan_id: plan for plan in self.store.planned_channel_brands()
        }
        completed = []
        failed = []
        for request in requests:
            try:
                plan = plans[request.brand_plan_id]
                completed.append(
                    self.import_brand_asset(
                        plan, request.asset_type, request.source_file
                    )
                )
            except Exception as exc:
                failed.append(ExternalAssetImportFailure(request, str(exc)))
        return ExternalAssetBatchResult(tuple(completed), tuple(failed))


def _checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
