import json

import pytest

from autoshorts.automation_config import (
    AccountProfile,
    AutomationConfig,
    Creator,
    EditorialProfile,
    GoogleIdentity,
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


def test_legacy_accounts_migrate_to_channels_and_policy_persists(tmp_path):
    store = AutomationStore(tmp_path / "automation.json")
    config = AutomationConfig(
        (
            ThemeProfile(
                "LoL",
                "prompt",
                (),
                "lol",
                accounts=(
                    AccountProfile("A", "a@example.test", 1),
                    AccountProfile("B", "b@example.test", 2, False),
                ),
                policy=ThemePolicy(3, 30),
            ),
        )
    )
    store.save(config)
    loaded_config = store.load()
    loaded = loaded_config.theme("LoL")
    assert [(item.name, item.slot) for item in loaded.channels] == [("A", 1), ("B", 2)]
    assert loaded.accounts == ()
    assert loaded.policy == ThemePolicy(3, 30, 3)


def test_channel_crud_preserves_slots_and_allows_archived_channels(tmp_path):
    store = AutomationStore(tmp_path / "automation.json")
    config = store.add_theme(
        AutomationConfig(()), name="LoL", output_subdirectory="lol"
    )
    for index in range(1, 4):
        config = store.add_channel(
            config,
            "LoL",
            name=f"Channel {index}",
            slot=index,
        )
    assert [item.name for item in config.theme("LoL").channels] == [
        "Channel 1",
        "Channel 2",
        "Channel 3",
    ]
    config = store.add_channel(
        config, "LoL", name="Archived Channel", slot=1, enabled=False
    )
    assert len(config.theme("LoL").channels) == 4
    with pytest.raises(ValueError, match="at most 3"):
        store.edit_channel(
            config,
            "LoL",
            config.theme("LoL").channels[-1].channel_id,
            enabled=True,
        )
    channel_id = config.theme("LoL").channels[1].channel_id
    config = store.edit_channel(
        config,
        "LoL",
        channel_id,
        name="Middle",
        slot=2,
        enabled=False,
        editorial_profile=EditorialProfile(tone="calmo"),
    )
    assert config.theme("LoL").channels[1].editorial_profile.tone == "calmo"
    config = store.remove_channel(config, "LoL", channel_id)
    assert [item.name for item in config.theme("LoL").channels] == [
        "Channel 1",
        "Channel 3",
        "Archived Channel",
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


def test_legacy_accounts_gain_slots_and_save_them(tmp_path):
    path = tmp_path / "automation.json"
    path.write_text(
        '{"themes":[{"name":"LoL","output_subdirectory":"lol","accounts":['
        '{"name":"A","email":"a@example.test"},'
        '{"name":"B","email":"b@example.test"},'
        '{"name":"C","email":"c@example.test"}]}]}',
        encoding="utf-8",
    )
    store = AutomationStore(path)
    config = store.load()
    assert [item.slot for item in config.theme("LoL").accounts] == [1, 2, 3]
    store.save(config)
    persisted = json.loads(path.read_text())
    assert "accounts" not in persisted["themes"][0]
    assert [item["slot"] for item in persisted["themes"][0]["channels"]] == [1, 2, 3]
    assert len(persisted["google_identities"]) == 3


def test_removing_channel_slot_two_does_not_renumber_slot_three(tmp_path):
    store = AutomationStore(tmp_path / "automation.json")
    config = store.add_theme(
        AutomationConfig(()), name="LoL", output_subdirectory="lol"
    )
    for slot in (1, 2, 3):
        config = store.add_channel(config, "LoL", name=f"A{slot}", slot=slot)
    middle_id = config.theme("LoL").channels[1].channel_id
    config = store.remove_channel(config, "LoL", middle_id)
    assert [(item.name, item.slot) for item in config.theme("LoL").channels] == [
        ("A1", 1),
        ("A3", 3),
    ]


def test_enabled_channel_slots_must_be_unique(tmp_path):
    store = AutomationStore(tmp_path / "automation.json")
    config = store.add_theme(
        AutomationConfig(()), name="LoL", output_subdirectory="lol"
    )
    config = store.add_channel(config, "LoL", name="A", slot=1)
    with pytest.raises(ValueError, match="slots must be unique"):
        store.add_channel(config, "LoL", name="B", slot=1)


def test_google_identity_can_serve_multiple_channels_and_cannot_be_removed(tmp_path):
    store = AutomationStore(tmp_path / "automation.json")
    config = AutomationConfig(
        (), (GoogleIdentity("google-a", "Google A", "a@example.test"),)
    )
    config = store.add_theme(config, name="LoL", output_subdirectory="lol")
    for slot in (1, 2):
        config = store.add_channel(
            config,
            "LoL",
            name=f"LoL {slot}",
            slot=slot,
            google_identity_id="google-a",
        )
    assert all(
        item.google_identity_id == "google-a"
        for item in config.theme("LoL").channels
    )
    with pytest.raises(ValueError, match="referenced"):
        store.remove_google_identity(config, "google-a")


@pytest.mark.parametrize("slot", [0, 4, -1])
def test_account_rejects_invalid_slot(slot):
    with pytest.raises(ValueError, match="slot"):
        AccountProfile("A", "a@example.test", slot)
