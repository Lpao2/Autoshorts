"""Network-free YouTube upload backend for tests and explicit development use."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from autoshorts.youtube_publishing import PublicationMetadata


@dataclass(slots=True)
class MockYouTubeUploadBackend:
    remote_video_id: str = "mock-youtube-video-id"
    calls: list[tuple[Path, PublicationMetadata]] = field(default_factory=list)

    def upload(
        self,
        credentials: object,
        video_path: Path,
        metadata: PublicationMetadata,
    ) -> str:
        del credentials
        self.calls.append((video_path, metadata))
        return self.remote_video_id
