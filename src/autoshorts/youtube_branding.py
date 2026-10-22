"""Conservative YouTube channel-branding provisioning."""

from __future__ import annotations

import hashlib
import json
import mimetypes
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

from autoshorts.automation_config import AutomationConfig, PublishingChannel
from autoshorts.brand_assets import (
    BrandAssetStatus,
    BrandAssetType,
    GeneratedBrandAsset,
)
from autoshorts.credential_store import FilesystemCredentialStore
from autoshorts.state_store import StateStore
from autoshorts.youtube_oauth import YOUTUBE_MANAGE_SCOPE, YouTubeConnectionService

DEFAULT_CHANNEL_LANGUAGE = "pt-BR"
DEFAULT_CHANNEL_COUNTRY = "BR"
WATERMARK_MAX_BYTES = 1024 * 1024
_WRITABLE_CHANNEL_KEYS = (
    "country",
    "description",
    "defaultLanguage",
    "keywords",
    "trackingAnalyticsAccountId",
    "unsubscribedTrailer",
)


@dataclass(frozen=True, slots=True)
class BrandingCapability:
    name: str
    api_field: str | None
    writable: bool
    automatic: bool


CAPABILITIES = (
    BrandingCapability(
        "Description", "brandingSettings.channel.description", True, True
    ),
    BrandingCapability("Keywords", "brandingSettings.channel.keywords", True, True),
    BrandingCapability(
        "Language", "brandingSettings.channel.defaultLanguage", True, True
    ),
    BrandingCapability("Country", "brandingSettings.channel.country", True, False),
    BrandingCapability(
        "Banner", "brandingSettings.image.bannerExternalUrl", True, True
    ),
    BrandingCapability("Watermark", "watermarks.set", True, True),
    BrandingCapability(
        "Channel audience", "status.selfDeclaredMadeForKids", True, False
    ),
    BrandingCapability("Profile picture", None, False, False),
    BrandingCapability("Handle", None, False, False),
    BrandingCapability("Channel name", None, False, False),
)


@dataclass(frozen=True, slots=True)
class ChannelBrandingPlan:
    publishing_channel_id: str
    youtube_channel_id: str
    youtube_channel_title: str
    brand_plan_id: str
    public_name: str
    handle: str
    description: str
    keywords: tuple[str, ...]
    default_language: str
    country: str | None
    made_for_kids: bool | None
    banner: GeneratedBrandAsset
    logo: GeneratedBrandAsset
    watermark_path: Path
    granted_scopes: tuple[str, ...]
    oauth_ready: bool


@dataclass(frozen=True, slots=True)
class BrandingApplyResult:
    plan: ChannelBrandingPlan
    remote: dict[str, Any]
    banner_active: bool
    watermark_set: bool


@dataclass(frozen=True, slots=True)
class WatermarkApplyResult:
    plan: ChannelBrandingPlan
    http_result: str


@dataclass(frozen=True, slots=True)
class BannerApplyResult:
    publishing_channel_id: str
    youtube_channel_id: str
    banner: GeneratedBrandAsset
    remote: dict[str, Any]
    banner_active: bool


class YouTubeBrandingBackend(Protocol):
    def get_channel(self, credentials: object) -> dict[str, Any]: ...
    def upload_banner(self, credentials: object, path: Path) -> str: ...
    def update_branding(
        self, credentials: object, channel_id: str, branding: dict[str, Any]
    ) -> dict[str, Any]: ...
    def set_watermark(
        self, credentials: object, channel_id: str, path: Path
    ) -> object: ...


class GoogleYouTubeBrandingBackend:
    def _api(self, credentials: object):
        from googleapiclient.discovery import build

        return build("youtube", "v3", credentials=credentials, cache_discovery=False)

    def get_channel(self, credentials: object) -> dict[str, Any]:
        response = (
            self._api(credentials)
            .channels()
            .list(part="snippet,brandingSettings,status", mine=True)
            .execute()
        )
        items = response.get("items", [])
        if len(items) != 1:
            raise RuntimeError("A credencial não retornou exatamente um canal.")
        return items[0]

    def upload_banner(self, credentials: object, path: Path) -> str:
        from googleapiclient.http import MediaFileUpload

        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        response = (
            self._api(credentials)
            .channelBanners()
            .insert(media_body=MediaFileUpload(str(path), mimetype=media_type))
            .execute()
        )
        url = response.get("url")
        if not isinstance(url, str) or not url:
            raise RuntimeError("O YouTube não retornou a URL temporária do banner.")
        return url

    def update_branding(
        self, credentials: object, channel_id: str, branding: dict[str, Any]
    ) -> dict[str, Any]:
        return (
            self._api(credentials)
            .channels()
            .update(
                part="brandingSettings",
                body={"id": channel_id, "brandingSettings": branding},
            )
            .execute()
        )

    def set_watermark(
        self, credentials: object, channel_id: str, path: Path
    ) -> object:
        from googleapiclient.http import MediaFileUpload

        request = (
            self._api(credentials)
            .watermarks()
            .set(
                channelId=channel_id,
                body={"timing": {"type": "offsetFromStart", "offsetMs": 0}},
                media_body=MediaFileUpload(str(path), mimetype="image/png"),
            )
        )
        # Some httplib2 releases try to decode an empty gzipped 204 response with
        # a zero-size chunk, raising after YouTube has already accepted the upload.
        request.headers["accept-encoding"] = "identity"
        return request.execute()


