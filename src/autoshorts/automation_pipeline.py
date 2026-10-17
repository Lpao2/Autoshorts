"""Orchestration for the legacy end-to-end production workflow.

The workflow depends on injected analysis and publishing adapters. This keeps
the deterministic media engine usable without browser sessions or credentials.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from autoshorts.ai_analysis import ClipAnalysis
from autoshorts.automation_config import AutomationConfig, ThemeProfile
from autoshorts.collection import (
    VideoCandidate,
    discover_creator_videos,
    download_video,
)
from autoshorts.config import Settings
from autoshorts.pipeline import PipelineResult, run_pipeline

AnalysisProvider = Callable[[Path, VideoCandidate, ThemeProfile], ClipAnalysis]


@dataclass(frozen=True, slots=True)
class AutomationRequest:
    theme: str
    videos_per_creator: int
    maximum_age_days: int
    workspace: Path
    download_only: bool = False


@dataclass(frozen=True, slots=True)
class AutomationItemResult:
    candidate: VideoCandidate
    source_path: Path
    analysis: ClipAnalysis | None
    render: PipelineResult | None


def run_automation(
    request: AutomationRequest,
    config: AutomationConfig,
    settings: Settings,
    *,
    analyze: AnalysisProvider | None = None,
) -> tuple[AutomationItemResult, ...]:
    """Run collection and optional analysis/rendering for one configured theme."""
    theme = config.theme(request.theme)
    if request.videos_per_creator < 1:
        raise ValueError("videos_per_creator must be positive.")
    if request.maximum_age_days < 0 or request.maximum_age_days > 3650:
        raise ValueError("maximum_age_days must be between 0 and 3650.")
    workspace = request.workspace.expanduser().resolve()
    if not request.download_only and analyze is None:
        raise ValueError("An analysis provider is required before rendering.")
    theme_root = workspace / theme.output_subdirectory
    results: list[AutomationItemResult] = []
    for creator in theme.creators:
        candidates = discover_creator_videos(
            creator.name,
            creator.videos_url,
            quantity=request.videos_per_creator,
            age_days=request.maximum_age_days,
        )
        for candidate in candidates:
            source_path = download_video(candidate, theme_root / "downloads")
            if request.download_only:
                results.append(AutomationItemResult(candidate, source_path, None, None))
                continue
            assert analyze is not None
            analysis = analyze(source_path, candidate, theme)
            output = theme_root / "outputs" / _safe_creator_directory(candidate.creator)
            render_settings = replace(settings, output_dir=output)
            render = run_pipeline(source_path, list(analysis.clips), render_settings)
            results.append(
                AutomationItemResult(candidate, source_path, analysis, render)
            )
    return tuple(results)


def _safe_creator_directory(name: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in "-_" else "_" for char in name)
    return cleaned.strip("._") or "creator"
