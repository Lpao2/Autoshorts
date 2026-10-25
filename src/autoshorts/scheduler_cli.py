"""Command-line interface for the local persistent scheduler."""

from __future__ import annotations

import argparse
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from autoshorts.automation_store import AutomationStore, default_automation_config_path
from autoshorts.credential_store import FilesystemCredentialStore
from autoshorts.media_retention import PublishedMediaRetention
from autoshorts.scheduler import (
    SchedulerExecutionError,
    SchedulerExecutionResult,
    SchedulerService,
)
from autoshorts.scheduler_models import SchedulerUrgency
from autoshorts.state_store import StateStore, default_state_db_path
from autoshorts.youtube_publishing import (
    YouTubePublicationService,
    YouTubeUploadFailure,
)


class YouTubeSchedulerExecutor:
    def __init__(
        self,
        service: YouTubePublicationService,
        retention: PublishedMediaRetention | None = None,
    ) -> None:
        self.service = service
        self.retention = retention

    def execute(self, publication_id: int) -> SchedulerExecutionResult:
        try:
            publication = self.service.upload_approved(publication_id)
        except YouTubeUploadFailure as exc:
            raise SchedulerExecutionError(
                str(exc), recoverable=exc.retryable, uncertain=exc.outcome_uncertain
            ) from exc
        except (FileNotFoundError, KeyError, OSError, RuntimeError, ValueError) as exc:
            raise SchedulerExecutionError(str(exc), recoverable=False) from exc
        if (
            self.retention is not None
            and publication.remote_video_id is not None
            and publication.status == "PUBLISHED"
        ):
            cleanup = self.retention.cleanup(publication.publication_id)
            for error in cleanup.errors:
                print(f"Media retention warning: {error}")
        return SchedulerExecutionResult(
            publication.remote_video_id is not None
            and publication.status == "PUBLISHED"
        )


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AutoShorts local scheduler")
    parser.add_argument("--config", type=Path, default=default_automation_config_path())
    parser.add_argument("--state-db", type=Path, default=default_state_db_path())
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "list", "plan", "pause", "resume"):
        commands.add_parser(name)
    for name in ("pause-channel", "resume-channel"):
        command = commands.add_parser(name)
        command.add_argument("channel_id")
    enqueue = commands.add_parser("enqueue")
    enqueue.add_argument("publication_id", type=int)
    enqueue.add_argument("--fresh", action="store_true")
    enqueue.add_argument("--experimental", action="store_true")
    enqueue.add_argument(
        "--at",
        type=datetime.fromisoformat,
        help="Explicit timezone-aware execution time, bypassing editorial windows.",
    )
    for name in ("run", "run-once"):
        command = commands.add_parser(name)
        command.add_argument("--confirm-private-uploads", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = create_parser().parse_args(argv)
    config = AutomationStore(arguments.config).load()
    with StateStore(arguments.state_db) as state:
        publishing = YouTubePublicationService(
            config, state, FilesystemCredentialStore()
        )
        retention_root = os.getenv("AUTOSHORTS_PUBLISHED_MEDIA_RETENTION_ROOT")
        retention = (
            PublishedMediaRetention(state, Path(retention_root))
            if retention_root
            else None
        )
        if retention is not None:
            for cleanup in retention.cleanup_all():
                for error in cleanup.errors:
                    print(f"Media retention warning: {error}")
        scheduler = SchedulerService(
            config, state, YouTubeSchedulerExecutor(publishing, retention)
        )
        command = arguments.command
        if command == "status":
            settings = state.scheduler_settings()
            jobs = state.scheduler_jobs()
            print(f"paused={settings.global_paused}")
            print(f"queued={sum(j.state in {'QUEUED', 'SCHEDULED'} for j in jobs)}")
            print(f"jobs={len(jobs)}")
            return 0
        if command == "list":
            for job in state.scheduler_jobs():
                print(
                    job.job_id,
                    job.state,
                    job.publishing_channel_id,
                    job.publication_id or "—",
                    job.resolved_publish_at or "—",
                    f"attempts={job.attempts}/{job.max_attempts}",
                )
            return 0
        if command == "plan":
            now = datetime.now(UTC)
            for theme in config.themes:
                for channel in theme.channels:
                    binding = state.get_channel_binding(channel.channel_id)
                    if (
                        not channel.enabled
                        or binding is None
                        or binding.status != "CONNECTED"
                    ):
                        continue
                    schedule, proposed = scheduler.plan_channel(channel.channel_id, now)
                    primary = next(w for w in schedule.windows if w.priority == 1)
                    print(channel.name)
                    print(f"Tema: {theme.name}")
                    print(f"Timezone: {schedule.posting_profile.timezone}")
                    print(f"Policy: {schedule.posting_profile.timing_policy}")
                    print(
                        f"Próxima janela: {primary.start_local_time}–"
                        f"{primary.end_local_time}"
                    )
                    local = proposed.astimezone(
                        ZoneInfo(schedule.posting_profile.timezone)
                    )
                    print(f"Próxima publicação proposta: {local.isoformat()}")
            return 0
        if command in {"pause", "resume"}:
            state.update_scheduler_settings(global_paused=command == "pause")
            print(f"paused={command == 'pause'}")
            return 0
        if command in {"pause-channel", "resume-channel"}:
            paused = command == "pause-channel"
            state.set_scheduler_channel_paused(arguments.channel_id, paused)
            print(f"channel={arguments.channel_id} paused={paused}")
            return 0
        if command == "enqueue":
            urgency = (
                SchedulerUrgency.FRESH if arguments.fresh else SchedulerUrgency.NORMAL
            )
            job = scheduler.enqueue(
                arguments.publication_id,
                datetime.now(UTC),
                urgency=urgency,
                experimental=arguments.experimental,
                requested_publish_at=arguments.at,
            )
            print(f"job={job.job_id} state={job.state} at={job.resolved_publish_at}")
            return 0
        if not arguments.confirm_private_uploads:
            raise ValueError("Scheduler execution requires --confirm-private-uploads.")
        scheduler.recover()
        if command == "run-once":
            completed = scheduler.run_once(datetime.now(UTC))
            print(f"processed={len(completed)}")
            return 0
        while True:
            scheduler.run_once(datetime.now(UTC))
            time.sleep(state.scheduler_settings().poll_interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
