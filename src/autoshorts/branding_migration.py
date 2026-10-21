"""Safe migration from BrandPlan-ID branding folders to human-readable names."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from autoshorts.brand_planning import branding_directory


@dataclass(frozen=True, slots=True)
class BrandingMigrationReport:
    folders_migrated: int
    sqlite_paths_updated: int
    manifests_updated: int
    files_verified: int


def migrate_branding_directories(project_root: Path, state) -> BrandingMigrationReport:
    """Move every legacy branding directory and atomically update references."""
    root = project_root.resolve()
    plans = state.planned_channel_brands()
    moves: list[tuple[Path, Path]] = []
    for plan in plans:
        source = root / "data" / "branding" / plan.theme_key / plan.brand_plan_id
        target = branding_directory(root, plan, plans)
        if source == target or not source.exists():
            continue
        if target.exists():
            raise FileExistsError(f"Branding migration target already exists: {target}")
        moves.append((source, target))

    before = {
        path.relative_to(root): _checksum(path)
        for source, _target in moves
        for path in source.rglob("*")
        if path.is_file()
    }
    manifest_backups: dict[Path, bytes] = {}
    paths_updated = 0
    manifests_updated = 0
    completed: list[tuple[Path, Path]] = []
    try:
        for source, target in moves:
            target.parent.mkdir(parents=True, exist_ok=True)
            source.rename(target)
            completed.append((source, target))

        state.connection.execute("BEGIN IMMEDIATE")
        rows = state.connection.execute(
            """SELECT id, file_path, preview_path, delivery_path,
                      source_preserved_path
               FROM brand_asset_metadata"""
        ).fetchall()
        columns = (
            "file_path",
            "preview_path",
            "delivery_path",
            "source_preserved_path",
        )
        for row in rows:
            values = {column: row[column] for column in columns}
            changed = False
            for source, target in moves:
                for column in columns:
                    rewritten = _rewrite_path(values[column], source, target)
                    if rewritten != values[column]:
                        values[column] = rewritten
                        paths_updated += 1
                        changed = True
            if changed:
                state.connection.execute(
                    """UPDATE brand_asset_metadata
                       SET file_path=?, preview_path=?, delivery_path=?,
                           source_preserved_path=? WHERE id=?""",
                    (*(values[column] for column in columns), row["id"]),
                )

        for _source, target in moves:
            for manifest in target.rglob("manifest.json"):
                original = manifest.read_bytes()
                payload = json.loads(original)
                rewritten = _rewrite_payload(payload, moves)
                if rewritten == payload:
                    continue
                manifest_backups[manifest] = original
                temporary = manifest.with_suffix(".tmp")
                temporary.write_text(
                    json.dumps(rewritten, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                os.replace(temporary, manifest)
                manifests_updated += 1

        after = {
            _post_migration_relative(path, moves, root): _checksum(path)
            for _source, target in moves
            for path in target.rglob("*")
            if path.is_file()
        }
        expected_after = {
            _post_migration_relative(root / relative, moves, root): digest
            for relative, digest in before.items()
        }
        # Manifests intentionally change because their embedded paths changed.
        expected_binary = {
            path: digest
            for path, digest in expected_after.items()
            if path.name != "manifest.json"
        }
        actual_binary = {
            path: digest
            for path, digest in after.items()
            if path.name != "manifest.json"
        }
        if actual_binary != expected_binary:
            raise RuntimeError("Branding file checksum changed during migration.")
        state.connection.commit()
    except Exception:
        state.connection.rollback()
        for manifest, contents in manifest_backups.items():
            manifest.write_bytes(contents)
        for source, target in reversed(completed):
            if target.exists() and not source.exists():
                target.rename(source)
        raise

    return BrandingMigrationReport(
        len(moves), paths_updated, manifests_updated, len(before)
    )


def _rewrite_path(value: str | None, source: Path, target: Path) -> str | None:
    if value is None:
        return None
    path = Path(value)
    try:
        relative = path.relative_to(source)
    except ValueError:
        return value
    return str(target / relative)


def _rewrite_payload(value, moves: list[tuple[Path, Path]]):
    if isinstance(value, dict):
        return {key: _rewrite_payload(item, moves) for key, item in value.items()}
    if isinstance(value, list):
        return [_rewrite_payload(item, moves) for item in value]
    if isinstance(value, str):
        for source, target in moves:
            rewritten = _rewrite_path(value, source, target)
            if rewritten != value:
                return rewritten
    return value


def _post_migration_relative(
    path: Path, moves: list[tuple[Path, Path]], root: Path
) -> Path:
    for source, target in moves:
        try:
            relative = path.relative_to(source)
        except ValueError:
            continue
        return (target / relative).relative_to(root)
    return path.relative_to(root)


def _checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
