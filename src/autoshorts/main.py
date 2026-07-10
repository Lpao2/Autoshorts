"""Command-line entry point for the AutoShorts public pipeline."""

from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path

from autoshorts.config import load_settings
from autoshorts.timestamp_parser import load_cuts
from autoshorts.video_processor import process_clips


def create_parser() -> argparse.ArgumentParser:
    """Create the command-line argument parser."""

    parser = argparse.ArgumentParser(
        description=(
            "Cut selected moments from a video and convert them "
            "into vertical short-form videos."
        )
    )

    parser.add_argument(
        "--input",
        required=True,
        type=Path,
        help="Path to the source video.",
    )

    parser.add_argument(
        "--cuts",
        required=True,
        type=Path,
        help="Text file containing the selected timestamps.",
    )

    parser.add_argument(
        "--output",
        type=Path,
        help="Optional output directory.",
    )

    return parser


def main() -> int:
    """Execute the local AutoShorts processing pipeline."""

    parser = create_parser()
    arguments = parser.parse_args()

    settings = load_settings()

    if arguments.output is not None:
        settings = replace(
            settings,
            output_dir=arguments.output,
        )

    clips = load_cuts(arguments.cuts)

    print()
    print("AutoShorts — Local Processing Pipeline")
    print("--------------------------------------")
    print(f"Input video: {arguments.input}")
    print(f"Clip definitions: {arguments.cuts}")
    print(f"Output directory: {settings.output_dir}")
    print(f"Selected clips: {len(clips)}")
    print()

    outputs = process_clips(
        input_video=arguments.input,
        clips=clips,
        settings=settings,
    )

    print()
    print("Pipeline completed successfully.")
    print(f"Generated videos: {len(outputs)}")

    for output in outputs:
        print(f" - {output}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
