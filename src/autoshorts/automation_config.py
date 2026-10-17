"""Safe, declarative configuration for automated source collection."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


@dataclass(frozen=True, slots=True)
class Creator:
    name: str
    videos_url: str


@dataclass(frozen=True, slots=True)
class ThemeProfile:
    name: str
    prompt: str
    creators: tuple[Creator, ...]
    output_subdirectory: str


@dataclass(frozen=True, slots=True)
class AutomationConfig:
    themes: tuple[ThemeProfile, ...]

    def theme(self, name: str) -> ThemeProfile:
        for item in self.themes:
            if item.name == name:
                return item
        raise KeyError(f"Theme not found: {name}")


def load_automation_config(path: str | Path) -> AutomationConfig:
    """Load public configuration; credentials and browser sessions are excluded."""
    config_path = Path(path)
    data = json.loads(config_path.read_text(encoding="utf-8"))
    themes: list[ThemeProfile] = []
    raw_themes = data.get("themes")
    if not isinstance(raw_themes, list):
        raise ValueError("Automation configuration must contain a themes list.")
    for raw_theme in raw_themes:
        if not isinstance(raw_theme, dict):
            raise ValueError("Each theme must be an object.")
        name = _required_text(raw_theme, "name", "theme")
        creators_data = raw_theme.get("creators")
        if not isinstance(creators_data, list) or not creators_data:
            raise ValueError(f"Theme '{name}' must contain at least one creator.")
        creators = tuple(_load_creator(item, name) for item in creators_data)
        output_subdirectory = str(raw_theme.get("output_subdirectory", name)).strip()
        if not output_subdirectory:
            raise ValueError(f"Theme '{name}' has an empty output_subdirectory.")
        themes.append(
            ThemeProfile(
                name=name,
                prompt=str(raw_theme.get("prompt", "")),
                creators=creators,
                output_subdirectory=output_subdirectory,
            )
        )
    if not themes:
        raise ValueError("Automation configuration must contain at least one theme.")
    return AutomationConfig(tuple(themes))


def _required_text(data: dict[str, object], key: str, context: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{context} requires a non-empty '{key}'.")
    return value.strip()


def _load_creator(data: object, theme_name: str) -> Creator:
    if not isinstance(data, dict):
        raise ValueError(f"Creators in theme '{theme_name}' must be objects.")
    name = _required_text(data, "name", "creator")
    videos_url = _required_text(data, "videos_url", f"creator '{name}'")
    parsed = urlparse(videos_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"Creator '{name}' must have an HTTP(S) videos_url.")
    return Creator(name, videos_url)
