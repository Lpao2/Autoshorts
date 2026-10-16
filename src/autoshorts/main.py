"""Command-line entry point for the AutoShorts local pipeline."""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path
from typing import cast

from autoshorts.config import OverwritePolicy, load_settings
from autoshorts.pipeline import PipelineExecutionError, run_pipeline
from autoshorts.timestamp_parser import load_cuts


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate, cut, and render selected moments as vertical short-form video."
        )
    )
    parser.add_argument("--input", required=True, type=Path, help="Source video.")
    parser.add_argument(
        "--cuts", required=True, type=Path, help="Text file with selected timestamps."
    )
    parser.add_argument("--output", type=Path, help="Output directory.")
    parser.add_argument(
        "--overwrite",
        choices=("error", "skip", "replace"),
        help="Behavior when a generated filename already exists.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        help="Maximum concurrent FFmpeg renders.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Probe and validate the complete plan without rendering clips.",
    )
    return parser


def main() -> int:
    parser = create_parser()
    arguments = parser.parse_args()

    try:
        settings = load_settings()
        if arguments.output is not None:
            settings = replace(settings, output_dir=arguments.output.resolve())
        if arguments.overwrite is not None:
            settings = replace(
                settings,
                overwrite_policy=cast(OverwritePolicy, arguments.overwrite),
            )
        if arguments.workers is not None:
            if not 1 <= arguments.workers <= 32:
                parser.error("--workers must be between 1 and 32")
            settings = replace(settings, max_workers=arguments.workers)

        clips = load_cuts(arguments.cuts)
        result = run_pipeline(
            arguments.input.resolve(), clips, settings, dry_run=arguments.dry_run
        )
    except PipelineExecutionError as exc:
        print(f"AutoShorts failed: {exc}", file=sys.stderr)
        print(f"Diagnostic manifest: {exc.manifest_path}", file=sys.stderr)
        return 2
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"AutoShorts failed: {exc}", file=sys.stderr)
        return 2

    print()
    print("AutoShorts — Local Processing Pipeline")
    print("--------------------------------------")
    print(f"Source: {result.plan.source.path}")
    print(
        "Media: "
        f"{result.plan.source.width}x{result.plan.source.height}, "
        f"{result.plan.source.duration_seconds:.2f}s, "
        f"audio={'yes' if result.plan.source.has_audio else 'no'}"
    )
    print(f"Output: {settings.output_dir}")
    print(f"Clips: {len(result.plan.clips)}")
    print(f"Mode: {'dry-run' if arguments.dry_run else 'render'}")

    for item in result.plan.clips:
        print(
            f" [{item.index:02d}] {item.clip.start_seconds}s + "
            f"{item.duration_seconds:.2f}s -> {item.output_path.name}"
        )

    if result.renders:
        generated = sum(item.status == "generated" for item in result.renders)
        skipped = sum(item.status == "skipped" for item in result.renders)
        print(f"Generated: {generated}; skipped: {skipped}")

    print(f"Manifest: {result.manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
