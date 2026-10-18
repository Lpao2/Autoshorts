from pathlib import Path

import pytest
from test_transcription import sample_transcript

from autoshorts.automation_config import AccountProfile, ThemeProfile
from autoshorts.media_probe import MediaInfo, MediaProbeError
from autoshorts.recovery import (
    RecoveryAction,
    inspect_video_state,
    prepare_recovery,
    reconcile_transcript,
    resume_rendered_distribution,
)
from autoshorts.state_store import RenderedShort, StateStore, VideoStatus
from autoshorts.transcription import write_transcript_atomic


def register(store, status):
    store.register_video(
        youtube_id="video-1",
        source_url="https://youtube.test/watch?v=video-1",
        creator_name="Creator",
        theme_name="Theme",
        title="Video",
    )
    store.connection.execute(
        "UPDATE videos SET status = ? WHERE youtube_id = 'video-1'", (status,)
    )
    store.connection.commit()


def theme():
    return ThemeProfile(
        "Theme",
        "",
        (),
        "theme",
        accounts=tuple(
            AccountProfile(f"Account {slot}", f"a{slot}@example.test", slot)
            for slot in range(1, 4)
        ),
    )


@pytest.fixture
def media_probe(monkeypatch):
    def probe(path, _ffprobe="ffprobe"):
        path = Path(path)
        if not path.is_file() or path.read_bytes() == b"corrupt":
            raise MediaProbeError("invalid")
        return MediaInfo(path, 10, 1080, 1920, 30, True, 0)

    monkeypatch.setattr("autoshorts.recovery.probe_media", probe)


def inspect(store, root):
    return inspect_video_state(
        store,
        "video-1",
        theme_root=root,
        creator_directory="Creator",
        expected_width=1080,
        expected_height=1920,
    )


def test_downloading_without_source_plans_clean_redownload(tmp_path, media_probe):
    with StateStore(tmp_path / "state.db") as store:
        register(store, VideoStatus.DOWNLOADING)
        residue = tmp_path / "downloads" / "Creator" / "video-1.f1.mp4.part"
        residue.parent.mkdir(parents=True)
        residue.touch()
        plan = inspect(store, tmp_path)
        assert plan.next_action == RecoveryAction.REDOWNLOAD
        assert plan.cleanup_actions == (residue,)
        prepare_recovery(store, plan)
        assert not residue.exists()
        assert store.get_video("video-1").status == VideoStatus.DISCOVERED


def test_downloaded_valid_source_is_reused(tmp_path, media_probe):
    with StateStore(tmp_path / "state.db") as store:
        register(store, VideoStatus.DOWNLOADED)
        source = tmp_path / "downloads" / "Creator" / "video-1.mp4"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"valid")
        plan = inspect(store, tmp_path)
        assert plan.next_action == RecoveryAction.REUSE_SOURCE
        assert plan.reusable_artifacts == (source,)


def test_downloaded_missing_source_plans_redownload(tmp_path, media_probe):
    with StateStore(tmp_path / "state.db") as store:
        register(store, VideoStatus.DOWNLOADED)
        plan = inspect(store, tmp_path)
        assert plan.next_action == RecoveryAction.REDOWNLOAD


def test_rendering_keeps_valid_master_and_removes_partial(tmp_path, media_probe):
    with StateStore(tmp_path / "state.db") as store:
        register(store, VideoStatus.RENDERING)
        source = tmp_path / "downloads" / "Creator" / "video-1.mp4"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"valid")
        masters = tmp_path / "creators" / "Creator" / "video-1"
        masters.mkdir(parents=True)
        valid = masters / "01.mp4"
        valid.write_bytes(b"valid")
        partial = masters / "02.part.mp4"
        partial.touch()
        plan = inspect(store, tmp_path)
        assert plan.next_action == RecoveryAction.RERENDER
        assert valid in plan.reusable_artifacts
        assert partial in plan.cleanup_actions
        prepare_recovery(store, plan)
        assert valid.exists()
        assert not partial.exists()
        assert store.get_video("video-1").status == VideoStatus.DOWNLOADED


def rendered_store(store, root):
    register(store, VideoStatus.RENDERING)
    masters = root / "creators" / "Creator" / "video-1"
    masters.mkdir(parents=True)
    paths = tuple(masters / f"{index:02d}.mp4" for index in range(1, 4))
    for path in paths:
        path.write_bytes(b"valid")
    store.record_rendered_shorts(
        "video-1",
        tuple(RenderedShort(index, path) for index, path in enumerate(paths, 1)),
    )
    return paths


def test_rendered_missing_master_does_not_queue(tmp_path, media_probe):
    with StateStore(tmp_path / "state.db") as store:
        paths = rendered_store(store, tmp_path)
        paths[1].unlink()
        plan = inspect(store, tmp_path)
        assert plan.next_action == RecoveryAction.INCONSISTENT
        assert store.get_video("video-1").status == VideoStatus.RENDERED


