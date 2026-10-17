"""Media inspection and output validation through FFprobe."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path


class MediaProbeError(RuntimeError):
    """Raised when FFprobe cannot inspect a media file."""


@dataclass(frozen=True, slots=True)
class MediaInfo:
    """Technical facts needed to validate a render plan."""

    path: Path
    duration_seconds: float
    width: int
    height: int
    frame_rate: float | None
    has_audio: bool
    rotation: int


def _parse_frame_rate(value: str | None) -> float | None:
    if not value or value == "0/0":
        return None
    try:
        return float(Fraction(value))
    except (ValueError, ZeroDivisionError):
        return None


def probe_media(path: str | Path, ffprobe_path: str = "ffprobe") -> MediaInfo:
    """Inspect one local media file and return normalized information."""

    media_path = Path(path)
    if not media_path.is_file():
        raise FileNotFoundError(f"Media file not found: {media_path}")

    command = [
        ffprobe_path,
        "-v",
        "error",
        "-show_entries",
        "format=duration:stream=index,codec_type,width,height,avg_frame_rate:"
        "stream_tags=rotate:stream_side_data=rotation",
        "-of",
        "json",
        str(media_path),
    ]

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise MediaProbeError(
            "FFprobe exceeded its 30-second inspection limit."
        ) from exc
    except FileNotFoundError as exc:
        raise MediaProbeError(f"FFprobe executable not found: {ffprobe_path}") from exc

    if result.returncode != 0:
        raise MediaProbeError(
            f"FFprobe failed for '{media_path}'.\n\n{result.stderr[-2000:]}"
        )

    try:
        payload = json.loads(result.stdout)
        duration = float(payload["format"]["duration"])
        streams = payload.get("streams", [])
        video = next(stream for stream in streams if stream["codec_type"] == "video")
    except (
        KeyError,
        StopIteration,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        raise MediaProbeError(f"Incomplete FFprobe data for '{media_path}'.") from exc

    rotation = int(video.get("tags", {}).get("rotate", 0))
    for item in video.get("side_data_list", []):
        if "rotation" in item:
            rotation = int(item["rotation"])
            break

    return MediaInfo(
        path=media_path.resolve(),
        duration_seconds=duration,
        width=int(video["width"]),
        height=int(video["height"]),
        frame_rate=_parse_frame_rate(video.get("avg_frame_rate")),
        has_audio=any(stream.get("codec_type") == "audio" for stream in streams),
        rotation=rotation % 360,
    )


def validate_rendered_media(
    path: str | Path,
    *,
    ffprobe_path: str,
    expected_width: int,
    expected_height: int,
    expected_duration: float,
) -> MediaInfo:
    """Ensure a rendered file has the expected stream and dimensions."""

    info = probe_media(path, ffprobe_path)
    if (info.width, info.height) != (expected_width, expected_height):
        raise MediaProbeError(
            f"Unexpected output dimensions for '{path}': "
            f"{info.width}x{info.height}; expected {expected_width}x{expected_height}."
        )
    if info.duration_seconds <= 0:
        raise MediaProbeError(f"Rendered output has no duration: {path}")
    if abs(info.duration_seconds - expected_duration) > 1.0:
        raise MediaProbeError(
            f"Unexpected output duration for '{path}': {info.duration_seconds:.2f}s; "
            f"expected approximately {expected_duration:.2f}s."
        )
    return info
