#!/usr/bin/env python3
"""Read-only production health report for an AutoShorts runtime."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from autoshorts.automation_config import load_automation_config
from autoshorts.credential_store import FilesystemCredentialStore
from autoshorts.state_store import StateStore
from autoshorts.youtube_oauth import YouTubeConnectionService


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--config", type=Path, required=True)
    result.add_argument("--state-db", type=Path, required=True)
    result.add_argument("--credentials-dir", type=Path, required=True)
    result.add_argument("--verify-oauth", action="store_true")
    return result


def main() -> int:
    args = parser().parse_args()
    config = load_automation_config(args.config)
    active = tuple(
        channel
        for theme in config.themes
        for channel in theme.channels
        if channel.enabled
    )
    failures: list[str] = []
    oauth_ready = 0
    with StateStore(args.state_db) as state:
        integrity = state.connection.execute("PRAGMA integrity_check").fetchone()[0]
        plans = state.connection.execute(
            "SELECT count(*) FROM planned_channel_brands WHERE status='APPROVED'"
        ).fetchone()[0]
        remote_channels = state.connection.execute(
            "SELECT count(*) FROM youtube_channel_provisioning WHERE status='EXISTS'"
        ).fetchone()[0]
        jobs = state.connection.execute(
            "SELECT state, count(*) count FROM publication_jobs GROUP BY state"
        ).fetchall()
        credentials = FilesystemCredentialStore(args.credentials_dir)
        connection = YouTubeConnectionService(config, state, credentials)
        for channel in active:
            binding = state.get_channel_binding(channel.channel_id)
            if binding is None or binding.status != "CONNECTED":
                failures.append(f"{channel.name}: NO_BINDING")
                continue
            if not (args.credentials_dir / binding.credential_ref).is_file():
                failures.append(f"{channel.name}: CREDENTIAL_MISSING")
                continue
            if args.verify_oauth:
                try:
                    verified = connection.verify_binding(channel.channel_id)
                except Exception as exc:  # report category, never credential data
                    failures.append(f"{channel.name}: {type(exc).__name__}")
                    continue
                if verified.youtube_channel_id != channel.youtube_channel_id:
                    failures.append(f"{channel.name}: CHANNEL_MISMATCH")
                    continue
            oauth_ready += 1
        database_status = "OK" if integrity == "ok" else "FAIL"
        print(f"DATABASE: {database_status} schema={state.schema_version}")
        print(f"QUEUE: OK {' '.join(f'{row[0]}={row[1]}' for row in jobs) or 'empty'}")
    print(f"PUBLISHING CHANNELS: {len(active)}")
    print(f"CHANNEL IDS RECONCILED: {remote_channels}")
    print(f"BRAND PLANS: {plans}")
    print(f"OAUTH READY: {oauth_ready}/{len(active)}")
    ffmpeg_status = (
        "OK" if shutil.which("ffmpeg") and shutil.which("ffprobe") else "FAIL"
    )
    print(f"FFMPEG: {ffmpeg_status}")
    for failure in failures:
        print(f"OAUTH NOT READY: {failure}")
    return 0 if integrity == "ok" and not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
