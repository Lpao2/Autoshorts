"""Market-specific editorial profiles and reusable content sources."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class SourceValidationStatus(StrEnum):
    PENDING = "PENDING"
    VALIDATED = "VALIDATED"
    DISABLED = "DISABLED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


class SourceRightsStatus(StrEnum):
    UNKNOWN = "UNKNOWN"
    AUTHORIZED = "AUTHORIZED"
    RESTRICTED = "RESTRICTED"


@dataclass(frozen=True, slots=True)
class ThemeMarketProfile:
    profile_id: int
    theme_key: str
    platform: str
    region_code: str
    language_code: str
    youtube_category_id: str
    youtube_category_name: str
    enabled: bool
    is_default_for_theme: bool
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class ContentSource:
    source_id: int
    platform: str
    external_channel_id: str
    display_name: str
    handle: str | None
    channel_url: str
    region_code: str
    language_code: str
    source_kind: str
    validation_status: SourceValidationStatus
    last_validated_at: str | None
    latest_upload_at: str | None
    sampled_video_count: int
    sampled_longform_count: int
    sampled_target_duration_count: int
    observed_duration_min_seconds: int | None
    observed_duration_max_seconds: int | None
    enabled: bool
    rights_status: SourceRightsStatus
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class ThemeContentSource:
    profile_id: int
    source_id: int
    priority: int
    enabled: bool
    target_duration_min_seconds: int
    target_duration_max_seconds: int
    created_at: str
    updated_at: str
