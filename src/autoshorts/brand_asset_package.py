"""Dry-run, canonical preparation, and audited import for external packages."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import struct
import subprocess
import unicodedata
import zipfile
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path, PurePosixPath

from autoshorts.brand_asset_import import (
    BrandAssetImportService,
    ExternalAssetImportFailure,
    ExternalAssetImportResult,
    ExternalAssetProvenance,
)
from autoshorts.brand_assets import BrandAssetType, validate_brand_asset


class PackageAssetStatus(StrEnum):
    MAPPED = "MAPPED"
    DUPLICATE_SOURCE = "DUPLICATE_SOURCE"
    NEEDS_ATTENTION = "NEEDS_ATTENTION"
    UNKNOWN = "UNKNOWN"
    SKIPPED_LOL = "SKIPPED_LOL"


class CanonicalTransform(StrEnum):
    NONE = "none"
    RESIZE_LANCZOS = "resize-lanczos"
    RESIZE_CENTER_CROP_LANCZOS = "resize-center-crop-lanczos"


@dataclass(frozen=True, slots=True)
class PackageAssetCandidate:
    source_path: Path
    source_file: str
    source_package: str
    source_checksum: str
    source_dimensions: tuple[int, int] | None
    brand_plan_id: str | None
    theme_key: str | None
    slot: int | None
    public_name: str | None
    asset_type: BrandAssetType | None
    transform: CanonicalTransform | None
    status: PackageAssetStatus
    issue: str = ""


@dataclass(frozen=True, slots=True)
class BrandPackageDryRun:
    package_root: Path
    source_package: str
    candidates: tuple[PackageAssetCandidate, ...]
    missing: tuple[tuple[str, BrandAssetType], ...]
    ignored_files: tuple[str, ...]

    @property
    def importable(self) -> tuple[PackageAssetCandidate, ...]:
        return tuple(
            item for item in self.candidates if item.status == PackageAssetStatus.MAPPED
        )


@dataclass(frozen=True, slots=True)
class BrandPackageImportResult:
    completed: tuple[ExternalAssetImportResult, ...]
    failed: tuple[ExternalAssetImportFailure, ...]
    needs_attention: tuple[PackageAssetCandidate, ...]


def normalize_asset_name(value: str) -> str:
    normalized = "".join(
        character
        for character in unicodedata.normalize("NFKD", value.casefold())
        if not unicodedata.combining(character)
    )
    return re.sub(r"[^a-z0-9]+", "", normalized)


def extract_zip_secure(
    archive: Path,
    destination: Path,
    *,
    maximum_files: int = 1000,
    maximum_uncompressed_bytes: int = 2 * 1024 * 1024 * 1024,
) -> Path:
    archive = Path(archive).resolve()
    destination = Path(destination).resolve()
    with zipfile.ZipFile(archive) as package:
        members = package.infolist()
        if len(members) > maximum_files:
            raise ValueError("ZIP contains too many entries.")
        if sum(item.file_size for item in members) > maximum_uncompressed_bytes:
            raise ValueError("ZIP exceeds the uncompressed size limit.")
        for item in members:
            path = PurePosixPath(item.filename)
            mode = item.external_attr >> 16
            if (
                not item.filename
                or path.is_absolute()
                or ".." in path.parts
                or stat.S_ISLNK(mode)
                or item.flag_bits & 0x1
            ):
                raise ValueError(f"Unsafe ZIP member: {item.filename}")
            target = destination.joinpath(*path.parts)
            if destination not in target.parents and target != destination:
                raise ValueError(f"ZIP member escapes destination: {item.filename}")
        destination.mkdir(parents=True, exist_ok=True)
        for item in members:
            target = destination.joinpath(*PurePosixPath(item.filename).parts)
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with package.open(item) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output)
    return destination


class BrandAssetPackageService:
    def __init__(self, project_root: Path, store) -> None:
        self.project_root = Path(project_root).resolve()
        self.store = store
        self.importer = BrandAssetImportService(self.project_root, store)

    def dry_run(self, package_root: Path) -> BrandPackageDryRun:
        package_root = Path(package_root).resolve()
        if not package_root.is_dir():
            raise ValueError(f"Package directory does not exist: {package_root}")
        plans = self.store.planned_channel_brands()
        plans_by_id = {plan.brand_plan_id.casefold(): plan for plan in plans}
        names: dict[str, list] = {}
        for plan in plans:
            names.setdefault(normalize_asset_name(plan.public_name), []).append(plan)
        candidates = []
        ignored_files = []
        for source in sorted(package_root.rglob("*")):
            if not source.is_file():
                continue
            if source.suffix.casefold() != ".png":
                ignored_files.append(source.relative_to(package_root).as_posix())
                continue
            asset_type = _asset_type(source)
            plan_by_id = plans_by_id.get(source.parent.name.casefold())
            matches = (
                [plan_by_id]
                if plan_by_id is not None
                else names.get(normalize_asset_name(source.parent.name), [])
            )
            checksum = _checksum(source)
            dimensions = _png_dimensions(source)
            relative = source.relative_to(package_root).as_posix()
            if asset_type is None:
                candidates.append(
                    PackageAssetCandidate(
                        source,
                        relative,
                        package_root.name,
                        checksum,
                        dimensions,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        PackageAssetStatus.UNKNOWN,
                        "Filename does not identify logo or banner explicitly.",
                    )
                )
                continue
            if len(matches) != 1:
                issue = (
                    "No exact normalized Brand Plan match."
                    if not matches
                    else "Ambiguous normalized Brand Plan match."
                )
                candidates.append(
                    PackageAssetCandidate(
                        source,
                        relative,
                        package_root.name,
                        checksum,
                        dimensions,
                        None,
                        None,
                        None,
                        None,
                        asset_type,
                        None,
                        PackageAssetStatus.UNKNOWN,
                        issue,
                    )
                )
                continue
            plan = matches[0]
            decode_error = _decode_error(source)
            transform, issue = (
                (None, decode_error)
                if decode_error
                else _canonical_transform(asset_type, dimensions)
            )
            status = (
                PackageAssetStatus.NEEDS_ATTENTION
                if issue
                else PackageAssetStatus.MAPPED
            )
            if plan.public_name in {"Tiltado", "Outplay", "MetaLab"}:
                status = PackageAssetStatus.SKIPPED_LOL
                issue = "LoL assets are already approved and are not auto-imported."
            candidates.append(
                PackageAssetCandidate(
                    source,
                    relative,
                    package_root.name,
                    checksum,
                    dimensions,
                    plan.brand_plan_id,
                    plan.theme_key,
                    plan.slot,
                    plan.public_name,
                    asset_type,
                    transform,
                    status,
                    issue,
                )
            )
        candidates = _mark_competing_sources(candidates)
        present = {
            (item.brand_plan_id, item.asset_type)
            for item in candidates
            if item.brand_plan_id and item.asset_type
        }
        missing = tuple(
            (plan.brand_plan_id, asset_type)
            for plan in plans
            if plan.public_name not in {"Tiltado", "Outplay", "MetaLab"}
            for asset_type in BrandAssetType
            if (plan.brand_plan_id, asset_type) not in present
        )
        return BrandPackageDryRun(
            package_root,
            package_root.name,
            tuple(candidates),
            missing,
            tuple(ignored_files),
        )

    def import_safe(self, dry_run: BrandPackageDryRun) -> BrandPackageImportResult:
        plans = {
            plan.brand_plan_id: plan for plan in self.store.planned_channel_brands()
        }
        completed = []
        failed = []
        attention = [
            item
            for item in dry_run.candidates
            if item.status
            in {PackageAssetStatus.NEEDS_ATTENTION, PackageAssetStatus.UNKNOWN}
        ]
        for candidate in dry_run.importable:
            assert candidate.brand_plan_id
            assert candidate.asset_type
            assert candidate.source_dimensions
            assert candidate.transform
            plan = plans[candidate.brand_plan_id]
            try:
                preserved = self._preserve_source(candidate)
                canonical = self._prepare_canonical(candidate, preserved)
                provenance = ExternalAssetProvenance(
                    candidate.source_package,
                    candidate.source_file,
                    preserved,
                    candidate.source_checksum,
                    candidate.source_dimensions[0],
                    candidate.source_dimensions[1],
                    candidate.transform.value,
                )
                completed.append(
                    self.importer.import_brand_asset(
                        plan,
                        candidate.asset_type,
                        canonical,
                        provenance=provenance,
                    )
                )
            except Exception as exc:
                request = self.importer.request(
                    plan, candidate.asset_type, candidate.source_path
                )
                failed.append(ExternalAssetImportFailure(request, str(exc)))
        return BrandPackageImportResult(
            tuple(completed), tuple(failed), tuple(attention)
        )

    def write_dry_run_report(
        self, dry_run: BrandPackageDryRun, destination: Path
    ) -> Path:
        payload = {
            "source_package": dry_run.source_package,
            "package_root": str(dry_run.package_root),
            "candidates": [
                {
                    "source_file": item.source_file,
                    "source_checksum": item.source_checksum,
                    "source_dimensions": list(item.source_dimensions)
                    if item.source_dimensions
                    else None,
                    "theme_key": item.theme_key,
                    "slot": item.slot,
                    "public_name": item.public_name,
                    "brand_plan_id": item.brand_plan_id,
                    "asset_type": item.asset_type.value
                    if item.asset_type
                    else None,
                    "transform": item.transform.value if item.transform else None,
                    "status": item.status.value,
                    "issue": item.issue,
                }
                for item in dry_run.candidates
            ],
            "missing": [
                {"brand_plan_id": brand_plan_id, "asset_type": asset_type.value}
                for brand_plan_id, asset_type in dry_run.missing
            ],
            "ignored_files": list(dry_run.ignored_files),
        }
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary, destination)
        return destination

    def _preserve_source(self, candidate: PackageAssetCandidate) -> Path:
        assert candidate.theme_key
        assert candidate.brand_plan_id
        assert candidate.asset_type
        package = _safe_component(candidate.source_package)
        destination = (
            self.project_root
            / "data"
            / "branding-import-sources"
            / package
            / candidate.theme_key
            / candidate.brand_plan_id
            / candidate.asset_type.value.lower()
            / f"{candidate.source_checksum}.png"
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            temporary = destination.with_suffix(".tmp")
            shutil.copyfile(candidate.source_path, temporary)
            if _checksum(temporary) != candidate.source_checksum:
                temporary.unlink(missing_ok=True)
                raise ValueError("Source checksum changed while preserving asset.")
            os.replace(temporary, destination)
            destination.chmod(0o444)
        elif _checksum(destination) != candidate.source_checksum:
            raise ValueError("Preserved source checksum does not match its provenance.")
        return destination

    def _prepare_canonical(
        self, candidate: PackageAssetCandidate, preserved: Path
    ) -> Path:
        assert candidate.theme_key
        assert candidate.brand_plan_id
        assert candidate.asset_type
        assert candidate.transform
        package = _safe_component(candidate.source_package)
        filename = (
            f"{candidate.asset_type.value.lower()}-"
            f"{candidate.source_checksum[:16]}.png"
        )
        destination = (
            self.project_root
            / "data"
            / "branding-import-prepared"
            / package
            / candidate.theme_key
            / candidate.brand_plan_id
            / filename
        )
        if destination.exists():
            validate_brand_asset(destination, candidate.asset_type)
            return destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(".tmp.png")
        if candidate.transform == CanonicalTransform.NONE:
            shutil.copyfile(preserved, temporary)
        else:
            if candidate.asset_type == BrandAssetType.LOGO:
                video_filter = "scale=1024:1024:flags=lanczos"
            else:
                video_filter = (
                    "scale=2560:1440:force_original_aspect_ratio=increase:"
                    "flags=lanczos,crop=2560:1440"
                )
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-y",
                    "-i",
                    str(preserved),
                    "-vf",
                    video_filter,
                    str(temporary),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode:
                temporary.unlink(missing_ok=True)
                raise ValueError(f"Canonical preparation failed: {result.stderr}")
        validate_brand_asset(temporary, candidate.asset_type)
        os.replace(temporary, destination)
        return destination

    def write_consolidated_manifest(self, destination: Path) -> Path:
        plans = self.store.planned_channel_brands()
        themes = {
            theme.theme_key: theme for theme in self.store.list_theme_templates()
        }
        rows = []
        for plan in plans:
            assets = self.store.brand_assets(plan.brand_plan_id)
            current = {
                asset_type: max(
                    (item for item in assets if item.asset_type == asset_type),
                    key=lambda item: item.version,
                    default=None,
                )
                for asset_type in BrandAssetType
            }
            row = {
                "theme": themes[plan.theme_key].display_name,
                "slot": plan.slot,
                "public_name": plan.public_name,
                "brand_plan_id": plan.brand_plan_id,
            }
            for asset_type in BrandAssetType:
                asset = current[asset_type]
                key = asset_type.value.lower()
                row[key] = _manifest_asset(asset) if asset else None
            rows.append(row)
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"assets": rows}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, destination)
        return destination


def _asset_type(path: Path) -> BrandAssetType | None:
    token = normalize_asset_name(path.stem)
    if token == "logo":
        return BrandAssetType.LOGO
    if token == "banner":
        return BrandAssetType.BANNER
    return None


def _canonical_transform(
    asset_type: BrandAssetType, dimensions: tuple[int, int] | None
) -> tuple[CanonicalTransform | None, str]:
    if dimensions is None:
        return None, "File is not a valid PNG."
    width, height = dimensions
    target = (1024, 1024) if asset_type == BrandAssetType.LOGO else (2560, 1440)
    if dimensions == target:
        return CanonicalTransform.NONE, ""
    if asset_type == BrandAssetType.LOGO:
        if width == height:
            return CanonicalTransform.RESIZE_LANCZOS, ""
        return None, "Non-square logo requires manual preparation."
    ratio = width / height
    target_ratio = target[0] / target[1]
    if abs(ratio - target_ratio) <= 0.02:
        if abs(ratio - target_ratio) < 0.000001:
            return CanonicalTransform.RESIZE_LANCZOS, ""
        return CanonicalTransform.RESIZE_CENTER_CROP_LANCZOS, ""
    return None, "Banner aspect ratio is not close enough to 16:9 for safe crop."


def _mark_competing_sources(
    candidates: list[PackageAssetCandidate],
) -> list[PackageAssetCandidate]:
    grouped: dict[tuple[str, BrandAssetType], list[int]] = {}
    for index, item in enumerate(candidates):
        if item.brand_plan_id and item.asset_type:
            grouped.setdefault((item.brand_plan_id, item.asset_type), []).append(index)
    result = list(candidates)
    for indexes in grouped.values():
        if len(indexes) < 2:
            continue
        checksums = {candidates[index].source_checksum for index in indexes}
        for position, index in enumerate(indexes):
            if len(checksums) == 1 and position:
                result[index] = replace(
                    candidates[index],
                    status=PackageAssetStatus.DUPLICATE_SOURCE,
                    issue="Identical duplicate source file.",
                )
            elif len(checksums) > 1:
                result[index] = replace(
                    candidates[index],
                    status=PackageAssetStatus.NEEDS_ATTENTION,
                    issue="Competing source files for the same asset.",
                )
    return result


def _png_dimensions(path: Path) -> tuple[int, int] | None:
    with path.open("rb") as stream:
        header = stream.read(24)
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    return struct.unpack(">II", header[16:24])


def _decode_error(path: Path) -> str:
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"],
        capture_output=True,
        text=True,
        check=False,
    )
    return "" if not result.returncode else "PNG decode failed."


def _checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_component(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-._") or "package"


def _manifest_asset(asset) -> dict[str, object]:
    return {
        "version": asset.version,
        "path": str(asset.file_path),
        "checksum": asset.checksum,
        "status": asset.status.value,
        "source_package": asset.source_package,
        "source_filename": asset.source_file,
        "source_preserved_path": str(asset.source_preserved_path)
        if asset.source_preserved_path
        else None,
        "source_checksum": asset.source_checksum,
        "source_dimensions": [asset.source_width, asset.source_height]
        if asset.source_width is not None and asset.source_height is not None
        else None,
        "canonical_dimensions": [asset.width, asset.height],
        "transform": asset.canonical_transform,
    }
