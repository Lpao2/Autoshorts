import sqlite3

import pytest

from autoshorts.state_store import (
    SCHEMA_VERSION,
    QueuedShort,
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


def test_migrates_v14_and_rejects_only_future_schema(tmp_path):
    current = tmp_path / "v14.db"
    with StateStore(current):
        pass
    connection = sqlite3.connect(current)
    connection.executescript(
        """
        DROP INDEX idx_one_approved_brand_asset;
        DROP INDEX IF EXISTS idx_brand_asset_version;
        DROP TABLE brand_asset_metadata;
        CREATE TABLE brand_asset_metadata (
            id INTEGER PRIMARY KEY AUTOINCREMENT, brand_plan_id TEXT NOT NULL,
            asset_type TEXT NOT NULL, file_path TEXT NOT NULL,
            provider TEXT NOT NULL, model TEXT NOT NULL, prompt TEXT NOT NULL,
            width INTEGER NOT NULL, height INTEGER NOT NULL,
            checksum TEXT NOT NULL, created_at TEXT NOT NULL, status TEXT NOT NULL,
            FOREIGN KEY (brand_plan_id)
                REFERENCES planned_channel_brands(brand_plan_id) ON DELETE CASCADE
        );
        PRAGMA user_version = 14;
        """
    )
    connection.close()
    with StateStore(current) as store:
        assert store.schema_version == SCHEMA_VERSION
        assert store.connection.execute(
            "SELECT name FROM pragma_table_info('brand_asset_metadata') "
            "WHERE name='version'"
        ).fetchone()

    future = tmp_path / "future.db"
    connection = sqlite3.connect(future)
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    connection.close()
    with pytest.raises(
        RuntimeError,
        match=f"Unsupported state schema version: {SCHEMA_VERSION + 1}",
    ):
        StateStore(future)


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
        assert store.get_video("video-1").title == "Changed title"


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
            tuple(RenderedShort(index, files[index - 1]) for index in range(1, 4)),
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
            RenderedShort(1, files[0]),
            RenderedShort(2, files[1]),
            RenderedShort(3, files[2]),
        )
        store.connection.execute(
            """
            CREATE TRIGGER fail_second_short BEFORE INSERT ON shorts
            WHEN NEW.clip_index = 2 BEGIN SELECT RAISE(ABORT, 'short 2 failed'); END
            """
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


def test_queue_three_shorts_and_video_atomically(tmp_path):
    with StateStore(tmp_path / "state.db") as store:
        register(store)
        advance_to_rendering(store)
        masters = tuple(tmp_path / f"{index:02d}.mp4" for index in range(1, 4))
        queues = tuple(tmp_path / f"queue-{index}.mp4" for index in range(1, 4))
        for path in (*masters, *queues):
            path.touch()
        store.record_rendered_shorts(
            "video-1",
            tuple(
                RenderedShort(
                    index,
                    path,
                    title=f"Title {index}",
                    headline=f"Headline {index}",
                    generator_score=0.9 - index / 100,
                    editorial_score=0.8 - index / 100,
                    candidate_id=f"candidate-{4 - index}",
                    duration_seconds=20 + index,
                )
                for index, path in enumerate(masters, 1)
            ),
        )
        rows = store.record_queued_shorts(
            "video-1",
            tuple(
                QueuedShort(index, index, f"a{index}@example.test", path)
                for index, path in enumerate(queues, 1)
            ),
        )
        assert store.get_video("video-1").status == VideoStatus.QUEUED
        assert [row.account_slot for row in rows] == [1, 2, 3]
        assert [row.queue_path for row in rows] == list(queues)
        metadata = store.queued_metadata("video-1")
        assert [item.rank for item in metadata] == [1, 2, 3]
        assert [item.account_slot for item in metadata] == [1, 2, 3]
        assert metadata[0].headline == "Headline 1"
        assert metadata[0].publishing_channel_id == "a1@example.test"
        assert metadata[0].candidate_id == "candidate-3"
        assert metadata[0].youtube_id == "video-1"
        assert metadata[0].creator_name == "Arthur"
        assert metadata[0].theme_name == "LoL"
        assert metadata[0].duration_seconds == 21


def test_queue_database_failure_rolls_back_video_and_shorts(tmp_path):
    with StateStore(tmp_path / "state.db") as store:
        register(store)
        advance_to_rendering(store)
        masters = tuple(tmp_path / f"{index:02d}.mp4" for index in range(1, 4))
        queues = tuple(tmp_path / f"queue-{index}.mp4" for index in range(1, 4))
        for path in (*masters, *queues):
            path.touch()
        store.record_rendered_shorts(
            "video-1",
            tuple(RenderedShort(index, path) for index, path in enumerate(masters, 1)),
        )
        store.connection.execute(
            """
            CREATE TRIGGER fail_queue_two BEFORE UPDATE ON shorts
            WHEN NEW.clip_index = 2 AND NEW.status = 'QUEUED'
            BEGIN SELECT RAISE(ABORT, 'queue 2 failed'); END
            """
        )
        queued = tuple(
            QueuedShort(index, index, f"a{index}@example.test", path)
            for index, path in enumerate(queues, 1)
        )
        with pytest.raises(sqlite3.IntegrityError, match="queue 2 failed"):
            store.record_queued_shorts("video-1", queued)
        assert store.get_video("video-1").status == VideoStatus.RENDERED
        assert all(
            row.status.value == "RENDERED" for row in store.shorts_for_video("video-1")
        )


@pytest.mark.parametrize(
    "status", [VideoStatus.RENDERED, VideoStatus.QUEUED, VideoStatus.COMPLETED]
)
def test_produced_states_are_deduplicated(tmp_path, status):
    with StateStore(tmp_path / "state.db") as store:
        register(store)
        store.connection.execute(
            "UPDATE videos SET status = ? WHERE youtube_id = 'video-1'", (status,)
        )
        store.connection.commit()
        assert store.is_produced("video-1")


def test_migrates_v1_database_and_preserves_data(tmp_path):
    path = tmp_path / "v1.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE videos (
            youtube_id TEXT PRIMARY KEY, source_url TEXT NOT NULL,
            creator_name TEXT NOT NULL, theme_name TEXT NOT NULL, title TEXT NOT NULL,
            status TEXT NOT NULL, discovered_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            processed_at TEXT, error TEXT
        );
        CREATE TABLE shorts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, youtube_id TEXT NOT NULL,
            clip_index INTEGER NOT NULL, account_slot INTEGER NOT NULL,
            account_identifier TEXT NOT NULL, status TEXT NOT NULL,
            file_path TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            posted_at TEXT, error TEXT
        );
        INSERT INTO videos VALUES (
            'old', 'url', 'creator', 'theme', 'title', 'RENDERED',
            'created', 'updated', NULL, NULL
        );
        INSERT INTO shorts VALUES (
            1, 'old', 1, 1, 'account', 'RENDERED', '/tmp/01.mp4',
            'created', 'updated', NULL, NULL
        );
        PRAGMA user_version = 1;
        """
    )
    connection.close()
    with StateStore(path) as store:
        assert store.schema_version == SCHEMA_VERSION
        assert store.get_video("old").title == "title"
        assert store.shorts_for_video("old")[0].account_identifier == "account"
        columns = {
            row[1] for row in store.connection.execute("PRAGMA table_info(shorts)")
        }
        assert "queue_path" in columns
        assert "transcripts" in {
            row[0]
            for row in store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        tables = {
            row[0]
            for row in store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"analyses", "ai_usage"} <= tables


def test_migrates_v2_to_v3_without_losing_video_or_shorts(tmp_path):
    path = tmp_path / "v2.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE videos (
            youtube_id TEXT PRIMARY KEY, source_url TEXT NOT NULL,
            creator_name TEXT NOT NULL, theme_name TEXT NOT NULL, title TEXT NOT NULL,
            status TEXT NOT NULL, discovered_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            processed_at TEXT, error TEXT
        );
        CREATE TABLE shorts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, youtube_id TEXT NOT NULL,
            clip_index INTEGER NOT NULL, account_slot INTEGER,
            account_identifier TEXT, status TEXT NOT NULL, file_path TEXT NOT NULL,
            queue_path TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            posted_at TEXT, error TEXT
        );
        INSERT INTO videos VALUES (
            'old', 'url', 'creator', 'theme', 'title', 'DOWNLOADED',
            'created', 'updated', NULL, NULL
        );
        PRAGMA user_version = 2;
        """
    )
    connection.close()
    with StateStore(path) as store:
        assert store.schema_version == SCHEMA_VERSION
        assert store.get_video("old").status == VideoStatus.DOWNLOADED
        assert store.get_transcript("old") is None


