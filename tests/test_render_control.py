import json
import os
import sys
from dataclasses import replace
from threading import Event, Timer

import pytest
from test_render_integration import make_settings, synthetic_video  # noqa: F401

from autoshorts.pipeline import PipelineExecutionError, run_pipeline
from autoshorts.render_events import RenderCancelled
from autoshorts.timestamp_parser import Clip
from autoshorts.video_processor import _run_ffmpeg


def test_silent_subprocess_cancellation_reaps_child(tmp_path):
    cancel = Event()
    pid_file = tmp_path / "pid"
    command = [
        sys.executable,
        "-c",
        "import os,sys,time; "
        "open(sys.argv[1], 'w').write(str(os.getpid())); time.sleep(30)",
        str(pid_file),
    ]
    timer = Timer(0.5, cancel.set)
    timer.start()
    try:
        with pytest.raises(RenderCancelled):
            _run_ffmpeg(command, 30, cancel, lambda value: None)
    finally:
        timer.cancel()
    assert pid_file.exists()
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)


def test_cancel_during_real_ffmpeg_cleans_partial_and_writes_manifest(
    tmp_path,
    synthetic_video,  # noqa: F811
):
    cancel = Event()
    events = []

    def progress(event):
        events.append(event)
        if event.status == "rendering" and event.progress > 0:
            cancel.set()

    settings = make_settings(tmp_path)
    with pytest.raises(RenderCancelled) as captured:
        run_pipeline(
            synthetic_video,
            [Clip(0, 2, "First"), Clip(0, 2, "Second")],
            settings,
            cancel=cancel,
            on_progress=progress,
        )
    manifest = json.loads(captured.value.manifest_path.read_text())
    assert manifest["status"] == "cancelled"
    assert not list(settings.output_dir.glob("*.mp4"))
    assert not list(settings.output_dir.glob("*.part.*"))
    assert any(0 < e.progress < 1 for e in events)


def test_cancel_preserves_completed_outputs_in_manifest(tmp_path, synthetic_video):  # noqa: F811
    cancel = Event()
    settings = replace(make_settings(tmp_path), max_workers=1)

    def progress(event):
        if event.index == 1 and event.status == "generated":
            cancel.set()

    with pytest.raises(RenderCancelled) as captured:
        run_pipeline(
            synthetic_video,
            [Clip(0, 1, "First"), Clip(1, 2, "Second")],
            settings,
            cancel=cancel,
            on_progress=progress,
        )
    manifest = json.loads(captured.value.manifest_path.read_text())
    assert manifest["status"] == "cancelled"
    assert [c["status"] for c in manifest["clips"]] == ["generated", "cancelled"]
    assert (settings.output_dir / "01_First.mp4").is_file()
    assert not (settings.output_dir / "02_Second.mp4").exists()


def test_failure_preserves_partial_success_and_cleans_temporary(
    tmp_path,
    synthetic_video,  # noqa: F811
):
    settings = replace(make_settings(tmp_path), max_workers=1)
    settings.output_dir.mkdir()
    (settings.output_dir / "02_Second.mp4").write_bytes(b"original")
    with pytest.raises(PipelineExecutionError) as captured:
        run_pipeline(
            synthetic_video, [Clip(0, 1, "First"), Clip(1, 2, "Second")], settings
        )
    manifest = json.loads(captured.value.manifest_path.read_text())
    assert manifest["status"] == "failed"
    assert [c["status"] for c in manifest["clips"]] == ["generated", "not_completed"]
    assert (settings.output_dir / "02_Second.mp4").read_bytes() == b"original"
    assert not list(settings.output_dir.glob("*.part.*"))


def test_reuse_and_replace_still_validate_outputs(tmp_path, synthetic_video):  # noqa: F811
    settings = make_settings(tmp_path)
    clips = [Clip(0, 1, "First")]
    first = run_pipeline(synthetic_video, clips, settings).renders[0].output_path
    original = first.stat().st_mtime_ns
    reused = run_pipeline(
        synthetic_video, clips, replace(settings, overwrite_policy="skip")
    )
    assert reused.renders[0].status == "skipped"
    assert first.stat().st_mtime_ns == original
    first.write_bytes(b"invalid")
    with pytest.raises(PipelineExecutionError):
        run_pipeline(synthetic_video, clips, replace(settings, overwrite_policy="skip"))
    result = run_pipeline(
        synthetic_video, clips, replace(settings, overwrite_policy="replace")
    )
    assert result.renders[0].status == "generated"
    assert first.stat().st_size > 100


def test_failed_replacement_preserves_previous_file(tmp_path, synthetic_video):  # noqa: F811
    settings = make_settings(tmp_path)
    clips = [Clip(0, 1, "First")]
    output = run_pipeline(synthetic_video, clips, settings).renders[0].output_path
    previous = output.read_bytes()
    with pytest.raises(PipelineExecutionError, match="FFmpeg failed"):
        run_pipeline(
            synthetic_video,
            clips,
            replace(
                settings,
                overwrite_policy="replace",
                video_codec="invalid_encoder",
            ),
        )
    assert output.read_bytes() == previous
    assert not list(settings.output_dir.glob("*.part.*"))
