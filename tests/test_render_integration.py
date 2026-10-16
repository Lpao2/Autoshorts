import json
import shutil
import subprocess
from pathlib import Path

import pytest

from autoshorts.config import Settings
from autoshorts.pipeline import PipelineExecutionError, run_pipeline
from autoshorts.timestamp_parser import Clip


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        ffmpeg_path="ffmpeg",
        ffprobe_path="ffprobe",
        input_dir=tmp_path,
        output_dir=tmp_path / "output",
        output_width=108,
        output_height=192,
        maximum_clip_duration=2,
        extra_clip_seconds=0,
        max_workers=2,
        video_codec="libx264",
        audio_codec="aac",
        video_preset="ultrafast",
        video_crf=28,
        audio_bitrate="96k",
        overwrite_policy="error",
    )


@pytest.fixture
def synthetic_video(tmp_path: Path) -> Path:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("FFmpeg and FFprobe are required")

    source = tmp_path / "source.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=24",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000",
            "-t",
            "3",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(source),
        ],
        check=True,
    )
    return source


def test_pipeline_renders_validated_outputs_and_manifest(
    tmp_path: Path, synthetic_video: Path
) -> None:
    settings = make_settings(tmp_path)
    clips = [Clip(0, 1, "First"), Clip(1, 2, "Second")]

    result = run_pipeline(synthetic_video, clips, settings)

    assert [item.status for item in result.renders] == ["generated", "generated"]
    assert all(item.output_path.is_file() for item in result.renders)
    assert not list(settings.output_dir.glob("*.part.*"))

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "completed"
    assert [item["status"] for item in manifest["clips"]] == [
        "generated",
        "generated",
    ]


def test_dry_run_probes_and_writes_plan_without_video_outputs(
    tmp_path: Path, synthetic_video: Path
) -> None:
    settings = make_settings(tmp_path)

    result = run_pipeline(
        synthetic_video, [Clip(0, 1, "Planned")], settings, dry_run=True
    )

    assert not result.renders
    assert result.manifest_path.is_file()
    assert not list(settings.output_dir.glob("*.mp4"))


def test_plan_rejects_clip_outside_source(
    tmp_path: Path, synthetic_video: Path
) -> None:
    with pytest.raises(ValueError, match="after the source ends"):
        run_pipeline(
            synthetic_video,
            [Clip(10, 12, "Outside")],
            make_settings(tmp_path),
            dry_run=True,
        )


def test_render_failure_persists_diagnostic_manifest(
    tmp_path: Path, synthetic_video: Path
) -> None:
    settings = make_settings(tmp_path)
    clips = [Clip(0, 1, "Existing")]
    run_pipeline(synthetic_video, clips, settings)

    with pytest.raises(PipelineExecutionError) as captured:
        run_pipeline(synthetic_video, clips, settings)

    manifest = json.loads(captured.value.manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert "Output already exists" in manifest["error"]
