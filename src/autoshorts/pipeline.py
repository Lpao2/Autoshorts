"""Application-level orchestration for probing, planning, and rendering."""

from __future__ import annotations

import json
import os
import platform
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from autoshorts.config import Settings
from autoshorts.media_probe import MediaInfo, probe_media
from autoshorts.render_plan import RenderPlan, build_render_plan
from autoshorts.timestamp_parser import Clip
from autoshorts.video_processor import RenderResult, process_render_plan


@dataclass(frozen=True, slots=True)
class PipelineResult:
    plan: RenderPlan
    renders: tuple[RenderResult, ...]
    manifest_path: Path


class PipelineExecutionError(RuntimeError):
    """A pipeline failure with a persisted diagnostic manifest."""

    def __init__(self, message: str, manifest_path: Path) -> None:
        super().__init__(message)
        self.manifest_path = manifest_path


def _media_payload(info: MediaInfo) -> dict[str, object]:
    return {
        "path": str(info.path),
        "duration_seconds": info.duration_seconds,
        "width": info.width,
        "height": info.height,
        "frame_rate": info.frame_rate,
        "has_audio": info.has_audio,
        "rotation": info.rotation,
    }


def _manifest_payload(
    plan: RenderPlan,
    renders: tuple[RenderResult, ...],
    settings: Settings,
    *,
    run_id: str,
    status: str,
    error: str | None = None,
) -> dict[str, object]:
    render_by_index = {result.index: result for result in renders}
    clips: list[dict[str, object]] = []
    for item in plan.clips:
        result = render_by_index.get(item.index)
        clips.append(
            {
                "index": item.index,
                "title": item.clip.title,
                "start_seconds": item.clip.start_seconds,
                "end_seconds": item.clip.end_seconds,
                "render_duration_seconds": item.duration_seconds,
                "output_path": str(item.output_path),
                "status": result.status if result else "planned",
            }
        )

    payload: dict[str, object] = {
        "schema_version": 1,
        "run_id": run_id,
        "created_at": datetime.now(UTC).isoformat(),
        "status": status,
        "source": _media_payload(plan.source),
        "output": {
            "width": settings.output_width,
            "height": settings.output_height,
            "video_codec": settings.video_codec,
            "audio_codec": settings.audio_codec,
            "overwrite_policy": settings.overwrite_policy,
            "max_workers": settings.max_workers,
        },
        "clips": clips,
        "runtime": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "pid": os.getpid(),
        },
    }
    if error is not None:
        payload["error"] = error
    return payload


def _write_manifest(output_dir: Path, payload: dict[str, object], run_id: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"manifest-{run_id}.json"
    temporary = path.with_suffix(".part.json")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    return path


def run_pipeline(
    source_path: Path,
    clips: list[Clip],
    settings: Settings,
    *,
    dry_run: bool = False,
) -> PipelineResult:
    """Probe input, build a plan, render it, and write a run manifest."""

    source = probe_media(source_path, settings.ffprobe_path)
    plan = build_render_plan(source, clips, settings)
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]

    renders: tuple[RenderResult, ...] = ()
    initial_status = "planned" if dry_run else "running"
    manifest_path = _write_manifest(
        settings.output_dir,
        _manifest_payload(
            plan, renders, settings, run_id=run_id, status=initial_status
        ),
        run_id,
    )

    if dry_run:
        return PipelineResult(plan, renders, manifest_path)

    try:
        renders = process_render_plan(plan, settings)
    except Exception as exc:
        manifest_path = _write_manifest(
            settings.output_dir,
            _manifest_payload(
                plan,
                renders,
                settings,
                run_id=run_id,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
            ),
            run_id,
        )
        raise PipelineExecutionError(str(exc), manifest_path) from exc

    manifest_path = _write_manifest(
        settings.output_dir,
        _manifest_payload(plan, renders, settings, run_id=run_id, status="completed"),
        run_id,
    )
    return PipelineResult(plan, renders, manifest_path)
