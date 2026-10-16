"""Local video processing through FFmpeg."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Iterable
from pathlib import Path

from autoshorts.config import Settings
from autoshorts.timestamp_parser import Clip, seconds_to_timestamp


class OutputExistsError(FileExistsError):
    """Raised when an output exists and replacement is disabled."""


def sanitize_filename(value: str) -> str:
    """Convert a clip title into a safe, bounded filename component."""

    normalized = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "", value.strip())
    normalized = re.sub(r"\s+", "_", normalized).strip(" ._")
    return normalized[:80].rstrip(" .") or "untitled_clip"


def build_vertical_filter(width: int, height: int) -> str:
    """Create a vertical layout with a blurred background."""

    if width <= 0 or height <= 0 or width % 2 or height % 2:
        raise ValueError("Output dimensions must be positive even integers.")

    return (
        "[0:v]split=2[background_source][foreground_source];"
        f"[background_source]scale={width}:{height}:"
        "force_original_aspect_ratio=increase,"
        f"crop={width}:{height},boxblur=20:10[background];"
        f"[foreground_source]scale={width}:{height}:"
        "force_original_aspect_ratio=decrease[foreground];"
        "[background][foreground]overlay=(W-w)/2:(H-h)/2,"
        "format=yuv420p[video]"
    )


def _output_path(clip: Clip, settings: Settings, index: int) -> Path:
    filename = f"{index:02d}_{sanitize_filename(clip.title)}.mp4"
    return settings.output_dir / filename


def process_clip(
    input_video: str | Path,
    clip: Clip,
    settings: Settings,
    index: int,
) -> Path:
    """Cut and convert one selected clip into a vertical MP4 atomically."""

    source = Path(input_video)
    if not source.is_file():
        raise FileNotFoundError(f"Input video not found: {source}")

    settings.output_dir.mkdir(parents=True, exist_ok=True)
    output_path = _output_path(clip, settings, index)

    if output_path.exists():
        if settings.overwrite_policy == "skip":
            return output_path
        if settings.overwrite_policy == "error":
            raise OutputExistsError(f"Output already exists: {output_path}")

    temporary_path = output_path.with_suffix(".part.mp4")
    if temporary_path.exists():
        temporary_path.unlink()

    duration = min(
        clip.duration_seconds + settings.extra_clip_seconds,
        settings.maximum_clip_duration,
    )

    command = [
        settings.ffmpeg_path,
        "-hide_banner",
        "-nostdin",
        "-y",
        "-ss",
        seconds_to_timestamp(clip.start_seconds),
        "-i",
        str(source),
        "-t",
        str(duration),
        "-filter_complex",
        build_vertical_filter(settings.output_width, settings.output_height),
        "-map",
        "[video]",
        "-map",
        "0:a?",
        "-c:v",
        settings.video_codec,
        "-preset",
        settings.video_preset,
        "-crf",
        str(settings.video_crf),
        "-c:a",
        settings.audio_codec,
        "-b:a",
        settings.audio_bitrate,
        "-movflags",
        "+faststart",
        str(temporary_path),
    ]

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    if result.returncode != 0:
        temporary_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"FFmpeg failed while processing '{clip.title}'.\n\n{result.stderr[-3000:]}"
        )

    if not temporary_path.is_file() or temporary_path.stat().st_size == 0:
        temporary_path.unlink(missing_ok=True)
        raise RuntimeError(f"FFmpeg created no usable output for '{clip.title}'.")

    temporary_path.replace(output_path)
    return output_path


def process_clips(
    input_video: str | Path,
    clips: Iterable[Clip],
    settings: Settings,
) -> list[Path]:
    """Process all selected clips sequentially."""

    outputs: list[Path] = []
    for index, clip in enumerate(clips, start=1):
        print(f"[{index}] Processing: {clip.title} ({clip.duration_seconds}s)")
        output = process_clip(input_video, clip, settings, index)
        outputs.append(output)
        print(f"    Ready: {output}")
    return outputs
