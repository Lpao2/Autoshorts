"""Deterministic analysis provider for explicit development tests only."""

from pathlib import Path

from autoshorts.ai_analysis import ClipAnalysis
from autoshorts.automation_config import ThemeProfile
from autoshorts.collection import VideoCandidate
from autoshorts.media_probe import probe_media
from autoshorts.timestamp_parser import Clip


def fake_analysis_provider(
    source_path: Path,
    _candidate: VideoCandidate,
    _theme: ThemeProfile,
) -> ClipAnalysis:
    """Return one valid short clip. TEST / DEVELOPMENT ONLY."""
    info = probe_media(source_path)
    end = max(1, min(5, int(info.duration_seconds)))
    return ClipAnalysis((Clip(0, end, "Development test clip"),), "FAKE")
