"""Explicit diagnostics and smoke-test CLI for one reviewed YouTube upload."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from autoshorts.automation_store import (
    AutomationStore,
    default_automation_config_path,
)
from autoshorts.channel_setup import link_approved_brand_plan
from autoshorts.credential_store import FilesystemCredentialStore
from autoshorts.state_store import StateStore, default_state_db_path
from autoshorts.youtube_publishing import (
    PublicationMetadata,
    YouTubePublicationService,
)


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit and run one controlled AutoShorts YouTube publication."
    )
    parser.add_argument("--config", type=Path, default=default_automation_config_path())
    parser.add_argument("--state-db", type=Path, default=default_state_db_path())
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("audit")
    commands.add_parser("list-identities")
    commands.add_parser("list-channels")
    commands.add_parser("list-publications")
    link = commands.add_parser("link-brand")
    link.add_argument("--channel", required=True)
    create = commands.add_parser("create")
    create.add_argument("--channel", required=True)
    create.add_argument("--video", required=True, type=Path)
    create.add_argument("--title", required=True)
    create.add_argument("--description", default="")
    create.add_argument("--tag", action="append", default=[])
    create.add_argument(
        "--privacy", choices=("private", "unlisted", "public"), default="private"
    )
    create.add_argument(
        "--made-for-kids", action=argparse.BooleanOptionalAction, default=False
    )
    create.add_argument(
        "--contains-synthetic-media",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    create.add_argument(
        "--paid-product-placement", action=argparse.BooleanOptionalAction, default=False
    )
    create.add_argument("--category-id")
    create.add_argument("--category-name")
    create.add_argument("--default-language", default="pt-BR")
    create.add_argument(
        "--license", choices=("youtube", "creativeCommon"), default="youtube"
    )
    create.add_argument(
        "--embeddable", action=argparse.BooleanOptionalAction, default=True
    )
    create.add_argument(
        "--public-stats", action=argparse.BooleanOptionalAction, default=True
    )
    create.add_argument("--publish-at")
    create.add_argument("--recording-date")
    for name in ("approve", "dry-run"):
        command = commands.add_parser(name)
        command.add_argument("publication_id", type=int)
    upload = commands.add_parser("upload")
    upload.add_argument("publication_id", type=int)
    upload.add_argument(
        "--confirm-upload",
        action="store_true",
        help="Required explicit confirmation that a real API upload may occur.",
    )
    upload.add_argument(
        "--allow-public",
        action="store_true",
        help="Additional confirmation required for privacyStatus=public.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = create_parser().parse_args(argv)
    try:
        config = AutomationStore(arguments.config).load()
        with StateStore(arguments.state_db) as state:
            return _run(arguments, config, state)
    except (FileNotFoundError, KeyError, OSError, RuntimeError, ValueError) as exc:
        print(f"AutoShorts publishing failed: {exc}", file=sys.stderr)
        return 2


def _run(arguments, config, state: StateStore) -> int:
    command = arguments.command
    if command == "list-identities":
        for identity in config.google_identities:
            status = "ENABLED" if identity.enabled else "DISABLED"
            print(identity.identity_id, identity.label, status)
        return 0
    channels = [channel for theme in config.themes for channel in theme.channels]
    if command == "list-channels":
        for channel in channels:
            binding = state.get_channel_binding(channel.channel_id)
            kit = state.get_approved_brand_kit(channel.channel_id)
            print(
                channel.channel_id,
                channel.name,
                channel.google_identity_id or "NO_IDENTITY",
                binding.status if binding else "NO_OAUTH",
                binding.youtube_channel_id if binding else "NO_YOUTUBE_CHANNEL",
                kit.public_name if kit else "NO_BRAND_PLAN",
            )
        return 0
    if command == "audit":
        binding_count = sum(
            state.get_channel_binding(item.channel_id) is not None for item in channels
        )
        brand_count = sum(
            state.get_approved_brand_kit(item.channel_id) is not None
            for item in channels
        )
        print(f"schema={state.schema_version}")
        print(f"identities={len(config.google_identities)}")
        print(f"publishing_channels={len(channels)}")
        print(f"oauth_bindings={binding_count}")
        print(f"brand_links={brand_count}")
        print(f"publications={len(state.publications())}")
        return 0
    if command == "list-publications":
        for item in state.publications():
            print(
                item.publication_id,
                item.status,
                item.privacy_status,
                item.publishing_channel_id,
                item.video_path,
                item.remote_video_id or "—",
                item.last_error or "—",
            )
        return 0
    if command == "link-brand":
        theme, channel = _channel(config, arguments.channel)
        plan = next(
            (
                item
                for item in state.planned_channel_brands(theme.output_subdirectory)
                if item.slot == channel.slot
            ),
            None,
        )
        if plan is None:
            raise ValueError("No catalog Brand Plan matches this theme and slot.")
        kit = link_approved_brand_plan(
            theme=theme,
            publishing_channel=channel,
            planned_brand=plan,
            state_store=state,
        )
        print(f"linked={kit.public_name} source_plan_id={kit.source_plan_id}")
        return 0
    service = YouTubePublicationService(config, state, FilesystemCredentialStore())
    if command == "create":
        record = service.create_for_review(
            arguments.channel,
            arguments.video,
            PublicationMetadata(
                title=arguments.title,
                description=arguments.description,
                tags=tuple(arguments.tag),
                privacy_status=arguments.privacy,
                made_for_kids=arguments.made_for_kids,
                contains_synthetic_media=arguments.contains_synthetic_media,
                has_paid_product_placement=arguments.paid_product_placement,
                category_id=arguments.category_id,
                category_name=arguments.category_name,
                default_language=arguments.default_language,
                license=arguments.license,
                embeddable=arguments.embeddable,
                public_stats_viewable=arguments.public_stats,
                publish_at=arguments.publish_at,
                recording_date=arguments.recording_date,
            ),
        )
        print(f"publication_id={record.publication_id} status={record.status}")
        return 0
    publication_id = arguments.publication_id
    if command == "approve":
        record = service.approve(publication_id)
        print(f"publication_id={record.publication_id} status={record.status}")
        return 0
    if command == "dry-run":
        result = service.preflight(publication_id)
        print(
            f"DRY_RUN_OK publication_id={publication_id} "
            f"channel={result.youtube_channel_title} "
            f"identity={result.google_identity_label} "
            f"media={result.media.width}x{result.media.height} "
            f"privacy={result.publication.privacy_status}"
        )
        publication = result.publication
        synthetic = "YES" if publication.contains_synthetic_media else "NO"
        paid = "YES" if publication.has_paid_product_placement else "NO"
        license_name = (
            "YouTube Standard"
            if publication.license == "youtube"
            else "Creative Commons"
        )
        print("Audience:")
        print(f"Made for kids: {'YES' if publication.made_for_kids else 'NO'}")
        print(f"Synthetic/altered media: {synthetic}")
        print(f"Paid product placement: {paid}")
        print(f"Category: {publication.category_name or publication.category_id}")
        print(f"Language: {publication.default_language}")
        print(f"License: {license_name}")
        print(f"Embeddable: {'YES' if publication.embeddable else 'NO'}")
        print(f"Public stats: {'YES' if publication.public_stats_viewable else 'NO'}")
        return 0
    record = state.publication(publication_id)
    if record is None:
        raise KeyError(f"Unknown publication: {publication_id}")
    if not arguments.confirm_upload:
        raise ValueError("Real upload requires --confirm-upload.")
    if record.privacy_status == "public" and not arguments.allow_public:
        raise ValueError("Public upload also requires --allow-public.")
    uploaded = service.upload_approved(publication_id)
    print(
        f"publication_id={uploaded.publication_id} status={uploaded.status} "
        f"remote_video_id={uploaded.remote_video_id}"
    )
    return 0


def _channel(config, channel_id):
    for theme in config.themes:
        for channel in theme.channels:
            if channel.channel_id == channel_id:
                return theme, channel
    raise KeyError(f"Publishing channel not found: {channel_id}")


if __name__ == "__main__":
    raise SystemExit(main())
