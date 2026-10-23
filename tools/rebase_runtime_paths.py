#!/usr/bin/env python3
"""Atomically rebase persisted AutoShorts paths after a runtime migration."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path
from uuid import uuid4


def _replace(value: object, old: str, new: str) -> tuple[object, int]:
    if isinstance(value, str):
        return value.replace(old, new), value.count(old)
    if isinstance(value, list):
        result, count = [], 0
        for item in value:
            replaced, matches = _replace(item, old, new)
            result.append(replaced)
            count += matches
        return result, count
    if isinstance(value, dict):
        result, count = {}, 0
        for key, item in value.items():
            replaced, matches = _replace(item, old, new)
            result[key] = replaced
            count += matches
        return result, count
    return value, 0


def _write_json(path: Path, payload: object) -> None:
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-db", type=Path, required=True)
    parser.add_argument("--json-root", type=Path, action="append", default=[])
    parser.add_argument("--old", required=True)
    parser.add_argument("--new", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not args.old.startswith("/") or not args.new.startswith("/"):
        raise SystemExit("Both path prefixes must be absolute.")
    database_matches = 0
    with sqlite3.connect(args.state_db) as connection:
        tables = connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        for (table,) in tables:
            columns = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
            for column in columns:
                name, declared = column[1], str(column[2]).upper()
                if "TEXT" not in declared:
                    continue
                count = connection.execute(
                    f'SELECT count(*) FROM "{table}" WHERE instr("{name}", ?) > 0',
                    (args.old,),
                ).fetchone()[0]
                database_matches += count
                if count and args.apply:
                    connection.execute(
                        f'UPDATE "{table}" SET "{name}"=replace("{name}", ?, ?) '
                        f'WHERE instr("{name}", ?) > 0',
                        (args.old, args.new, args.old),
                    )
        if args.apply:
            connection.commit()
        else:
            connection.rollback()
    json_matches = 0
    json_files = set()
    for root in args.json_root:
        if root.is_file() and root.suffix == ".json":
            json_files.add(root)
        elif root.is_dir():
            json_files.update(root.rglob("*.json"))
    for path in sorted(json_files):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        replaced, count = _replace(payload, args.old, args.new)
        json_matches += count
        if count and args.apply:
            _write_json(path, replaced)
    print(f"database_rows={database_matches}")
    print(f"json_values={json_matches}")
    print(f"applied={args.apply}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
