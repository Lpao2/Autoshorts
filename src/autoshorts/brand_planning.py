"""Catalog-level brand planning, independent from operational channels."""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from uuid import NAMESPACE_URL, uuid5

from autoshorts.analysis_cost import estimate_cost_usd
from autoshorts.automation_config import PublishingChannel
from autoshorts.brand_identity import (
    BrandCandidate,
    BrandKitStatus,
    ChannelBrandKit,
    HandleAvailability,
    is_transient_brand_error,
)
from autoshorts.gemini_analysis_provider import _usage_from_response
from autoshorts.theme_catalog import ChannelArchetype, ThemeTemplate
from autoshorts.transcript_analysis import AnalysisUsage


class PlannedBrandStatus(StrEnum):
    PROPOSED_FOR_REVIEW = "PROPOSED_FOR_REVIEW"
    APPROVED = "APPROVED"
    RETIRED = "RETIRED"


@dataclass(frozen=True, slots=True)
class CatalogBrandTarget:
    channel_id: str
    name: str
    slot: int


@dataclass(frozen=True, slots=True)
class PlannedChannelBrand:
    brand_plan_id: str
    theme_key: str
    slot: int
    public_name: str
    preferred_handle: str
    backup_handles: tuple[str, str]
    description: str
    tagline: str
    brand_keywords: tuple[str, ...]
    visual_direction: str
    palette: tuple[str, str, str, str]
    logo_brief: str
    banner_brief: str
    status: PlannedBrandStatus
    handle_availability: HandleAvailability
    provider: str
    model: str
    version: int = 1
    source_generation_id: int | None = None


@dataclass(frozen=True, slots=True)
class PlannedIdentityAssignment:
    brand_plan_id: str
    planned_identity_id: str


@dataclass(frozen=True, slots=True)
class BrandRerankResult:
    selected_names: tuple[tuple[int, str], ...]
    reasons: tuple[tuple[int, str], ...]
    provider: str
    model: str
    usage: AnalysisUsage


class BrandCandidateReranker(Protocol):
    provider_name: str
    model: str

    def rerank(
        self, theme: ThemeTemplate, candidates: tuple[BrandCandidate, ...]
    ) -> BrandRerankResult: ...


def stable_brand_plan_id(theme_key: str, slot: int) -> str:
    return str(uuid5(NAMESPACE_URL, f"autoshorts:brand-plan:{theme_key}:{slot}"))


def catalog_brand_targets(
    theme: ThemeTemplate,
) -> tuple[tuple[ChannelArchetype, CatalogBrandTarget], ...]:
    return tuple(
        (
            archetype,
            CatalogBrandTarget(
                f"catalog:{theme.theme_key}:{archetype.slot}",
                archetype.name,
                archetype.slot,
            ),
        )
        for archetype in sorted(theme.channel_archetypes, key=lambda item: item.slot)
    )


def planned_identity_for(theme_index: int, slot: int) -> str:
    return f"google-plan-{((theme_index + slot - 1) % 4) + 1}"


def materialize_brand_plan(
    planned_brand: PlannedChannelBrand, publishing_channel: PublishingChannel
) -> ChannelBrandKit:
    if planned_brand.status not in {
        PlannedBrandStatus.PROPOSED_FOR_REVIEW,
        PlannedBrandStatus.APPROVED,
    }:
        raise ValueError("Only an active brand plan can be materialized.")
    if planned_brand.slot != publishing_channel.slot:
        raise ValueError("Brand plan and publishing channel slots must match.")
    return ChannelBrandKit(
        publishing_channel.channel_id,
        planned_brand.public_name,
        planned_brand.preferred_handle,
        planned_brand.description,
        planned_brand.tagline,
        planned_brand.brand_keywords,
        planned_brand.visual_direction,
        planned_brand.palette,
        planned_brand.logo_brief,
        planned_brand.banner_brief,
        BrandKitStatus.DRAFT,
        0,
        source_plan_id=planned_brand.brand_plan_id,
    )


def branding_directory(project_root: Path, brand: PlannedChannelBrand) -> Path:
    return project_root / "data" / "branding" / brand.theme_key / brand.brand_plan_id


def global_brand_collisions(
    plans: tuple[PlannedChannelBrand, ...],
) -> tuple[str, ...]:
    """Return stable plan IDs requiring review for exact or near collisions."""
    collisions: set[str] = set()
    names: dict[str, str] = {}
    handles: dict[str, str] = {}
    for plan in plans:
        name_key = re.sub(r"[^a-z0-9]", "", plan.public_name.casefold())
        handle_values = (plan.preferred_handle, *plan.backup_handles)
        if name_key in names:
            collisions.update((plan.brand_plan_id, names[name_key]))
        names[name_key] = plan.brand_plan_id
        for handle in handle_values:
            handle_key = re.sub(r"[._-]", "", handle.casefold())
            if handle_key in handles:
                collisions.update((plan.brand_plan_id, handles[handle_key]))
            handles[handle_key] = plan.brand_plan_id
    return tuple(sorted(collisions))


RERANK_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["winners"],
    "properties": {
        "winners": {
            "type": "array",
            "minItems": 3,
            "maxItems": 3,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["slot", "public_name", "reason"],
                "properties": {
                    "slot": {"type": "integer"},
                    "public_name": {"type": "string"},
                    "reason": {"type": "string"},
                },
            },
        }
    },
}


