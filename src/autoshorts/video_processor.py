"""Validated local video rendering through FFmpeg."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue
from tempfile import TemporaryFile
from threading import Event, Thread

from autoshorts.automation_config import VisualProfile
from autoshorts.config import Settings
from autoshorts.media_probe import MediaInfo, probe_media, validate_rendered_media
from autoshorts.render_events import (
    ProgressCallback,
    RenderEvent,
    check_cancelled,
)
from autoshorts.render_plan import PlannedClip, RenderPlan
from autoshorts.timestamp_parser import seconds_to_timestamp


class OutputExistsError(FileExistsError):
    """Raised when an output exists and replacement is disabled."""


@dataclass(frozen=True, slots=True)
class RenderResult:
    index: int
    output_path: Path
    status: str


def build_vertical_filter(
    width: int, height: int, profile: VisualProfile | None = None
) -> str:
    """Create a vertical layout with a blurred background."""

    if width <= 0 or height <= 0 or width % 2 or height % 2:
        raise ValueError("Output dimensions must be positive even integers.")

    profile = profile or VisualProfile()
    if profile.crop_mode == "center_crop":
        return (
            f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height}:"
            f"(in_w-out_w)*{profile.focal_x:.3f}:"
            f"(in_h-out_h)*{profile.focal_y:.3f},format=yuv420p[video]"
        )
    zoom = profile.foreground_scale
    return (
        "[0:v]split=2[background_source][foreground_source];"
        f"[background_source]scale={width}:{height}:"
        "force_original_aspect_ratio=increase,"
        f"crop={width}:{height},boxblur={profile.blur_strength}:10[background];"
        f"[foreground_source]scale={width}:{height}:"
        "force_original_aspect_ratio=decrease,"
        f"scale=trunc(iw*{zoom:.3f}/2)*2:trunc(ih*{zoom:.3f}/2)*2[foreground];"
        "[background][foreground]overlay="
        f"(W-w)*{profile.focal_x:.3f}:(H-h)*{profile.focal_y:.3f},"
        "format=yuv420p[video]"
    )


def _ffmpeg_command(
    source: Path,
    planned: PlannedClip,
    temporary_path: Path,
    settings: Settings,
    visual_profile: VisualProfile | None = None,
) -> list[str]:
    return [
        settings.ffmpeg_path,
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-progress",
        "pipe:1",
        "-nostats",
        "-y",
        "-ss",
        seconds_to_timestamp(planned.clip.start_seconds),
        "-i",
        str(source),
        "-t",
        f"{planned.duration_seconds:.3f}",
        "-filter_complex",
        build_vertical_filter(
            settings.output_width, settings.output_height, visual_profile
        ),
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


def _run_ffmpeg(
    command: list[str],
    duration: float,
    cancel: Event,
    report: Callable[[float], None],
) -> None:
    """Drain progress while polling cancellation, even if FFmpeg is silent."""
    check_cancelled(cancel)
    lines: Queue[str | None] = Queue()
    # A file avoids pipe deadlocks and unbounded stderr memory on long renders.
    with TemporaryFile(mode="w+b") as errors:
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=errors,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"FFmpeg executable not found: {command[0]}") from exc

        def read_progress() -> None:
            assert process.stdout is not None
            try:
                for line in process.stdout:
                    lines.put(line)
            finally:
                lines.put(None)

        reader = Thread(target=read_progress, daemon=True)
        reader.start()
        try:
            while True:
                check_cancelled(cancel)
                try:
                    line = lines.get(timeout=0.1)
                except Empty:
                    continue
                if line is None:
                    while process.poll() is None:
                        check_cancelled(cancel)
                        cancel.wait(0.1)
                    break
                key, _, value = line.strip().partition("=")
                if key == "out_time_us":
                    try:
                        # Reserve 100% for successful FFprobe validation/rename.
                        report(min(0.99, max(0.0, int(value) / 1e6 / duration)))
                    except ValueError:
                        pass
            code = process.wait()
            check_cancelled(cancel)
            if code:
                errors.seek(0, 2)
                size = errors.tell()
                errors.seek(max(0, size - 3000))
                detail = errors.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"FFmpeg failed (exit {code}).\n{detail}")
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            reader.join(timeout=2)
            assert process.stdout is not None
            process.stdout.close()


def render_planned_clip(
    source: Path,
    planned: PlannedClip,
    settings: Settings,
    *,
    cancel: Event | None = None,
    on_progress: ProgressCallback | None = None,
    visual_profile: VisualProfile | None = None,
) -> RenderResult:
    """Render and validate one planned clip atomically."""
    cancel = cancel if cancel is not None else Event()
    check_cancelled(cancel)

    def report(status: str, progress: float = 0.0) -> None:
        if on_progress is not None:
            on_progress(RenderEvent(planned.index, status, progress))

    output_path = planned.output_path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    report("rendering")
    if on_progress is None:
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
            check_cancelled(cancel)
            if on_progress is None:
                print(f"[{planned.index:02d}] Reused: {output_path}")
            report("skipped", 1.0)
            return RenderResult(planned.index, output_path, "skipped")
        if settings.overwrite_policy == "error":
            raise OutputExistsError(f"Output already exists: {output_path}")

    temporary_path = output_path.with_suffix(".part.mp4")
    temporary_path.unlink(missing_ok=True)
    try:
        _run_ffmpeg(
            _ffmpeg_command(
                source, planned, temporary_path, settings, visual_profile
            ),
            planned.duration_seconds,
            cancel,
            lambda value: report("rendering", value),
        )
        report("validating", 0.99)
        validate_rendered_media(
            temporary_path,
            ffprobe_path=settings.ffprobe_path,
            expected_width=settings.output_width,
            expected_height=settings.output_height,
            expected_duration=planned.duration_seconds,
        )
        check_cancelled(cancel)
        temporary_path.replace(output_path)
    finally:
        temporary_path.unlink(missing_ok=True)

    report("generated", 1.0)
    if on_progress is None:
        print(f"[{planned.index:02d}] Ready: {output_path}")
    return RenderResult(planned.index, output_path, "generated")


def process_render_plan(
    plan: RenderPlan,
    settings: Settings,
    *,
    cancel: Event | None = None,
    on_progress: ProgressCallback | None = None,
    on_result: Callable[[RenderResult], None] | None = None,
) -> tuple[RenderResult, ...]:
    """Bounded parallelism; drain workers before returning or raising."""
    cancel = cancel if cancel is not None else Event()
    check_cancelled(cancel)
    results: dict[int, RenderResult] = {}
    first_error: Exception | None = None
    with ThreadPoolExecutor(
        max_workers=min(settings.max_workers, len(plan.clips)),
        thread_name_prefix="autoshorts-render",
    ) as executor:
        futures = {
            executor.submit(
                render_planned_clip,
                plan.source.path,
                item,
                settings,
                cancel=cancel,
                on_progress=on_progress,
            ): item
            for item in plan.clips
        }
        for future in as_completed(futures):
            try:
                result = future.result()
                results[result.index] = result
                if on_result is not None:
                    on_result(result)
            except Exception as exc:
                if first_error is None:
                    first_error = exc
                    cancel.set()
    if first_error is not None:
        raise first_error
    return tuple(results[index] for index in sorted(results))


def render_crop_preview(
    source: Path,
    output: Path,
    *,
    start_seconds: float,
    duration_seconds: float,
    settings: Settings,
    visual_profile: VisualProfile,
) -> MediaInfo:
    """Render a short, non-persistent sample for theme crop calibration."""
    if duration_seconds <= 0:
        raise ValueError("Preview duration must be positive.")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".part.mp4")
    temporary.unlink(missing_ok=True)
    command = [
        settings.ffmpeg_path,
        "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
        "-ss", seconds_to_timestamp(start_seconds), "-i", str(source),
        "-t", f"{duration_seconds:.3f}",
        "-filter_complex",
        build_vertical_filter(
            settings.output_width, settings.output_height, visual_profile
        ),
        "-map", "[video]", "-map", "0:a?", "-c:v", settings.video_codec,
        "-preset", settings.video_preset, "-crf", str(settings.video_crf),
        "-c:a", settings.audio_codec, "-b:a", settings.audio_bitrate,
        "-movflags", "+faststart", str(temporary),
    ]
    try:
        subprocess.run(command, check=True)
        validate_rendered_media(
            temporary,
            ffprobe_path=settings.ffprobe_path,
            expected_width=settings.output_width,
            expected_height=settings.output_height,
            expected_duration=duration_seconds,
        )
        temporary.replace(output)
        return probe_media(output, settings.ffprobe_path)
    finally:
        temporary.unlink(missing_ok=True)
