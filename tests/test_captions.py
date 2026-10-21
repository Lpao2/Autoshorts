import json
import subprocess

import pytest

from autoshorts.captions import (
    CaptionPreset,
    SafeArea,
    caption_words,
    render_captioned_short,
    resolve_caption_font,
    segment_captions,
    wrap_caption,
    write_ass,
)
from autoshorts.production import SegmentSelection
from autoshorts.state_store import StateStore
from autoshorts.transcription import (
    Transcript,
    TranscriptSegment,
    TranscriptWord,
    write_transcript_atomic,
)


def selection(duration=4.0):
    return SegmentSelection(
        "video-1",
        10.0,
        10.0 + duration,
        duration,
        "Title",
        "Headline",
        "Reason",
        0.9,
        "2026-01-01T00:00:00+00:00",
        "test",
        "v1",
        "context",
    )


def transcript():
    words = (
        TranscriptWord(9.8, 10.2, " Olá"),
        TranscriptWord(10.2, 10.7, " família,"),
        TranscriptWord(10.7, 11.1, " minha"),
        TranscriptWord(11.1, 11.5, " vida"),
        TranscriptWord(11.5, 12.0, " não"),
        TranscriptWord(12.0, 12.6, " desce!"),
        TranscriptWord(13.7, 14.2, " fora"),
    )
    segment = TranscriptSegment(9.8, 14.2, "Olá família", words)
    return Transcript("pt", 1.0, 20, "Olá família", (segment,), "fake", "v1")


def test_normalizes_source_words_and_clamps_to_short():
    words = caption_words(transcript(), selection())
    assert words[0].start == 0
    assert words[-1].end == 4
    assert all(0 <= word.start < word.end <= 4 for word in words)
    assert words[0].text == "Olá"


def test_segments_have_safe_timing_accents_and_at_most_two_lines():
    blocks = segment_captions(caption_words(transcript(), selection()), 4)
    assert blocks
    assert all(0 <= block.start < block.end <= 4 for block in blocks)
    assert all(len(block.lines) <= 2 for block in blocks)
    assert any("família" in block.text for block in blocks)


def test_wrap_uses_estimated_width_and_safe_margins():
    preset = CaptionPreset(
        font_size=84,
        minimum_font_size=72,
        safe_area=SafeArea(margin_left=100, margin_right=100),
    )
    lines, size = wrap_caption(
        "A gente ainda nem tem Life Steel direito", preset
    )
    assert len(lines) == 2
    assert 72 <= size <= 84
    assert preset.safe_area.text_width == 880


def test_font_fallback_requires_exact_installed_match(monkeypatch, tmp_path):
    font = tmp_path / "DejaVuSans-Bold.ttf"
    font.touch()

    class Result:
        stdout = f"Noto Sans\tRegular\t/nope.ttf\nDejaVu Sans\tBold\t{font}\n"

    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: Result())
    resolved = resolve_caption_font()
    assert resolved.family == "DejaVu Sans"
    assert resolved.style == "Bold"
    assert resolved.path == font.resolve()


def test_ass_generation_records_style_and_two_line_break(tmp_path):
    blocks = segment_captions(caption_words(transcript(), selection()), 4)
    from autoshorts.captions import ResolvedFont

    font_path = tmp_path / "font.ttf"
    font_path.touch()
    destination = tmp_path / "captions.ass"
    write_ass(
        destination,
        blocks,
        ResolvedFont("DejaVu Sans", "Bold", font_path, "DejaVu Sans Bold"),
        CaptionPreset(),
    )
    content = destination.read_text(encoding="utf-8")
    assert "[V4+ Styles]" in content
    assert "Style: Default,DejaVu Sans,84" in content
    assert "Dialogue:" in content


@pytest.mark.skipif(
    subprocess.run(["which", "ffmpeg"], capture_output=True).returncode != 0,
    reason="FFmpeg unavailable",
)
def test_render_preserves_vertical_media_audio_and_is_idempotent(tmp_path):
    base = tmp_path / "base.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=1080x1920:r=30:d=4",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=4",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-c:a",
            "aac",
            "-shortest",
            str(base),
        ],
        check=True,
    )
    artifact = tmp_path / "transcript.json"
    write_transcript_atomic(artifact, transcript(), "video-1")
    first = render_captioned_short(
        base_video=base,
        transcript=transcript(),
        selection=selection(),
        transcript_path=artifact,
    )
    second = render_captioned_short(
        base_video=base,
        transcript=transcript(),
        selection=selection(),
        transcript_path=artifact,
    )
    assert (first.media.width, first.media.height) == (1080, 1920)
    assert first.media.has_audio
    assert len(first.sample_paths) == 3
    assert second.reused
    assert first.video_path == second.video_path
    manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
    assert manifest["full_decode"] == "PASS"
    with StateStore(tmp_path / "state.db") as state:
        count = state.connection.execute(
            "SELECT count(*) FROM publications"
        ).fetchone()[0]
        assert count == 0
