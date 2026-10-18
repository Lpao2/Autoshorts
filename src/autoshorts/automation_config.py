"""Safe, declarative configuration for automated source collection."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from urllib.parse import urlparse


@dataclass(frozen=True, slots=True)
class Creator:
    name: str
    videos_url: str
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class AccountProfile:
    name: str
    email: str
    enabled: bool = True


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


@dataclass(frozen=True, slots=True)
class AutomationConfig:
    themes: tuple[ThemeProfile, ...]

    def theme(self, name: str) -> ThemeProfile:
        for item in self.themes:
            if item.name == name:
                return item
        raise KeyError(f"Theme not found: {name}")


def load_automation_config(path: str | Path) -> AutomationConfig:
    """Load configuration while retaining compatibility with pre-enabled JSON."""
    config_path = Path(path)
    data = json.loads(config_path.read_text(encoding="utf-8"))
    return automation_config_from_dict(data)


def automation_config_from_dict(data: object) -> AutomationConfig:
    if not isinstance(data, dict):
        raise ValueError("Automation configuration must be an object.")
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
        accounts = tuple(_load_account(item, name) for item in accounts_data)
        if len(accounts) > 3:
            raise ValueError(f"Theme '{name}' can contain at most 3 accounts.")
        account_names = [account.name.casefold() for account in accounts]
        if len(account_names) != len(set(account_names)):
            raise ValueError(f"Theme '{name}' has duplicate account names.")
        if sum(account.enabled for account in accounts) > 3:
            raise ValueError(f"Theme '{name}' can enable at most 3 accounts.")
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
            )
        )
    return AutomationConfig(tuple(themes))


def automation_config_to_dict(config: AutomationConfig) -> dict[str, object]:
    return {
        "themes": [
            {
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
                "accounts": [
                    {
                        "name": account.name,
                        "email": account.email,
                        "enabled": account.enabled,
                    }
                    for account in theme.accounts
                ],
                "policy": {
                    "videos_per_creator": theme.policy.videos_per_creator,
                    "maximum_age_days": theme.policy.maximum_age_days,
                    "shorts_per_video": theme.policy.shorts_per_video,
                },
            }
            for theme in config.themes
        ]
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


def _load_account(data: object, theme_name: str) -> AccountProfile:
    if not isinstance(data, dict):
        raise ValueError(f"Accounts in theme '{theme_name}' must be objects.")
    name = _required_text(data, "name", "account")
    email = _required_text(data, "email", f"account '{name}'")
    name, email = validate_account(name, email)
    return AccountProfile(
        name,
        email,
        _boolean(data, "enabled", True, f"account '{name}'"),
    )


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
