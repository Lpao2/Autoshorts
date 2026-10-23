import pytest

from autoshorts.automation_config import VisualProfile
from autoshorts.naming import sanitize_filename
from autoshorts.video_processor import build_vertical_filter


def test_sanitize_filename_removes_windows_unsafe_characters() -> None:
    assert sanitize_filename("  A: useful / clip?  ") == "A_useful_clip"


def test_sanitize_filename_has_fallback() -> None:
    assert sanitize_filename("***") == "untitled_clip"


def test_vertical_filter_contains_requested_dimensions() -> None:
    value = build_vertical_filter(1080, 1920)
    assert "scale=1080:1920" in value
    assert "crop=1080:1920" in value
    assert value.endswith("format=yuv420p[video]")


def test_vertical_filter_receives_theme_visual_parameters() -> None:
    profile = VisualProfile(
        crop_preset="lol-gameplay-v1",
        focal_x=0.45,
        focal_y=0.38,
        foreground_scale=1.1,
        blur_strength=24,
    )
    value = build_vertical_filter(1080, 1920, profile)
    assert "boxblur=24:10" in value
    assert "iw*1.100" in value
    assert "(W-w)*0.450:(H-h)*0.380" in value


def test_center_crop_profile_protects_configured_focal_point() -> None:
    value = build_vertical_filter(
        1080, 1920, VisualProfile(crop_mode="center_crop", focal_x=0.4)
    )
    assert "force_original_aspect_ratio=increase" in value
    assert "(in_w-out_w)*0.400" in value


@pytest.mark.parametrize("width,height", [(0, 1920), (1081, 1920), (1080, 1919)])
def test_vertical_filter_rejects_invalid_dimensions(width: int, height: int) -> None:
    with pytest.raises(ValueError, match="even integers"):
        build_vertical_filter(width, height)
