"""Provider-neutral brand identity proposals for publishing channels."""

from __future__ import annotations

import json
import os
import re
import time
import unicodedata
from collections.abc import Callable
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any, Protocol

from autoshorts.analysis_cost import estimate_cost_usd
from autoshorts.automation_config import PublishingChannel
from autoshorts.gemini_analysis_provider import _usage_from_response
from autoshorts.theme_catalog import ChannelArchetype, ThemeTemplate
from autoshorts.transcript_analysis import AnalysisUsage

DEFAULT_GEMINI_BRAND_MODEL = "gemini-3.5-flash-lite"
BRAND_MODEL_ENV = "AUTOSHORTS_GEMINI_BRAND_MODEL"


class BrandKitStatus(StrEnum):
    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    RETIRED = "RETIRED"


class HandleAvailability(StrEnum):
    UNKNOWN = "UNKNOWN"
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class HandleNormalization:
    original: str
    normalized: str


def normalize_handle_candidate(value: str) -> str:
    """Normalize a proposed handle without making availability claims."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Handle candidate cannot be empty.")
    original = value.strip()
    youtube_match = re.fullmatch(
        r"(?i)(?:https?://)?(?:www\.)?youtube\.com/@([^/?#]+)(?:[/?#].*)?",
        original,
    )
    if youtube_match:
        original = youtube_match.group(1)
    elif re.search(r"(?i)(?:https?://|www\.|/|\.(?:com|net|org)(?:/|$))", original):
        raise ValueError("Handle candidate cannot be a URL.")
    if re.fullmatch(r"\+?[\d\s().-]{10,}", original):
        raise ValueError("Handle candidate cannot look like a phone number.")
    text = original.lstrip("@").strip()
    text = "".join(
        character
        for character in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(character)
    )
    if not text.isascii():
        raise ValueError("Handle candidate contains unsupported characters.")
    text = re.sub(r"\s+", "-", text.casefold())
    if re.search(r"[^a-z0-9._-]", text):
        raise ValueError("Handle candidate contains unsupported characters.")
    text = text.strip("._-")
    normalized = f"@{text}"
    if not re.fullmatch(r"@[a-z0-9](?:[a-z0-9._-]{1,28}[a-z0-9])?", normalized):
        raise ValueError("Handle candidate does not satisfy the local handle policy.")
    return normalized


@dataclass(frozen=True, slots=True)
class BrandCandidate:
    publishing_channel_id: str
    public_name: str
    handle_candidates: tuple[str, ...]
    description: str
    tagline: str
    brand_keywords: tuple[str, ...]
    logo_brief: str
    banner_brief: str
    reason: str

    def __post_init__(self) -> None:
        fields = (
            self.publishing_channel_id,
            self.public_name,
            self.description,
            self.tagline,
            self.logo_brief,
            self.banner_brief,
            self.reason,
        )
        if any(not value.strip() for value in fields):
            raise ValueError("Brand candidate fields cannot be empty.")
        if len(self.handle_candidates) != 3 or len(set(self.handle_candidates)) != 3:
            raise ValueError("A brand candidate requires three unique handles.")
        if any(normalize_handle_candidate(x) != x for x in self.handle_candidates):
            raise ValueError("Handles must be normalized candidates prefixed with @.")
        if not self.brand_keywords:
            raise ValueError("Brand keywords cannot be empty.")


@dataclass(frozen=True, slots=True)
class ChannelBrandKit:
    publishing_channel_id: str
    public_name: str
    handle: str
    description: str
    tagline: str
    brand_keywords: tuple[str, ...]
    visual_direction: str
    palette: tuple[str, str, str, str]
    logo_brief: str
    banner_brief: str
    status: BrandKitStatus
    version: int

    def __post_init__(self) -> None:
        required = (
            self.publishing_channel_id,
            self.public_name,
            self.handle,
            self.description,
            self.tagline,
            self.visual_direction,
            self.logo_brief,
            self.banner_brief,
        )
        if any(not value.strip() for value in required):
            raise ValueError("Brand kit fields cannot be empty.")
        if normalize_handle_candidate(self.handle) != self.handle:
            raise ValueError("Brand kit handle must be normalized and start with @.")
        if not self.brand_keywords:
            raise ValueError("Brand kit keywords cannot be empty.")
        if len(self.palette) != 4 or any(not value.strip() for value in self.palette):
            raise ValueError("Brand kit requires four palette colors.")
        if self.version < 0:
            raise ValueError("Brand kit version cannot be negative.")


@dataclass(frozen=True, slots=True)
class BrandGenerationResult:
    candidates: tuple[BrandCandidate, ...]
    provider: str
    model: str
    usage: AnalysisUsage
    normalizations: tuple[HandleNormalization, ...] = ()


class BrandGenerationValidationError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        provider: str,
        model: str,
        usage: AnalysisUsage,
        normalizations: tuple[HandleNormalization, ...] = (),
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.model = model
        self.usage = usage
        self.normalizations = normalizations


class BrandIdentityProvider(Protocol):
    provider_name: str
    model: str

    def generate(
        self,
        theme: ThemeTemplate,
        channels: tuple[tuple[ChannelArchetype, PublishingChannel], ...],
    ) -> BrandGenerationResult: ...


class BrandImageProvider(Protocol):
    """Reserved interface; no image implementation exists yet."""

    def generate_assets(self, brand_kit: ChannelBrandKit) -> tuple[str, ...]: ...


CANDIDATE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "public_name",
        "handle_candidates",
        "description",
        "tagline",
        "brand_keywords",
        "logo_brief",
        "banner_brief",
        "reason",
    ],
    "properties": {
        "public_name": {"type": "string"},
        "handle_candidates": {
            "type": "array",
            "minItems": 3,
            "maxItems": 3,
            "items": {"type": "string"},
        },
        "description": {"type": "string"},
        "tagline": {"type": "string"},
        "brand_keywords": {
            "type": "array",
            "minItems": 3,
            "items": {"type": "string"},
        },
        "logo_brief": {"type": "string"},
        "banner_brief": {"type": "string"},
        "reason": {"type": "string"},
    },
}
RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["channels"],
    "properties": {
        "channels": {
            "type": "array",
            "minItems": 3,
            "maxItems": 3,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["publishing_channel_id", "candidates"],
                "properties": {
                    "publishing_channel_id": {"type": "string"},
                    "candidates": {
                        "type": "array",
                        "minItems": 5,
                        "maxItems": 5,
                        "items": CANDIDATE_SCHEMA,
                    },
                },
            },
        }
    },
}


SYSTEM_PROMPT = """Você é estrategista de marcas para canais brasileiros.
Crie cinco propostas para cada um dos três canais. Nomes devem ser curtos,
memoráveis, pronunciáveis em português e diferentes entre si. Os canais devem
pertencer ao mesmo nicho, mas ter vocabulário, tagline e direção visual distintos.
Não copie marcas conhecidas, não use Riot ou League of Legends no nome e não
pareça canal oficial. Handles são apenas candidatos: nunca afirme disponibilidade.
Every handle candidate must be returned already normalized and prefixed with @.
Não gere imagens; escreva briefs originais para logo e banner."""


class GeminiBrandIdentityProvider:
    provider_name = "gemini"

    def __init__(self, *, model: str | None = None, client: Any | None = None) -> None:
        self.model = model or os.environ.get(
            BRAND_MODEL_ENV, DEFAULT_GEMINI_BRAND_MODEL
        )
        api_key = os.environ.get("GEMINI_API_KEY")
        if client is None and not api_key:
            raise RuntimeError("Gemini não configurado. Defina GEMINI_API_KEY.")
        if client is None:
            from google import genai

            client = genai.Client(api_key=api_key)
        self._client = client

    def generate(self, theme, channels) -> BrandGenerationResult:
        if len(channels) != 3:
            raise ValueError("Coordinated generation requires exactly three channels.")
        estimate_cost_usd(self.model, 0, 0)
        context = {
            "theme": {
                "key": theme.theme_key,
                "name": theme.display_name,
                "prompt": theme.base_prompt,
            },
            "channels": [
                {
                    "publishing_channel": {
                        "channel_id": channel.channel_id,
                        "internal_name": channel.name,
                    },
                    "archetype": asdict(archetype),
                }
                for archetype, channel in channels
            ],
        }
        started = time.monotonic()
        response = self._client.models.generate_content(
            model=self.model,
            contents=json.dumps(context, ensure_ascii=False),
            config={
                "system_instruction": SYSTEM_PROMPT,
                "response_mime_type": "application/json",
                "response_json_schema": RESPONSE_SCHEMA,
                "thinking_config": {"thinking_level": "minimal"},
                "temperature": 0.7,
            },
        )
        usage = _usage_from_response(response, self.model, time.monotonic() - started)
        normalizations: list[HandleNormalization] = []
        try:
            raw = response.parsed
            expected = {channel.channel_id for _, channel in channels}
            candidates = []
            for group in raw["channels"]:
                channel_id = group["publishing_channel_id"]
                if channel_id not in expected:
                    raise ValueError("Gemini returned an unknown publishing channel.")
                for item in group["candidates"]:
                    handles = []
                    for original in item["handle_candidates"]:
                        normalized = normalize_handle_candidate(original)
                        handles.append(normalized)
                        if original != normalized:
                            normalizations.append(
                                HandleNormalization(original, normalized)
                            )
                    candidates.append(
                        BrandCandidate(
                            publishing_channel_id=channel_id,
                            public_name=item["public_name"].strip(),
                            handle_candidates=tuple(handles),
                            description=item["description"].strip(),
                            tagline=item["tagline"].strip(),
                            brand_keywords=tuple(item["brand_keywords"]),
                            logo_brief=item["logo_brief"].strip(),
                            banner_brief=item["banner_brief"].strip(),
                            reason=item["reason"].strip(),
                        )
                    )
            if len(candidates) != 15 or any(
                sum(x.publishing_channel_id == cid for x in candidates) != 5
                for cid in expected
            ):
                raise ValueError("Gemini must return five candidates for each channel.")
            if len({x.public_name.casefold() for x in candidates}) != 15:
                raise ValueError("All public names must be distinct.")
        except (KeyError, TypeError, ValueError) as exc:
            raise BrandGenerationValidationError(
                str(exc),
                provider=self.provider_name,
                model=self.model,
                usage=usage,
                normalizations=tuple(normalizations),
            ) from exc
        return BrandGenerationResult(
            tuple(candidates),
            self.provider_name,
            self.model,
            usage,
            tuple(normalizations),
        )


def is_transient_brand_error(exc: Exception) -> bool:
    if isinstance(exc, BrandGenerationValidationError):
        return False
    text = str(exc).casefold()
    transient_markers = (
        "429",
        "500",
        "502",
        "503",
        "504",
        "unavailable",
        "timeout",
        "timed out",
        "connection reset",
        "connection aborted",
        "temporarily unavailable",
        "high demand",
    )
    return any(marker in text for marker in transient_markers)


def generate_brand_identity_with_retry(
    provider: BrandIdentityProvider,
    theme: ThemeTemplate,
    channels: tuple[tuple[ChannelArchetype, PublishingChannel], ...],
    *,
    on_attempt: Callable[[int, int], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[BrandGenerationResult, int]:
    """Perform one logical generation with at most three transient attempts."""
    delays = (0.0, 2.0, 6.0)
    for attempt, delay in enumerate(delays, 1):
        if delay:
            sleep(delay)
        if on_attempt:
            on_attempt(attempt, len(delays))
        try:
            return provider.generate(theme, channels), attempt
        except Exception as exc:
            if not is_transient_brand_error(exc) or attempt == len(delays):
                raise
    raise AssertionError("unreachable")
