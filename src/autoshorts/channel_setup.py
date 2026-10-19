"""Resolved, user-facing channel preparation state."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from autoshorts.automation_config import (
    GoogleIdentity,
    PostingPolicy,
    PublishingChannel,
    ThemeProfile,
)
from autoshorts.brand_identity import ChannelBrandKit
from autoshorts.state_store import ChannelCredentialBinding, StateStore
from autoshorts.theme_catalog import ChannelArchetype, ThemeTemplate


class ChannelSetupStatus(StrEnum):
    UNCONFIGURED = "UNCONFIGURED"
    BRANDING_PENDING = "BRANDING_PENDING"
    YOUTUBE_PENDING = "YOUTUBE_PENDING"
    READY_FOR_SCHEDULING = "READY_FOR_SCHEDULING"
    ACTIVE = "ACTIVE"
    ERROR = "ERROR"


@dataclass(frozen=True, slots=True)
class ResolvedChannelSetup:
    theme: ThemeProfile
    archetype: ChannelArchetype
    publishing_channel: PublishingChannel
    google_identity: GoogleIdentity | None
    brand_kit: ChannelBrandKit | None
    channel_binding: ChannelCredentialBinding | None
    posting_policy: PostingPolicy
    setup_status: ChannelSetupStatus


def resolve_channel_setup_status(
    *,
    identity: GoogleIdentity | None,
    brand_kit: ChannelBrandKit | None,
    binding: ChannelCredentialBinding | None,
    posting_policy: PostingPolicy,
) -> ChannelSetupStatus:
    if identity is None or not identity.enabled:
        return ChannelSetupStatus.UNCONFIGURED
    if binding is not None and binding.status not in {"CONNECTED", "DISABLED"}:
        return ChannelSetupStatus.ERROR
    if brand_kit is None:
        return ChannelSetupStatus.BRANDING_PENDING
    if binding is None or binding.status != "CONNECTED":
        return ChannelSetupStatus.YOUTUBE_PENDING
    if not posting_policy.enabled:
        return ChannelSetupStatus.READY_FOR_SCHEDULING
    return ChannelSetupStatus.ACTIVE


def resolve_theme_channel_setups(
    theme: ThemeProfile,
    template: ThemeTemplate,
    identities: tuple[GoogleIdentity, ...],
    state_store: StateStore,
) -> tuple[ResolvedChannelSetup, ...]:
    by_identity = {item.identity_id: item for item in identities}
    by_slot = {item.slot: item for item in template.channel_archetypes}
    resolved = []
    for channel in sorted(theme.channels, key=lambda item: item.slot):
        identity = by_identity.get(channel.google_identity_id or "")
        brand_kit = state_store.get_approved_brand_kit(channel.channel_id)
        binding = state_store.get_channel_binding(channel.channel_id)
        resolved.append(
            ResolvedChannelSetup(
                theme=theme,
                archetype=by_slot[channel.slot],
                publishing_channel=channel,
                google_identity=identity,
                brand_kit=brand_kit,
                channel_binding=binding,
                posting_policy=channel.posting_policy,
                setup_status=resolve_channel_setup_status(
                    identity=identity,
                    brand_kit=brand_kit,
                    binding=binding,
                    posting_policy=channel.posting_policy,
                ),
            )
        )
    return tuple(resolved)
