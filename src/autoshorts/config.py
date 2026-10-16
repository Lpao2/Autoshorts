"""Application configuration loaded from environment variables."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OverwritePolicy = Literal["error", "skip", "replace"]

load_dotenv(PROJECT_ROOT / ".env")


def _read_integer(
    name: str,
    default: int,
    *,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    raw_value = os.getenv(name)

    try:
        value = default if raw_value is None else int(raw_value)
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be an integer.") from exc

    if value < minimum or maximum is not None and value > maximum:
        expected = f">= {minimum}"
        if maximum is not None:
            expected += f" and <= {maximum}"
        raise ValueError(f"Environment variable {name} must be {expected}.")

    return value


def _read_path(name: str, default: str) -> Path:
    path = Path(os.getenv(name, default)).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def _read_overwrite_policy() -> OverwritePolicy:
    value = os.getenv("OVERWRITE_POLICY", "error").strip().lower()
    if value not in {"error", "skip", "replace"}:
        raise ValueError(
            "Environment variable OVERWRITE_POLICY must be error, skip, or replace."
        )
    return cast(OverwritePolicy, value)


@dataclass(frozen=True, slots=True)
class Settings:
    """Configuration used by the local processing pipeline."""

    ffmpeg_path: str
    ffprobe_path: str
    input_dir: Path
    output_dir: Path
    output_width: int
    output_height: int
    maximum_clip_duration: int
    extra_clip_seconds: int
    max_workers: int
    video_codec: str
    audio_codec: str
    video_preset: str
    video_crf: int
    audio_bitrate: str
    overwrite_policy: OverwritePolicy


def load_settings() -> Settings:
    """Load and validate pipeline settings from environment variables."""

    ffmpeg_path = os.getenv("FFMPEG_PATH") or shutil.which("ffmpeg") or "ffmpeg"
    ffprobe_path = os.getenv("FFPROBE_PATH") or shutil.which("ffprobe") or "ffprobe"

    return Settings(
        ffmpeg_path=ffmpeg_path,
        ffprobe_path=ffprobe_path,
        input_dir=_read_path("INPUT_DIR", "./data/input"),
        output_dir=_read_path("OUTPUT_DIR", "./data/output"),
        output_width=_read_integer("OUTPUT_WIDTH", 1080, minimum=2),
        output_height=_read_integer("OUTPUT_HEIGHT", 1920, minimum=2),
        maximum_clip_duration=_read_integer("MAX_CLIP_DURATION", 60, minimum=1),
        extra_clip_seconds=_read_integer("EXTRA_CLIP_SECONDS", 6, minimum=0),
        max_workers=_read_integer("MAX_WORKERS", 1, minimum=1, maximum=32),
        video_codec=os.getenv("VIDEO_CODEC", "libx264").strip() or "libx264",
        audio_codec=os.getenv("AUDIO_CODEC", "aac").strip() or "aac",
        video_preset=os.getenv("VIDEO_PRESET", "medium").strip() or "medium",
        video_crf=_read_integer("VIDEO_CRF", 20, minimum=0, maximum=51),
        audio_bitrate=os.getenv("AUDIO_BITRATE", "192k").strip() or "192k",
        overwrite_policy=_read_overwrite_policy(),
    )
