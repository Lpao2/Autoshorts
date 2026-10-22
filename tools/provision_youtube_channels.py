#!/usr/bin/env python3
"""Thin CLI wrapper around YouTubeProvisioningService."""

from __future__ import annotations

import argparse
from pathlib import Path

from autoshorts.automation_store import AutomationStore, default_automation_config_path
from autoshorts.browser_runtime import browser_display_name, resolve_browser_executable
from autoshorts.credential_store import FilesystemCredentialStore
from autoshorts.oauth_onboarding import (
    authorize_provisioned_channel,
    reconcile_publishing_channels,
)
from autoshorts.state_store import StateStore, default_state_db_path
from autoshorts.youtube_oauth import YouTubeConnectionService
from autoshorts.youtube_playwright import PlaywrightYouTubeAdapter
from autoshorts.youtube_provisioning import YouTubeProvisioningService

ROOT = Path(__file__).resolve().parents[1]
PROTECTED = {"google-a": ("Lucas Lp",), "google-b": ("Marcelo Clic",)}


def service() -> YouTubeProvisioningService:
    return YouTubeProvisioningService(
        project_root=ROOT,
        state_db_path=default_state_db_path(ROOT),
        config=AutomationStore(default_automation_config_path(ROOT)).load(),
        adapter_factory=PlaywrightYouTubeAdapter,
        protected_channels=PROTECTED,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AutoShorts YouTube provisioner")
    parser.add_argument("--account", required=True, help="Persisted GoogleIdentity ID")
    parser.add_argument("--inspect", action="store_true")
    parser.add_argument("--verify-session", action="store_true")
    parser.add_argument("--brand-plan-id")
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--connect-oauth", action="store_true")
    return parser.parse_args()


def run() -> int:
    args = parse_args()
    app = service()
    if args.dry_run:
        for item in app.list_planned_channels(args.account):
            print(
                item.theme_key,
                item.request.expected_name,
                item.request.handle_candidates[0],
                item.status,
                item.request.logo_path,
                sep=" | ",
            )
        return 0
    if args.verify_session:
        result = app.verify_session(args.account)
        executable = resolve_browser_executable()
        print(f"GoogleIdentity: {args.account}")
        print(f"Browser: {browser_display_name(executable)}")
        print(f"Executable: {executable or 'bundled Chromium'}")
        print(f"Profile: {result.profile_path}")
        print(f"Status: {result.status}")
    elif args.inspect:
        for item in app.inspect_account(args.account):
            print(item)
    elif args.brand_plan_id:
        result = app.provision_channel(args.brand_plan_id)
        print(result)
        if args.connect_oauth and result.youtube_channel_id:
            config_path = default_automation_config_path(ROOT)
            config_store = AutomationStore(config_path)
            with StateStore(default_state_db_path(ROOT)) as state:
                mapping = reconcile_publishing_channels(
                    config_store.load(), state, frozenset({args.account})
                )
                config = config_store.save(mapping.config)
                connection = YouTubeConnectionService(
                    config, state, FilesystemCredentialStore()
                )
                oauth = authorize_provisioned_channel(
                    project_root=ROOT,
                    config=config,
                    connection_service=connection,
                    brand_plan_id=args.brand_plan_id,
                )
                print(oauth)
    else:
        if args.connect_oauth:
            parser_error = "--connect-oauth requires --brand-plan-id"
            raise ValueError(parser_error)
        for result in app.resume(args.account, limit=args.limit):
            print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
