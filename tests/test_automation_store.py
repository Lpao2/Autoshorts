import json

import pytest

from autoshorts.automation_config import AutomationConfig, Creator, ThemeProfile
from autoshorts.automation_store import (
    AutomationStore,
    default_automation_config_path,
)


def old_config(path):
    path.write_text(
        json.dumps(
            {
                "themes": [
                    {
                        "name": "LoL",
                        "prompt": "moments",
                        "output_subdirectory": "lol",
                        "creators": [
                            {
                                "name": "Arthur",
                                "videos_url": "https://youtube.com/@arthur/videos",
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def test_legacy_config_defaults_enabled_to_true(tmp_path):
    path = tmp_path / "automation.json"
    old_config(path)
    config = AutomationStore(path).load()
    assert config.theme("LoL").enabled
    assert config.theme("LoL").creators[0].enabled


def test_new_config_preserves_disabled_flags(tmp_path):
    path = tmp_path / "automation.json"
    path.write_text(
        '{"themes":[{"name":"LoL","output_subdirectory":"lol",'
        '"enabled":false,"creators":[{"name":"Arthur",'
        '"videos_url":"https://youtube.com/@arthur/videos",'
        '"enabled":false}]}]}',
        encoding="utf-8",
    )
    theme = AutomationStore(path).load().theme("LoL")
    assert not theme.enabled
    assert not theme.creators[0].enabled


def test_theme_crud_and_duplicate_protection(tmp_path):
    store = AutomationStore(tmp_path / "automation.json")
    config = store.add_theme(
        AutomationConfig(()), name="LoL", output_subdirectory="lol"
    )
    assert store.load().theme("LoL").creators == ()
    with pytest.raises(ValueError, match="Já existe"):
        store.add_theme(config, name="lol", output_subdirectory="other")
    config = store.edit_theme(
        config,
        "LoL",
        name="League",
        output_subdirectory="league",
        prompt="highlights",
        enabled=False,
    )
    assert not config.theme("League").enabled
    assert store.remove_theme(config, "League").themes == ()


def test_rejects_dangerous_output_directory(tmp_path):
    store = AutomationStore(tmp_path / "automation.json")
    with pytest.raises(ValueError, match="nome simples"):
        store.add_theme(
            AutomationConfig(()), name="LoL", output_subdirectory="../outside"
        )


def test_creator_crud_and_enabled_toggle(tmp_path):
    store = AutomationStore(tmp_path / "automation.json")
    config = store.add_theme(
        AutomationConfig(()), name="LoL", output_subdirectory="lol"
    )
    config = store.add_creator(
        config,
        "LoL",
        name="Arthur",
        videos_url="https://youtube.com/@arthur/videos",
    )
    config = store.edit_creator(
        config,
        "LoL",
        "Arthur",
        name="Arthur Lanches",
        videos_url="https://youtube.com/@ArthurLanches/videos",
        enabled=False,
    )
    assert not config.theme("LoL").creators[0].enabled
    config = store.set_creator_enabled(config, "LoL", "Arthur Lanches", True)
    assert config.theme("LoL").creators[0].enabled
    removed = store.remove_creator(config, "LoL", "Arthur Lanches")
    assert removed.theme("LoL").creators == ()


def test_save_is_atomic_fsynced_and_reloadable(tmp_path, monkeypatch):
    path = tmp_path / "automation.json"
    calls = []
    monkeypatch.setattr("autoshorts.automation_store.os.fsync", calls.append)
    config = AutomationConfig(
        (
            ThemeProfile(
                "LoL",
                "prompt",
                (Creator("Arthur", "https://youtube.com/@arthur/videos"),),
                "lol",
            ),
        )
    )
    store = AutomationStore(path)
    store.save(config)
    assert store.load() == config
    assert calls
    assert not path.with_name("automation.json.tmp").exists()


def test_environment_overrides_default_config_path(tmp_path, monkeypatch):
    selected = tmp_path / "server" / "automation.json"
    monkeypatch.setenv("AUTOSHORTS_AUTOMATION_CONFIG", str(selected))
    assert default_automation_config_path(tmp_path / "project") == selected
