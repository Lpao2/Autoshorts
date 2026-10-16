from pathlib import Path

import pytest

from autoshorts.media_probe import MediaProbeError, probe_media


def test_probe_missing_file() -> None:
    with pytest.raises(FileNotFoundError):
        probe_media(Path("missing-video.mp4"))


def test_probe_missing_executable(tmp_path: Path) -> None:
    source = tmp_path / "not-media.bin"
    source.write_bytes(b"not media")
    with pytest.raises(MediaProbeError, match="executable not found"):
        probe_media(source, "definitely-missing-ffprobe")
