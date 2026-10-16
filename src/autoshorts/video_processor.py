"""Validated local video rendering through FFmpeg."""

from __future__ import annotations

import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from autoshorts.config import Settings
from autoshorts.media_probe import validate_rendered_media
from autoshorts.render_plan import PlannedClip, RenderPlan
from autoshorts.timestamp_parser import seconds_to_timestamp


class OutputExistsError(FileExistsError):
    """Raised when an output exists and replacement is disabled."""


@dataclass(frozen=True, slots=True)
class RenderResult:
    index: int
    output_path: Path
    status: str


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


def _ffmpeg_command(
    source: Path,
    planned: PlannedClip,
    temporary_path: Path,
    settings: Settings,
) -> list[str]:
    return [
        settings.ffmpeg_path,
        "-hide_banner",
        "-nostdin",
        "-y",
        "-ss",
        seconds_to_timestamp(planned.clip.start_seconds),
        "-i",
        str(source),
        "-t",
        f"{planned.duration_seconds:.3f}",
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


def render_planned_clip(
    source: Path,
    planned: PlannedClip,
    settings: Settings,
) -> RenderResult:
    """Render and validate one planned clip atomically."""

    output_path = planned.output_path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"[{planned.index:02d}] Rendering: {planned.clip.title}")

    if output_path.exists():
        if settings.overwrite_policy == "skip":
            validate_rendered_media(
                output_path,
                ffprobe_path=settings.ffprobe_path,
                expected_width=settings.output_width,
                expected_height=settings.output_height,
                expected_duration=planned.duration_seconds,
            )
            print(f"[{planned.index:02d}] Reused: {output_path}")
            return RenderResult(planned.index, output_path, "skipped")
        if settings.overwrite_policy == "error":
            raise OutputExistsError(f"Output already exists: {output_path}")

    temporary_path = output_path.with_suffix(".part.mp4")
    temporary_path.unlink(missing_ok=True)

    try:
        result = subprocess.run(
            _ffmpeg_command(source, planned, temporary_path, settings),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"FFmpeg executable not found: {settings.ffmpeg_path}"
        ) from exc

    if result.returncode != 0:
        temporary_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"FFmpeg failed while processing '{planned.clip.title}'.\n\n"
            f"{result.stderr[-3000:]}"
        )

    try:
        validate_rendered_media(
            temporary_path,
            ffprobe_path=settings.ffprobe_path,
            expected_width=settings.output_width,
            expected_height=settings.output_height,
            expected_duration=planned.duration_seconds,
        )
        temporary_path.replace(output_path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise

    print(f"[{planned.index:02d}] Ready: {output_path}")
    return RenderResult(planned.index, output_path, "generated")


def process_render_plan(
    plan: RenderPlan,
    settings: Settings,
) -> tuple[RenderResult, ...]:
    """Render a plan with bounded parallelism and deterministic result order."""

    if settings.max_workers == 1 or len(plan.clips) == 1:
        return tuple(
            render_planned_clip(plan.source.path, item, settings) for item in plan.clips
        )

    results: dict[int, RenderResult] = {}
    with ThreadPoolExecutor(
        max_workers=min(settings.max_workers, len(plan.clips)),
        thread_name_prefix="autoshorts-render",
    ) as executor:
        futures = {
            executor.submit(render_planned_clip, plan.source.path, item, settings): item
            for item in plan.clips
        }
        try:
            for future in as_completed(futures):
                result = future.result()
                results[result.index] = result
        except Exception:
            for future in futures:
                future.cancel()
            raise

    return tuple(results[index] for index in sorted(results))
