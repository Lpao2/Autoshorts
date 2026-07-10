"""Local video processing through FFmpeg."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Iterable

from autoshorts.config import Settings
from autoshorts.timestamp_parser import Clip, seconds_to_timestamp


def sanitize_filename(value: str) -> str:
    """Convert a clip title into a safe filename."""

    normalized = re.sub(
        r'[<>:"/\\|?*]+',
        "",
        value.strip(),
    )

    normalized = re.sub(r"\s+", "_", normalized)

    return normalized[:80] or "untitled_clip"


def build_vertical_filter(width: int, height: int) -> str:
    """
    Create a vertical layout with a blurred background.

    The complete original frame remains visible in the foreground.
    """

    return (
        "[0:v]split=2[background_source][foreground_source];"
        f"[background_source]"
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},"
        "boxblur=20:10[background];"
        f"[foreground_source]"
        f"scale={width}:{height}:force_original_aspect_ratio=decrease"
        "[foreground];"
        "[background][foreground]"
        "overlay=(W-w)/2:(H-h)/2,"
        "format=yuv420p[video]"
    )


def process_clip(
    input_video: str | Path,
    clip: Clip,
    settings: Settings,
    index: int,
) -> Path:
    """Cut and convert one selected clip into a vertical MP4."""

    source = Path(input_video)

    if not source.is_file():
        raise FileNotFoundError(f"Input video not found: {source}")

    settings.output_dir.mkdir(parents=True, exist_ok=True)

    duration = min(
        clip.duration_seconds + settings.extra_clip_seconds,
        settings.maximum_clip_duration,
    )

    filename = (
        f"{index:02d}_"
        f"{sanitize_filename(clip.title)}.mp4"
    )

    output_path = settings.output_dir / filename

    command = [
        settings.ffmpeg_path,
        "-y",
        "-ss",
        seconds_to_timestamp(clip.start_seconds),
        "-i",
        str(source),
        "-t",
        str(duration),
        "-filter_complex",
        build_vertical_filter(
            settings.output_width,
            settings.output_height,
        ),
        "-map",
        "[video]",
        "-map",
        "0:a?",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "20",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        str(output_path),
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
        error_message = result.stderr[-3000:]

        raise RuntimeError(
            "FFmpeg failed while processing "
            f"'{clip.title}'.\n\n{error_message}"
        )

    return output_path


def process_clips(
    input_video: str | Path,
    clips: Iterable[Clip],
    settings: Settings,
) -> list[Path]:
    """Process all selected clips sequentially."""

    outputs: list[Path] = []

    for index, clip in enumerate(clips, start=1):
        print(
            f"[{index}] Processing: {clip.title} "
            f"({clip.duration_seconds}s)"
        )

        output = process_clip(
            input_video=input_video,
            clip=clip,
            settings=settings,
            index=index,
        )

        outputs.append(output)
        print(f"    Created: {output}")

    return outputs
