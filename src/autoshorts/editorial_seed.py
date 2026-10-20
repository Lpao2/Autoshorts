"""Import reviewed editorial market profiles into the operational SQLite store."""

from __future__ import annotations

import json
from pathlib import Path

from autoshorts.editorial_sources import SourceRightsStatus, SourceValidationStatus
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
