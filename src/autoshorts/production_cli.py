"""CLI for deterministic one-winner production."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from autoshorts.automation_config import load_automation_config
from autoshorts.captions import render_captioned_short
from autoshorts.config import load_settings
from autoshorts.credential_store import FilesystemCredentialStore
from autoshorts.faster_whisper_provider import FasterWhisperProvider
from autoshorts.gemini_analysis_provider import GeminiAnalysisProvider
from autoshorts.metadata_pipeline import (
    GeminiMetadataProvider,
    approve_and_enqueue_metadata,
    generate_metadata_draft,
)
from autoshorts.production import SegmentSelection, produce_source_video
from autoshorts.scheduler import SchedulerService
from autoshorts.state_store import StateStore, default_state_db_path
from autoshorts.transcription import load_transcript
from autoshorts.video_processor import render_crop_preview
from autoshorts.youtube_publishing import YouTubePublicationService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="autoshorts-produce")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--source-video-id", required=True)
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--state-db", type=Path, default=default_state_db_path())
    run.add_argument(
        "--config", type=Path, default=Path("config/automation.local.json")
    )
    run.add_argument("--workspace", type=Path, default=Path("data/automation"))
    caption = sub.add_parser("caption")
    caption.add_argument("--source-video-id", required=True)
    caption.add_argument("--state-db", type=Path, default=default_state_db_path())
    preview = sub.add_parser("preview-crop")
    preview.add_argument("--source-video-id", required=True)
    preview.add_argument("--seconds", type=float, default=6.0)
    preview.add_argument("--output", type=Path)
    preview.add_argument("--state-db", type=Path, default=default_state_db_path())
    preview.add_argument(
        "--config", type=Path, default=Path("config/automation.local.json")
    )
    metadata = sub.add_parser("metadata")
    metadata.add_argument("--source-video-id", required=True)
    metadata.add_argument("--channel", required=True)
    metadata.add_argument("--state-db", type=Path, default=default_state_db_path())
    metadata.add_argument(
        "--config", type=Path, default=Path("config/automation.local.json")
    )
    approve = sub.add_parser("approve-metadata")
    approve.add_argument("draft_id", type=int)
    approve.add_argument("--state-db", type=Path, default=default_state_db_path())
    approve.add_argument(
        "--config", type=Path, default=Path("config/automation.local.json")
    )
    approve.add_argument("--title")
    approve.add_argument("--description")
    approve.add_argument("--tag", action="append", default=[])
    approve.add_argument("--hashtag", action="append", default=[])
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "caption":
        return _caption(args)
    if args.command == "preview-crop":
        return _preview_crop(args)
    if args.command == "metadata":
        return _metadata(args)
    if args.command == "approve-metadata":
        return _approve_metadata(args)
    config = load_automation_config(args.config)
    with StateStore(args.state_db) as state:
        video = state.get_video(args.source_video_id)
        if video is None:
            raise SystemExit(f"Source video not found: {args.source_video_id}")
        theme = _resolve_video_theme(config, state, video)
        result = produce_source_video(
            youtube_id=args.source_video_id,
            state=state,
            theme=theme,
            settings=load_settings(),
            transcription_provider=FasterWhisperProvider(language="pt"),
            analysis_provider=GeminiAnalysisProvider(),
            workspace=args.workspace.resolve(),
            dry_run=args.dry_run,
        )
    item = result.base.selection
    print(f"Source: {item.source_video_id}")
    print(
        f"Selected: {item.start_seconds:.3f}s - {item.end_seconds:.3f}s "
        f"({item.duration_seconds:.3f}s)"
    )
    print(f"Reason: {item.reason}")
    print(f"Context: {item.context}")
    print(f"Base output: {result.base.output_path}")
    if result.ready_artifact is not None:
        print(f"Final artifact: {result.ready_artifact.file_path}")
        print("State: READY")
    outcome = "DRY-RUN" if args.dry_run else "REUSED" if result.reused else "READY"
    print(f"Result: {outcome}")
    return 0


def _resolve_video_theme(config, state, video):
    row = state.connection.execute(
        """SELECT DISTINCT profile.theme_key
        FROM theme_content_sources link
        JOIN theme_market_profiles profile ON profile.id=link.profile_id
        WHERE link.source_id=? AND link.enabled=1 AND profile.enabled=1""",
        (video.content_source_id,),
    ).fetchall()
    keys = {item["theme_key"] for item in row}
    if len(keys) > 1:
        raise SystemExit(f"Ambiguous source theme: {video.youtube_id}")
    theme_key = next(iter(keys), video.theme_name)
    theme = next(
        (
            item
            for item in config.themes
            if theme_key
            in {
                item.theme_id,
                item.name,
                item.name.casefold(),
                item.output_subdirectory,
            }
        ),
        None,
    )
    if theme is None:
        raise SystemExit(f"Theme not found: {theme_key}")
    if video.theme_name.casefold() not in {
        theme.name.casefold(),
        theme.output_subdirectory.casefold(),
    }:
        raise SystemExit(f"Source theme mismatch: {video.youtube_id}")
    return theme


def _caption(args) -> int:
    with StateStore(args.state_db) as state:
        video = state.get_video(args.source_video_id)
        transcript_record = state.get_transcript(args.source_video_id)
        shorts = state.shorts_for_video(args.source_video_id)
        if video is None or transcript_record is None or len(shorts) != 1:
            raise SystemExit("Captioning requires one produced Short and transcript.")
        selection_path = transcript_record.file_path.parent / "selection.json"
        if not selection_path.is_file():
            raise SystemExit(f"Selection artifact not found: {selection_path}")
        selection = SegmentSelection(
            **json.loads(selection_path.read_text(encoding="utf-8"))
        )
        transcript = load_transcript(
            transcript_record.file_path, youtube_id=video.youtube_id
        )
        settings = load_settings()
        result = render_captioned_short(
            base_video=shorts[0].file_path,
            transcript=transcript,
            selection=selection,
            transcript_path=transcript_record.file_path,
            ffmpeg_path=settings.ffmpeg_path,
            ffprobe_path=settings.ffprobe_path,
        )
    print(f"Transcript: REUSED ({transcript_record.file_path})")
    print(f"Caption blocks: {len(result.blocks)}")
    print(f"Font: {result.font.family} {result.font.style} ({result.font.path})")
    print(f"Subtitle: {result.subtitle_path}")
    print(f"Video: {result.video_path}")
    print(f"SHA-256: {result.checksum}")
    print(f"Result: {'REUSED' if result.reused else 'RENDERED'}")
    return 0


def _preview_crop(args) -> int:
    config = load_automation_config(args.config)
    settings = load_settings()
    with StateStore(args.state_db) as state:
        video = state.get_video(args.source_video_id)
        transcript = state.get_transcript(args.source_video_id)
        if video is None or video.local_path is None or transcript is None:
            raise SystemExit("Crop preview requires a downloaded, transcribed source.")
        theme = _resolve_video_theme(config, state, video)
        selection_path = transcript.file_path.parent / "selection.json"
        if not selection_path.is_file():
            raise SystemExit(f"Selection artifact not found: {selection_path}")
        selection = SegmentSelection(
            **json.loads(selection_path.read_text(encoding="utf-8"))
        )
        duration = min(args.seconds, selection.duration_seconds)
        output = args.output or (
            Path("data/automation")
            / theme.output_subdirectory
            / "previews"
            / f"{video.youtube_id}-{theme.visual_profile.crop_preset}.mp4"
        )
        media = render_crop_preview(
            video.local_path,
            output.resolve(),
            start_seconds=selection.start_seconds,
            duration_seconds=duration,
            settings=settings,
            visual_profile=theme.visual_profile,
        )
    print(f"Theme: {theme.output_subdirectory}")
    print(f"Visual profile: {theme.visual_profile.crop_preset}")
    print(f"Preview: {media.path}")
    print(f"Media: {media.width}x{media.height} {media.duration_seconds:.3f}s")
    return 0


def _metadata(args) -> int:
    config = load_automation_config(args.config)
    channel = _find_channel(config, args.channel)
    with StateStore(args.state_db) as state:
        draft = generate_metadata_draft(
            youtube_id=args.source_video_id,
            publishing_channel_id=channel.channel_id,
            config=config,
            state=state,
            provider=GeminiMetadataProvider(),
        )
    print(f"Draft ID: {draft.draft_id}")
    print(f"Status: {draft.status}")
    print(f"Review policy: {draft.review_policy}")
    print(f"Title: {draft.title}")
    print(f"Description: {draft.description}")
    print(f"Tags: {', '.join(draft.tags)}")
    return 0


def _approve_metadata(args) -> int:
    config = load_automation_config(args.config)
    with StateStore(args.state_db) as state:
        overrides = (args.title, args.description, args.tag, args.hashtag)
        if any(overrides):
            if args.title is None or args.description is None or not args.tag:
                raise SystemExit(
                    "Metadata revision requires --title, --description "
                    "and at least one --tag."
                )
            state.revise_metadata_draft(
                args.draft_id,
                title=args.title,
                description=args.description,
                tags=tuple(args.tag),
                hashtags=tuple(args.hashtag),
            )
        publication_service = YouTubePublicationService(
            config, state, FilesystemCredentialStore()
        )
        scheduler = SchedulerService(config, state, _NoUploadExecutor())
        draft, job = approve_and_enqueue_metadata(
            args.draft_id,
            state=state,
            publication_service=publication_service,
            scheduler=scheduler,
            now=datetime.now(UTC),
        )
    print(f"Draft ID: {draft.draft_id} status={draft.status}")
    print(f"Publication ID: {draft.publication_id}")
    print(f"Scheduler Job ID: {job.job_id} state={job.state}")
    print(f"Scheduled for: {job.resolved_publish_at}")
    return 0


class _NoUploadExecutor:
    def execute(self, publication_id):
        raise RuntimeError("The metadata CLI never executes scheduler uploads.")


def _find_channel(config, value):
    matches = [
        channel
        for theme in config.themes
        for channel in theme.channels
        if channel.channel_id == value or channel.name.casefold() == value.casefold()
    ]
    if len(matches) != 1:
        raise ValueError(f"Publishing channel is ambiguous or missing: {value}")
    return matches[0]


if __name__ == "__main__":
    raise SystemExit(main())
