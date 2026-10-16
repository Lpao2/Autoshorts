import pytest

from autoshorts.video_processor import build_vertical_filter, sanitize_filename


def test_sanitize_filename_removes_windows_unsafe_characters() -> None:
    assert sanitize_filename("  A: useful / clip?  ") == "A_useful_clip"


def test_sanitize_filename_has_fallback() -> None:
    assert sanitize_filename("***") == "untitled_clip"


def test_vertical_filter_contains_requested_dimensions() -> None:
    value = build_vertical_filter(1080, 1920)
    assert "scale=1080:1920" in value
    assert "crop=1080:1920" in value
    assert value.endswith("format=yuv420p[video]")


@pytest.mark.parametrize("width,height", [(0, 1920), (1081, 1920), (1080, 1919)])
def test_vertical_filter_rejects_invalid_dimensions(width: int, height: int) -> None:
    with pytest.raises(ValueError, match="even integers"):
        build_vertical_filter(width, height)
