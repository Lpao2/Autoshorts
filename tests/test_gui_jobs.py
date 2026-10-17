from dataclasses import replace
from pathlib import Path
from queue import Empty
from threading import Event

import pytest
from test_render_integration import make_settings, synthetic_video  # noqa: F401

from autoshorts.gui_jobs import JobController, JobRequest, prepare_job


def request_for(source: Path, output: Path) -> JobRequest:
    return JobRequest(str(source), "[00:00 - 00:01] - First", str(output))


def test_validation_is_read_only(tmp_path, synthetic_video):  # noqa: F811
    settings = make_settings(tmp_path)
    _, _, _, plan = prepare_job(
        request_for(synthetic_video, settings.output_dir), settings
    )
    assert len(plan.clips) == 1
    assert not settings.output_dir.exists()


def test_validation_rejects_existing_and_source_overwrite(tmp_path, synthetic_video):  # noqa: F811
    settings = make_settings(tmp_path)
    source = synthetic_video.rename(tmp_path / "01_First.mp4")
    with pytest.raises(ValueError, match="overwrite the source"):
        prepare_job(request_for(source, tmp_path), settings)
    settings.output_dir.mkdir()
    (settings.output_dir / "01_First.mp4").touch()
    with pytest.raises(FileExistsError, match="Já existe"):
        prepare_job(request_for(source, settings.output_dir), settings)


def test_job_rejects_second_start_and_cancels_during_validation(tmp_path, monkeypatch):
    from autoshorts import gui_jobs

    started, release = Event(), Event()

    def prepare(*args):
        started.set()
        assert release.wait(5)
        return None, [], None, None

    monkeypatch.setattr(gui_jobs, "prepare_job", prepare)
    controller = JobController(make_settings(tmp_path))
    controller.start(JobRequest("source", "cuts", "out"), validate_only=True)
    try:
        assert started.wait(5)
        with pytest.raises(RuntimeError, match="ativo"):
            controller.start(JobRequest("source", "cuts", "out"))
        controller.cancel()
    finally:
        release.set()
        controller.thread.join(5)
    assert not controller.busy
    messages = []
    while True:
        try:
            messages.append(controller.messages.get_nowait()[0])
        except Empty:
            break
    assert messages == ["cancelled", "finished"]


def test_background_failure_is_reported_and_can_retry(tmp_path, synthetic_video):  # noqa: F811
    controller = JobController(replace(make_settings(tmp_path), max_workers=1))
    controller.start(JobRequest("missing", "bad cuts", "output"))
    controller.thread.join(5)
    assert controller.messages.get()[0] == "failed"
    assert controller.messages.get()[0] == "finished"
    controller.start(
        request_for(synthetic_video, tmp_path / "output"), validate_only=True
    )
    controller.thread.join(5)
    assert not controller.busy
    assert [controller.messages.get()[0] for _ in range(3)] == [
        "plan",
        "validated",
        "finished",
    ]
