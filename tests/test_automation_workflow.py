from datetime import date

from test_render_integration import make_settings

from autoshorts.ai_analysis import ClipAnalysis
from autoshorts.automation_config import AutomationConfig, Creator, ThemeProfile
from autoshorts.automation_events import AutomationCancelled
from autoshorts.automation_jobs import AutomationController
from autoshorts.automation_pipeline import AutomationRequest, run_automation
from autoshorts.collection import (
    CandidateSkip,
    VideoCandidate,
    YouTubeAccess,
    download_progress_from_hook,
)
from autoshorts.timestamp_parser import Clip


def candidate(title="Video"):
    return VideoCandidate(
        "Creator", "https://example.com/watch?v=1", title, date.today(), 10
    )


def config():
    return AutomationConfig(
        (
            ThemeProfile(
                "tech",
                "prompt",
                (Creator("Creator", "https://example.com/videos"),),
                "tech",
            ),
        )
    )


def test_download_progress_uses_exact_or_estimated_total():
    exact = download_progress_from_hook(
        {
            "status": "downloading",
            "downloaded_bytes": 25,
            "total_bytes": 100,
            "speed": 5,
            "eta": 15,
        }
    )
    assert exact.percent == 0.25
    assert not exact.total_is_estimate
    estimated = download_progress_from_hook(
        {"status": "downloading", "downloaded_bytes": 10, "total_bytes_estimate": 40}
    )
    assert estimated.percent == 0.25
    assert estimated.total_is_estimate
    unknown = download_progress_from_hook(
        {"status": "downloading", "downloaded_bytes": 10}
    )
    assert unknown.percent is None


def test_download_only_emits_lifecycle_without_network(tmp_path, monkeypatch):
    from autoshorts import automation_pipeline

    source = tmp_path / "video.mp4"
    source.touch()
    monkeypatch.setattr(
        automation_pipeline,
        "discover_creator_videos",
        lambda *args, **kwargs: [candidate()],
    )
    monkeypatch.setattr(
        automation_pipeline, "download_video", lambda *args, **kwargs: source
    )
    events = []
    result = run_automation(
        AutomationRequest("tech", 1, 30, tmp_path, download_only=True),
        config(),
        make_settings(tmp_path),
        event_handler=events.append,
    )
    assert result[0].source_path == source
    kinds = [event.kind for event in events]
    assert kinds[0] == "automation_started"
    assert "discovery_started" in kinds
    assert "download_finished" in kinds
    assert kinds[-1] == "automation_completed"


def test_fake_provider_path_delegates_to_existing_pipeline(tmp_path, monkeypatch):
    from autoshorts import automation_pipeline

    source = tmp_path / "video.mp4"
    source.touch()
    render_sentinel = object()
    monkeypatch.setattr(
        automation_pipeline,
        "discover_creator_videos",
        lambda *args, **kwargs: [candidate()],
    )
    monkeypatch.setattr(
        automation_pipeline, "download_video", lambda *args, **kwargs: source
    )
    monkeypatch.setattr(
        automation_pipeline, "run_pipeline", lambda *args, **kwargs: render_sentinel
    )
    result = run_automation(
        AutomationRequest("tech", 1, 30, tmp_path),
        config(),
        make_settings(tmp_path),
        analyze=lambda *args: ClipAnalysis((Clip(0, 1, "Fake"),), "FAKE"),
    )
    assert result[0].render is render_sentinel


def test_partial_failure_continues_with_next_candidate(tmp_path, monkeypatch):
    from autoshorts import automation_pipeline

    items = [candidate("Bad"), candidate("Good")]
    monkeypatch.setattr(
        automation_pipeline, "discover_creator_videos", lambda *args, **kwargs: items
    )

    def download(item, *args, **kwargs):
        if item.title == "Bad":
            raise RuntimeError("download failed")
        path = tmp_path / "good.mp4"
        path.touch()
        return path

    monkeypatch.setattr(automation_pipeline, "download_video", download)
    result = run_automation(
        AutomationRequest("tech", 2, 30, tmp_path, download_only=True),
        config(),
        make_settings(tmp_path),
    )
    assert result[0].error == "download failed"
    assert result[1].error is None


def test_controller_reports_cancelled_without_touching_tk(tmp_path, monkeypatch):
    from autoshorts import automation_jobs

    def cancelled(*args, **kwargs):
        raise AutomationCancelled("cancelled")

    monkeypatch.setattr(automation_jobs, "run_automation", cancelled)
    controller = AutomationController(make_settings(tmp_path))
    controller.start(AutomationRequest("tech", 1, 1, tmp_path, True), config())
    controller.thread.join(5)
    assert [controller.messages.get()[0], controller.messages.get()[0]] == [
        "cancelled",
        "finished",
    ]


def test_pipeline_emits_candidate_skipped_and_passes_auth(tmp_path, monkeypatch):
    from autoshorts import automation_pipeline

    source = tmp_path / "video.mp4"
    source.touch()
    received = {}

    def discover(*args, **kwargs):
        received["discovery_access"] = kwargs["access"]
        kwargs["skip_handler"](
            CandidateSkip(
                "Creator",
                "Blocked",
                "https://example.com/blocked",
                "requires_authentication",
                "technical detail",
            )
        )
        return [candidate()]

    def download(*args, **kwargs):
        received["download_access"] = kwargs["access"]
        return source

    monkeypatch.setattr(automation_pipeline, "discover_creator_videos", discover)
    monkeypatch.setattr(automation_pipeline, "download_video", download)
    events = []
    access = YouTubeAccess("brave")
    run_automation(
        AutomationRequest(
            "tech", 1, 30, tmp_path, download_only=True, youtube_access=access
        ),
        config(),
        make_settings(tmp_path),
        event_handler=events.append,
    )
    skipped = next(event for event in events if event.kind == "candidate_skipped")
    assert skipped.message == "requires_authentication"
    assert skipped.data["technical_detail"] == "technical detail"
    assert received == {"discovery_access": access, "download_access": access}


def test_gui_request_passes_selected_youtube_browser(tmp_path):
    from autoshorts.gui import AutoShortsWindow

    class Variable:
        def __init__(self, value):
            self.value = value

        def get(self):
            return self.value

    class Creators:
        def curselection(self):
            return (0,)

        def get(self, _index):
            return "Creator"

    window = object.__new__(AutoShortsWindow)
    window.auto_creators = Creators()
    window.auto_workspace = Variable(str(tmp_path))
    window.auto_quantity = Variable(1)
    window.auto_age = Variable(30)
    window.auto_theme = Variable("tech")
    window.auto_download_only = Variable(True)
    window.auto_youtube_auth = Variable("Brave")
    request = window._automation_request()
    assert request.youtube_access == YouTubeAccess("brave")
