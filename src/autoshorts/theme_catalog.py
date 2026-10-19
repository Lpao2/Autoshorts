"""Reusable theme templates, independent from active production configuration."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from autoshorts.automation_config import (
    ColorPalette,
    EditorialProfile,
    PublishingChannel,
    ThemePolicy,
    ThemeProfile,
    VisualProfile,
)


class CatalogStatus(StrEnum):
    DRAFT = "DRAFT"
    READY = "READY"
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


class CatalogProvenance(StrEnum):
    LEGACY = "LEGACY"
    MANUAL = "MANUAL"
    GENERATED = "GENERATED"
    REFINED = "REFINED"


class AuthorizationStatus(StrEnum):
    UNKNOWN = "UNKNOWN"
    REQUIRED = "REQUIRED"
    AUTHORIZED = "AUTHORIZED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class ReuseRisk(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


@dataclass(frozen=True, slots=True)
class SourceStrategy:
    source_types: tuple[str, ...]
    description: str
    requires_creator_authorization: bool
    preferred_sources: tuple[str, ...]
    avoid_sources: tuple[str, ...]
    notes: str = ""


@dataclass(frozen=True, slots=True)
class RightsProfile:
    authorization_required: bool
    authorization_status: AuthorizationStatus
    reuse_risk: ReuseRisk
    notes: str


@dataclass(frozen=True, slots=True)
class ThemeReadiness:
    editorial_ready: bool
    channel_profiles_ready: bool
    source_strategy_ready: bool
    rights_strategy_ready: bool
    brand_direction_ready: bool
    production_fit_ready: bool

    @property
    def ready(self) -> bool:
        return all((
            self.editorial_ready, self.channel_profiles_ready,
            self.source_strategy_ready, self.rights_strategy_ready,
            self.brand_direction_ready, self.production_fit_ready,
        ))


@dataclass(frozen=True, slots=True)
class BrandBlueprint:
    keywords: tuple[str, ...] = ()
    name_direction: str = ""
    handle_direction: str = ""
    description_direction: str = ""
    tagline_direction: str = ""
    logo_brief: str = ""
    banner_brief: str = ""


@dataclass(frozen=True, slots=True)
class BrandProfile:
    public_name: str = ""
    handle: str = ""
    description: str = ""
    tagline: str = ""
    logo_path: str = ""
    banner_path: str = ""


@dataclass(frozen=True, slots=True)
class PromptVersion:
    version: int
    prompt: str
    provenance: CatalogProvenance
    active: bool = False
    created_at: str = ""


@dataclass(frozen=True, slots=True)
class ChannelArchetype:
    slot: int
    name: str
    editorial_focus: tuple[str, ...] = ()
    editorial_prompt: str = ""
    headline_style: str = ""
    tone: str = ""
    visual_direction: str = ""
    audience: str = ""
    content_preferences: tuple[str, ...] = ()
    content_avoid: tuple[str, ...] = ()
    palette: ColorPalette = field(default_factory=ColorPalette)
    brand: BrandProfile = field(default_factory=BrandProfile)
    brand_blueprint: BrandBlueprint = field(default_factory=BrandBlueprint)

    def __post_init__(self) -> None:
        if self.slot not in {1, 2, 3}:
            raise ValueError("Archetype slot must be 1, 2 or 3.")


@dataclass(frozen=True, slots=True)
class ThemeTemplate:
    theme_key: str
    display_name: str
    description: str
    base_prompt: str
    tags: tuple[str, ...]
    source_strategy: SourceStrategy
    rights_profile: RightsProfile
    readiness: ThemeReadiness
    production_defaults: dict[str, object]
    channel_archetypes: tuple[ChannelArchetype, ...]
    status: CatalogStatus
    provenance: CatalogProvenance
    prompt_versions: tuple[PromptVersion, ...] = ()

    def __post_init__(self) -> None:
        if not self.theme_key.strip() or not self.display_name.strip():
            raise ValueError("Theme key and display name cannot be empty.")
        slots = [item.slot for item in self.channel_archetypes]
        if len(slots) != len(set(slots)) or any(
            slot not in {1, 2, 3} for slot in slots
        ):
            raise ValueError("Channel archetypes must use unique slots 1, 2 and 3.")


def branding_directory(
    project_root: Path, theme_key: str, channel_id: str
) -> Path:
    """Return the planned filesystem location; images are never stored in SQLite."""
    return project_root / "data" / "branding" / theme_key / channel_id


def instantiate_theme(
    template: ThemeTemplate, *, enabled: bool = False
) -> ThemeProfile:
    """Create an inactive production profile explicitly from a catalog template."""
    policy = ThemePolicy(
        videos_per_creator=int(
            template.production_defaults.get("videos_per_creator", 1)
        ),
        maximum_age_days=int(
            template.production_defaults.get("maximum_age_days", 3650)
        ),
        shorts_per_video=3,
    )
    theme_id = str(uuid5(NAMESPACE_URL, f"autoshorts:theme:{template.theme_key}"))
    channels = tuple(
        PublishingChannel(
            channel_id=str(
                uuid5(NAMESPACE_URL, f"autoshorts:channel:{theme_id}:{item.slot}")
            ),
            name=item.name,
            slot=item.slot,
            enabled=False,
            editorial_profile=EditorialProfile(
                prompt_append=item.editorial_prompt,
                headline_style=item.headline_style,
                tone=item.tone,
            ),
            visual_profile=VisualProfile(palette=item.palette),
        )
        for item in template.channel_archetypes
    )
    return ThemeProfile(
        name=template.display_name,
        prompt=template.base_prompt,
        creators=(),
        output_subdirectory=template.theme_key,
        enabled=enabled,
        policy=policy,
        theme_id=theme_id,
        channels=channels,
    )


def load_theme_seed(path: str | Path) -> tuple[ThemeTemplate, ...]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema_version") not in {1, 2}:
        raise ValueError("Unsupported theme catalog seed schema.")
    result = []
    for raw in payload["themes"]:
        archetypes = []
        for item in raw["channel_archetypes"]:
            palette = item["palette"]
            archetypes.append(
                ChannelArchetype(
                    slot=item["slot"],
                    name=item["name"],
                    editorial_focus=tuple(item["editorial_focus"]),
                    editorial_prompt=item["editorial_prompt"],
                    headline_style=item["headline_style"],
                    tone=item["tone"],
                    visual_direction=item["visual_direction"],
                    audience=item["audience"],
                    content_preferences=tuple(item["content_preferences"]),
                    content_avoid=tuple(item["content_avoid"]),
                    palette=ColorPalette(*palette),
                    brand=BrandProfile(**item["brand"]),
                    brand_blueprint=BrandBlueprint(
                        keywords=tuple(item["brand_blueprint"]["keywords"]),
                        **{k: v for k, v in item["brand_blueprint"].items()
                           if k != "keywords"},
                    ),
                )
            )
        provenance = CatalogProvenance(raw["provenance"])
        result.append(
            ThemeTemplate(
                theme_key=raw["theme_key"],
                display_name=raw["display_name"],
                description=raw["description"],
                base_prompt=raw["base_prompt"],
                tags=tuple(raw["tags"]),
                source_strategy=SourceStrategy(
                    source_types=tuple(raw["source_strategy"]["source_types"]),
                    preferred_sources=tuple(
                        raw["source_strategy"]["preferred_sources"]
                    ),
                    avoid_sources=tuple(raw["source_strategy"]["avoid_sources"]),
                    **{
                        k: v for k, v in raw["source_strategy"].items()
                        if k not in {
                            "source_types", "preferred_sources", "avoid_sources"
                        }
                    },
                ),
                rights_profile=RightsProfile(
                    authorization_status=AuthorizationStatus(
                        raw["rights_profile"]["authorization_status"]
                    ),
                    reuse_risk=ReuseRisk(raw["rights_profile"]["reuse_risk"]),
                    authorization_required=raw["rights_profile"][
                        "authorization_required"
                    ],
                    notes=raw["rights_profile"]["notes"],
                ),
                readiness=ThemeReadiness(**raw["readiness"]),
                production_defaults=raw["production_defaults"],
                channel_archetypes=tuple(archetypes),
                status=CatalogStatus(raw["status"]),
                provenance=provenance,
                prompt_versions=(
                    PromptVersion(1, raw["base_prompt"], provenance, True),
                ),
            )
        )
    return tuple(result)
