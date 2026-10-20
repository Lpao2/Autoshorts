"""Market-specific editorial profiles and reusable content sources."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from zoneinfo import ZoneInfo


class SourceValidationStatus(StrEnum):
    PENDING = "PENDING"
    VALIDATED = "VALIDATED"
    DISABLED = "DISABLED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


class SourceRightsStatus(StrEnum):
    UNKNOWN = "UNKNOWN"
    AUTHORIZED = "AUTHORIZED"
    RESTRICTED = "RESTRICTED"


class TimingPolicy(StrEnum):
    SCHEDULED = "SCHEDULED"
    HYBRID = "HYBRID"
    FRESHNESS_FIRST = "FRESHNESS_FIRST"


class PostingStrategy(StrEnum):
    SEEDED = "SEEDED"
    LEARNED = "LEARNED"


class PostingConfidence(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


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


@dataclass(frozen=True, slots=True)
class ThemePostingProfile:
    posting_profile_id: int
    theme_market_profile_id: int
    timezone: str
    timing_policy: TimingPolicy
    strategy: PostingStrategy
    confidence: PostingConfidence
    enabled: bool
    source_note: str
    last_reviewed_at: str | None
    created_at: str
    updated_at: str

    def __post_init__(self) -> None:
        ZoneInfo(self.timezone)


@dataclass(frozen=True, slots=True)
class PostingWindow:
    window_id: int
    posting_profile_id: int
    priority: int
    start_local_time: str
    end_local_time: str
    days_of_week: tuple[str, ...]
    experimental: bool
    enabled: bool
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class ChannelPostingOverride:
    publishing_channel_id: str
    posting_profile_id: int
    timezone: str | None
    timing_policy: TimingPolicy | None
    strategy: PostingStrategy | None
    confidence: PostingConfidence | None
    enabled: bool
    source_note: str
    created_at: str
    updated_at: str