class YouTubeBrandingService:
    def __init__(
        self,
        config: AutomationConfig,
        state_store: StateStore,
        credential_store: FilesystemCredentialStore,
        *,
        connection_service: YouTubeConnectionService | None = None,
        backend: YouTubeBrandingBackend | None = None,
    ) -> None:
        self.config = config
        self.state_store = state_store
        self.credential_store = credential_store
        self.connection_service = connection_service or YouTubeConnectionService(
            config, state_store, credential_store
        )
        self.backend = backend or GoogleYouTubeBrandingBackend()

    def dry_run(self, publishing_channel_id: str) -> ChannelBrandingPlan:
        channel = self._channel(publishing_channel_id)
        binding = self.state_store.get_channel_binding(channel.channel_id)
        if binding is None or binding.status != "CONNECTED":
            raise ValueError("Publishing channel has no connected OAuth binding.")
        kit = self.state_store.get_approved_brand_kit(channel.channel_id)
        if kit is None or not kit.source_plan_id:
            raise ValueError("Publishing channel has no approved Brand Plan.")
        assets = self.state_store.brand_assets(kit.source_plan_id)
        banner = _approved_asset(assets, BrandAssetType.BANNER)
        logo = _approved_asset(assets, BrandAssetType.LOGO)
        banner = _youtube_banner_asset(banner)
        _validate_asset(banner, 2560, 1440, 6 * 1024 * 1024)
        _validate_asset(logo, 1024, 1024, None)
        watermark_path = _watermark_path(logo)
        scopes = self._granted_scopes(binding.credential_ref)
        return ChannelBrandingPlan(
            channel.channel_id,
            binding.youtube_channel_id,
            binding.youtube_channel_title,
            kit.source_plan_id,
            kit.public_name,
            kit.handle,
            kit.description,
            kit.brand_keywords,
            DEFAULT_CHANNEL_LANGUAGE,
            DEFAULT_CHANNEL_COUNTRY,
            None,
            banner,
            logo,
            watermark_path,
            scopes,
            YOUTUBE_MANAGE_SCOPE in scopes,
        )

    def apply(self, publishing_channel_id: str) -> BrandingApplyResult:
        credentials = self.connection_service.load_verified_credentials(
            publishing_channel_id
        )
        # Verification can refresh the persisted remote title. Build the plan
        # afterwards so target validation never compares against stale binding data.
        plan = self.dry_run(publishing_channel_id)
        if not plan.oauth_ready:
            raise ValueError(
                "OAuth scope insufficient; reconnect before applying branding."
            )
        before = self.backend.get_channel(credentials)
        self._assert_target(before, plan)
        immutable_before = _immutable_channel_identity(before)
        banner_url = self.backend.upload_banner(credentials, plan.banner.file_path)
        branding = _conservative_branding(before.get("brandingSettings", {}))
        channel_settings = branding.setdefault("channel", {})
        channel_settings.update(
            {
                "description": plan.description,
                "keywords": " ".join(plan.keywords),
                "defaultLanguage": plan.default_language,
            }
        )
        if plan.country is not None:
            channel_settings["country"] = plan.country
        branding["image"] = {"bannerExternalUrl": banner_url}
        self.backend.update_branding(credentials, plan.youtube_channel_id, branding)
        self.backend.set_watermark(
            credentials, plan.youtube_channel_id, plan.watermark_path
        )
        after = self.backend.get_channel(credentials)
        self._assert_target(after, plan)
        if _immutable_channel_identity(after) != immutable_before:
            raise RuntimeError("Remote validation found changed channel identity.")
        current = after.get("brandingSettings", {}).get("channel", {})
        if current.get("description") != plan.description:
            raise RuntimeError("Remote validation failed for channel description.")
        if not _language_matches(
            plan.default_language, current.get("defaultLanguage")
        ):
            raise RuntimeError("Remote validation failed for channel language.")
        if current.get("country") != plan.country:
            raise RuntimeError("Remote validation failed for channel country.")
        if _normalized_keywords(current.get("keywords")) != _normalized_keywords(
            " ".join(plan.keywords)
        ):
            raise RuntimeError("Remote validation failed for channel keywords.")
        image = after.get("brandingSettings", {}).get("image", {})
        active = bool(
            image.get("bannerExternalUrl")
            or image.get("bannerImageUrl")
            or image.get("bannerTvHighImageUrl")
        )
        if not active:
            raise RuntimeError("Remote validation did not confirm an active banner.")
        return BrandingApplyResult(plan, after, active, True)

    def apply_watermark(self, publishing_channel_id: str) -> WatermarkApplyResult:
        """Apply only the watermark; never rewrite channel branding settings."""
        plan = self.dry_run(publishing_channel_id)
        if not plan.oauth_ready:
            raise ValueError(
                "OAuth scope insufficient; reconnect before applying watermark."
            )
        credentials = self.connection_service.load_verified_credentials(
            publishing_channel_id
        )
        remote = self.backend.get_channel(credentials)
        self._assert_target(remote, plan)
        self.backend.set_watermark(
            credentials, plan.youtube_channel_id, plan.watermark_path
        )
        # watermarks.set has no response resource and succeeds with HTTP 204.
        return WatermarkApplyResult(plan, "HTTP 204 No Content")

    def apply_banner_only(self, publishing_channel_id: str) -> BannerApplyResult:
        """Apply only the latest valid banner and preserve all other settings."""
        channel = self._channel(publishing_channel_id)
        kit = self.state_store.get_approved_brand_kit(channel.channel_id)
        brand_plan_id = channel.brand_plan_id or (
            kit.source_plan_id if kit is not None else None
        )
        if not brand_plan_id:
            raise ValueError("Publishing channel has no Brand Plan.")
        banner = self.state_store.latest_valid_brand_asset(
            brand_plan_id, BrandAssetType.BANNER
        )
        if banner is None:
            raise ValueError("Publishing channel has no valid banner asset.")
        banner = _youtube_banner_asset(banner)
        _validate_asset(banner, 2560, 1440, 6 * 1024 * 1024)
        credentials = self.connection_service.load_verified_credentials(
            publishing_channel_id
        )
        binding = self.state_store.get_channel_binding(publishing_channel_id)
        if binding is None or binding.status != "CONNECTED":
            raise ValueError("Publishing channel has no connected OAuth binding.")
        target_id = channel.youtube_channel_id or binding.youtube_channel_id
        if binding.youtube_channel_id != target_id:
            raise ValueError("OAuth binding Channel ID does not match target.")
        before = self.backend.get_channel(credentials)
        if before.get("id") != target_id:
            raise ValueError("Authenticated channel ID does not match target.")
        immutable_before = _immutable_channel_identity(before)
        banner_url = self.backend.upload_banner(credentials, banner.file_path)
        after_upload = self.backend.get_channel(credentials)
        if after_upload.get("id") != target_id:
            raise RuntimeError("Remote validation returned the wrong channel.")
        if _active_banner(after_upload):
            return BannerApplyResult(
                publishing_channel_id,
                target_id,
                banner,
                after_upload,
                True,
            )
        branding = _conservative_branding(before.get("brandingSettings", {}))
        remote_title = before.get("snippet", {}).get("title")
        if not isinstance(remote_title, str) or not remote_title:
            raise RuntimeError("Remote channel title is unavailable.")
        # YouTube requires channel.title in channels.update even when the title is
        # unchanged. Echo the exact remote value; this flow never renames channels.
        branding.setdefault("channel", {})["title"] = remote_title
        branding["image"] = {"bannerExternalUrl": banner_url}
        self.backend.update_branding(credentials, target_id, branding)
        after = self.backend.get_channel(credentials)
        if after.get("id") != target_id:
            raise RuntimeError("Remote validation returned the wrong channel.")
        if _immutable_channel_identity(after) != immutable_before:
            raise RuntimeError("Remote validation found changed channel identity.")
        active = _active_banner(after)
        if not active:
            raise RuntimeError("Remote validation did not confirm an active banner.")
        return BannerApplyResult(
            publishing_channel_id,
            target_id,
            banner,
            after,
            True,
        )

    def _channel(self, channel_id: str) -> PublishingChannel:
        channel = next(
            (
                item
                for theme in self.config.themes
                for item in theme.channels
                if item.channel_id == channel_id
            ),
            None,
        )
        if channel is None or not channel.enabled:
            raise ValueError("Publishing channel is missing or disabled.")
        return channel

    def _granted_scopes(self, credential_ref: str) -> tuple[str, ...]:
        payload = json.loads(self.credential_store.read(credential_ref))
        return tuple(payload.get("scopes") or ())

    @staticmethod
    def _assert_target(remote: dict[str, Any], plan: ChannelBrandingPlan) -> None:
        if remote.get("id") != plan.youtube_channel_id:
            raise ValueError(
                "Authenticated channel ID does not match the approved target."
            )
        title = remote.get("snippet", {}).get("title")
        if title != plan.youtube_channel_title:
            raise ValueError("Authenticated channel title does not match the binding.")


