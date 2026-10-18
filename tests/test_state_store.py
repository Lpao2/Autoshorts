import sqlite3

import pytest

from autoshorts.state_store import (
    SCHEMA_VERSION,
    RenderedShort,
    StateStore,
    VideoStatus,
    default_state_db_path,
)


def register(store, *, theme="LoL"):
    return store.register_video(
        youtube_id="video-1",
        source_url="https://youtube.com/watch?v=video-1",
        creator_name="Arthur",
        theme_name=theme,
        title="Video",
    )


def advance_to_rendering(store):
    store.transition_video("video-1", VideoStatus.DOWNLOADING)
    store.transition_video("video-1", VideoStatus.DOWNLOADED)
    store.transition_video("video-1", VideoStatus.RENDERING)


def test_initializes_schema_foreign_keys_and_wal(tmp_path):
    with StateStore(tmp_path / "state.db") as store:
        tables = {
            row[0]
            for row in store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"videos", "shorts"} <= tables
        assert store.schema_version == SCHEMA_VERSION
        assert store.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert store.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_global_deduplication_uses_only_youtube_id(tmp_path):
    with StateStore(tmp_path / "state.db") as store:
        assert register(store, theme="LoL")
        assert not store.register_video(
            youtube_id="video-1",
            source_url="https://youtube.com/watch?v=video-1",
            creator_name="Other creator",
            theme_name="Other theme",
            title="Changed title",
        )
        assert store.get_video("video-1").theme_name == "LoL"


def test_lifecycle_error_and_invalid_transition(tmp_path):
    with StateStore(tmp_path / "state.db") as store:
        register(store)
        store.transition_video("video-1", VideoStatus.DOWNLOADING)
        failed = store.fail_video("video-1", "network")
        assert failed.status == VideoStatus.FAILED
        assert failed.error == "network"
        with pytest.raises(ValueError, match="Invalid"):
            store.transition_video("video-1", VideoStatus.COMPLETED)
        assert store.transition_video("video-1", VideoStatus.DISCOVERED)


def test_restart_reopens_state_and_lists_interrupted(tmp_path):
    path = tmp_path / "state.db"
    with StateStore(path) as store:
        register(store)
        store.transition_video("video-1", VideoStatus.DOWNLOADING)
    with StateStore(path) as reopened:
        assert reopened.get_video("video-1").status == VideoStatus.DOWNLOADING
        assert [item.youtube_id for item in reopened.interrupted_videos()] == [
            "video-1"
        ]


def test_three_shorts_and_rendered_transition_are_atomic(tmp_path):
    with StateStore(tmp_path / "state.db") as store:
        register(store)
        advance_to_rendering(store)
        files = []
        for index in range(1, 4):
            path = tmp_path / f"{index:02d}.mp4"
            path.touch()
            files.append(path)
        rows = store.record_rendered_shorts(
            "video-1",
            tuple(
                RenderedShort(index, index, f"account-{index}", files[index - 1])
                for index in range(1, 4)
            ),
        )
        assert [row.clip_index for row in rows] == [1, 2, 3]
        assert store.get_video("video-1").status == VideoStatus.RENDERED


def test_short_transaction_rolls_back_on_insert_failure(tmp_path):
    with StateStore(tmp_path / "state.db") as store:
        register(store)
        advance_to_rendering(store)
        files = []
        for index in range(1, 4):
            path = tmp_path / f"{index:02d}.mp4"
            path.touch()
            files.append(path)
        records = (
            RenderedShort(1, 1, "account-1", files[0]),
            RenderedShort(2, 2, None, files[1]),  # type: ignore[arg-type]
            RenderedShort(3, 3, "account-3", files[2]),
        )
        with pytest.raises(sqlite3.IntegrityError):
            store.record_rendered_shorts("video-1", records)
        assert store.shorts_for_video("video-1") == ()
        assert store.get_video("video-1").status == VideoStatus.RENDERING


def test_foreign_keys_are_enforced(tmp_path):
    with StateStore(tmp_path / "state.db") as store:
        with pytest.raises(sqlite3.IntegrityError), store.connection:
            store.connection.execute(
                """
                INSERT INTO shorts (
                    youtube_id, clip_index, account_slot, account_identifier,
                    status, file_path, created_at, updated_at
                ) VALUES ('missing', 1, 1, 'a', 'RENDERED', 'x', 'now', 'now')
                """
            )


def test_default_db_path_can_be_overridden(tmp_path, monkeypatch):
    selected = tmp_path / "server.db"
    monkeypatch.setenv("AUTOSHORTS_STATE_DB", str(selected))
    assert default_state_db_path(tmp_path) == selected.resolve()
