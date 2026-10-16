import shutil
import subprocess
from pathlib import Path

import pytest

from autoshorts.config import Settings
from autoshorts.timestamp_parser import Clip
from autoshorts.video_processor import process_clip


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is unavailable")
def test_process_clip_generates_atomic_vertical_output(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    output_dir = tmp_path / "output"

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
            "2",
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

    settings = Settings(
        ffmpeg_path="ffmpeg",
        ffprobe_path="ffprobe",
        input_dir=tmp_path,
        output_dir=output_dir,
        output_width=108,
        output_height=192,
        maximum_clip_duration=2,
        extra_clip_seconds=0,
        max_workers=1,
        video_codec="libx264",
        audio_codec="aac",
        video_preset="ultrafast",
        video_crf=28,
        audio_bitrate="96k",
        overwrite_policy="error",
    )

    output = process_clip(source, Clip(0, 1, "Integration clip"), settings, 1)

    assert output.is_file()
    assert output.stat().st_size > 0
    assert not output.with_suffix(".part.mp4").exists()
