"""Deterministic one-winner production for an already ingested source video."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from autoshorts.analysis_service import analyze_transcript
from autoshorts.automation_config import ThemeProfile
from autoshorts.config import Settings
from autoshorts.media_probe import MediaInfo, probe_media
from autoshorts.render_plan import build_render_plan
from autoshorts.state_store import (
    ReadyArtifact,
    RenderedShort,
    StateStore,
    VideoStatus,
)
from autoshorts.transcript_analysis import (
    ResolvedAnalysis,
    TranscriptAnalysisProvider,
    load_analysis,
)
from autoshorts.transcription import Transcript, TranscriptionProvider, load_transcript
from autoshorts.transcription_service import transcript_video
from autoshorts.video_processor import render_planned_clip

if TYPE_CHECKING:
    from autoshorts.captions import CaptionResult

PRODUCTION_VERSION = "single-winner-v1"


@dataclass(frozen=True, slots=True)
class SegmentSelection:
    source_video_id: str
    start_seconds: float
    end_seconds: float
    duration_seconds: float
    title: str
    headline: str
    reason: str
    score: float
    selected_at: str
    method: str
    version: str
    context: str


@dataclass(frozen=True, slots=True)
class ProductionResult:
    selection: SegmentSelection
    output_path: Path
    manifest_path: Path
    media: MediaInfo | None
    checksum: str | None
    rendered: bool
    reused: bool


@dataclass(frozen=True, slots=True)
class ReadyProductionResult:
    base: ProductionResult
    final: CaptionResult | None
    ready_artifact: ReadyArtifact | None
    reused: bool


def run_single_production(
    *,
    youtube_id: str,
    state: StateStore,
    theme: ThemeProfile,
    settings: Settings,
    transcription_provider: TranscriptionProvider,
    analysis_provider: TranscriptAnalysisProvider,
    workspace: Path,
    dry_run: bool = False,
) -> ProductionResult:
    video = state.get_video(youtube_id)
    if video is None or video.local_path is None:
        raise ValueError(f"Downloaded source not found in state: {youtube_id}")
    if not video.local_path.is_file():
        raise FileNotFoundError(video.local_path)
    work = workspace / theme.output_subdirectory / ".work" / youtube_id
    output_dir = (
        workspace
        / theme.output_subdirectory
        / "outputs"
        / _safe(video.creator_name)
    )
    transcript_path = work / "transcript.json"
    analysis_path = work / "analysis.json"
    selection_path = work / "selection.json"
    visual_fingerprint = _visual_fingerprint(theme)
    manifest_path = work / f"production-{visual_fingerprint}.json"

    transcript = _get_transcript(
        state, video, transcript_path, transcription_provider
    )
    analysis = _get_analysis(
        state, video, transcript, analysis_path, analysis_provider, theme
    )
    selection = _get_selection(selection_path, youtube_id, transcript, analysis)
    current = state.get_video(youtube_id)
    assert current is not None
    if current.status == VideoStatus.ANALYZED:
        state.transition_video(youtube_id, VideoStatus.SEGMENT_SELECTED)

    output_path = output_dir / (
        f"01_{_safe(selection.title)}_visual-{visual_fingerprint}.mp4"
    )
    if dry_run:
        return ProductionResult(
            selection, output_path, manifest_path, None, None, False, False
        )

    current = state.get_video(youtube_id)
    assert current is not None
    shorts = state.shorts_for_video(youtube_id)
    recalibrating_ready = current.status is VideoStatus.READY
    if current.status in {
        VideoStatus.RENDERED,
        VideoStatus.CAPTIONED,
        VideoStatus.READY,
    }:
        if (
            len(shorts) == 1
            and shorts[0].file_path == output_path
            and output_path.is_file()
        ):
            media = probe_media(output_path, settings.ffprobe_path)
            checksum = _sha256(output_path)
            _validate_manifest(manifest_path, selection, output_path, checksum)
            return ProductionResult(
                selection, output_path, manifest_path, media, checksum, False, True
            )
        if not recalibrating_ready:
            raise RuntimeError(
                "Rendered state is inconsistent; refusing duplicate output."
            )

    if not recalibrating_ready:
        state.transition_video(youtube_id, VideoStatus.RENDERING)
    render_settings = replace(
        settings,
        output_dir=output_dir,
        extra_clip_seconds=0,
        max_workers=1,
        overwrite_policy="skip" if recalibrating_ready else "error",
    )
    plan = build_render_plan(
        probe_media(video.local_path, settings.ffprobe_path),
        [_selection_clip(selection)],
        render_settings,
    )
    planned = replace(plan.clips[0], output_path=output_path)
    result = render_planned_clip(
        video.local_path,
        planned,
        render_settings,
        visual_profile=theme.visual_profile,
    )
    _decode_fully(result.output_path, settings.ffmpeg_path)
    media = probe_media(result.output_path, settings.ffprobe_path)
    checksum = _sha256(result.output_path)
    rendered_short = RenderedShort(
        1,
        result.output_path,
        selection.title,
        selection.headline,
        selection.score,
        candidate_id=f"{youtube_id}:winner:1",
        duration_seconds=selection.duration_seconds,
    )
    if recalibrating_ready:
        state.replace_ready_rendered_short(youtube_id, rendered_short)
    else:
        state.record_single_rendered_short(youtube_id, rendered_short)
    _write_json_atomic(
        manifest_path,
        {
            "schema_version": 1,
            "production_version": PRODUCTION_VERSION,
            "visual_profile": asdict(theme.visual_profile),
            "visual_fingerprint": visual_fingerprint,
            "selection": asdict(selection),
            "output_path": str(result.output_path),
            "checksum_sha256": checksum,
            "media": {
                "duration_seconds": media.duration_seconds,
                "width": media.width,
                "height": media.height,
                "video_codec": settings.video_codec,
                "audio_codec": settings.audio_codec,
            },
            "validated_at": datetime.now(UTC).isoformat(),
        },
    )
    return ProductionResult(
        selection, result.output_path, manifest_path, media, checksum, True, False
    )


def produce_source_video(
    *,
    youtube_id: str,
    state: StateStore,
    theme: ThemeProfile,
    settings: Settings,
    transcription_provider: TranscriptionProvider,
    analysis_provider: TranscriptAnalysisProvider,
    workspace: Path,
    dry_run: bool = False,
) -> ReadyProductionResult:
    """Take one downloaded source through the reusable pipeline to READY."""
    base = run_single_production(
        youtube_id=youtube_id,
        state=state,
        theme=theme,
        settings=settings,
        transcription_provider=transcription_provider,
        analysis_provider=analysis_provider,
        workspace=workspace,
        dry_run=dry_run,
    )
    if dry_run:
        return ReadyProductionResult(base, None, None, base.reused)

    from autoshorts.captions import (
        DEFAULT_CAPTION_PRESET,
        SafeArea,
        render_captioned_short,
    )

    transcript_record = state.get_transcript(youtube_id)
    if transcript_record is None:
        raise RuntimeError("READY production requires a persisted transcript.")
    transcript = load_transcript(transcript_record.file_path, youtube_id=youtube_id)
    captioned = render_captioned_short(
        base_video=base.output_path,
        transcript=transcript,
        selection=base.selection,
        transcript_path=transcript_record.file_path,
        ffmpeg_path=settings.ffmpeg_path,
        ffprobe_path=settings.ffprobe_path,
        preset=replace(
            DEFAULT_CAPTION_PRESET,
            name=theme.visual_profile.caption_preset,
            safe_area=SafeArea(
                margin_top=theme.visual_profile.safe_top,
                margin_bottom=theme.visual_profile.safe_bottom,
            ),
        ),
    )
    manifest = json.loads(captioned.manifest_path.read_text(encoding="utf-8"))
    fingerprint = str(manifest["fingerprint"])
    previous = state.get_ready_artifact(youtube_id)
    same_ready = bool(
        previous
        and previous.file_path == captioned.video_path
        and previous.checksum == captioned.checksum
        and previous.caption_fingerprint == fingerprint
    )
    if same_ready:
        assert previous is not None
        ready = previous
    else:
        ready = state.record_ready_artifact(
            youtube_id,
            file_path=captioned.video_path,
            checksum=captioned.checksum,
            manifest_path=captioned.manifest_path,
            subtitle_path=captioned.subtitle_path,
            caption_fingerprint=fingerprint,
        )
    return ReadyProductionResult(
        base,
        captioned,
        ready,
        base.reused and captioned.reused and same_ready,
    )


def _get_transcript(state, video, path, provider) -> Transcript:
    record = state.get_transcript(video.youtube_id)
    if record is not None:
        return load_transcript(record.file_path, youtube_id=video.youtube_id)
    if video.status != VideoStatus.DOWNLOADED:
        raise RuntimeError(f"Cannot transcribe from state {video.status}.")
    return transcript_video(
        youtube_id=video.youtube_id,
        source_path=video.local_path,
        artifact_path=path,
        provider=provider,
        state_store=state,
        creator=video.creator_name,
        title=video.title,
        url=video.source_url,
    )


def _get_analysis(state, video, transcript, path, provider, theme) -> ResolvedAnalysis:
    record = state.get_analysis(video.youtube_id)
    if record is not None:
        return load_analysis(record.file_path, transcript, youtube_id=video.youtube_id)
    current = state.get_video(video.youtube_id)
    if current is None or current.status not in {
        VideoStatus.TRANSCRIBED,
        VideoStatus.ANALYZING,
    }:
        status = current.status if current else None
        raise RuntimeError(f"Cannot analyze from state {status}.")
    return analyze_transcript(
        youtube_id=video.youtube_id,
        transcript=transcript,
        artifact_path=path,
        provider=provider,
        state_store=state,
        title=video.title,
        creator=video.creator_name,
        theme=theme.name,
        theme_prompt=theme.prompt,
    )


def _get_selection(path, youtube_id, transcript, analysis) -> SegmentSelection:
    if path.is_file():
        raw = json.loads(path.read_text(encoding="utf-8"))
        selection = SegmentSelection(**raw)
        if (
            selection.source_video_id != youtube_id
            or selection.version != PRODUCTION_VERSION
        ):
            raise ValueError("Persisted selection does not match this production run.")
        return selection
    winner = analysis.result.selections[0]
    clip = analysis.clips.clips[0]
    start_segment = transcript.segments[winner.start_segment]
    end_segment = transcript.segments[winner.end_segment]
    context = " ".join(
        item.text.strip()
        for item in transcript.segments[winner.start_segment : winner.end_segment + 1]
    ).strip()
    timestamps_match = (
        abs(clip.start_seconds - start_segment.start) <= 0.001
        and abs(clip.end_seconds - end_segment.end) <= 0.001
    )
    if not timestamps_match:
        raise ValueError("Resolved timestamps do not match transcript segment bounds.")
    selection = SegmentSelection(
        youtube_id,
        clip.start_seconds,
        clip.end_seconds,
        clip.duration_seconds,
        winner.title.strip(),
        winner.headline.strip(),
        winner.reason.strip(),
        winner.score,
        datetime.now(UTC).isoformat(),
        "gemini-ranked-transcript-segments",
        PRODUCTION_VERSION,
        context,
    )
    _write_json_atomic(path, asdict(selection))
    return selection


def _selection_clip(selection):
    from autoshorts.timestamp_parser import Clip

    return Clip(selection.start_seconds, selection.end_seconds, selection.title)


def _safe(value: str) -> str:
    from autoshorts.naming import sanitize_filename

    return sanitize_filename(value)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _visual_fingerprint(theme: ThemeProfile) -> str:
    payload = json.dumps(asdict(theme.visual_profile), sort_keys=True).encode()
    return hashlib.sha256(payload).hexdigest()[:8]


def _decode_fully(path: Path, ffmpeg: str) -> None:
    result = subprocess.run(
        [ffmpeg, "-v", "error", "-i", str(path), "-f", "null", "-"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"Full decode validation failed: {result.stderr[-2000:]}")


def _write_json_atomic(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _validate_manifest(path, selection, output, checksum) -> None:
    if not path.is_file():
        raise RuntimeError("Production manifest is missing.")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if (
        raw.get("selection") != asdict(selection)
        or raw.get("output_path") != str(output)
        or raw.get("checksum_sha256") != checksum
    ):
        raise RuntimeError("Production manifest does not match persisted output.")