def test_restart_resumes_rendered_partial_distribution_and_is_idempotent(
    tmp_path, media_probe
):
    db = tmp_path / "state.db"
    with StateStore(db) as store:
        masters = rendered_store(store, tmp_path)
        partial = (
            tmp_path / "accounts" / "slot-1-Account_1" / "queue" / "video-1_01.mp4"
        )
        partial.parent.mkdir(parents=True)
        partial.write_bytes(b"stale")

    with StateStore(db) as reopened:
        plan = inspect(reopened, tmp_path)
        assert plan.next_action == RecoveryAction.DISTRIBUTE
        assert partial in plan.cleanup_actions
        queues = resume_rendered_distribution(
            reopened, plan, theme(), tmp_path / "accounts"
        )
        assert len(queues) == 3
        assert reopened.get_video("video-1").status == VideoStatus.QUEUED
        assert all(
            path.samefile(master) for path, master in zip(queues, masters, strict=True)
        )

        second = inspect(reopened, tmp_path)
        assert second.next_action == RecoveryAction.NOOP
        assert (
            resume_rendered_distribution(
                reopened, second, theme(), tmp_path / "accounts"
            )
            == queues
        )


@pytest.mark.parametrize("status", [VideoStatus.QUEUED, VideoStatus.COMPLETED])
def test_final_states_are_noop(tmp_path, media_probe, status):
    with StateStore(tmp_path / "state.db") as store:
        register(store, status)
        assert inspect(store, tmp_path).next_action == RecoveryAction.NOOP


def test_future_and_failed_states_are_explicit(tmp_path, media_probe):
    with StateStore(tmp_path / "state.db") as store:
        register(store, VideoStatus.ANALYZING)
        assert inspect(store, tmp_path).next_action == RecoveryAction.UNSUPPORTED
        store.connection.execute(
            "UPDATE videos SET status = 'FAILED' WHERE youtube_id = 'video-1'"
        )
        store.connection.commit()
        assert inspect(store, tmp_path).next_action == RecoveryAction.MANUAL_RETRY


def test_transcribing_valid_artifact_reconciles_after_restart(tmp_path, media_probe):
    db = tmp_path / "state.db"
    artifact = tmp_path / ".work" / "video-1" / "transcript.json"
    with StateStore(db) as store:
        register(store, VideoStatus.TRANSCRIBING)
        write_transcript_atomic(artifact, sample_transcript(), "video-1")
    with StateStore(db) as reopened:
        plan = inspect(reopened, tmp_path)
        assert plan.next_action == RecoveryAction.RECONCILE_TRANSCRIPT
        assert reconcile_transcript(reopened, plan) == sample_transcript()
        assert reopened.get_video("video-1").status == VideoStatus.TRANSCRIBED


def test_transcribing_invalid_artifact_retranscribes(tmp_path, media_probe):
    with StateStore(tmp_path / "state.db") as store:
        register(store, VideoStatus.TRANSCRIBING)
        source = tmp_path / "downloads" / "Creator" / "video-1.mp4"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"valid")
        artifact = tmp_path / ".work" / "video-1" / "transcript.json"
        artifact.parent.mkdir(parents=True)
        artifact.write_text("broken")
        plan = inspect(store, tmp_path)
        assert plan.next_action == RecoveryAction.RETRANSCRIBE
        prepare_recovery(store, plan)
        assert store.get_video("video-1").status == VideoStatus.DOWNLOADED
        assert not artifact.exists()


def test_transcribed_valid_artifact_is_reusable(tmp_path, media_probe):
    with StateStore(tmp_path / "state.db") as store:
        register(store, VideoStatus.TRANSCRIBING)
        artifact = tmp_path / ".work" / "video-1" / "transcript.json"
        write_transcript_atomic(artifact, sample_transcript(), "video-1")
        store.record_transcript(
            "video-1",
            file_path=artifact,
            provider="mock",
            model="test",
            language="pt",
        )
        plan = inspect(store, tmp_path)
        assert plan.next_action == RecoveryAction.REUSE_TRANSCRIPT
        assert plan.reusable_artifacts == (artifact,)


@pytest.mark.parametrize("contents", [None, "broken"])
def test_transcribed_missing_or_corrupt_artifact_requires_retranscription(
    tmp_path, media_probe, contents
):
    with StateStore(tmp_path / "state.db") as store:
        register(store, VideoStatus.TRANSCRIBING)
        source = tmp_path / "downloads" / "Creator" / "video-1.mp4"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"valid")
        artifact = tmp_path / ".work" / "video-1" / "transcript.json"
        if contents is not None:
            artifact.parent.mkdir(parents=True)
            artifact.write_text(contents)
        store.connection.execute(
            "UPDATE videos SET status = 'TRANSCRIBED' WHERE youtube_id = 'video-1'"
        )
        store.connection.commit()
        assert inspect(store, tmp_path).next_action == RecoveryAction.RETRANSCRIBE
