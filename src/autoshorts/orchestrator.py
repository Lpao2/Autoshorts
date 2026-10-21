"""Inventory-driven coordination of the existing AutoShorts pipeline."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from autoshorts.automation_config import (
    AutomationConfig,
    PublishingChannel,
    ThemeProfile,
)
from autoshorts.collection import YouTubeAccess
from autoshorts.config import Settings
from autoshorts.credential_store import FilesystemCredentialStore
from autoshorts.faster_whisper_provider import FasterWhisperProvider
from autoshorts.gemini_analysis_provider import GeminiAnalysisProvider
from autoshorts.ingestion import discover_next_source_video, ingest
from autoshorts.metadata_pipeline import (
    GeminiMetadataProvider,
    approve_and_enqueue_metadata,
    generate_metadata_draft,
)
from autoshorts.production import produce_source_video
from autoshorts.scheduler import SchedulerService
from autoshorts.state_store import (
    InventoryPolicy,
    MetadataDraftStatus,
    PublicationStatus,
    ReviewPolicy,
    StateStore,
)
from autoshorts.youtube_publishing import YouTubePublicationService

LOGGER = logging.getLogger(__name__)
FUTURE_PUBLICATION_STATES = {
    PublicationStatus.READY_FOR_REVIEW.value,
    PublicationStatus.APPROVED_FOR_PUBLISH.value,
    PublicationStatus.UPLOADING.value,
    PublicationStatus.FAILED_RETRYABLE.value,
    PublicationStatus.UPLOAD_UNCERTAIN.value,
}


@dataclass(frozen=True, slots=True)
class InventoryItem:
    checksum: str
    stage: str
    youtube_id: str | None
    path: Path


@dataclass(frozen=True, slots=True)
class ChannelInventory:
    publishing_channel_id: str
    items: tuple[InventoryItem, ...]

    @property
    def count(self) -> int:
        return len(self.items)


@dataclass(frozen=True, slots=True)
class ChannelContext:
    theme: ThemeProfile
    channel: PublishingChannel


@dataclass(frozen=True, slots=True)
class ChannelPlan:
    context: ChannelContext
    inventory: ChannelInventory
    policy: InventoryPolicy
    deficit: int
    action: str
    next_post: str | None


@dataclass(frozen=True, slots=True)
class ProductionOutcome:
    source_video_id: str
    metadata_draft_id: int
    result: str


@dataclass(frozen=True, slots=True)
class CycleResult:
    plans: tuple[ChannelPlan, ...]
    productions_started: int
    outcomes: tuple[tuple[str, ProductionOutcome], ...]
    errors: tuple[tuple[str, str], ...]


class ChannelProducer(Protocol):
    def produce(self, context: ChannelContext) -> ProductionOutcome: ...


def get_channel_inventory(
    state: StateStore, publishing_channel_id: str
) -> ChannelInventory:
    """Return unique, valid future artifacts for one destination channel."""
    items: dict[str, InventoryItem] = {}
    publications = state.connection.execute(
        """SELECT video_checksum, video_path, status FROM publications
        WHERE publishing_channel_id=?""",
        (publishing_channel_id,),
    ).fetchall()
    for row in publications:
        path = Path(row["video_path"])
        if row["status"] in FUTURE_PUBLICATION_STATES and path.is_file():
            items[row["video_checksum"]] = InventoryItem(
                row["video_checksum"], f"PUBLICATION_{row['status']}", None, path
            )

    drafts = state.connection.execute(
        """SELECT d.youtube_id, d.ready_checksum, d.status, r.file_path
        FROM metadata_drafts d JOIN ready_artifacts r ON r.youtube_id=d.youtube_id
        WHERE d.publishing_channel_id=? AND d.status IN (?, ?)""",
        (
            publishing_channel_id,
            MetadataDraftStatus.REVIEW_REQUIRED,
            MetadataDraftStatus.APPROVED,
        ),
    ).fetchall()
    for row in drafts:
        path = Path(row["file_path"])
        if path.is_file():
            items.setdefault(
                row["ready_checksum"],
                InventoryItem(
                    row["ready_checksum"],
                    f"METADATA_{row['status']}",
                    row["youtube_id"],
                    path,
                ),
            )

    assigned = state.connection.execute(
        """SELECT a.youtube_id, r.checksum, r.file_path
        FROM orchestrator_artifact_assignments a
        JOIN ready_artifacts r ON r.youtube_id=a.youtube_id
        JOIN videos v ON v.youtube_id=a.youtube_id
        WHERE a.publishing_channel_id=? AND v.status='READY'""",
        (publishing_channel_id,),
    ).fetchall()
    for row in assigned:
        path = Path(row["file_path"])
        if path.is_file():
            items.setdefault(
                row["checksum"],
                InventoryItem(
                    row["checksum"], "READY", row["youtube_id"], path
                ),
            )
    return ChannelInventory(publishing_channel_id, tuple(items.values()))


class PipelineChannelProducer:
    """Thin adapter that composes the existing ingestion/production APIs."""

    def __init__(
        self,
        config: AutomationConfig,
        state: StateStore,
        settings: Settings,
        workspace: Path,
    ) -> None:
        self.config = config
        self.state = state
        self.settings = settings
        self.workspace = workspace

    def produce(self, context: ChannelContext) -> ProductionOutcome:
        profiles = tuple(
            profile
            for profile in self.state.theme_market_profiles()
            if profile.theme_key == context.theme.output_subdirectory
            and profile.platform == "youtube"
            and profile.enabled
        )
        preferred = tuple(
            profile for profile in profiles if profile.is_default_for_theme
        )
        if len(preferred or profiles) != 1:
            raise ValueError(
                "Theme requires exactly one active/default market profile."
            )
        profile = (preferred or profiles)[0]
        report = discover_next_source_video(
            profile,
            self.state,
            maximum_age_days=context.theme.policy.maximum_age_days,
            access=YouTubeAccess(),
        )
        if report.selected is None:
            raise RuntimeError("No eligible source video was discovered.")
        source_id = report.selected.external_video_id
        ingest(
            report.selected,
            profile,
            self.state,
            workspace=self.workspace,
            ffprobe_path=self.settings.ffprobe_path,
            ffmpeg_path=self.settings.ffmpeg_path,
        )
        self.state.assign_source_to_channel(source_id, context.channel.channel_id)
        produce_source_video(
            youtube_id=source_id,
            state=self.state,
            theme=context.theme,
            settings=self.settings,
            transcription_provider=FasterWhisperProvider(language="pt"),
            analysis_provider=GeminiAnalysisProvider(),
            workspace=self.workspace,
        )
        draft = generate_metadata_draft(
            youtube_id=source_id,
            publishing_channel_id=context.channel.channel_id,
            config=self.config,
            state=self.state,
            provider=GeminiMetadataProvider(),
        )
        result = draft.status.value
        if draft.review_policy is ReviewPolicy.AUTO_APPROVE:
            service = YouTubePublicationService(
                self.config, self.state, FilesystemCredentialStore()
            )
            scheduler = SchedulerService(
                self.config, self.state, _NoUploadExecutor()
            )
            draft, _job = approve_and_enqueue_metadata(
                draft.draft_id,
                state=self.state,
                publication_service=service,
                scheduler=scheduler,
                now=datetime.now(UTC),
            )
            result = draft.status.value
        return ProductionOutcome(source_id, draft.draft_id, result)


class Orchestrator:
    def __init__(
        self,
        config: AutomationConfig,
        state: StateStore,
        producer: ChannelProducer,
        *,
        owner: str | None = None,
        lease_seconds: int = 1800,
        backoff_seconds: int = 900,
    ) -> None:
        self.config = config
        self.state = state
        self.producer = producer
        self.owner = owner or f"orchestrator-{uuid4()}"
        self.lease_seconds = lease_seconds
        self.backoff_seconds = backoff_seconds

    def active_channels(self) -> tuple[ChannelContext, ...]:
        contexts = []
        for theme in self.config.themes:
            if not theme.enabled:
                continue
            for channel in theme.channels:
                if not channel.enabled:
                    continue
                binding = self.state.get_channel_binding(channel.channel_id)
                kit = self.state.get_approved_brand_kit(channel.channel_id)
                if binding is not None and binding.status == "CONNECTED" and kit:
                    contexts.append(ChannelContext(theme, channel))
        return tuple(contexts)

    def inspect_channel(self, context: ChannelContext) -> ChannelPlan:
        inventory = get_channel_inventory(self.state, context.channel.channel_id)
        policy = self.state.inventory_policy(context.channel.channel_id)
        deficit = (
            policy.target_ready_videos - inventory.count
            if inventory.count < policy.min_ready_videos
            else 0
        )
        paused = self.state.scheduler_channel_paused(context.channel.channel_id)
        action = "PAUSED" if paused else "PRODUCE" if deficit else "NONE"
        return ChannelPlan(
            context,
            inventory,
            policy,
            deficit,
            action,
            self._next_post(context.channel.channel_id),
        )

    def plan_channel(self, context: ChannelContext) -> ChannelPlan:
        return self.inspect_channel(context)

    def run_cycle(
        self, *, dry_run: bool = False, max_productions: int = 1
    ) -> CycleResult:
        if max_productions < 0:
            raise ValueError("max_productions cannot be negative.")
        plans = tuple(self.inspect_channel(item) for item in self.active_channels())
        if dry_run:
            return CycleResult(plans, 0, (), ())
        cycle_id = self.state.start_orchestrator_cycle(
            max_productions=max_productions
        )
        outcomes: list[tuple[str, ProductionOutcome]] = []
        errors: list[tuple[str, str]] = []
        started = 0
        try:
            for plan in self._prioritized(plans):
                channel_id = plan.context.channel.channel_id
                if plan.action != "PRODUCE":
                    self._record(cycle_id, plan, plan.action)
                    continue
                if started >= max_productions:
                    self._record(cycle_id, plan, "LIMITED")
                    continue
                if self._in_backoff(channel_id, datetime.now(UTC)):
                    self._record(cycle_id, plan, "BACKOFF")
                    continue
                if not self.state.acquire_orchestrator_lease(
                    channel_id,
                    self.owner,
                    datetime.now(UTC),
                    self.lease_seconds,
                ):
                    self._record(cycle_id, plan, "LOCKED")
                    continue
                started += 1
                try:
                    outcome = self.producer.produce(plan.context)
                    outcomes.append((plan.context.channel.name, outcome))
                    self._record(cycle_id, plan, outcome.result)
                except Exception as exc:  # a channel must not abort the cycle
                    LOGGER.exception("Channel replenishment failed: %s", channel_id)
                    errors.append((plan.context.channel.name, str(exc)))
                    self._record(
                        cycle_id, plan, self._failure_result(exc), str(exc)
                    )
                finally:
                    self.state.release_orchestrator_lease(channel_id, self.owner)
            self.state.finish_orchestrator_cycle(
                cycle_id, result="COMPLETED_WITH_ERRORS" if errors else "COMPLETED"
            )
        except Exception as exc:
            self.state.finish_orchestrator_cycle(
                cycle_id, result="FAILED", error=str(exc)
            )
            raise
        return CycleResult(plans, started, tuple(outcomes), tuple(errors))

    def run(
        self, *, interval_seconds: int = 300, max_productions: int = 1
    ) -> None:
        if interval_seconds < 1:
            raise ValueError("interval_seconds must be positive.")
        while True:
            self.run_cycle(max_productions=max_productions)
            time.sleep(interval_seconds)

    def _prioritized(self, plans: tuple[ChannelPlan, ...]) -> tuple[ChannelPlan, ...]:
        return tuple(
            sorted(
                plans,
                key=lambda plan: (
                    plan.action != "PRODUCE",
                    plan.next_post is None,
                    plan.next_post or "",
                    plan.inventory.count,
                    -plan.deficit,
                    plan.context.channel.channel_id,
                ),
            )
        )

    def _next_post(self, channel_id: str) -> str | None:
        row = self.state.connection.execute(
            """SELECT MIN(resolved_publish_at) AS next_post
            FROM publication_jobs WHERE publishing_channel_id=?
            AND state IN ('QUEUED', 'SCHEDULED')""",
            (channel_id,),
        ).fetchone()
        return row["next_post"] if row else None

    def _in_backoff(self, channel_id: str, now: datetime) -> bool:
        row = self.state.connection.execute(
            """SELECT finished_at FROM orchestrator_decisions
            WHERE publishing_channel_id=? AND result LIKE 'FAILED%'
            ORDER BY id DESC LIMIT 1""",
            (channel_id,),
        ).fetchone()
        if row is None:
            return False
        failed_at = datetime.fromisoformat(row["finished_at"])
        return failed_at > now.astimezone(UTC) - timedelta(seconds=self.backoff_seconds)

    @staticmethod
    def _failure_result(exc: Exception) -> str:
        if isinstance(exc, (KeyError, ValueError)):
            return "FAILED_CONFIGURATION"
        return "FAILED_RECOVERABLE"

    def _record(
        self,
        cycle_id: int,
        plan: ChannelPlan,
        result: str,
        error: str | None = None,
    ) -> None:
        self.state.record_orchestrator_decision(
            cycle_id,
            plan.context.channel.channel_id,
            inventory_before=plan.inventory.count,
            minimum=plan.policy.min_ready_videos,
            target=plan.policy.target_ready_videos,
            planned_count=plan.deficit,
            result=result,
            error=error,
        )


class _NoUploadExecutor:
    def execute(self, publication_id: int) -> None:
        raise RuntimeError("The orchestrator never directly executes uploads.")
