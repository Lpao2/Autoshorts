"""Atomic persistence and immutable editing for automation configuration."""

from __future__ import annotations

import json
import os
import re
from dataclasses import replace
from pathlib import Path

from autoshorts.automation_config import (
    AutomationConfig,
    Creator,
    ThemeProfile,
    automation_config_from_dict,
    automation_config_to_dict,
    load_automation_config,
    validate_creator,
    validate_output_subdirectory,
)

CONFIG_ENV = "AUTOSHORTS_AUTOMATION_CONFIG"


def default_automation_config_path(project_root: Path | None = None) -> Path:
    configured = os.environ.get(CONFIG_ENV)
    if configured:
        return Path(configured).expanduser()
    root = project_root or Path(__file__).resolve().parents[2]
    return root / "config" / "automation.local.json"


def suggest_output_subdirectory(name: str) -> str:
    normalized = re.sub(r"[^a-z0-9_-]+", "-", name.casefold()).strip("-_")
    return normalized or "tema"


class AutomationStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()

    def load(self) -> AutomationConfig:
        if not self.path.exists():
            return AutomationConfig(())
        return load_automation_config(self.path)

    def save(self, config: AutomationConfig) -> AutomationConfig:
        data = automation_config_to_dict(config)
        validated = automation_config_from_dict(data)
        payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f"{self.path.name}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.path)
            _fsync_directory(self.path.parent)
        finally:
            temporary.unlink(missing_ok=True)
        return validated

    def add_theme(
        self,
        config: AutomationConfig,
        *,
        name: str,
        output_subdirectory: str,
        prompt: str = "",
        enabled: bool = True,
    ) -> AutomationConfig:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("O nome do tema não pode ficar vazio.")
        self._ensure_unique_theme(config, clean_name)
        validate_output_subdirectory(output_subdirectory.strip(), clean_name)
        theme = ThemeProfile(
            clean_name,
            prompt,
            (),
            output_subdirectory.strip(),
            enabled,
        )
        return self.save(replace(config, themes=(*config.themes, theme)))

    def edit_theme(
        self,
        config: AutomationConfig,
        original_name: str,
        *,
        name: str,
        output_subdirectory: str,
        prompt: str,
        enabled: bool,
    ) -> AutomationConfig:
        current = config.theme(original_name)
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("O nome do tema não pode ficar vazio.")
        self._ensure_unique_theme(config, clean_name, excluding=original_name)
        validate_output_subdirectory(output_subdirectory.strip(), clean_name)
        updated = replace(
            current,
            name=clean_name,
            output_subdirectory=output_subdirectory.strip(),
            prompt=prompt,
            enabled=enabled,
        )
        return self.save(self._replace_theme(config, original_name, updated))

    def remove_theme(self, config: AutomationConfig, name: str) -> AutomationConfig:
        config.theme(name)
        return self.save(
            replace(
                config,
                themes=tuple(item for item in config.themes if item.name != name),
            )
        )

    def set_theme_enabled(
        self, config: AutomationConfig, name: str, enabled: bool
    ) -> AutomationConfig:
        theme = config.theme(name)
        return self.save(
            self._replace_theme(config, name, replace(theme, enabled=enabled))
        )

    def add_creator(
        self,
        config: AutomationConfig,
        theme_name: str,
        *,
        name: str,
        videos_url: str,
        enabled: bool = True,
    ) -> AutomationConfig:
        theme = config.theme(theme_name)
        clean_name, clean_url = validate_creator(name, videos_url)
        self._ensure_unique_creator(theme, clean_name)
        updated = replace(
            theme, creators=(*theme.creators, Creator(clean_name, clean_url, enabled))
        )
        return self.save(self._replace_theme(config, theme_name, updated))

    def edit_creator(
        self,
        config: AutomationConfig,
        theme_name: str,
        original_name: str,
        *,
        name: str,
        videos_url: str,
        enabled: bool,
    ) -> AutomationConfig:
        theme = config.theme(theme_name)
        current = next(
            (item for item in theme.creators if item.name == original_name), None
        )
        if current is None:
            raise KeyError(f"Creator not found: {original_name}")
        clean_name, clean_url = validate_creator(name, videos_url)
        self._ensure_unique_creator(theme, clean_name, excluding=original_name)
        creator = replace(
            current, name=clean_name, videos_url=clean_url, enabled=enabled
        )
        creators = tuple(
            creator if item.name == original_name else item for item in theme.creators
        )
        return self.save(
            self._replace_theme(config, theme_name, replace(theme, creators=creators))
        )

    def remove_creator(
        self, config: AutomationConfig, theme_name: str, creator_name: str
    ) -> AutomationConfig:
        theme = config.theme(theme_name)
        if not any(item.name == creator_name for item in theme.creators):
            raise KeyError(f"Creator not found: {creator_name}")
        updated = replace(
            theme,
            creators=tuple(
                item for item in theme.creators if item.name != creator_name
            ),
        )
        return self.save(self._replace_theme(config, theme_name, updated))

    def set_creator_enabled(
        self,
        config: AutomationConfig,
        theme_name: str,
        creator_name: str,
        enabled: bool,
    ) -> AutomationConfig:
        theme = config.theme(theme_name)
        if not any(item.name == creator_name for item in theme.creators):
            raise KeyError(f"Creator not found: {creator_name}")
        creators = tuple(
            replace(item, enabled=enabled) if item.name == creator_name else item
            for item in theme.creators
        )
        return self.save(
            self._replace_theme(config, theme_name, replace(theme, creators=creators))
        )

    @staticmethod
    def _replace_theme(
        config: AutomationConfig, name: str, updated: ThemeProfile
    ) -> AutomationConfig:
        return replace(
            config,
            themes=tuple(
                updated if item.name == name else item for item in config.themes
            ),
        )

    @staticmethod
    def _ensure_unique_theme(
        config: AutomationConfig, name: str, *, excluding: str | None = None
    ) -> None:
        if any(
            item.name != excluding and item.name.casefold() == name.casefold()
            for item in config.themes
        ):
            raise ValueError(f"Já existe um tema chamado '{name}'.")

    @staticmethod
    def _ensure_unique_creator(
        theme: ThemeProfile, name: str, *, excluding: str | None = None
    ) -> None:
        if any(
            item.name != excluding and item.name.casefold() == name.casefold()
            for item in theme.creators
        ):
            raise ValueError(f"Já existe um criador chamado '{name}' neste tema.")


def _fsync_directory(directory: Path) -> None:
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
