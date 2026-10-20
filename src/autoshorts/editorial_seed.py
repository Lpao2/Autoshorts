"""Import reviewed editorial market profiles into the operational SQLite store."""

from __future__ import annotations

import json
from pathlib import Path

from autoshorts.editorial_sources import (
    PostingConfidence,
    PostingStrategy,
    SourceRightsStatus,
    SourceValidationStatus,
    TimingPolicy,
)
from autoshorts.state_store import StateStore


def import_editorial_seed(path: Path, state: StateStore) -> tuple[int, int]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise ValueError("Unsupported editorial seed schema version.")
    profiles = payload.get("profiles")
    if not isinstance(profiles, list):
        raise ValueError("Editorial seed profiles must be a list.")
    association_count = 0
    for raw_profile in profiles:
        sources = raw_profile.get("sources")
        if not isinstance(sources, list) or len(sources) != 5:
            raise ValueError("Each editorial profile must contain exactly 5 sources.")
        profile = state.save_theme_market_profile(
            theme_key=raw_profile["theme_key"],
            platform=raw_profile["platform"],
            region_code=raw_profile["region_code"],
            language_code=raw_profile["language_code"],
            youtube_category_id=raw_profile["youtube_category_id"],
            youtube_category_name=raw_profile["youtube_category_name"],
            enabled=bool(raw_profile["enabled"]),
            is_default_for_theme=bool(raw_profile["is_default_for_theme"]),
        )
        for raw_source in sources:
            source = state.save_content_source(
                platform=raw_profile["platform"],
                external_channel_id=raw_source["youtube_channel_id"],
                display_name=raw_source["display_name"],
                handle=raw_source.get("handle"),
                channel_url=raw_source["channel_url"],
                region_code=raw_profile["region_code"],
                language_code=raw_profile["language_code"],
                validation_status=SourceValidationStatus(
                    raw_source["validation_status"]
                ),
                last_validated_at=raw_source.get("last_validated_at"),
                latest_upload_at=raw_source.get("latest_upload_at"),
                sampled_video_count=int(raw_source["sampled_video_count"]),
                sampled_longform_count=int(raw_source["sampled_longform_count"]),
                sampled_target_duration_count=int(
                    raw_source["sampled_target_duration_count"]
                ),
                observed_duration_min_seconds=raw_source.get(
                    "observed_duration_min_seconds"
                ),
                observed_duration_max_seconds=raw_source.get(
                    "observed_duration_max_seconds"
                ),
                rights_status=SourceRightsStatus(raw_source["rights_status"]),
            )
            state.link_theme_content_source(
                profile_id=profile.profile_id,
                source_id=source.source_id,
                priority=int(raw_source["priority"]),
                target_duration_min_seconds=int(
                    raw_source["target_duration_min_seconds"]
                ),
                target_duration_max_seconds=int(
                    raw_source["target_duration_max_seconds"]
                ),
            )
            association_count += 1
    return len(profiles), association_count


def import_posting_seed(path: Path, state: StateStore) -> tuple[int, int]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise ValueError("Unsupported posting seed schema version.")
    profiles = payload.get("profiles")
    if not isinstance(profiles, list):
        raise ValueError("Posting seed profiles must be a list.")
    window_count = 0
    for raw in profiles:
        market = state.theme_market_profile(
            raw["theme_key"],
            raw["platform"],
            raw["region_code"],
            raw["language_code"],
        )
        if market is None:
            raise KeyError(f"Theme market profile not found: {raw['theme_key']}")
        posting = state.save_theme_posting_profile(
            theme_market_profile_id=market.profile_id,
            timezone=raw["timezone"],
            timing_policy=TimingPolicy(raw["timing_policy"]),
            strategy=PostingStrategy(raw["strategy"]),
            confidence=PostingConfidence(raw["confidence"]),
            enabled=bool(raw["enabled"]),
            source_note=raw.get("source_note", ""),
            last_reviewed_at=raw.get("last_reviewed_at"),
        )
        windows = raw.get("windows")
        if not isinstance(windows, list) or len(windows) < 1:
            raise ValueError("Each posting profile must contain posting windows.")
        state.replace_posting_windows(
            posting.posting_profile_id,
            tuple(
                (
                    int(window["priority"]),
                    window["start_local_time"],
                    window["end_local_time"],
                    tuple(window["days_of_week"]),
                    bool(window["experimental"]),
                    bool(window["enabled"]),
                )
                for window in windows
            ),
        )
        window_count += len(windows)
    return len(profiles), window_count