def _approved_asset(
    assets: tuple[GeneratedBrandAsset, ...], asset_type: BrandAssetType
) -> GeneratedBrandAsset:
    matching = [
        item
        for item in assets
        if item.asset_type == asset_type and item.status == BrandAssetStatus.APPROVED
    ]
    if not matching:
        raise ValueError(f"No approved {asset_type.value.lower()} asset.")
    return max(matching, key=lambda item: item.version)


def _validate_asset(
    asset: GeneratedBrandAsset, width: int, height: int, maximum_bytes: int | None
) -> None:
    if (asset.width, asset.height) != (width, height):
        raise ValueError(f"Invalid {asset.asset_type.value.lower()} dimensions.")
    if maximum_bytes is not None and asset.file_size > maximum_bytes:
        raise ValueError("Banner exceeds YouTube's 6 MB limit.")
    if not asset.file_path.is_file():
        raise FileNotFoundError(asset.file_path)
    digest = hashlib.sha256(asset.file_path.read_bytes()).hexdigest()
    if digest != asset.checksum:
        raise ValueError(f"{asset.asset_type.value.title()} checksum mismatch.")


def _youtube_banner_asset(asset: GeneratedBrandAsset) -> GeneratedBrandAsset:
    if asset.file_size <= 6 * 1024 * 1024:
        return asset
    delivery = asset.delivery_path
    if delivery is None or not delivery.is_file():
        return asset
    payload = delivery.read_bytes()
    return replace(
        asset,
        file_path=delivery,
        file_size=len(payload),
        checksum=hashlib.sha256(payload).hexdigest(),
    )


