"""CLI for deterministic one-winner production."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from autoshorts.automation_config import load_automation_config
from autoshorts.captions import render_captioned_short
from autoshorts.config import load_settings
from autoshorts.faster_whisper_provider import FasterWhisperProvider
from autoshorts.gemini_analysis_provider import GeminiAnalysisProvider
from autoshorts.production import SegmentSelection, run_single_production
from autoshorts.state_store import StateStore, default_state_db_path
from autoshorts.transcription import load_transcript


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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "caption":
        return _caption(args)
    config = load_automation_config(args.config)
    with StateStore(args.state_db) as state:
        video = state.get_video(args.source_video_id)
        if video is None:
            raise SystemExit(f"Source video not found: {args.source_video_id}")
        try:
            theme = config.theme(video.theme_name)
        except KeyError:
            theme = next(
                (
                    item
                    for item in config.themes
                    if item.name.casefold() == video.theme_name.casefold()
                ),
                None,
            )
            if theme is None:
                raise SystemExit(f"Theme not found: {video.theme_name}") from None
        result = run_single_production(
            youtube_id=args.source_video_id,
            state=state,
            theme=theme,
            settings=load_settings(),
            transcription_provider=FasterWhisperProvider(language="pt"),
            analysis_provider=GeminiAnalysisProvider(),
            workspace=args.workspace.resolve(),
            dry_run=args.dry_run,
        )
    item = result.selection
    print(f"Source: {item.source_video_id}")
    print(
        f"Selected: {item.start_seconds:.3f}s - {item.end_seconds:.3f}s "
        f"({item.duration_seconds:.3f}s)"
    )
    print(f"Reason: {item.reason}")
    print(f"Context: {item.context}")
    print(f"Output: {result.output_path}")
    outcome = "DRY-RUN" if args.dry_run else "REUSED" if result.reused else "RENDERED"
    print(f"Result: {outcome}")
    return 0


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


if __name__ == "__main__":
    raise SystemExit(main())
