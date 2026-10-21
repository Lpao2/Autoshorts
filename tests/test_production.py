import json
from types import SimpleNamespace

from autoshorts.automation_config import ThemeProfile
from autoshorts.config import Settings
from autoshorts.production import (
    ProductionResult,
    SegmentSelection,
    produce_source_video,
    run_single_production,
)
from autoshorts.state_store import RenderedShort, StateStore, VideoStatus
from autoshorts.transcript_analysis import AnalysisResult, ClipSelection
from autoshorts.transcription import Transcript, TranscriptSegment


class FakeTranscriber:
    calls = 0

    def transcribe(self, source_path, *, cancel=None, progress_handler=None):
        self.calls += 1
        segments = tuple(
            TranscriptSegment(index * 10, (index + 1) * 10, f"segment {index}")
            for index in range(15)
        )
        return Transcript("pt", 1.0, 150, "text", segments, "fake", "fake-v1")


class FakeAnalyzer:
    provider_name = "fake"
    model = "fake-v1"
    calls = 0

    def prompt_hash(self, theme_prompt):
        return "prompt-hash"

    def analyze(self, transcript, **kwargs):
        self.calls += 1
        candidates = tuple(
            ClipSelection(
                index * 3,
                index * 3 + 2,
                f"title {index}",
                f"headline {index}",
                f"reason {index}",
                0.95 - index * 0.02,
            )
            for index in range(5)
        )
        return AnalysisResult((), "fake", self.model, candidates=candidates)


def settings(tmp_path):
    return Settings(
        "ffmpeg",
        "ffprobe",
        tmp_path,
        tmp_path,
        1080,
        1920,
        60,
        0,
        1,
        "libx264",
        "aac",
        "medium",
        20,
        "192k",
        "error",
    )


def downloaded_video(store, tmp_path):
    source = tmp_path / "source.mp4"
    source.touch()
    store.register_video(
        youtube_id="source-1",
        source_url="https://youtube.test/source-1",
        creator_name="Creator",
        theme_name="lol",
        title="Long video",
    )
    store.transition_video("source-1", VideoStatus.DOWNLOADING)
    store.record_downloaded_video(
        "source-1", local_path=source, checksum="abc", duration_seconds=150
    )


def test_dry_run_persists_and_reuses_transcript_analysis_and_winner(tmp_path):
    transcriber = FakeTranscriber()
    analyzer = FakeAnalyzer()
    theme = ThemeProfile("lol", "gaming", (), "lol")
    with StateStore(tmp_path / "state.db") as store:
        downloaded_video(store, tmp_path)
        first = run_single_production(
            youtube_id="source-1",
            state=store,
            theme=theme,
            settings=settings(tmp_path),
            transcription_provider=transcriber,
            analysis_provider=analyzer,
            workspace=tmp_path / "automation",
            dry_run=True,
        )
        second = run_single_production(
            youtube_id="source-1",
            state=store,
            theme=theme,
            settings=settings(tmp_path),
            transcription_provider=transcriber,
            analysis_provider=analyzer,
            workspace=tmp_path / "automation",
            dry_run=True,
        )
        assert first.selection == second.selection
        assert first.selection.duration_seconds == 30
        assert first.selection.reason == "reason 0"
        assert transcriber.calls == analyzer.calls == 1
        assert store.get_video("source-1").status == VideoStatus.SEGMENT_SELECTED
        assert store.shorts_for_video("source-1") == ()


def test_records_exactly_one_short_atomically(tmp_path):
    with StateStore(tmp_path / "state.db") as store:
        downloaded_video(store, tmp_path)
        store.transition_video("source-1", VideoStatus.RENDERING)
        output = tmp_path / "01.mp4"
        output.touch()
        row = store.record_single_rendered_short(
            "source-1", RenderedShort(1, output, duration_seconds=30)
        )
        assert row.clip_index == 1
        assert len(store.shorts_for_video("source-1")) == 1
        assert store.get_video("source-1").status == VideoStatus.RENDERED


def test_consolidated_pipeline_persists_and_reuses_ready_artifact(
    tmp_path, monkeypatch
):
    source = tmp_path / "base.mp4"
    source.touch()
    transcript_path = tmp_path / "transcript.json"
    transcript_path.write_text("{}", encoding="utf-8")
    final = tmp_path / "captioned.mp4"
    subtitle = tmp_path / "captioned.ass"
    manifest = tmp_path / "caption.json"
    for path in (final, subtitle):
        path.touch()
    manifest.write_text(json.dumps({"fingerprint": "caption-v1"}), encoding="utf-8")
    selected = SegmentSelection(
        "source-1",
        10,
        40,
        30,
        "title",
        "headline",
        "reason",
        0.9,
        "now",
        "test",
        "v1",
        "context",
    )
    base_result = ProductionResult(
        selected, source, tmp_path / "production.json", None, "base", False, True
    )
    caption_result = SimpleNamespace(
        video_path=final,
        subtitle_path=subtitle,
        manifest_path=manifest,
        checksum="final-checksum",
        reused=True,
    )
    monkeypatch.setattr(
        "autoshorts.production.run_single_production",
        lambda **kwargs: base_result,
    )
    monkeypatch.setattr(
        "autoshorts.captions.render_captioned_short",
        lambda **kwargs: caption_result,
    )
    monkeypatch.setattr(
        "autoshorts.production.load_transcript", lambda *args, **kwargs: object()
    )
    with StateStore(tmp_path / "state.db") as store:
        downloaded_video(store, tmp_path)
        store.connection.execute(
            "INSERT INTO transcripts VALUES (?, ?, ?, ?, ?, ?)",
            ("source-1", str(transcript_path), "fake", "v1", "pt", "now"),
        )
        store.connection.execute(
            "UPDATE videos SET status = ? WHERE youtube_id = ?",
            (VideoStatus.RENDERED, "source-1"),
        )
        first = produce_source_video(
            youtube_id="source-1",
            state=store,
            theme=ThemeProfile("lol", "", (), "lol"),
            settings=settings(tmp_path),
            transcription_provider=FakeTranscriber(),
            analysis_provider=FakeAnalyzer(),
            workspace=tmp_path,
        )
        second = produce_source_video(
            youtube_id="source-1",
            state=store,
            theme=ThemeProfile("lol", "", (), "lol"),
            settings=settings(tmp_path),
            transcription_provider=FakeTranscriber(),
            analysis_provider=FakeAnalyzer(),
            workspace=tmp_path,
        )
        assert not first.reused
        assert second.reused
        assert store.get_video("source-1").status == VideoStatus.READY
        assert store.get_ready_artifact("source-1").file_path == final
