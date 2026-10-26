"""AI metadata drafts and optional approval for READY production artifacts."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from autoshorts.automation_config import AutomationConfig
from autoshorts.gemini_calls import (
    GeminiPermanentError,
    call_gemini,
    create_gemini_client,
)
from autoshorts.scheduler import SchedulerService
from autoshorts.scheduler_models import PublicationJob
from autoshorts.state_store import (
    MetadataDraft,
    MetadataDraftStatus,
    PublicationStatus,
    StateStore,
)
from autoshorts.youtube_publishing import (
    PublicationMetadata,
    YouTubePublicationService,
)

METADATA_VERSION = "ready-metadata-v1"
DEFAULT_MODEL = "gemini-3.8-flash"


@dataclass(frozen=True, slots=True)
class GeneratedMetadata:
    title_candidates: tuple[str, ...]
    selected_title: str
    description: str
    tags: tuple[str, ...]
    hashtags: tuple[str, ...]
    selection_reason: str
    provider: str
    model: str


class MetadataProvider(Protocol):
    provider_name: str
    model: str

    def generate(self, prompt: str) -> GeneratedMetadata: ...


class GeminiMetadataProvider:
    provider_name = "gemini"

    def __init__(self, *, model: str | None = None, client: Any | None = None):
        self.model = model or os.getenv(
            "AUTOSHORTS_GEMINI_METADATA_MODEL", DEFAULT_MODEL
        )
        api_key = os.getenv("GEMINI_API_KEY")
        if client is None and not api_key:
            raise RuntimeError("Gemini metadata requires GEMINI_API_KEY.")
        if client is None:
            client = create_gemini_client(api_key=api_key)
        self._client = client

    def generate(self, prompt: str) -> GeneratedMetadata:
        response = call_gemini(
            operation="metadata",
            model=self.model,
            request=lambda: self._client.models.generate_content(
                model=self.model,
                contents=prompt,
                config={
                    "system_instruction": (
                        "Você cria metadata honesta e concisa para YouTube Shorts. "
                        "Use somente fatos presentes no trecho. Evite clickbait falso, "
                        "ALL CAPS, links inventados e tags irrelevantes."
                    ),
                    "response_mime_type": "application/json",
                    "response_json_schema": _RESPONSE_SCHEMA,
                    "temperature": 0.35,
                    "thinking_config": {"thinking_level": "low"},
                },
            ),
        )
        raw = response.parsed
        if not isinstance(raw, dict):
            raise ValueError("Gemini returned invalid metadata JSON.")
        candidates = tuple(str(item).strip() for item in raw["title_candidates"])
        selected = str(raw["selected_title"]).strip()
        if len(candidates) != 3 or selected not in candidates:
            raise ValueError("Metadata must contain three titles and one winner.")
        return GeneratedMetadata(
            candidates,
            selected,
            str(raw["description"]).strip(),
            tuple(str(item).strip() for item in raw["tags"] if str(item).strip()),
            tuple(
                str(item).strip() for item in raw["hashtags"] if str(item).strip()
            ),
            str(raw["selection_reason"]).strip(),
            self.provider_name,
            self.model,
        )


def generate_metadata_draft(
    *,
    youtube_id: str,
    publishing_channel_id: str,
    config: AutomationConfig,
    state: StateStore,
    provider: MetadataProvider,
) -> MetadataDraft:
    privacy_status = os.environ.get("AUTOSHORTS_PUBLICATION_PRIVACY", "private")
    if privacy_status not in {"private", "unlisted", "public"}:
        raise ValueError(
            "AUTOSHORTS_PUBLICATION_PRIVACY must be private, unlisted or public."
        )
    ready = state.get_ready_artifact(youtube_id)
    video = state.get_video(youtube_id)
    if ready is None or video is None or video.status.value != "READY":
        raise ValueError("Metadata generation requires a READY source artifact.")
    theme, channel = _resolve_channel(config, publishing_channel_id)
    if theme.output_subdirectory != video.theme_name.casefold():
        raise ValueError("Publishing channel theme does not match the source theme.")
    market = state.theme_market_profile(
        theme.output_subdirectory, "youtube", "BR", "pt-BR"
    )
    if market is None or not market.enabled:
        raise ValueError("Active youtube/BR/pt-BR market profile is required.")
    kit = state.get_approved_brand_kit(channel.channel_id)
    if kit is None or not kit.source_plan_id:
        raise ValueError("Publishing channel requires an approved Brand Plan.")
    transcript = state.get_transcript(youtube_id)
    analysis = state.get_analysis(youtube_id)
    if transcript is None or analysis is None:
        raise ValueError("READY source is missing transcript or analysis provenance.")
    selection_path = transcript.file_path.parent / "selection.json"
    if not selection_path.is_file():
        raise ValueError("Persisted segment selection is required.")
    selection = json.loads(selection_path.read_text(encoding="utf-8"))
    prompt = _prompt(video.title, selection, theme.prompt, market, kit, channel)
    prompt_hash = hashlib.sha256(
        f"{prompt}\nPublication privacy: {privacy_status}".encode()
    ).hexdigest()
    fingerprint = _fingerprint(
        ready.checksum,
        channel.channel_id,
        kit.source_plan_id,
        provider.model,
        prompt_hash,
    )
    existing = state.metadata_draft_by_fingerprint(fingerprint)
    if existing and existing.status not in {
        MetadataDraftStatus.FAILED_RECOVERABLE,
        MetadataDraftStatus.FAILED_CONFIGURATION,
    }:
        return existing
    policy = state.metadata_review_policy(channel.channel_id)
    base = _draft_values(
        youtube_id,
        ready.checksum,
        channel,
        kit.source_plan_id,
        fingerprint,
        policy,
        market.youtube_category_id,
        market.youtube_category_name,
        provider,
        prompt_hash,
    )
    base["privacy_status"] = privacy_status
    try:
        generated = provider.generate(prompt)
        metadata = PublicationMetadata(
            generated.selected_title,
            _description(generated.description, generated.hashtags),
            generated.tags,
            privacy_status,
            False,
            False,
            False,
            market.youtube_category_id,
            market.youtube_category_name,
            "pt-BR",
            "youtube",
            True,
            True,
        ).validated()
    except Exception as exc:
        failure_status = (
            MetadataDraftStatus.FAILED_CONFIGURATION
            if isinstance(exc, GeminiPermanentError)
            else MetadataDraftStatus.FAILED_RECOVERABLE
        )
        state.save_metadata_draft(
            **base,
            status=failure_status,
            title="",
            description="",
            tags=(),
            hashtags=(),
            title_candidates=(),
            selection_reason="",
            error=str(exc),
        )
        raise
    return state.save_metadata_draft(
        **base,
        status=MetadataDraftStatus.REVIEW_REQUIRED,
        title=metadata.title,
        description=metadata.description,
        tags=metadata.tags,
        hashtags=generated.hashtags,
        title_candidates=generated.title_candidates,
        selection_reason=generated.selection_reason,
        error=None,
    )


def approve_and_enqueue_metadata(
    draft_id: int,
    *,
    state: StateStore,
    publication_service: YouTubePublicationService,
    scheduler: SchedulerService,
    now: datetime,
) -> tuple[MetadataDraft, PublicationJob]:
    draft = state.metadata_draft(draft_id)
    if draft is None:
        raise KeyError(f"Unknown metadata draft: {draft_id}")
    ready = state.get_ready_artifact(draft.youtube_id)
    if ready is None or ready.checksum != draft.ready_checksum:
        raise ValueError("Metadata draft READY artifact changed before approval.")
    metadata = PublicationMetadata(
        draft.title,
        draft.description,
        draft.tags,
        draft.privacy_status,
        draft.made_for_kids,
        draft.contains_synthetic_media,
        draft.has_paid_product_placement,
        draft.category_id,
        draft.category_name,
        draft.default_language,
        draft.license,
        draft.embeddable,
        draft.public_stats_viewable,
    ).validated()
    publication = publication_service.create_for_review(
        draft.publishing_channel_id, ready.file_path, metadata
    )
    preflight = publication_service.preflight(publication.publication_id)
    if preflight.publication.youtube_channel_id != publication.youtube_channel_id:
        raise ValueError("Remote OAuth channel does not match the Publication target.")
    if publication.status is not PublicationStatus.APPROVED_FOR_PUBLISH:
        publication = publication_service.approve(publication.publication_id)
    job = scheduler.enqueue(publication.publication_id, now)
    approved = state.approve_metadata_draft(draft_id, publication.publication_id)
    return approved, job


def _draft_values(
    youtube_id, checksum, channel, brand_plan_id, fingerprint, policy,
    category_id, category_name, provider, prompt_hash
):
    return {
        "youtube_id": youtube_id,
        "ready_checksum": checksum,
        "publishing_channel_id": channel.channel_id,
        "brand_plan_id": brand_plan_id,
        "fingerprint": fingerprint,
        "review_policy": policy,
        "category_id": category_id,
        "category_name": category_name,
        "default_language": "pt-BR",
        "privacy_status": "private",
        "made_for_kids": False,
        "contains_synthetic_media": False,
        "has_paid_product_placement": False,
        "license": "youtube",
        "embeddable": True,
        "public_stats_viewable": True,
        "provider": provider.provider_name,
        "model": provider.model,
        "prompt_hash": prompt_hash,
        "generation_version": METADATA_VERSION,
    }


def _resolve_channel(config, channel_id):
    for theme in config.themes:
        for channel in theme.channels:
            if channel.channel_id == channel_id:
                return theme, channel
    raise KeyError(f"Publishing channel not found: {channel_id}")


def _prompt(source_title, selection, theme_prompt, market, kit, channel):
    return (
        f"SOURCE TITLE\n{source_title}\n\nSHORT TRANSCRIPT\n"
        f"{selection['context']}\n\nSELECTION REASON\n{selection['reason']}\n\n"
        f"THEME GUIDANCE\n{theme_prompt}\n\nDESTINATION\n"
        f"channel={channel.name}\nbrand={kit.public_name}\n"
        f"brand_description={kit.description}\ncategory="
        f"{market.youtube_category_id} {market.youtube_category_name}\n"
        "language=pt-BR\nGenerate exactly 3 title candidates and select one."
    )


def _description(description: str, hashtags: tuple[str, ...]) -> str:
    clean = tuple(f"#{item.lstrip('#')}" for item in hashtags if item.strip("# "))
    return "\n\n".join(item for item in (description.strip(), " ".join(clean)) if item)


def _fingerprint(*parts: str) -> str:
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


_RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "title_candidates", "selected_title", "description", "tags",
        "hashtags", "selection_reason",
    ],
    "properties": {
        "title_candidates": {
            "type": "array", "minItems": 3, "maxItems": 3,
            "items": {"type": "string"},
        },
        "selected_title": {"type": "string"},
        "description": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "hashtags": {"type": "array", "items": {"type": "string"}},
        "selection_reason": {"type": "string"},
    },
}
