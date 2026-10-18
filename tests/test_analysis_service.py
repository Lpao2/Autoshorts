import pytest
from test_transcript_analysis import selections, transcript

from autoshorts.analysis_service import analyze_transcript
from autoshorts.recovery import RecoveryAction, inspect_video_state
from autoshorts.state_store import StateStore, VideoStatus
from autoshorts.transcript_analysis import AnalysisResult, AnalysisUsage
from autoshorts.transcription import write_transcript_atomic


class Provider:
    provider_name = "gemini"
    model = "gemini-3.8-flash"

    def __init__(self):
        self.calls = 0

    def analyze(self, *_args, **_kwargs):
        self.calls += 1
        return AnalysisResult(
            selections(),
            self.provider_name,
            self.model,
            AnalysisUsage(1000, 100, 50, 1150, 1.2, 0.0013125),
        )


class BilledFailure(Exception):
    def __init__(self):
        super().__init__("invalid response")
        self.usage = AnalysisUsage(100, 10, 5, 115, 1, 0.00013125)


class FailingProvider(Provider):
    def analyze(self, *_args, **_kwargs):
        self.calls += 1
        raise BilledFailure


def prepared_store(tmp_path):
    store = StateStore(tmp_path / "state.db")
    store.register_video(
        youtube_id="video",
        source_url="url",
        creator_name="creator",
        theme_name="lol",
        title="title",
    )
    store.transition_video("video", VideoStatus.DOWNLOADING)
    store.transition_video("video", VideoStatus.DOWNLOADED)
    store.transition_video("video", VideoStatus.TRANSCRIBING)
    transcript_path = tmp_path / "lol" / ".work" / "video" / "transcript.json"
    write_transcript_atomic(transcript_path, transcript(), "video")
    store.record_transcript(
        "video",
        file_path=transcript_path,
        provider="faster-whisper",
        model="small",
        language="pt",
    )
    return store, transcript_path


def test_analysis_persists_usage_and_recovery_reuses_without_new_call(tmp_path):
    store, _ = prepared_store(tmp_path)
    provider = Provider()
    artifact = tmp_path / "lol" / ".work" / "video" / "analysis.json"
    resolved = analyze_transcript(
        youtube_id="video",
        transcript=transcript(),
        artifact_path=artifact,
        provider=provider,
        state_store=store,
        title="title",
        creator="creator",
        theme="lol",
        theme_prompt="humor",
    )
    assert resolved.result.selections[0].headline
    assert store.get_video("video").status == VideoStatus.ANALYZED
    assert store.monthly_ai_cost("gemini") == pytest.approx(0.0013125)
    plan = inspect_video_state(
        store, "video", theme_root=tmp_path / "lol", creator_directory="creator"
    )
    assert plan.next_action == RecoveryAction.REUSE_ANALYSIS
    assert provider.calls == 1
    store.close()


def test_monthly_budget_blocks_call(tmp_path, monkeypatch):
    store, _ = prepared_store(tmp_path)
    provider = Provider()
    monkeypatch.setenv("AUTOSHORTS_GEMINI_MONTHLY_BUDGET_USD", "0")
    with pytest.raises(RuntimeError, match="Limite mensal"):
        analyze_transcript(
            youtube_id="video",
            transcript=transcript(),
            artifact_path=tmp_path / "analysis.json",
            provider=provider,
            state_store=store,
            title="title",
            creator="creator",
            theme="lol",
            theme_prompt="",
        )
    assert provider.calls == 0
    assert store.get_video("video").status == VideoStatus.TRANSCRIBED
    store.close()


def test_billed_invalid_response_is_counted_once(tmp_path):
    store, _ = prepared_store(tmp_path)
    provider = FailingProvider()
    with pytest.raises(BilledFailure):
        analyze_transcript(
            youtube_id="video",
            transcript=transcript(),
            artifact_path=tmp_path / "analysis.json",
            provider=provider,
            state_store=store,
            title="title",
            creator="creator",
            theme="lol",
            theme_prompt="",
        )
    assert provider.calls == 1
    assert store.monthly_ai_cost("gemini") == pytest.approx(0.00013125)
    store.close()
