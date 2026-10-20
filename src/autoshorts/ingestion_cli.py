"""CLI for one-at-a-time market-aware source ingestion."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from autoshorts.collection import YouTubeAccess
from autoshorts.config import PROJECT_ROOT, load_settings
from autoshorts.ingestion import discover_next_source_video, ingest
from autoshorts.state_store import StateStore, default_state_db_path


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AutoShorts source ingestion")
    parser.add_argument("--state-db", type=Path, default=default_state_db_path())
    parser.add_argument(
        "--workspace", type=Path, default=PROJECT_ROOT / "data/automation"
    )
    command = parser.add_subparsers(dest="command", required=True).add_parser("run")
    command.add_argument("--theme", required=True)
    command.add_argument("--platform", default="youtube")
    command.add_argument("--region", default="BR")
    command.add_argument("--language", default="pt-BR")
    command.add_argument("--limit", type=int, choices=(1,), default=1)
    command.add_argument("--per-source", type=int, default=5)
    command.add_argument("--maximum-age-days", type=int, default=3650)
    command.add_argument("--browser", choices=("brave", "firefox"))
    command.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = create_parser().parse_args(argv)
    try:
        with StateStore(args.state_db) as state:
            profile = state.theme_market_profile(
                args.theme, args.platform, args.region, args.language
            )
            if profile is None or not profile.enabled:
                raise ValueError("Enabled ThemeMarketProfile not found.")
            access = YouTubeAccess(args.browser)
            report = discover_next_source_video(
                profile,
                state,
                per_source=args.per_source,
                maximum_age_days=args.maximum_age_days,
                access=access,
            )
            _print_report(report)
            if args.dry_run or report.selected is None:
                return 0 if report.selected is not None else 3
            settings = load_settings()
            result = ingest(
                report.selected,
                profile,
                state,
                workspace=args.workspace,
                ffprobe_path=settings.ffprobe_path,
                ffmpeg_path=settings.ffmpeg_path,
                access=access,
            )
            record = state.get_video(report.selected.external_video_id)
            print(f"Local path: {result.local_path}")
            print(f"Checksum: {result.checksum}")
            print(f"FFprobe: PASS ({result.media.duration_seconds:.3f}s)")
            print("Decode: PASS")
            print(f"State: {record.status if record else 'MISSING'}")
            return 0
    except (FileNotFoundError, KeyError, OSError, RuntimeError, ValueError) as exc:
        print(f"AutoShorts ingestion failed: {exc}", file=sys.stderr)
        return 2


def _print_report(report) -> None:
    print(
        f"Theme: {report.profile.theme_key} | Market: "
        f"{report.profile.region_code}/{report.profile.language_code}"
    )
    print(f"Sources considered: {len(report.sources)}")
    for association, source in report.sources:
        print(
            f"SOURCE priority={association.priority} name={source.display_name} "
            f"channel_id={source.external_channel_id} handle={source.handle or '—'}"
        )
    print(f"Uploads examined: {report.examined}")
    print(f"Eligible: {len(report.eligible)}")
    for item in report.skipped:
        print(
            f"SKIP source={item.source} video_id={item.external_video_id or '—'} "
            f"reason={item.reason} detail={item.detail} title={item.title}"
        )
    if report.selected is None:
        print("Selected: NONE")
        return
    item = report.selected
    print(f"Selected creator: {item.source.display_name}")
    print(f"Source Channel ID: {item.source.external_channel_id}")
    print(f"Video: {item.video.title}")
    print(f"YouTube Video ID: {item.external_video_id}")
    print(f"URL: {item.video.url}")
    print(f"Published: {item.video.upload_date or '—'}")
    print(f"Duration: {item.video.duration_seconds}")


if __name__ == "__main__":
    raise SystemExit(main())