def test_migrates_v6_channel_reference_without_losing_legacy_identifier(tmp_path):
    path = tmp_path / "v6.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE videos (
            youtube_id TEXT PRIMARY KEY, source_url TEXT NOT NULL,
            creator_name TEXT NOT NULL, theme_name TEXT NOT NULL, title TEXT NOT NULL,
            status TEXT NOT NULL, discovered_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            processed_at TEXT, error TEXT
        );
        CREATE TABLE shorts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, youtube_id TEXT NOT NULL,
            clip_index INTEGER NOT NULL, account_slot INTEGER,
            account_identifier TEXT, status TEXT NOT NULL, file_path TEXT NOT NULL,
            queue_path TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            posted_at TEXT, error TEXT, title TEXT NOT NULL DEFAULT '',
            headline TEXT NOT NULL DEFAULT '', generator_score REAL,
            editorial_score REAL, candidate_id TEXT, duration_seconds REAL
        );
        INSERT INTO videos VALUES (
            'old', 'url', 'creator', 'theme', 'title', 'QUEUED',
            'created', 'updated', NULL, NULL
        );
        INSERT INTO shorts VALUES (
            1, 'old', 1, 1, 'legacy@example.test', 'QUEUED', '/tmp/01.mp4',
            '/tmp/q01.mp4', 'created', 'updated', NULL, NULL, '', '', NULL,
            NULL, NULL, NULL
        );
        PRAGMA user_version = 6;
        """
    )
    connection.close()
    with StateStore(path) as store:
        row = store.shorts_for_video("old")[0]
        assert store.schema_version == SCHEMA_VERSION
        assert row.account_identifier == "legacy@example.test"
        assert row.publishing_channel_id == "legacy@example.test"


def test_migrates_v7_to_v8_with_non_secret_channel_bindings_table(tmp_path):
    path = tmp_path / "v7.db"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version = 7")
    connection.close()
    with StateStore(path) as store:
        columns = {
            row[1]
            for row in store.connection.execute("PRAGMA table_info(channel_bindings)")
        }
        assert store.schema_version == SCHEMA_VERSION
        assert "credential_ref" in columns
        assert "youtube_channel_id" in columns
        assert "access_token" not in columns
        assert "refresh_token" not in columns