class GeminiBrandCandidateReranker:
    provider_name = "gemini"

    def __init__(self, *, model: str | None = None, client: Any | None = None) -> None:
        self.model = model or os.environ.get(
            "AUTOSHORTS_GEMINI_BRAND_MODEL", "gemini-3.5-flash-lite"
        )
        if client is None:
            from google import genai

            client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
        self._client = client

    def rerank(self, theme, candidates) -> BrandRerankResult:
        estimate_cost_usd(self.model, 0, 0)
        by_slot = {}
        for candidate in candidates:
            match = re.fullmatch(
                r"catalog:[^:]+:([123])", candidate.publishing_channel_id
            )
            if not match:
                raise ValueError("Catalog candidate has an invalid target ID.")
            by_slot.setdefault(int(match.group(1)), []).append(candidate)
        if set(by_slot) != {1, 2, 3} or any(
            len(items) != 5 for items in by_slot.values()
        ):
            raise ValueError("Reranking requires five candidates for each slot.")
        payload = {
            "theme": {"name": theme.display_name, "description": theme.description},
            "channels": [
                {
                    "slot": archetype.slot,
                    "archetype": {
                        "name": archetype.name,
                        "editorial_prompt": archetype.editorial_prompt,
                        "visual_direction": archetype.visual_direction,
                    },
                    "candidates": [
                        {
                            "public_name": item.public_name,
                            "handles": item.handle_candidates,
                            "description": item.description,
                            "tagline": item.tagline,
                            "keywords": item.brand_keywords,
                            "logo_brief": item.logo_brief,
                            "banner_brief": item.banner_brief,
                        }
                        for item in by_slot[archetype.slot]
                    ],
                }
                for archetype in sorted(theme.channel_archetypes, key=lambda x: x.slot)
            ],
        }
        instruction = (
            "Escolha exatamente um candidato existente para cada slot, avaliando os "
            "três em conjunto. Considere memorabilidade, clareza, pronúncia, nicho, "
            "diferenciação, potencial visual/editorial, handles, longevidade, "
            "aparência "
            "não oficial e ausência de cópia evidente. Não invente ou altere nomes."
        )
        started = time.monotonic()
        response = self._client.models.generate_content(
            model=self.model,
            contents=json.dumps(payload, ensure_ascii=False),
            config={
                "system_instruction": instruction,
                "response_mime_type": "application/json",
                "response_json_schema": RERANK_SCHEMA,
                "thinking_config": {"thinking_level": "minimal"},
                "temperature": 0.2,
            },
        )
        usage = _usage_from_response(response, self.model, time.monotonic() - started)
        winners = response.parsed["winners"]
        selected = tuple((int(item["slot"]), item["public_name"]) for item in winners)
        if {slot for slot, _ in selected} != {1, 2, 3}:
            raise ValueError("Reranker must select each slot exactly once.")
        for slot, name in selected:
            if name not in {item.public_name for item in by_slot[slot]}:
                raise ValueError("Reranker selected an unknown candidate.")
        return BrandRerankResult(
            selected,
            tuple((int(item["slot"]), item["reason"]) for item in winners),
            self.provider_name,
            self.model,
            usage,
        )


def rerank_brand_candidates_with_retry(
    provider: BrandCandidateReranker,
    theme: ThemeTemplate,
    candidates: tuple[BrandCandidate, ...],
    *,
    sleep=time.sleep,
) -> tuple[BrandRerankResult, int]:
    for attempt, delay in enumerate((0.0, 2.0, 6.0), 1):
        if delay:
            sleep(delay)
        try:
            return provider.rerank(theme, candidates), attempt
        except Exception as exc:
            if attempt == 3 or not is_transient_brand_error(exc):
                raise
    raise AssertionError("unreachable")


def plans_from_winners(
    theme: ThemeTemplate,
    candidates: tuple[BrandCandidate, ...],
    ranking: BrandRerankResult,
    *,
    generation_id: int | None = None,
) -> tuple[PlannedChannelBrand, ...]:
    archetypes = {item.slot: item for item in theme.channel_archetypes}
    result = []
    for slot, name in ranking.selected_names:
        target = f"catalog:{theme.theme_key}:{slot}"
        candidate = next(
            item
            for item in candidates
            if item.publishing_channel_id == target and item.public_name == name
        )
        archetype = archetypes[slot]
        palette = archetype.palette
        result.append(
            PlannedChannelBrand(
                stable_brand_plan_id(theme.theme_key, slot),
                theme.theme_key,
                slot,
                candidate.public_name,
                candidate.handle_candidates[0],
                (candidate.handle_candidates[1], candidate.handle_candidates[2]),
                candidate.description,
                candidate.tagline,
                candidate.brand_keywords,
                archetype.visual_direction,
                (
                    palette.primary,
                    palette.secondary,
                    palette.accent,
                    palette.background,
                ),
                candidate.logo_brief,
                candidate.banner_brief,
                PlannedBrandStatus.PROPOSED_FOR_REVIEW,
                HandleAvailability.UNKNOWN,
                ranking.provider,
                ranking.model,
                source_generation_id=generation_id,
            )
        )
    return tuple(sorted(result, key=lambda item: item.slot))
