"""Safe, declarative configuration for automated source collection."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from dataclasses import replace as field_replace
from pathlib import Path, PurePath
from urllib.parse import urlparse
from uuid import NAMESPACE_URL, uuid5


@dataclass(frozen=True, slots=True)
class Creator:
    name: str
    videos_url: str
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class AccountProfile:
    """Deprecated compatibility model; use PublishingChannel."""
    name: str
    email: str
    slot: int
    enabled: bool = True

    def __post_init__(self) -> None:
        if self.slot not in {1, 2, 3}:
            raise ValueError("Account slot must be 1, 2 or 3.")


@dataclass(frozen=True, slots=True)
class GoogleIdentity:
    identity_id: str
    label: str
    email: str
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class EditorialProfile:
    prompt_append: str = ""
    headline_style: str = ""
    tone: str = ""


@dataclass(frozen=True, slots=True)
class ColorPalette:
    primary: str = "#ffffff"
    secondary: str = "#000000"
    accent: str = "#3b82f6"
    background: str = "#0f172a"


@dataclass(frozen=True, slots=True)
class VisualProfile:
    palette: ColorPalette = field(default_factory=ColorPalette)
    crop_preset: str = "default"
    caption_preset: str = "default"
    branding_preset: str = "none"


@dataclass(frozen=True, slots=True)
class PostingPolicy:
    enabled: bool = False
    daily_limit: int | None = None
    timezone: str = "America/Sao_Paulo"
    posting_windows: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.daily_limit is not None and self.daily_limit < 1:
            raise ValueError("daily_limit must be positive when configured.")
        if not self.timezone.strip():
            raise ValueError("timezone cannot be empty.")


@dataclass(frozen=True, slots=True)
class PublishingChannel:
    channel_id: str
    name: str
    slot: int
    enabled: bool = True
    google_identity_id: str | None = None
    youtube_channel_id: str | None = None
    youtube_channel_title: str | None = None
    editorial_profile: EditorialProfile = field(default_factory=EditorialProfile)
    visual_profile: VisualProfile = field(default_factory=VisualProfile)
    posting_policy: PostingPolicy = field(default_factory=PostingPolicy)

    def __post_init__(self) -> None:
        if not self.channel_id.strip() or not self.name.strip():
            raise ValueError("Channel ID and name cannot be empty.")
        if self.slot not in {1, 2, 3}:
            raise ValueError("Channel slot must be 1, 2 or 3.")


@dataclass(frozen=True, slots=True)
class ThemePolicy:
    videos_per_creator: int = 1
    maximum_age_days: int = 3650
    shorts_per_video: int = 3

    def __post_init__(self) -> None:
        if self.videos_per_creator < 1:
            raise ValueError("videos_per_creator must be positive.")
        if not 0 <= self.maximum_age_days <= 3650:
            raise ValueError("maximum_age_days must be between 0 and 3650.")
        if self.shorts_per_video != 3:
            raise ValueError("shorts_per_video must be exactly 3.")


@dataclass(frozen=True, slots=True)
class ThemeProfile:
    name: str
    prompt: str
    creators: tuple[Creator, ...]
    output_subdirectory: str
    enabled: bool = True
    accounts: tuple[AccountProfile, ...] = ()
    policy: ThemePolicy = field(default_factory=ThemePolicy)
    theme_id: str = ""
    channels: tuple[PublishingChannel, ...] = ()

    def __post_init__(self) -> None:
        if not self.theme_id:
            object.__setattr__(self, "theme_id", stable_theme_id(self.name))
        if not self.channels and self.accounts:
            object.__setattr__(
                self,
                "channels",
                tuple(
                    legacy_account_channel(self.theme_id, item)
                    for item in self.accounts
                ),
            )
        validate_theme_channels(self.channels)

    @property
    def production_policy(self) -> ThemePolicy:
        return self.policy


@dataclass(frozen=True, slots=True)
class AutomationConfig:
    themes: tuple[ThemeProfile, ...]
    google_identities: tuple[GoogleIdentity, ...] = ()

    def __post_init__(self) -> None:
        ids = [item.identity_id for item in self.google_identities]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate Google identity ID.")
        known = set(ids)
        channel_ids: list[str] = []
        youtube_ids: list[str] = []
        for theme in self.themes:
            for channel in theme.channels:
                channel_ids.append(channel.channel_id)
                if channel.youtube_channel_id:
                    youtube_ids.append(channel.youtube_channel_id)
                if channel.google_identity_id not in {None, *known}:
                    raise ValueError(
                        f"Channel '{channel.name}' references an unknown "
                        "Google identity."
                    )
        if len(channel_ids) != len(set(channel_ids)):
            raise ValueError("Publishing channel IDs must be globally unique.")
        if len(youtube_ids) != len(set(youtube_ids)):
            raise ValueError("YouTube channel IDs must be globally unique.")

    def theme(self, name: str) -> ThemeProfile:
        for item in self.themes:
            if item.name == name:
                return item
        raise KeyError(f"Theme not found: {name}")

    def theme_by_id(self, theme_id: str) -> ThemeProfile:
        for item in self.themes:
            if item.theme_id == theme_id:
                return item
        raise KeyError(f"Theme not found: {theme_id}")


@dataclass(frozen=True, slots=True)
class ResolvedPublishingProfile:
    theme_id: str
    theme_name: str
    channel: PublishingChannel
    editorial_context: str
    visual_profile: VisualProfile
    posting_policy: PostingPolicy


def resolve_editorial_context(
    theme: ThemeProfile, channel: PublishingChannel, *, base_prompt: str = ""
) -> str:
    guidance = channel.editorial_profile
    pieces = [base_prompt.strip(), theme.prompt.strip(), guidance.prompt_append.strip()]
    if guidance.headline_style.strip():
        pieces.append(f"Estilo de headline: {guidance.headline_style.strip()}")
    if guidance.tone.strip():
        pieces.append(f"Tom: {guidance.tone.strip()}")
    return "\n\n".join(piece for piece in pieces if piece)


def resolve_publishing_profile(
    theme: ThemeProfile, channel: PublishingChannel, *, base_prompt: str = ""
) -> ResolvedPublishingProfile:
    if channel not in theme.channels:
        raise ValueError("Publishing channel does not belong to the theme.")
    return ResolvedPublishingProfile(
        theme.theme_id,
        theme.name,
        channel,
        resolve_editorial_context(theme, channel, base_prompt=base_prompt),
        channel.visual_profile,
        channel.posting_policy,
    )


def stable_theme_id(name: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"autoshorts:theme:{name.strip().casefold()}"))


def legacy_account_channel(theme_id: str, account: AccountProfile) -> PublishingChannel:
    channel_id = str(
        uuid5(
            NAMESPACE_URL,
            f"autoshorts:channel:{theme_id}:{account.slot}:{account.name.casefold()}",
        )
    )
    return PublishingChannel(channel_id, account.name, account.slot, account.enabled)


def validate_theme_channels(channels: tuple[PublishingChannel, ...]) -> None:
    if sum(item.enabled for item in channels) > 3:
        raise ValueError("A theme can enable at most 3 publishing channels.")
    enabled_slots = [item.slot for item in channels if item.enabled]
    if len(enabled_slots) != len(set(enabled_slots)):
        raise ValueError("Enabled publishing channel slots must be unique.")
    ids = [item.channel_id for item in channels]
    if len(ids) != len(set(ids)):
        raise ValueError("Publishing channel IDs must be unique within a theme.")


def load_automation_config(path: str | Path) -> AutomationConfig:
    """Load configuration while retaining compatibility with pre-enabled JSON."""
    config_path = Path(path)
    data = json.loads(config_path.read_text(encoding="utf-8"))
    return automation_config_from_dict(data)


def automation_config_from_dict(data: object) -> AutomationConfig:
    if not isinstance(data, dict):
        raise ValueError("Automation configuration must be an object.")
    raw_identities = data.get("google_identities", [])
    if not isinstance(raw_identities, list):
        raise ValueError("google_identities must be a list.")
    identities = list(_load_google_identity(item) for item in raw_identities)
    raw_themes = data.get("themes")
    if not isinstance(raw_themes, list):
        raise ValueError("Automation configuration must contain a themes list.")
    themes: list[ThemeProfile] = []
    seen: set[str] = set()
    for raw_theme in raw_themes:
        if not isinstance(raw_theme, dict):
            raise ValueError("Each theme must be an object.")
        name = _required_text(raw_theme, "name", "theme")
        key = name.casefold()
        if key in seen:
            raise ValueError(f"Duplicate theme name: {name}")
        seen.add(key)
        creators_data = raw_theme.get("creators", [])
        if not isinstance(creators_data, list):
            raise ValueError(f"Theme '{name}' creators must be a list.")
        creators = tuple(_load_creator(item, name) for item in creators_data)
        creator_names = [creator.name.casefold() for creator in creators]
        if len(creator_names) != len(set(creator_names)):
            raise ValueError(f"Theme '{name}' has duplicate creator names.")
        accounts_data = raw_theme.get("accounts", [])
        if not isinstance(accounts_data, list):
            raise ValueError(f"Theme '{name}' accounts must be a list.")
        accounts = tuple(
            _load_account(item, name, legacy_slot=index)
            for index, item in enumerate(accounts_data, start=1)
        )
        theme_id = str(raw_theme.get("theme_id", "")).strip() or stable_theme_id(name)
        channels_data = raw_theme.get("channels")
        if channels_data is not None and not isinstance(channels_data, list):
            raise ValueError(f"Theme '{name}' channels must be a list.")
        if channels_data is not None:
            channels = tuple(_load_channel(item, name) for item in channels_data)
        else:
            migrated = []
            for account in accounts:
                identity_id = str(
                    uuid5(
                        NAMESPACE_URL,
                        f"autoshorts:google-identity:{account.email.casefold()}",
                    )
                )
                if not any(x.identity_id == identity_id for x in identities):
                    identities.append(
                        GoogleIdentity(identity_id, account.name, account.email)
                    )
                migrated.append(
                    field_replace(
                        legacy_account_channel(theme_id, account),
                        google_identity_id=identity_id,
                    )
                )
            channels = tuple(migrated)
        if len(accounts) > 3:
            raise ValueError(f"Theme '{name}' can contain at most 3 accounts.")
        account_names = [account.name.casefold() for account in accounts]
        if len(account_names) != len(set(account_names)):
            raise ValueError(f"Theme '{name}' has duplicate account names.")
        if sum(account.enabled for account in accounts) > 3:
            raise ValueError(f"Theme '{name}' can enable at most 3 accounts.")
        enabled_slots = [account.slot for account in accounts if account.enabled]
        if len(enabled_slots) != len(set(enabled_slots)):
            raise ValueError(f"Theme '{name}' has duplicate enabled account slots.")
        policy = _load_policy(raw_theme.get("policy"), name)
        output = str(raw_theme.get("output_subdirectory", name)).strip()
        validate_output_subdirectory(output, name)
        themes.append(
            ThemeProfile(
                name=name,
                prompt=str(raw_theme.get("prompt", "")),
                creators=creators,
                output_subdirectory=output,
                enabled=_boolean(raw_theme, "enabled", True, f"theme '{name}'"),
                accounts=accounts,
                policy=policy,
                theme_id=theme_id,
                channels=channels,
            )
        )
    return AutomationConfig(tuple(themes), tuple(identities))


def automation_config_to_dict(config: AutomationConfig) -> dict[str, object]:
    return {
        "google_identities": [
            {
                "identity_id": item.identity_id,
                "label": item.label,
                "email": item.email,
                "enabled": item.enabled,
            }
            for item in config.google_identities
        ],
        "themes": [
            {
                "theme_id": theme.theme_id,
                "name": theme.name,
                "enabled": theme.enabled,
                "prompt": theme.prompt,
                "output_subdirectory": theme.output_subdirectory,
                "creators": [
                    {
                        "name": creator.name,
                        "videos_url": creator.videos_url,
                        "enabled": creator.enabled,
                    }
                    for creator in theme.creators
                ],
                "channels": [_channel_to_dict(channel) for channel in theme.channels],
                "policy": {
                    "videos_per_creator": theme.policy.videos_per_creator,
                    "maximum_age_days": theme.policy.maximum_age_days,
                    "shorts_per_video": theme.policy.shorts_per_video,
                },
            }
            for theme in config.themes
        ]
    }


def _channel_to_dict(channel: PublishingChannel) -> dict[str, object]:
    return {
        "channel_id": channel.channel_id,
        "name": channel.name,
        "slot": channel.slot,
        "enabled": channel.enabled,
        "google_identity_id": channel.google_identity_id,
        "youtube_channel_id": channel.youtube_channel_id,
        "youtube_channel_title": channel.youtube_channel_title,
        "editorial_profile": {
            "prompt_append": channel.editorial_profile.prompt_append,
            "headline_style": channel.editorial_profile.headline_style,
            "tone": channel.editorial_profile.tone,
        },
        "visual_profile": {
            "palette": {
                "primary": channel.visual_profile.palette.primary,
                "secondary": channel.visual_profile.palette.secondary,
                "accent": channel.visual_profile.palette.accent,
                "background": channel.visual_profile.palette.background,
            },
            "crop_preset": channel.visual_profile.crop_preset,
            "caption_preset": channel.visual_profile.caption_preset,
            "branding_preset": channel.visual_profile.branding_preset,
        },
        "posting_policy": {
            "enabled": channel.posting_policy.enabled,
            "daily_limit": channel.posting_policy.daily_limit,
            "timezone": channel.posting_policy.timezone,
            "posting_windows": list(channel.posting_policy.posting_windows),
        },
    }


def validate_output_subdirectory(value: str, theme_name: str = "theme") -> None:
    if not value:
        raise ValueError(f"Theme '{theme_name}' has an empty output_subdirectory.")
    path = PurePath(value)
    if path.is_absolute() or ".." in path.parts or len(path.parts) != 1:
        raise ValueError("A pasta de saída deve ser um nome simples, sem '/' ou '..'.")
    if value in {".", ".."}:
        raise ValueError("A pasta de saída informada é inválida.")


def validate_creator(name: str, videos_url: str) -> tuple[str, str]:
    clean_name = name.strip()
    clean_url = videos_url.strip()
    if not clean_name:
        raise ValueError("O nome do criador não pode ficar vazio.")
    parsed = urlparse(clean_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"Creator '{clean_name}' must have an HTTP(S) videos_url.")
    return clean_name, clean_url


def validate_account(name: str, email: str) -> tuple[str, str]:
    clean_name = name.strip()
    clean_email = email.strip()
    if not clean_name:
        raise ValueError("O nome da conta não pode ficar vazio.")
    if not clean_email or "@" not in clean_email or clean_email.startswith("@"):
        raise ValueError("Informe um email válido para identificar a conta.")
    return clean_name, clean_email


def _required_text(data: dict[str, object], key: str, context: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{context} requires a non-empty '{key}'.")
    return value.strip()


def _boolean(
    data: dict[str, object], key: str, default: bool, context: str
) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{context} field '{key}' must be boolean.")
    return value


def _load_creator(data: object, theme_name: str) -> Creator:
    if not isinstance(data, dict):
        raise ValueError(f"Creators in theme '{theme_name}' must be objects.")
    name = _required_text(data, "name", "creator")
    videos_url = _required_text(data, "videos_url", f"creator '{name}'")
    name, videos_url = validate_creator(name, videos_url)
    return Creator(
        name,
        videos_url,
        _boolean(data, "enabled", True, f"creator '{name}'"),
    )


def _load_account(
    data: object, theme_name: str, *, legacy_slot: int
) -> AccountProfile:
    if not isinstance(data, dict):
        raise ValueError(f"Accounts in theme '{theme_name}' must be objects.")
    name = _required_text(data, "name", "account")
    email = _required_text(data, "email", f"account '{name}'")
    name, email = validate_account(name, email)
    raw_slot = data.get("slot", legacy_slot)
    if isinstance(raw_slot, bool):
        raise ValueError(f"Account '{name}' slot must be 1, 2 or 3.")
    try:
        slot = int(raw_slot)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Account '{name}' slot must be 1, 2 or 3.") from exc
    return AccountProfile(
        name,
        email,
        slot,
        _boolean(data, "enabled", True, f"account '{name}'"),
    )


def _load_google_identity(data: object) -> GoogleIdentity:
    if not isinstance(data, dict):
        raise ValueError("Google identities must be objects.")
    identity_id = _required_text(data, "identity_id", "Google identity")
    label = _required_text(data, "label", f"Google identity '{identity_id}'")
    email = _required_text(data, "email", f"Google identity '{identity_id}'")
    _, email = validate_account(label, email)
    return GoogleIdentity(
        identity_id,
        label,
        email,
        _boolean(data, "enabled", True, f"Google identity '{identity_id}'"),
    )


def _load_channel(data: object, theme_name: str) -> PublishingChannel:
    if not isinstance(data, dict):
        raise ValueError(f"Channels in theme '{theme_name}' must be objects.")
    channel_id = _required_text(data, "channel_id", "publishing channel")
    name = _required_text(data, "name", f"channel '{channel_id}'")
    raw_slot = data.get("slot")
    try:
        slot = int(raw_slot)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Channel '{name}' slot must be 1, 2 or 3.") from exc
    editorial = _mapping(data.get("editorial_profile"), "editorial_profile")
    visual = _mapping(data.get("visual_profile"), "visual_profile")
    palette = _mapping(visual.get("palette"), "palette")
    posting = _mapping(data.get("posting_policy"), "posting_policy")
    windows = posting.get("posting_windows", [])
    if not isinstance(windows, list) or not all(isinstance(x, str) for x in windows):
        raise ValueError("posting_windows must be a list of strings.")
    daily_limit = posting.get("daily_limit")
    if daily_limit is not None:
        try:
            daily_limit = int(daily_limit)
        except (TypeError, ValueError) as exc:
            raise ValueError("daily_limit must be an integer or null.") from exc
    return PublishingChannel(
        channel_id=channel_id,
        name=name,
        slot=slot,
        enabled=_boolean(data, "enabled", True, f"channel '{name}'"),
        google_identity_id=_optional_text(data.get("google_identity_id")),
        youtube_channel_id=_optional_text(data.get("youtube_channel_id")),
        youtube_channel_title=_optional_text(data.get("youtube_channel_title")),
        editorial_profile=EditorialProfile(
            str(editorial.get("prompt_append", "")),
            str(editorial.get("headline_style", "")),
            str(editorial.get("tone", "")),
        ),
        visual_profile=VisualProfile(
            ColorPalette(
                str(palette.get("primary", "#ffffff")),
                str(palette.get("secondary", "#000000")),
                str(palette.get("accent", "#3b82f6")),
                str(palette.get("background", "#0f172a")),
            ),
            str(visual.get("crop_preset", "default")),
            str(visual.get("caption_preset", "default")),
            str(visual.get("branding_preset", "none")),
        ),
        posting_policy=PostingPolicy(
            _boolean(posting, "enabled", False, f"channel '{name}' posting policy"),
            daily_limit,
            str(posting.get("timezone", "America/Sao_Paulo")),
            tuple(windows),
        ),
    )


def _mapping(value: object, name: str) -> dict[str, object]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object.")
    return value


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Optional identifiers must be strings or null.")
    return value.strip() or None


def _load_policy(data: object, theme_name: str) -> ThemePolicy:
    if data is None:
        return ThemePolicy()
    if not isinstance(data, dict):
        raise ValueError(f"Theme '{theme_name}' policy must be an object.")
    try:
        return ThemePolicy(
            videos_per_creator=int(data.get("videos_per_creator", 1)),
            maximum_age_days=int(data.get("maximum_age_days", 3650)),
            shorts_per_video=int(data.get("shorts_per_video", 3)),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Theme '{theme_name}' has an invalid policy: {exc}") from exc
