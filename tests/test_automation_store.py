import json

import pytest

from autoshorts.automation_config import (
    AccountProfile,
    AutomationConfig,
    Creator,
    ThemePolicy,
    ThemeProfile,
)
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
    assert config.theme("LoL").accounts == ()
    assert config.theme("LoL").policy == ThemePolicy()


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


def test_accounts_and_policy_persist_and_reload(tmp_path):
    store = AutomationStore(tmp_path / "automation.json")
    config = AutomationConfig(
        (
            ThemeProfile(
                "LoL",
                "prompt",
                (),
                "lol",
                accounts=(
                    AccountProfile("A", "a@example.test"),
                    AccountProfile("B", "b@example.test", False),
                ),
                policy=ThemePolicy(3, 30),
            ),
        )
    )
    store.save(config)
    loaded = store.load().theme("LoL")
    assert loaded.accounts == config.theme("LoL").accounts
    assert loaded.policy == ThemePolicy(3, 30, 3)


def test_account_crud_preserves_slot_order_and_limit(tmp_path):
    store = AutomationStore(tmp_path / "automation.json")
    config = store.add_theme(
        AutomationConfig(()), name="LoL", output_subdirectory="lol"
    )
    for index in range(1, 4):
        config = store.add_account(
            config,
            "LoL",
            name=f"Account {index}",
            email=f"a{index}@example.test",
        )
    assert [item.name for item in config.theme("LoL").accounts] == [
        "Account 1",
        "Account 2",
        "Account 3",
    ]
    with pytest.raises(ValueError, match="máximo 3"):
        store.add_account(
            config, "LoL", name="Account 4", email="a4@example.test"
        )
    config = store.edit_account(
        config,
        "LoL",
        "Account 2",
        name="Middle",
        email="middle@example.test",
        enabled=False,
    )
    assert config.theme("LoL").accounts[1] == AccountProfile(
        "Middle", "middle@example.test", False
    )
    config = store.remove_account(config, "LoL", "Middle")
    assert [item.name for item in config.theme("LoL").accounts] == [
        "Account 1",
        "Account 3",
    ]


def test_policy_rejects_non_three_shorts_and_can_be_edited(tmp_path):
    with pytest.raises(ValueError, match="exactly 3"):
        ThemePolicy(shorts_per_video=2)
    store = AutomationStore(tmp_path / "automation.json")
    config = store.add_theme(
        AutomationConfig(()), name="LoL", output_subdirectory="lol"
    )
    config = store.set_theme_policy(config, "LoL", ThemePolicy(5, 60))
    assert store.load().theme("LoL").policy == ThemePolicy(5, 60)
