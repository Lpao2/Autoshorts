"""Reusable neutral caption preset, ASS generation, and burned-in rendering."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from autoshorts.media_probe import MediaInfo, probe_media
from autoshorts.production import SegmentSelection
from autoshorts.transcription import Transcript, TranscriptWord

CAPTION_VERSION = "neutral-ass-v1"


@dataclass(frozen=True, slots=True)
class SafeArea:
    width: int = 1080
    height: int = 1920
    margin_left: int = 100
    margin_right: int = 100
    margin_top: int = 120
    margin_bottom: int = 450

    @property
    def text_width(self) -> int:
        return self.width - self.margin_left - self.margin_right


@dataclass(frozen=True, slots=True)
class CaptionPreset:
    name: str = "base-neutral"
    requested_font: str = "Montserrat ExtraBold"
    font_size: int = 84
    minimum_font_size: int = 72
    primary_color: str = "&H00FFFFFF"
    outline_color: str = "&H00000000"
    outline: int = 7
    shadow: int = 2
    maximum_lines: int = 2
    minimum_duration: float = 0.45
    maximum_duration: float = 3.0
    minimum_words: int = 2
    maximum_words: int = 6
    maximum_gap: float = 0.70
    safe_area: SafeArea = SafeArea()


DEFAULT_CAPTION_PRESET = CaptionPreset()


@dataclass(frozen=True, slots=True)
class ResolvedFont:
    family: str
    style: str
    path: Path
    requested: str


@dataclass(frozen=True, slots=True)
class CaptionBlock:
    start: float
    end: float
    text: str
    lines: tuple[str, ...]
    font_size: int
    words: tuple[TranscriptWord, ...]


@dataclass(frozen=True, slots=True)
class CaptionResult:
    subtitle_path: Path
    video_path: Path
    manifest_path: Path
    sample_paths: tuple[Path, ...]
    font: ResolvedFont
    blocks: tuple[CaptionBlock, ...]
    media: MediaInfo
    checksum: str
    reused: bool


FONT_FALLBACKS = (
    ("Montserrat", "ExtraBold", "Montserrat ExtraBold"),
    ("Montserrat", "Bold", "Montserrat Bold"),
    ("Inter", "ExtraBold", "Inter ExtraBold"),
    ("Inter", "Bold", "Inter Bold"),
    ("DejaVu Sans", "Bold", "DejaVu Sans Bold"),
)


def resolve_caption_font() -> ResolvedFont:
    """Resolve an exact installed font without accepting fontconfig substitution."""
    try:
        result = subprocess.run(
            ["fc-list", "-f", "%{family}\t%{style}\t%{file}\n"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.SubprocessError) as exc:
        raise RuntimeError(
            "fontconfig fc-list is required for caption rendering."
        ) from exc
    installed = []
    for line in result.stdout.splitlines():
        family, separator, remainder = line.partition("\t")
        style, separator2, file_path = remainder.partition("\t")
        if separator and separator2 and Path(file_path).is_file():
            installed.append((family, style, Path(file_path)))
    for family, weight, requested in FONT_FALLBACKS:
        matches = []
        for found_family, found_style, path in installed:
            families = {item.strip().casefold() for item in found_family.split(",")}
            styles = {item.strip().casefold() for item in found_style.split(",")}
            if family.casefold() in families and weight.casefold() in styles:
                exact_family = found_family.strip().casefold() == family.casefold()
                condensed = "condensed" in path.name.casefold()
                matches.append((not exact_family, condensed, path))
        if matches:
            _, _, path = min(matches, key=lambda item: (item[0], item[1], str(item[2])))
            return ResolvedFont(family, weight, path.resolve(), requested)
    raise RuntimeError("No supported caption font is installed.")


def caption_words(
    transcript: Transcript, selection: SegmentSelection
) -> tuple[TranscriptWord, ...]:
    words = []
    for segment in transcript.segments:
        for word in segment.words:
            if (
                word.end <= selection.start_seconds
                or word.start >= selection.end_seconds
            ):
                continue
            start = max(0.0, word.start - selection.start_seconds)
            end = min(selection.duration_seconds, word.end - selection.start_seconds)
            if end > start:
                words.append(TranscriptWord(start, end, _normalize_text(word.text)))
    if not words:
        raise ValueError("Selected transcript interval has no word timestamps.")
    return tuple(words)


def segment_captions(
    words: tuple[TranscriptWord, ...],
    duration: float,
    preset: CaptionPreset = DEFAULT_CAPTION_PRESET,
) -> tuple[CaptionBlock, ...]:
    if not words:
        raise ValueError("Caption segmentation requires word timestamps.")
    groups: list[list[TranscriptWord]] = []
    current: list[TranscriptWord] = []
    for word in words:
        gap = word.start - current[-1].end if current else 0.0
        projected_duration = word.end - current[0].start if current else 0.0
        must_break = bool(
            current
            and (
                gap > preset.maximum_gap
                or len(current) >= preset.maximum_words
                or projected_duration > preset.maximum_duration
            )
        )
        if must_break:
            groups.append(current)
            current = []
        current.append(word)
        if (
            len(current) >= preset.minimum_words
            and _ends_phrase(word.text)
            and word.text.strip().casefold() not in _JOINING_WORDS
        ):
            groups.append(current)
            current = []
    if current:
        if groups and len(current) < preset.minimum_words:
            previous = groups[-1]
            combined_duration = current[-1].end - previous[0].start
            if (
                len(previous) + len(current) <= preset.maximum_words
                and combined_duration <= preset.maximum_duration
            ):
                groups[-1] = previous + current
            else:
                groups.append(current)
        else:
            groups.append(current)
    _repair_joining_word_boundaries(groups, preset)

    blocks = []
    for index, group in enumerate(groups):
        start = max(0.0, group[0].start)
        natural_end = min(duration, group[-1].end)
        next_start = groups[index + 1][0].start if index + 1 < len(groups) else duration
        end = min(
            duration,
            max(natural_end, min(start + preset.minimum_duration, next_start)),
        )
        if end <= start:
            continue
        text = _normalize_text(" ".join(word.text for word in group))
        lines, font_size = wrap_caption(text, preset)
        blocks.append(CaptionBlock(start, end, text, lines, font_size, tuple(group)))
    return tuple(blocks)


def wrap_caption(
    text: str, preset: CaptionPreset = DEFAULT_CAPTION_PRESET
) -> tuple[tuple[str, ...], int]:
    words = text.split()
    for size in range(preset.font_size, preset.minimum_font_size - 1, -2):
        if _estimated_width(text, size) <= preset.safe_area.text_width:
            return (text,), size
        split = _best_two_line_split(words, size, preset.safe_area.text_width)
        if split is not None:
            return split, size
    raise ValueError(f"Caption cannot fit inside the safe width: {text}")


def write_ass(
    path: Path,
    blocks: tuple[CaptionBlock, ...],
    font: ResolvedFont,
    preset: CaptionPreset,
) -> Path:
    style = (
        f"Style: Default,{font.family},{preset.font_size},{preset.primary_color},"
        "&H00FFFFFF," f"{preset.outline_color},&H64000000,-1,0,0,0,100,100,0,0,"
        f"1,{preset.outline},{preset.shadow},2,{preset.safe_area.margin_left},"
        f"{preset.safe_area.margin_right},{preset.safe_area.margin_bottom},1"
    )
    events = []
    for block in blocks:
        escaped = r"\N".join(_escape_ass(line) for line in block.lines)
        override = (
            f"{{\\fs{block.font_size}}}"
            if block.font_size != preset.font_size
            else ""
        )
        events.append(
            "Dialogue: 0,"
            f"{_ass_time(block.start)},{_ass_time(block.end)},Default,,0,0,0,,"
            f"{override}{escaped}"
        )
    content = "\n".join(
        [
            "[Script Info]",
            f"; AutoShorts {CAPTION_VERSION}",
            "ScriptType: v4.00+",
            f"PlayResX: {preset.safe_area.width}",
            f"PlayResY: {preset.safe_area.height}",
            "WrapStyle: 2",
            "ScaledBorderAndShadow: yes",
            "YCbCr Matrix: TV.709",
            "",
            "[V4+ Styles]",
            "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,"
            "OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,"
            "ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,"
            "MarginR,MarginV,Encoding",
            style,
            "",
            "[Events]",
            "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
            *events,
            "",
        ]
    )
    _write_text_atomic(path, content)
    return path


def render_captioned_short(
    *,
    base_video: Path,
    transcript: Transcript,
    selection: SegmentSelection,
    transcript_path: Path,
    ffmpeg_path: str = "ffmpeg",
    ffprobe_path: str = "ffprobe",
    preset: CaptionPreset = DEFAULT_CAPTION_PRESET,
) -> CaptionResult:
    font = resolve_caption_font()
    base_checksum = _sha256(base_video)
    transcript_checksum = _sha256(transcript_path)
    fingerprint = _fingerprint(
        base_checksum, transcript_checksum, selection, preset, font
    )
    stem = f"{base_video.stem}_captioned-{fingerprint[:8]}"
    subtitle_path = base_video.with_name(f"{stem}.ass")
    video_path = base_video.with_name(f"{stem}.mp4")
    manifest_path = base_video.with_name(f"{stem}.caption.json")
    samples_dir = base_video.with_name(f"{stem}.samples")
    words = caption_words(transcript, selection)
    blocks = segment_captions(words, selection.duration_seconds, preset)
    expected_samples = tuple(
        samples_dir / f"sample-{index}.png" for index in range(1, 4)
    )

    if manifest_path.is_file() and video_path.is_file() and subtitle_path.is_file():
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        checksum = _sha256(video_path)
        if (
            raw.get("fingerprint") == fingerprint
            and raw.get("checksum_sha256") == checksum
            and all(path.is_file() for path in expected_samples)
        ):
            _validate_codecs(video_path, ffprobe_path)
            return CaptionResult(
                subtitle_path,
                video_path,
                manifest_path,
                expected_samples,
                font,
                blocks,
                probe_media(video_path, ffprobe_path),
                checksum,
                True,
            )

    write_ass(subtitle_path, blocks, font, preset)
    temporary = video_path.with_suffix(".part.mp4")
    temporary.unlink(missing_ok=True)
    filter_value = (
        f"ass=filename='{_escape_filter_path(subtitle_path)}':"
        f"fontsdir='{_escape_filter_path(font.path.parent)}'"
    )
    try:
        _run(
            [
                ffmpeg_path,
                "-hide_banner",
                "-nostdin",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(base_video),
                "-vf",
                filter_value,
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "20",
                "-c:a",
                "copy",
                "-movflags",
                "+faststart",
                str(temporary),
            ]
        )
        _decode_fully(temporary, ffmpeg_path)
        media = probe_media(temporary, ffprobe_path)
        base_media = probe_media(base_video, ffprobe_path)
        _validate_media(media, base_media)
        video_codec, audio_codec = _validate_codecs(temporary, ffprobe_path)
        temporary.replace(video_path)
    finally:
        temporary.unlink(missing_ok=True)
    checksum = _sha256(video_path)
    samples = _extract_samples(
        video_path, blocks, samples_dir, ffmpeg_path=ffmpeg_path
    )
    payload = {
        "schema_version": 1,
        "caption_version": CAPTION_VERSION,
        "fingerprint": fingerprint,
        "created_at": datetime.now(UTC).isoformat(),
        "caption_source": "persisted-word-timestamps",
        "transcript": {
            "path": str(transcript_path),
            "checksum_sha256": transcript_checksum,
            "provider": transcript.provider,
            "model": transcript.model,
            "schema_version": 1,
        },
        "source": {
            "video_path": str(base_video),
            "checksum_sha256": base_checksum,
            "source_video_id": selection.source_video_id,
            "selected_start": selection.start_seconds,
            "selected_end": selection.end_seconds,
        },
        "subtitle_format": "ASS",
        "preset": _preset_payload(preset),
        "font": {
            "requested": preset.requested_font,
            "family": font.family,
            "weight": font.style,
            "path": str(font.path),
        },
        "blocks": [_block_payload(block) for block in blocks],
        "subtitle_path": str(subtitle_path),
        "captioned_video_path": str(video_path),
        "checksum_sha256": checksum,
        "media": {
            "width": media.width,
            "height": media.height,
            "duration_seconds": media.duration_seconds,
            "frame_rate": media.frame_rate,
            "has_audio": media.has_audio,
            "video_codec": video_codec,
            "audio_codec": audio_codec,
        },
        "visual_samples": [str(path) for path in samples],
        "full_decode": "PASS",
    }
    _write_json_atomic(manifest_path, payload)
    return CaptionResult(
        subtitle_path,
        video_path,
        manifest_path,
        samples,
        font,
        blocks,
        media,
        checksum,
        False,
    )


_JOINING_WORDS = {
    "a",
    "as",
    "com",
    "da",
    "das",
    "de",
    "do",
    "dos",
    "e",
    "em",
    "mas",
    "na",
    "nas",
    "no",
    "nos",
    "nem",
    "o",
    "os",
    "para",
    "por",
    "que",
    "um",
    "uma",
}


def _repair_joining_word_boundaries(
    groups: list[list[TranscriptWord]], preset: CaptionPreset
) -> None:
    for index in range(len(groups) - 1):
        current = groups[index]
        following = groups[index + 1]
        while (
            len(current) > preset.minimum_words
            and current[-1].text.casefold().strip(".,!?:;") in _JOINING_WORDS
            and len(following) < preset.maximum_words
        ):
            following.insert(0, current.pop())


def _ends_phrase(text: str) -> bool:
    return bool(re.search(r"[.!?,;:]$", text.strip()))


def _normalize_text(text: str) -> str:
    return " ".join(text.split())


def _estimated_width(text: str, font_size: int) -> float:
    units = 0.0
    for char in text:
        if char.isspace():
            units += 0.32
        elif char in "ilI.,'!|:;":
            units += 0.30
        elif char in "mwMW@%":
            units += 0.92
        elif char.isupper():
            units += 0.70
        else:
            units += 0.58
    return units * font_size


def _best_two_line_split(
    words: list[str], font_size: int, maximum_width: int
) -> tuple[str, str] | None:
    candidates = []
    for index in range(1, len(words)):
        left = " ".join(words[:index])
        right = " ".join(words[index:])
        left_width = _estimated_width(left, font_size)
        right_width = _estimated_width(right, font_size)
        if max(left_width, right_width) > maximum_width:
            continue
        penalty = abs(left_width - right_width)
        if words[index - 1].casefold().strip(".,!?:;") in _JOINING_WORDS:
            penalty += maximum_width
        candidates.append((penalty, left, right))
    if not candidates:
        return None
    _, left, right = min(candidates)
    return left, right


def _ass_time(seconds: float) -> str:
    centiseconds = max(0, round(seconds * 100))
    hours, remainder = divmod(centiseconds, 360000)
    minutes, remainder = divmod(remainder, 6000)
    whole_seconds, fraction = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{whole_seconds:02d}.{fraction:02d}"


def _escape_ass(text: str) -> str:
    return text.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}")


def _escape_filter_path(path: Path) -> str:
    return (
        str(path.resolve())
        .replace("\\", r"\\")
        .replace(":", r"\:")
        .replace("'", r"\'")
    )


def _fingerprint(base_checksum, transcript_checksum, selection, preset, font) -> str:
    payload = {
        "version": CAPTION_VERSION,
        "base_checksum": base_checksum,
        "transcript_checksum": transcript_checksum,
        "selection": asdict(selection),
        "preset": _preset_payload(preset),
        "font": asdict(font) | {"path": str(font.path)},
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _preset_payload(preset: CaptionPreset) -> dict[str, object]:
    return asdict(preset)


def _block_payload(block: CaptionBlock) -> dict[str, object]:
    return {
        "start": block.start,
        "end": block.end,
        "text": block.text,
        "lines": list(block.lines),
        "font_size": block.font_size,
        "words": [asdict(word) for word in block.words],
    }


def _run(command: list[str]) -> None:
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise RuntimeError(f"Executable not found: {command[0]}") from exc
    if result.returncode:
        raise RuntimeError(
            f"Command failed ({result.returncode}): {result.stderr[-3000:]}"
        )


def _decode_fully(path: Path, ffmpeg_path: str) -> None:
    _run([ffmpeg_path, "-v", "error", "-i", str(path), "-f", "null", "-"])


def _validate_media(media: MediaInfo, base: MediaInfo) -> None:
    if (media.width, media.height) != (1080, 1920):
        raise RuntimeError("Captioned output is not 1080x1920.")
    if not media.has_audio:
        raise RuntimeError("Captioned output lost its audio stream.")
    if abs(media.duration_seconds - base.duration_seconds) > 0.1:
        raise RuntimeError("Captioned output duration differs from its base video.")


def _validate_codecs(path: Path, ffprobe_path: str) -> tuple[str, str]:
    try:
        result = subprocess.run(
            [
                ffprobe_path,
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type,codec_name",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        streams = json.loads(result.stdout).get("streams", [])
        video = next(
            item["codec_name"]
            for item in streams
            if item["codec_type"] == "video"
        )
        audio = next(
            item["codec_name"]
            for item in streams
            if item["codec_type"] == "audio"
        )
    except (
        FileNotFoundError,
        subprocess.SubprocessError,
        ValueError,
        KeyError,
        StopIteration,
    ) as exc:
        raise RuntimeError("Could not validate captioned output codecs.") from exc
    if result.returncode or video != "h264" or audio != "aac":
        raise RuntimeError(
            f"Unexpected captioned codecs: video={video}, audio={audio}."
        )
    return video, audio


def _extract_samples(
    video: Path,
    blocks: tuple[CaptionBlock, ...],
    samples_dir: Path,
    *,
    ffmpeg_path: str,
) -> tuple[Path, ...]:
    samples_dir.mkdir(parents=True, exist_ok=True)
    indices = (0, len(blocks) // 2, len(blocks) - 1)
    paths = []
    for number, index in enumerate(indices, 1):
        block = blocks[index]
        timestamp = (block.start + block.end) / 2
        destination = samples_dir / f"sample-{number}.png"
        temporary = destination.with_suffix(".part.png")
        temporary.unlink(missing_ok=True)
        _run(
            [
                ffmpeg_path,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                f"{timestamp:.3f}",
                "-i",
                str(video),
                "-frames:v",
                "1",
                str(temporary),
            ]
        )
        temporary.replace(destination)
        paths.append(destination)
    return tuple(paths)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_json_atomic(path: Path, payload: object) -> None:
    _write_text_atomic(
        path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    )