def _watermark_path(logo: GeneratedBrandAsset) -> Path:
    if logo.file_size <= WATERMARK_MAX_BYTES:
        return logo.file_path
    channel_root = logo.file_path.parents[2]
    derived = channel_root / "watermark" / "v1" / "master.png"
    if not derived.is_file() or derived.stat().st_size > WATERMARK_MAX_BYTES:
        raise ValueError("Approved watermark derivative is missing or exceeds 1 MiB.")
    return derived


def _conservative_branding(remote: dict[str, Any]) -> dict[str, Any]:
    """Keep every writable channel value; exclude read-only/deprecated image URLs."""
    source = remote.get("channel", {})
    channel = {
        key: deepcopy(source[key]) for key in _WRITABLE_CHANNEL_KEYS if key in source
    }
    return {"channel": channel}


def _active_banner(remote: dict[str, Any]) -> bool:
    image = remote.get("brandingSettings", {}).get("image", {})
    return bool(
        image.get("bannerExternalUrl")
        or image.get("bannerImageUrl")
        or image.get("bannerTvHighImageUrl")
    )


def _language_matches(requested: str, returned: object) -> bool:
    """YouTube may normalize a regional language such as pt-BR to pt."""
    if not isinstance(returned, str):
        return False
    requested_parts = requested.casefold().replace("_", "-").split("-")
    returned_parts = returned.casefold().replace("_", "-").split("-")
    return requested_parts == returned_parts or (
        len(returned_parts) == 1 and requested_parts[0] == returned_parts[0]
    )


def _normalized_keywords(value: object) -> tuple[str, ...]:
    if not isinstance(value, str):
        return ()
    return tuple(part.casefold() for part in value.split() if part)


def _immutable_channel_identity(remote: dict[str, Any]) -> tuple[object, ...]:
    snippet = remote.get("snippet", {})
    return (
        snippet.get("title"),
        snippet.get("customUrl"),
        _stable_thumbnail_identity(snippet.get("thumbnails")),
    )


def _stable_thumbnail_identity(value: object) -> object:
    """Ignore expiring CDN query strings without ignoring a changed logo path."""
    if not isinstance(value, dict):
        return deepcopy(value)
    result = deepcopy(value)
    for thumbnail in result.values():
        if not isinstance(thumbnail, dict) or not isinstance(thumbnail.get("url"), str):
            continue
        parts = urlsplit(thumbnail["url"])
        thumbnail["url"] = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    return result
