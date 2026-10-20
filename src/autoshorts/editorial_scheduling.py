"""Resolve inherited posting policy without implementing a scheduler."""

from __future__ import annotations

from dataclasses import dataclass, replace

from autoshorts.automation_config import AutomationConfig
from autoshorts.editorial_sources import PostingWindow, ThemePostingProfile
from autoshorts.state_store import StateStore


@dataclass(frozen=True, slots=True)
class ResolvedPostingSchedule:
    theme_key: str
    posting_profile: ThemePostingProfile
    windows: tuple[PostingWindow, ...]
    publishing_channel_id: str
    inherited: bool


def resolve_channel_posting_schedule(
    config: AutomationConfig,
    state: StateStore,
    publishing_channel_id: str,
    *,
    platform: str = "youtube",
    region_code: str = "BR",
    language_code: str = "pt-BR",
) -> ResolvedPostingSchedule:
    theme = next(
        (
            theme
            for theme in config.themes
            if any(c.channel_id == publishing_channel_id for c in theme.channels)
        ),
        None,
    )
    if theme is None:
        raise KeyError(f"Publishing channel not found: {publishing_channel_id}")
    market = state.theme_market_profile(
        theme.output_subdirectory, platform, region_code, language_code
    )
    if market is None:
        raise KeyError("Theme market profile not found for publishing channel.")
    posting = state.theme_posting_profile(market.profile_id)
    if posting is None or not posting.enabled:
        raise KeyError("Active theme posting profile not found.")
    override = state.channel_posting_override(publishing_channel_id)
    inherited = override is None or not override.enabled
    if override and override.enabled:
        posting = replace(
            posting,
            timezone=override.timezone or posting.timezone,
            timing_policy=override.timing_policy or posting.timing_policy,
            strategy=override.strategy or posting.strategy,
            confidence=override.confidence or posting.confidence,
        )
    return ResolvedPostingSchedule(
        theme.output_subdirectory,
        posting,
        state.posting_windows(posting.posting_profile_id),
        publishing_channel_id,
        inherited,
    )
