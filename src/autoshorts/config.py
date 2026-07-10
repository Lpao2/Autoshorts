"""Application configuration loaded from environment variables."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]

load_dotenv(PROJECT_ROOT / ".env")


def _read_integer(name: str, default: int) -> int:
    value = os.getenv(name)

    if value is None:
        return default

    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(
            f"Environment variable {name} must be an integer."
        ) from exc


@dataclass(frozen=True, slots=True)
class Settings:
    """Configuration used by the local processing pipeline."""

    ffmpeg_path: str
    input_dir: Path
    output_dir: Path
    output_width: int
    output_height: int
    maximum_clip_duration: int
    extra_clip_seconds: int


def load_settings() -> Settings:
    """Load pipeline settings from environment variables."""

    ffmpeg_path = (
        os.getenv("FFMPEG_PATH")
        or shutil.which("ffmpeg")
        or "ffmpeg"
    )

    input_dir = Path(
        os.getenv("INPUT_DIR", "./data/input")
    ).expanduser()

    output_dir = Path(
        os.getenv("OUTPUT_DIR", "./data/output")
    ).expanduser()

    return Settings(
        ffmpeg_path=ffmpeg_path,
        input_dir=input_dir,
        output_dir=output_dir,
        output_width=_read_integer("OUTPUT_WIDTH", 1080),
        output_height=_read_integer("OUTPUT_HEIGHT", 1920),
        maximum_clip_duration=_read_integer(
            "MAX_CLIP_DURATION",
            60,
        ),
        extra_clip_seconds=_read_integer(
            "EXTRA_CLIP_SECONDS",
            6,
        ),
    )
