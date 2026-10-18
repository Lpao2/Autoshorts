import importlib
import json
import sys
from threading import Event

import pytest

from autoshorts.automation_events import AutomationCancelled
from autoshorts.state_store import StateStore, VideoStatus
from autoshorts.transcription import (
    Transcript,
    TranscriptSegment,
    TranscriptWord,
    load_transcript,
    transcript_from_dict,
    transcript_to_dict,
    write_transcript_atomic,
)
from autoshorts.transcription_service import transcript_video


def sample_transcript():
    return Transcript(
        language="pt",
        language_probability=0.97,
        duration_seconds=10.0,
        text="olá mundo",
        segments=(
            TranscriptSegment(
                0.0,
                2.0,
                "olá mundo",
                (
                    TranscriptWord(0.0, 0.8, "olá"),
                    TranscriptWord(0.9, 1.8, "mundo"),
                ),
            ),
        ),
        provider="mock",
        model="test",
    )


def register_downloaded(store):
    store.register_video(
        youtube_id="video-1",
        source_url="url",
        creator_name="creator",
        theme_name="theme",
        title="title",
    )
    store.transition_video("video-1", VideoStatus.DOWNLOADING)
    store.transition_video("video-1", VideoStatus.DOWNLOADED)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: TranscriptWord(-1, 1, "word"),
        lambda: TranscriptWord(1, 1, "word"),
        lambda: TranscriptSegment(0, 1, "x", (TranscriptWord(0.5, 1.5, "x"),)),
        lambda: Transcript(
            "pt",
            2,
            1,
            "x",
            (TranscriptSegment(0, 1, "x"),),
            "p",
            "m",
        ),
    ],
)
def test_transcript_validation(factory):
    with pytest.raises(ValueError):
        factory()


def test_transcript_serialization_round_trip(tmp_path):
    transcript = sample_transcript()
    data = transcript_to_dict(transcript, "video-1")
    assert transcript_from_dict(data, youtube_id="video-1") == transcript
    path = write_transcript_atomic(tmp_path / "transcript.json", transcript, "video-1")
    assert load_transcript(path, youtube_id="video-1") == transcript
    assert not list(tmp_path.glob("*.tmp"))


def test_corrupt_or_mismatched_transcript_is_rejected(tmp_path):
    path = tmp_path / "transcript.json"
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid transcript JSON"):
        load_transcript(path)
    path.write_text(json.dumps(transcript_to_dict(sample_transcript(), "other")))
    with pytest.raises(ValueError, match="youtube_id"):
        load_transcript(path, youtube_id="video-1")


def test_transcription_lifecycle_persists_file_and_sqlite(tmp_path):
    class Provider:
        def transcribe(self, _source, *, cancel=None, progress_handler=None):
            progress_handler(0.5)
            return sample_transcript()

    source = tmp_path / "source.mp4"
    source.touch()
    artifact = tmp_path / ".work" / "video-1" / "transcript.json"
    events = []
    with StateStore(tmp_path / "state.db") as store:
        register_downloaded(store)
        result = transcript_video(
            youtube_id="video-1",
            source_path=source,
            artifact_path=artifact,
            provider=Provider(),
            state_store=store,
            creator="creator",
            title="title",
            url="url",
            event_handler=events.append,
        )
        assert result == sample_transcript()
        assert store.get_video("video-1").status == VideoStatus.TRANSCRIBED
        assert store.get_transcript("video-1").file_path == artifact
    assert [event.kind for event in events] == [
        "transcription_started",
        "transcription_progress",
        "transcription_finished",
    ]


def test_transcription_failure_does_not_promote(tmp_path):
    class Provider:
        def transcribe(self, *_args, **_kwargs):
            raise RuntimeError("failed")

    artifact = tmp_path / "transcript.json"
    with StateStore(tmp_path / "state.db") as store:
        register_downloaded(store)
        with pytest.raises(RuntimeError, match="failed"):
            transcript_video(
                youtube_id="video-1",
                source_path=tmp_path / "source.mp4",
                artifact_path=artifact,
                provider=Provider(),
                state_store=store,
                creator="c",
                title="t",
                url="u",
            )
        assert store.get_video("video-1").status == VideoStatus.TRANSCRIBING
        assert store.get_transcript("video-1") is None
        assert not artifact.exists()


def test_cancelled_provider_does_not_publish_partial(tmp_path):
    class Provider:
        def transcribe(self, *_args, **_kwargs):
            raise AutomationCancelled("cancelled")

    artifact = tmp_path / "transcript.json"
    artifact.write_text("partial")
    with StateStore(tmp_path / "state.db") as store:
        register_downloaded(store)
        with pytest.raises(AutomationCancelled):
            transcript_video(
                youtube_id="video-1",
                source_path=tmp_path / "source.mp4",
                artifact_path=artifact,
                provider=Provider(),
                state_store=store,
                creator="c",
                title="t",
                url="u",
                cancel=Event(),
            )
        assert store.get_video("video-1").status == VideoStatus.TRANSCRIBING
        assert not artifact.exists()


def test_optional_provider_module_imports_without_faster_whisper(monkeypatch):
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    module = importlib.import_module("autoshorts.faster_whisper_provider")
    provider = module.FasterWhisperProvider(model="small")
    with pytest.raises(RuntimeError, match="extra"):
        provider._load_engine()
