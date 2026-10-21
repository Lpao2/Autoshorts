"""Command line interface for inventory-driven orchestration."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from autoshorts.automation_config import load_automation_config
from autoshorts.collection import YouTubeAccess
from autoshorts.config import PROJECT_ROOT, load_settings
from autoshorts.orchestrator import Orchestrator, PipelineChannelProducer
from autoshorts.state_store import StateStore, default_state_db_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="autoshorts-orchestrator")
    parser.add_argument("--state-db", type=Path, default=default_state_db_path())
    parser.add_argument(
        "--config", type=Path, default=Path("config/automation.local.json")
    )
    parser.add_argument(
        "--workspace", type=Path, default=PROJECT_ROOT / "data/automation"
    )
    parser.add_argument("--youtube-browser", choices=("brave", "firefox"))
    parser.add_argument("--youtube-cookie-file", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    once = commands.add_parser("run-once")
    once.add_argument("--dry-run", action="store_true")
    once.add_argument("--max-productions", type=int, default=1)
    run = commands.add_parser("run")
    run.add_argument("--interval", type=int, default=300)
    run.add_argument("--max-productions", type=int, default=1)
    commands.add_parser("status")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_automation_config(args.config)
        with StateStore(args.state_db) as state:
            environmental = YouTubeAccess.from_environment()
            access = YouTubeAccess(
                browser=args.youtube_browser or environmental.browser,
                cookie_file=args.youtube_cookie_file or environmental.cookie_file,
                timeout_seconds=environmental.timeout_seconds,
                retries=environmental.retries,
                fragment_retries=environmental.fragment_retries,
                pacing_seconds=environmental.pacing_seconds,
            )
            producer = PipelineChannelProducer(
                config, state, load_settings(), args.workspace.resolve(), access
            )
            orchestrator = Orchestrator(config, state, producer)
            if args.command == "run":
                orchestrator.run(
                    interval_seconds=args.interval,
                    max_productions=args.max_productions,
                )
                return 0
            result = orchestrator.run_cycle(
                dry_run=args.command == "status" or args.dry_run,
                max_productions=(
                    0 if args.command == "status" else args.max_productions
                ),
            )
            _print_result(result)
            return 2 if result.errors else 0
    except (FileNotFoundError, KeyError, OSError, RuntimeError, ValueError) as exc:
        print(f"AutoShorts orchestrator failed: {exc}", file=sys.stderr)
        return 2


def _print_result(result) -> None:
    print("Channel | Inventory | Min | Target | Deficit | Next post | Action")
    for plan in result.plans:
        print(
            f"{plan.context.channel.name} | {plan.inventory.count} | "
            f"{plan.policy.min_ready_videos} | {plan.policy.target_ready_videos} | "
            f"{plan.deficit} | {plan.next_post or '—'} | {plan.action}"
        )
        for item in plan.inventory.items:
            print(f"  {item.stage}: {item.checksum} ({item.path})")
    for channel, outcome in result.outcomes:
        print(
            f"PRODUCED {channel}: source={outcome.source_video_id} "
            f"draft={outcome.metadata_draft_id} result={outcome.result}"
        )
    for channel, error in result.errors:
        print(f"FAILED {channel}: {error}")
    print(f"Productions started: {result.productions_started}")


if __name__ == "__main__":
    raise SystemExit(main())
