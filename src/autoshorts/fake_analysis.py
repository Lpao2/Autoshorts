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
    """Return exactly three deterministic clips. TEST / DEVELOPMENT ONLY."""
    info = probe_media(source_path)
    duration = int(info.duration_seconds)
    clip_duration = min(5, duration // 3)
    if clip_duration < 1:
        raise ValueError(
            "O vídeo é curto demais para gerar três cortes de desenvolvimento."
        )
    clips = tuple(
        Clip(
            (index - 1) * clip_duration,
            index * clip_duration,
            f"Development clip {index}",
        )
        for index in range(1, 4)
    )
    return ClipAnalysis(clips, "FAKE / TEST / DEVELOPMENT ONLY")
