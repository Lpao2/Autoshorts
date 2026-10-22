"""Restart-safe local mapping and guided OAuth onboarding for planned channels."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from autoshorts.automation_config import AutomationConfig, PublishingChannel
from autoshorts.brand_planning import PlannedChannelBrand
from autoshorts.state_store import StateStore
from autoshorts.theme_catalog import instantiate_theme
from autoshorts.youtube_oauth import YouTubeConnectionService
from autoshorts.youtube_provisioning import persistent_profile_path

ALLOWED_IDENTITIES = frozenset({"google-a", "google-c"})
PROTECTED_NAMES = frozenset({"lucas lp", "marcelo clic"})


@dataclass(frozen=True, slots=True)
class MappingResult:
    config: AutomationConfig
    planned: int
    created: int
    reused: int
    missing_channel_id: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OAuthQueueItem:
    publishing_channel_id: str
    channel_name: str
    google_identity_id: str
    expected_youtube_channel_id: str
    status: str


@dataclass(frozen=True, slots=True)
class ProvisioningOAuthResult:
    publishing_channel_id: str
    youtube_channel_id: str
    status: str


def reconcile_publishing_channels(
    config: AutomationConfig,
    store: StateStore,
    identities: frozenset[str] = ALLOWED_IDENTITIES,
) -> MappingResult:
    """Map plans to local channels without making any remote mutation."""
    assignments = store.planned_identity_assignments()
    records = {
        item.brand_plan_id: item for item in store.youtube_provisioning_records()
    }
    plans = tuple(
        plan
        for plan in store.planned_channel_brands()
        if assignments.get(plan.brand_plan_id) in identities
    )
    themes = list(config.themes)
    created = 0
    reused = 0
    missing = []
    for plan in sorted(plans, key=_plan_sort_key):
        identity_id = assignments[plan.brand_plan_id]
        record = records.get(plan.brand_plan_id)
        youtube_id = record.youtube_channel_id if record else None
        if not youtube_id:
            missing.append(plan.public_name)
        if not any(
            theme.output_subdirectory == plan.theme_key for theme in themes
        ):
            template = store.get_theme_template(plan.theme_key)
            if template is None:
                raise ValueError(f"Missing ThemeTemplate: {plan.theme_key}")
            themes.append(
                replace(instantiate_theme(template, enabled=False), channels=())
            )
        theme_index = next(
            i
            for i, theme in enumerate(themes)
            if theme.output_subdirectory == plan.theme_key
        )
        theme = themes[theme_index]
        channel = next(
            (x for x in theme.channels if x.brand_plan_id == plan.brand_plan_id), None
        )
        if channel is None:
            channel = _linked_channel(store, theme.channels, plan)
        if channel is None:
            channel = _reusable_slot(store, theme.channels, plan, identity_id)
        if channel is None:
            channel = PublishingChannel(
                channel_id=str(
                    uuid5(NAMESPACE_URL, f"autoshorts:{plan.brand_plan_id}")
                ),
                name=plan.public_name,
                slot=plan.slot,
                google_identity_id=identity_id,
                youtube_channel_id=youtube_id,
                youtube_channel_title=plan.public_name if youtube_id else None,
                brand_plan_id=plan.brand_plan_id,
            )
            channels = (*theme.channels, channel)
            created += 1
        else:
            updated = replace(
                channel,
                name=plan.public_name,
                enabled=True,
                google_identity_id=identity_id,
                youtube_channel_id=youtube_id,
                youtube_channel_title=plan.public_name if youtube_id else None,
                brand_plan_id=plan.brand_plan_id,
            )
            channels = tuple(
                updated if x.channel_id == channel.channel_id else x
                for x in theme.channels
            )
            reused += 1
        themes[theme_index] = replace(theme, channels=channels)
    return MappingResult(
        replace(config, themes=tuple(themes)),
        len(plans),
        created,
        reused,
        tuple(missing),
    )


def oauth_queue(
    config: AutomationConfig,
    store: StateStore,
    identities: frozenset[str] = ALLOWED_IDENTITIES,
) -> tuple[OAuthQueueItem, ...]:
    result = []
    for theme in config.themes:
        for channel in theme.channels:
            identity = channel.google_identity_id
            if (
                identity not in identities
                or not channel.brand_plan_id
                or _protected(channel.name)
                or not channel.youtube_channel_id
            ):
                continue
            binding = store.get_channel_binding(channel.channel_id)
            if binding is None:
                status = "MISSING"
            elif (
                binding.status == "CONNECTED"
                and binding.youtube_channel_id == channel.youtube_channel_id
            ):
                status = "READY"
            else:
                status = "MISMATCH"
            result.append(
                OAuthQueueItem(
                    channel.channel_id,
                    channel.name,
                    identity,
                    channel.youtube_channel_id,
                    status,
                )
            )
    return tuple(
        sorted(result, key=lambda x: (x.google_identity_id, x.channel_name.casefold()))
    )


def run_guided_oauth(
    service: YouTubeConnectionService,
    items: tuple[OAuthQueueItem, ...],
) -> tuple[int, int, int]:
    """Interactively process missing items; READY items are remotely reverified."""
    ready = skipped = mismatches = 0
    pending = []
    for item in items:
        if item.status == "READY":
            try:
                service.verify_binding(item.publishing_channel_id)
                ready += 1
                continue
            except Exception:
                pass
        pending.append(item)
    for index, item in enumerate(pending, 1):
        print("=" * 50)
        print(f"[{index}/{len(pending)}]")
        print(f"Canal: {item.channel_name}")
        print(f"Identity: {item.google_identity_id}")
        print(f"Channel ID esperado: {item.expected_youtube_channel_id}")
        choice = input("ENTER = autorizar | s = pular | q = sair: ").strip().lower()
        if choice == "q":
            break
        if choice == "s":
            skipped += 1
            continue
        pending_connection = service.begin_connection(item.publishing_channel_id)
        try:
            binding = service.confirm_expected_connection(
                pending_connection, item.expected_youtube_channel_id
            )
        except Exception:
            mismatches += 1
            raise
        print(
            f"✅ READY\nCanal: {item.channel_name}\n"
            f"Channel ID: {binding.youtube_channel_id}"
        )
        ready += 1
    return ready, skipped, mismatches


def authorize_provisioned_channel(
    *,
    project_root: Path,
    config: AutomationConfig,
    connection_service: YouTubeConnectionService,
    brand_plan_id: str,
    browser_factory=None,
) -> ProvisioningOAuthResult:
    """Join provisioning to OAuth without duplicating either implementation."""
    channel = next(
        (
            item
            for theme in config.themes
            for item in theme.channels
            if item.brand_plan_id == brand_plan_id
        ),
        None,
    )
    if channel is None:
        raise ValueError("Brand Plan has no local PublishingChannel mapping.")
    if not channel.google_identity_id or not channel.youtube_channel_id:
        raise ValueError("PublishingChannel is not ready for OAuth.")
    try:
        verified = connection_service.verify_binding(channel.channel_id)
    except (KeyError, RuntimeError, ValueError):
        verified = None
    if verified and verified.youtube_channel_id == channel.youtube_channel_id:
        return ProvisioningOAuthResult(
            channel.channel_id, channel.youtube_channel_id, "READY"
        )
    if browser_factory is None:
        from autoshorts.youtube_oauth_playwright import PlaywrightOAuthBrowser

        browser_factory = PlaywrightOAuthBrowser
    browser = browser_factory(
        persistent_profile_path(project_root, channel.google_identity_id),
        channel.name,
    )
    pending = connection_service.begin_connection(
        channel.channel_id, authorization_browser=browser
    )
    binding = connection_service.confirm_expected_connection(
        pending, channel.youtube_channel_id
    )
    return ProvisioningOAuthResult(
        channel.channel_id, binding.youtube_channel_id, "READY"
    )


def _linked_channel(
    store: StateStore,
    channels: tuple[PublishingChannel, ...],
    plan: PlannedChannelBrand,
) -> PublishingChannel | None:
    for channel in channels:
        kit = store.get_approved_brand_kit(channel.channel_id)
        if (
            not _protected(channel.name)
            and kit
            and kit.source_plan_id == plan.brand_plan_id
        ):
            return channel
    return None


def _reusable_slot(
    store: StateStore,
    channels: tuple[PublishingChannel, ...],
    plan: PlannedChannelBrand,
    identity_id: str,
) -> PublishingChannel | None:
    return next(
        (
            channel
            for channel in channels
            if channel.slot == plan.slot
            and not channel.brand_plan_id
            and not _protected(channel.name)
            and store.get_channel_binding(channel.channel_id) is None
            and channel.google_identity_id in {None, identity_id}
        ),
        None,
    )


def _protected(name: str) -> bool:
    normalized = name.casefold().replace("(piloto legado)", "").strip()
    return normalized in PROTECTED_NAMES


def _plan_sort_key(plan: PlannedChannelBrand) -> tuple[str, int, str]:
    return plan.theme_key, plan.slot, plan.public_name.casefold()
