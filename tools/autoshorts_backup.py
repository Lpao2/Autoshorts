#!/usr/bin/env python3
"""Create a restricted, bounded backup of essential AutoShorts state."""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--keep", type=int, default=7)
    args = parser.parse_args()
    runtime = args.runtime.resolve()
    root = runtime / "backups"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    target = root / datetime.now(UTC).strftime("backup-%Y%m%dT%H%M%SZ")
    target.mkdir(mode=0o700)
    source_db = runtime / "data" / "autoshorts.db"
    with sqlite3.connect(source_db) as source, sqlite3.connect(
        target / "autoshorts.db"
    ) as destination:
        source.backup(destination)
    for source, name in (
        (runtime / "config", "config"),
        (runtime / "credentials", "credentials"),
        (runtime / "data" / "branding", "branding"),
    ):
        if source.exists():
            shutil.copytree(source, target / name)
    for path in target.rglob("*"):
        os.chmod(path, 0o700 if path.is_dir() else 0o600)
    backups = sorted(path for path in root.glob("backup-*") if path.is_dir())
    for expired in backups[: max(0, len(backups) - args.keep)]:
        shutil.rmtree(expired)
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
