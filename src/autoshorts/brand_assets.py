"""Provider-neutral generation and local lifecycle for catalog brand images."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import subprocess
import tempfile
import zlib
from collections.abc import Iterable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from autoshorts.brand_planning import PlannedBrandStatus, PlannedChannelBrand
from autoshorts.theme_catalog import ChannelArchetype, ThemeTemplate


class BrandAssetType(StrEnum):
    LOGO = "LOGO"
    BANNER = "BANNER"


class BrandAssetStatus(StrEnum):
    GENERATED = "GENERATED"
    VALIDATED = "VALIDATED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    RETIRED = "RETIRED"


@dataclass(frozen=True, slots=True)
class GeneratedBrandAsset:
    brand_plan_id: str
    asset_type: BrandAssetType
    file_path: Path
    provider: str
    model: str
    prompt: str
    width: int
    height: int
    file_size: int
    checksum: str
    status: BrandAssetStatus
    created_at: str
    version: int = 0
    preview_path: Path | None = None
    delivery_path: Path | None = None


class BrandImageProvider(Protocol):
    provider_name: str
    model: str

    def generate_logo(
        self,
        planned_brand: PlannedChannelBrand,
        *,
        output_path: Path,
        prompt: str,
    ) -> GeneratedBrandAsset: ...

    def generate_banner(
        self,
        planned_brand: PlannedChannelBrand,
        *,
        output_path: Path,
        prompt: str,
    ) -> GeneratedBrandAsset: ...


ASSET_SPECS = {
    BrandAssetType.LOGO: (1024, 1024),
    BrandAssetType.BANNER: (2560, 1440),
}
BANNER_MAX_BYTES = 6 * 1024 * 1024

_GLOBAL_RULES = """Crie uma identidade visual original e não oficial.
Não reproduza logos existentes, trade dress, personagens protegidos, brasões
oficiais de times ou marcas oficiais de jogos. Não inclua watermark do gerador,
mockup de produto, UI do YouTube, URL ou @handle. Preserve alto contraste,
leitura clara e uma composição profissional."""


def brand_asset_prompt(
    asset_type: BrandAssetType,
    theme: ThemeTemplate,
    archetype: ChannelArchetype,
    brand: PlannedChannelBrand,
) -> str:
    width, height = ASSET_SPECS[asset_type]
    tagline_context = (
        f"Tagline: {brand.tagline}. " if asset_type == BrandAssetType.LOGO else ""
    )
    shared = (
        f"Tema: {theme.display_name}. Contexto: {theme.description}\n"
        f"Arquétipo editorial: {archetype.name}. Tom: {archetype.tone}. "
        f"Público: {archetype.audience}.\n"
        f"Marca: {brand.public_name}. Descrição: {brand.description}. "
        f"{tagline_context}Palavras-chave: "
        f"{', '.join(brand.brand_keywords)}.\n"
        f"Direção visual: {brand.visual_direction}. Paleta: "
        f"{', '.join(brand.palette)}.\n"
    )
    if asset_type == BrandAssetType.LOGO:
        specific = (
            f"Brief: {brand.logo_brief}\n"
            "Crie um master quadrado 1024x1024, logo-brand, símbolo ou monograma "
            "icônico, composição simples, alto contraste, poucos elementos e "
            "legibilidade em tamanho pequeno. Prefira não usar texto; se "
            f"indispensável, use somente o nome exato '{brand.public_name}'."
        )
    else:
        specific = (
            f"Brief: {brand.banner_brief}\n"
            "Minimal brand design. Use generous negative space. Use one dominant "
            "graphic motif and, optionally, one very subtle secondary motif. Use a "
            "limited number of visual elements. Avoid the visual density and "
            "ornamental complexity commonly associated with AI-generated artwork. "
            "Prefer intentional graphic-design composition, restrained shapes, "
            "flat or subtly dimensional forms, strong hierarchy, and a visually "
            "calm area. The result must look like clean brand design or motion "
            "graphics, not concept art, an AI illustration, a gaming wallpaper, or "
            "cinematic key art. Avoid particles, explosions, sparks, multiple light "
            "beams, accumulated geometry, floating objects, pseudo HUD, pseudo UI, "
            "fractals, collage, excessive texture, excessive glow, excessive "
            "gradients, complex scenery, depth of field, and volumetric effects.\n"
            "Crie um banner 16:9 em 2560x1440. Mantenha o motivo dominante e o nome "
            "na zona segura central de 1235x338 pixels. O banner deve compartilhar "
            "paleta, linguagem e tom com o logo, mas não ser um logo esticado. Use "
            f"somente o nome exato '{brand.public_name}', sem tagline, slogan, "
            "handle ou URL. Não coloque informação importante nas extremidades."
        )
    return f"{_GLOBAL_RULES}\n\n{shared}{specific}\nSaída: {width}x{height}."


def brand_asset_root(project_root: Path, brand: PlannedChannelBrand) -> Path:
    return project_root / "data" / "branding" / brand.theme_key / brand.brand_plan_id


def brand_asset_version_directory(
    project_root: Path,
    brand: PlannedChannelBrand,
    asset_type: BrandAssetType,
    version: int,
) -> Path:
    return (
        brand_asset_root(project_root, brand) / asset_type.value.lower() / f"v{version}"
    )


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def write_test_png(path: Path, width: int, height: int) -> None:
    """Write a small deterministic RGB PNG without optional image libraries."""
    path.parent.mkdir(parents=True, exist_ok=True)
    row = b"\x00" + (b"\x24\x3b\x66" * width)
    payload = b"\x89PNG\r\n\x1a\n"
    payload += _png_chunk(
        b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    )
    payload += _png_chunk(b"IDAT", zlib.compress(row * height, 9))
    payload += _png_chunk(b"IEND", b"")
    path.write_bytes(payload)


def _png_dimensions(path: Path) -> tuple[int, int]:
    with path.open("rb") as stream:
        header = stream.read(24)
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("Asset is not a decodable PNG.")
    return struct.unpack(">II", header[16:24])


def _decode_image(path: Path) -> None:
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise ValueError(f"Image is not decodable: {result.stderr.strip()}")


def _checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_brand_asset(path: Path, asset_type: BrandAssetType) -> tuple[int, int]:
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError("Generated asset file is missing or empty.")
    if path.suffix.casefold() != ".png":
        raise ValueError("Brand masters must be PNG files.")
    dimensions = _png_dimensions(path)
    if dimensions != ASSET_SPECS[asset_type]:
        raise ValueError(
            f"Invalid {asset_type.value.lower()} dimensions: {dimensions}."
        )
    _decode_image(path)
    return dimensions


def _render_preview(master: Path, preview: Path, asset_type: BrandAssetType) -> None:
    size = "384:384" if asset_type == BrandAssetType.LOGO else "640:360"
    result = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-i",
            str(master),
            "-vf",
            f"scale={size}:force_original_aspect_ratio=decrease",
            str(preview),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise ValueError(f"Could not create asset preview: {result.stderr.strip()}")


def _render_banner_export(master: Path, export: Path) -> None:
    for quality in (2, 4, 6, 8):
        result = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-i",
                str(master),
                "-q:v",
                str(quality),
                str(export),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if not result.returncode and export.stat().st_size <= BANNER_MAX_BYTES:
            return
    raise ValueError("Could not create a banner export below the 6 MB limit.")


class FakeBrandImageProvider:
    provider_name = "fake"
    model = "solid-color-test-v1"

    def _generate(self, brand, asset_type, output_path, prompt):
        width, height = ASSET_SPECS[asset_type]
        write_test_png(output_path, width, height)
        return GeneratedBrandAsset(
            brand.brand_plan_id,
            asset_type,
            output_path,
            self.provider_name,
            self.model,
            prompt,
            width,
            height,
            output_path.stat().st_size,
            _checksum(output_path),
            BrandAssetStatus.GENERATED,
            datetime.now(UTC).isoformat(),
        )

    def generate_logo(self, planned_brand, *, output_path, prompt):
        return self._generate(planned_brand, BrandAssetType.LOGO, output_path, prompt)

    def generate_banner(self, planned_brand, *, output_path, prompt):
        return self._generate(planned_brand, BrandAssetType.BANNER, output_path, prompt)


class PreparedFileBrandImageProvider:
    """Adapter for real images produced by an out-of-process image provider."""

    def __init__(
        self,
        sources: dict[tuple[str, BrandAssetType], Path],
        *,
        provider: str,
        model: str,
    ):
        self.sources = sources
        self.provider_name = provider
        self.model = model

    def _generate(self, brand, asset_type, output_path, prompt):
        source = self.sources[(brand.brand_plan_id, asset_type)]
        width, height = ASSET_SPECS[asset_type]
        result = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-i",
                str(source),
                "-vf",
                f"scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height}",
                str(output_path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(result.stderr.strip())
        return GeneratedBrandAsset(
            brand.brand_plan_id,
            asset_type,
            output_path,
            self.provider_name,
            self.model,
            prompt,
            width,
            height,
            output_path.stat().st_size,
            _checksum(output_path),
            BrandAssetStatus.GENERATED,
            datetime.now(UTC).isoformat(),
        )

    def generate_logo(self, planned_brand, *, output_path, prompt):
        return self._generate(planned_brand, BrandAssetType.LOGO, output_path, prompt)

    def generate_banner(self, planned_brand, *, output_path, prompt):
        return self._generate(planned_brand, BrandAssetType.BANNER, output_path, prompt)


class BrandAssetService:
    def __init__(self, project_root: Path, store, provider: BrandImageProvider) -> None:
        self.project_root = project_root
        self.store = store
        self.provider = provider

    def reconcile(self) -> tuple[GeneratedBrandAsset, ...]:
        recovered = []
        branding = self.project_root / "data" / "branding"
        if not branding.exists():
            return ()
        for manifest_path in branding.glob("*/*/*/v*/manifest.json"):
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            asset = _asset_from_manifest(payload)
            if self.store.brand_asset(
                asset.brand_plan_id, asset.asset_type, asset.version
            ):
                continue
            validate_brand_asset(asset.file_path, asset.asset_type)
            self.store.save_brand_asset(asset)
            recovered.append(asset)
        for partial in branding.glob("*/*/*/.tmp-*"):
            shutil.rmtree(partial, ignore_errors=True)
        return tuple(recovered)

    def generate(
        self,
        brand: PlannedChannelBrand,
        theme: ThemeTemplate,
        archetype: ChannelArchetype,
        asset_type: BrandAssetType,
        *,
        regenerate: bool = False,
        prompt_override: str | None = None,
    ) -> GeneratedBrandAsset:
        if brand.status != PlannedBrandStatus.APPROVED:
            raise ValueError("Only approved brand plans can generate assets.")
        self.reconcile()
        reusable = self.store.latest_valid_brand_asset(brand.brand_plan_id, asset_type)
        if reusable is not None and not regenerate:
            return reusable
        version = self.store.next_brand_asset_version(brand.brand_plan_id, asset_type)
        parent = brand_asset_root(self.project_root, brand) / asset_type.value.lower()
        parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".tmp-", dir=parent))
        final = brand_asset_version_directory(
            self.project_root, brand, asset_type, version
        )
        try:
            master = staging / "master.png"
            prompt = prompt_override or brand_asset_prompt(
                asset_type, theme, archetype, brand
            )
            if asset_type == BrandAssetType.LOGO:
                generated = self.provider.generate_logo(
                    brand, output_path=master, prompt=prompt
                )
            else:
                generated = self.provider.generate_banner(
                    brand, output_path=master, prompt=prompt
                )
            width, height = validate_brand_asset(master, asset_type)
            preview = staging / "preview.png"
            _render_preview(master, preview, asset_type)
            file_size = master.stat().st_size
            delivery = final / "master.png"
            if asset_type == BrandAssetType.BANNER and file_size > BANNER_MAX_BYTES:
                _render_banner_export(master, staging / "youtube.jpg")
                delivery = final / "youtube.jpg"
            final_asset = replace(
                generated,
                file_path=final / "master.png",
                prompt=prompt,
                width=width,
                height=height,
                file_size=file_size,
                checksum=_checksum(master),
                status=BrandAssetStatus.VALIDATED,
                version=version,
                preview_path=final / "preview.png",
                delivery_path=delivery,
            )
            manifest = _asset_manifest(final_asset)
            (staging / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            if final.exists():
                raise FileExistsError(f"Asset version already exists: {final}")
            os.replace(staging, final)
            self.store.save_brand_asset(final_asset)
            return final_asset
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    def generate_pending(
        self,
        requests: Iterable[
            tuple[PlannedChannelBrand, ThemeTemplate, ChannelArchetype, BrandAssetType]
        ],
        *,
        max_workers: int = 2,
    ) -> tuple[GeneratedBrandAsset, ...]:
        if not 1 <= max_workers <= 4:
            raise ValueError("Asset concurrency must be between 1 and 4.")
        pending = [
            request
            for request in requests
            if self.store.latest_valid_brand_asset(request[0].brand_plan_id, request[3])
            is None
        ]

        # The queue is deliberately processed serially for now. This is a valid
        # bounded concurrency of one and keeps a single SQLite connection safe.
        return tuple(self.generate(*request) for request in pending)


def _asset_manifest(asset: GeneratedBrandAsset) -> dict[str, object]:
    payload = asdict(asset)
    payload["asset_type"] = asset.asset_type.value
    payload["status"] = asset.status.value
    payload["file_path"] = str(asset.file_path)
    payload["preview_path"] = str(asset.preview_path) if asset.preview_path else None
    payload["delivery_path"] = str(asset.delivery_path) if asset.delivery_path else None
    return payload


def _asset_from_manifest(payload: dict[str, object]) -> GeneratedBrandAsset:
    return GeneratedBrandAsset(
        brand_plan_id=str(payload["brand_plan_id"]),
        asset_type=BrandAssetType(str(payload["asset_type"])),
        file_path=Path(str(payload["file_path"])),
        provider=str(payload["provider"]),
        model=str(payload["model"]),
        prompt=str(payload["prompt"]),
        width=int(payload["width"]),
        height=int(payload["height"]),
        file_size=int(payload["file_size"]),
        checksum=str(payload["checksum"]),
        status=BrandAssetStatus(str(payload["status"])),
        created_at=str(payload["created_at"]),
        version=int(payload["version"]),
        preview_path=Path(str(payload["preview_path"]))
        if payload.get("preview_path")
        else None,
        delivery_path=Path(str(payload["delivery_path"]))
        if payload.get("delivery_path")
        else None,
    )
