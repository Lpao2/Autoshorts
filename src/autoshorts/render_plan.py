"""Validated render-plan construction."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from autoshorts.config import Settings
from autoshorts.media_probe import MediaInfo
from autoshorts.naming import sanitize_filename
from autoshorts.timestamp_parser import Clip


@dataclass(frozen=True, slots=True)
class PlannedClip:
    index: int
    clip: Clip
    duration_seconds: float
    output_path: Path


@dataclass(frozen=True, slots=True)
class RenderPlan:
    source: MediaInfo
    clips: tuple[PlannedClip, ...]


def build_render_plan(
    source: MediaInfo,
    clips: list[Clip],
    settings: Settings,
) -> RenderPlan:
    """Validate clip boundaries and freeze output names and durations."""

    if not clips:
        raise ValueError("A render plan requires at least one clip.")

    planned: list[PlannedClip] = []
    seen_outputs: set[Path] = set()

    for index, clip in enumerate(clips, start=1):
        if clip.start_seconds >= source.duration_seconds:
            raise ValueError(
                f"Clip {index} starts at {clip.start_seconds}s, after the source "
                f"ends at {source.duration_seconds:.2f}s."
            )

        requested_duration = min(
            clip.duration_seconds + settings.extra_clip_seconds,
            settings.maximum_clip_duration,
        )
        available_duration = source.duration_seconds - clip.start_seconds
        duration = min(float(requested_duration), available_duration)
        if duration <= 0:
            raise ValueError(f"Clip {index} has no renderable duration.")

        filename = (
            f"{index:02d}.mp4"
            if settings.exact_index_filenames
            else f"{index:02d}_{sanitize_filename(clip.title)}.mp4"
        )
        output_path = settings.output_dir / filename
        if output_path.resolve() == source.path.resolve():
            raise ValueError(
                "Output would overwrite the source. Choose another folder."
            )
        if output_path in seen_outputs:
            raise ValueError(f"Duplicate output path in render plan: {output_path}")
        seen_outputs.add(output_path)

        planned.append(PlannedClip(index, clip, duration, output_path))

    return RenderPlan(source=source, clips=tuple(planned))
