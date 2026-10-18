import pytest

from autoshorts.collection import VideoCandidate
from autoshorts.fake_analysis import fake_analysis_provider
from autoshorts.media_probe import MediaInfo


def test_fake_provider_returns_three_valid_non_overlapping_clips(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.touch()
    monkeypatch.setattr(
        "autoshorts.fake_analysis.probe_media",
        lambda _path: MediaInfo(source, 20, 1280, 720, 30, True, 0),
    )
    result = fake_analysis_provider(
        source, VideoCandidate("c", "u", "t", None, 20), None
    )
    assert len(result.clips) == 3
    assert [clip.title for clip in result.clips] == [
        "Development clip 1",
        "Development clip 2",
        "Development clip 3",
    ]
    assert all(
        current.end_seconds <= following.start_seconds
        for current, following in zip(result.clips, result.clips[1:], strict=False)
    )


def test_fake_provider_rejects_video_too_short(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.touch()
    monkeypatch.setattr(
        "autoshorts.fake_analysis.probe_media",
        lambda _path: MediaInfo(source, 2.9, 1280, 720, 30, True, 0),
    )
    with pytest.raises(ValueError, match="curto demais"):
        fake_analysis_provider(source, VideoCandidate("c", "u", "t", None, 2), None)
