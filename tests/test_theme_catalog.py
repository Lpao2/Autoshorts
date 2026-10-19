import sqlite3
from dataclasses import replace
from pathlib import Path

from autoshorts.state_store import SCHEMA_VERSION, StateStore
from autoshorts.theme_catalog import (
    CatalogProvenance,
    CatalogStatus,
    instantiate_theme,
    load_theme_seed,
)


def seed_path():
    return Path(__file__).resolve().parents[1] / "config/theme-catalog.legacy.json"


def test_legacy_seed_has_real_themes_prompts_and_three_archetypes():
    templates = load_theme_seed(seed_path())
    assert len(templates) == 13
    assert {item.theme_key for item in templates} >= {
        "lol", "academia", "atual", "anime", "animais", "comida",
        "comedia", "desenho", "fe", "filmes", "futebol", "influ", "mine",
    }
    assert all(item.base_prompt for item in templates)
    assert all(len(item.channel_archetypes) == 3 for item in templates)
    assert all(item.provenance == CatalogProvenance.LEGACY for item in templates)


def test_seed_is_idempotent_and_does_not_overwrite_manual_edit(tmp_path):
    templates = load_theme_seed(seed_path())
    with StateStore(tmp_path / "state.db") as store:
        assert len(store.list_theme_templates()) == 13
        lol = store.get_theme_template("lol")
        store.save_theme_template(
            replace(
                lol,
                description="edição manual",
                base_prompt="prompt refinado",
                provenance=CatalogProvenance.REFINED,
                prompt_versions=(),
            )
        )
        assert store.seed_theme_templates(templates) == 0
    with StateStore(tmp_path / "state.db") as reopened:
        lol = reopened.get_theme_template("lol")
        assert lol.description == "edição manual"
        assert lol.base_prompt == "prompt refinado"
        assert len(lol.prompt_versions) == 2


def test_instantiation_is_explicit_inactive_and_does_not_persist(tmp_path):
    with StateStore(tmp_path / "state.db") as store:
        template = store.get_theme_template("futebol")
        before = len(store.list_theme_templates())
        profile = instantiate_theme(template)
        assert profile.name == "Futebol"
        assert not profile.enabled
        assert len(profile.channels) == 3
        assert all(not channel.enabled for channel in profile.channels)
        assert len(store.list_theme_templates()) == before


def test_migrates_v8_to_v9_without_losing_channel_binding(tmp_path):
    path = tmp_path / "v8.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE channel_bindings (
            publishing_channel_id TEXT PRIMARY KEY,
            google_identity_id TEXT NOT NULL, provider TEXT NOT NULL,
            credential_ref TEXT NOT NULL, youtube_channel_id TEXT NOT NULL,
            youtube_channel_title TEXT NOT NULL, connected_at TEXT NOT NULL,
            last_verified_at TEXT NOT NULL, status TEXT NOT NULL
        );
        INSERT INTO channel_bindings VALUES
            ('lol-a', 'identity', 'youtube', 'ref', 'UC123', 'Canal LoL',
             'connected', 'verified', 'CONNECTED');
        PRAGMA user_version = 8;
        """
    )
    connection.close()
    with StateStore(path) as store:
        binding = store.get_channel_binding("lol-a")
        assert store.schema_version == SCHEMA_VERSION
        assert binding.youtube_channel_id == "UC123"
        assert len(store.list_theme_templates()) == 13


def test_catalog_statuses_are_closed_set():
    assert set(CatalogStatus) == {"DRAFT", "READY", "ACTIVE", "ARCHIVED"}
