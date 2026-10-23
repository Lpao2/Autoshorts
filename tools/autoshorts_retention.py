#!/usr/bin/env python3
"""Conservative cleanup of unambiguously disposable runtime artifacts."""

from __future__ import annotations

import argparse
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    runtime = args.runtime.resolve()
    now = time.time()
    candidates: list[Path] = []
    for path in (runtime / "data").rglob("*.part.mp4"):
        if now - path.stat().st_mtime > 24 * 3600:
            candidates.append(path)
    previews = runtime / "data" / "automation"
    for path in previews.glob("*/previews/*.mp4"):
        if now - path.stat().st_mtime > 7 * 24 * 3600:
            candidates.append(path)
    for path in candidates:
        print(path)
        if not args.dry_run:
            path.unlink(missing_ok=True)
    removed = 0 if args.dry_run else len(candidates)
    print(f"removed={removed} candidates={len(candidates)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
