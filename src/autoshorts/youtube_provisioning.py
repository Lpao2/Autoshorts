"""Administrative, restart-safe YouTube channel provisioning domain service."""

from __future__ import annotations

import re
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from threading import Event
from typing import Protocol

from autoshorts.automation_config import AutomationConfig, GoogleIdentity
from autoshorts.brand_assets import BrandAssetType
from autoshorts.brand_planning import PlannedChannelBrand
from autoshorts.state_store import StateStore, YouTubeProvisioningRecord


class ProvisioningStatus(StrEnum):
    PENDING = "PENDING"
    INSPECTING = "INSPECTING"
    EXISTS = "EXISTS"
    MISSING = "MISSING"
    CREATING = "CREATING"
    CREATED = "CREATED"
    HANDLE_CONFLICT = "HANDLE_CONFLICT"
    MISSING_ASSET = "MISSING_ASSET"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    POSSIBLE_DUPLICATE = "POSSIBLE_DUPLICATE"
    FAILED_RECOVERABLE = "FAILED_RECOVERABLE"
    FAILED = "FAILED"


ACCOUNT_BLOCKING = {
    "CAPTCHA",
    "2FA",
    "SECURITY_CHECK",
    "CHANNEL_CREATION_LIMIT",
    "AUTH_REQUIRED",
}


@dataclass(frozen=True, slots=True)
class ExistingYouTubeChannel:
    channel_id: str | None
    name: str
    handle: str | None
    href: str | None = None


@dataclass(frozen=True, slots=True)
class ProvisioningRequest:
    google_identity_id: str
    brand_plan_id: str
    expected_name: str
    handle_candidates: tuple[str, str, str]
    logo_path: Path


@dataclass(frozen=True, slots=True)
class ProvisioningResult:
    status: ProvisioningStatus
    request: ProvisioningRequest
    actual_handle: str | None = None
    youtube_channel_id: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class PlannedProvisioningChannel:
    request: ProvisioningRequest
    theme_key: str
    status: ProvisioningStatus
    actual_handle: str | None = None
    youtube_channel_id: str | None = None
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class WebSessionStatus:
    google_identity_id: str
    status: str
    profile_path: Path


class NeedsHuman(RuntimeError):
    pass


class NavigationTimeout(RuntimeError):
    pass


class HandleConflict(RuntimeError):
    pass


class ProvisioningAdapter(Protocol):
    def open(self, identity: GoogleIdentity, profile_path: Path) -> None: ...
    def close(self) -> None: ...
    def verify_session(self) -> bool: ...
    def inspect_channels(self) -> tuple[ExistingYouTubeChannel, ...]: ...
    def create_channel(
        self, request: ProvisioningRequest
    ) -> ExistingYouTubeChannel: ...


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = "".join(c for c in value if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def pretty_handle_base(channel_name: str) -> str:
    raw = unicodedata.normalize("NFKD", channel_name)
    raw = "".join(c for c in raw if not unicodedata.combining(c))
    words = re.findall(r"[A-Za-z0-9]+", raw)
    if not words:
        raise ValueError(f"Cannot generate a handle for {channel_name!r}.")
    parts = [
        word
        if any(c.isupper() for c in word[1:])
        else word[:1].upper() + word[1:].lower()
        for word in words
    ]
    return "@" + "".join(parts) + "Oficial"


def handle_candidates(channel_name: str) -> tuple[str, str, str]:
    base = pretty_handle_base(channel_name)
    return base, f"{base}1", f"{base}2"


def persistent_profile_path(project_root: Path, google_identity_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", google_identity_id):
        raise ValueError("Unsafe GoogleIdentity ID for browser profile.")
    return (
        project_root / "data" / "playwright" / "youtube-profiles" / google_identity_id
    )


def classify_existing(
    request: ProvisioningRequest,
    existing: tuple[ExistingYouTubeChannel, ...],
    persisted: YouTubeProvisioningRecord | None = None,
) -> ProvisioningResult:
    if persisted and persisted.youtube_channel_id:
        matches = [x for x in existing if x.channel_id == persisted.youtube_channel_id]
        if matches:
            item = matches[0]
            return ProvisioningResult(
                ProvisioningStatus.EXISTS, request, item.handle, item.channel_id
            )
    candidate_handles = {normalize(item) for item in request.handle_candidates}
    handle_matches = [
        item
        for item in existing
        if item.handle and normalize(item.handle) in candidate_handles
    ]
    name_matches = [
        item
        for item in existing
        if normalize(item.name) == normalize(request.expected_name)
    ]
    generated_name = normalize(request.handle_candidates[0].removeprefix("@"))
    alias_matches = [
        item for item in existing if normalize(item.name) == generated_name
    ]
    matches = {
        id(item): item for item in (*handle_matches, *name_matches, *alias_matches)
    }
    if len(matches) == 1:
        item = next(iter(matches.values()))
        return ProvisioningResult(
            ProvisioningStatus.EXISTS, request, item.handle, item.channel_id
        )
    if len(matches) > 1:
        return ProvisioningResult(
            ProvisioningStatus.POSSIBLE_DUPLICATE,
            request,
            error=f"{len(matches)} channels match name/handle",
        )
    return ProvisioningResult(ProvisioningStatus.MISSING, request)


class YouTubeProvisioningService:
    def __init__(
        self,
        *,
        project_root: Path,
        state_db_path: Path,
        config: AutomationConfig,
        adapter_factory: Callable[[], ProvisioningAdapter],
        protected_channels: dict[str, tuple[str, ...]] | None = None,
    ) -> None:
        self.project_root = project_root
        self.state_db_path = state_db_path
        self.config = config
        self.adapter_factory = adapter_factory
        self.protected_channels = protected_channels or {}
        self.cancel_event = Event()
        self.cancel_after_current = Event()

    def list_accounts(self) -> tuple[GoogleIdentity, ...]:
        return tuple(item for item in self.config.google_identities if item.enabled)

    def profile_path(self, google_identity_id: str) -> Path:
        return persistent_profile_path(self.project_root, google_identity_id)

    def list_planned_channels(
        self, google_identity_id: str | None = None
    ) -> tuple[PlannedProvisioningChannel, ...]:
        with StateStore(self.state_db_path) as store:
            plans = store.planned_channel_brands()
            assignments = store.planned_identity_assignments()
            records = {
                item.brand_plan_id: item
                for item in store.youtube_provisioning_records()
            }
            result = []
            for plan in plans:
                identity_id = assignments.get(plan.brand_plan_id)
                if not identity_id or (
                    google_identity_id is not None and identity_id != google_identity_id
                ):
                    continue
                logo = self._logo(store, plan)
                record = records.get(plan.brand_plan_id)
                status = (
                    ProvisioningStatus(record.status)
                    if record
                    else (
                        ProvisioningStatus.PENDING
                        if logo
                        else ProvisioningStatus.MISSING_ASSET
                    )
                )
                request = ProvisioningRequest(
                    identity_id,
                    plan.brand_plan_id,
                    plan.public_name,
                    handle_candidates(plan.public_name),
                    logo or Path(),
                )
                result.append(
                    PlannedProvisioningChannel(
                        request,
                        plan.theme_key,
                        status,
                        record.actual_handle if record else None,
                        record.youtube_channel_id if record else None,
                        record.last_error if record else None,
                    )
                )
            return tuple(result)

    def inspect_account(
        self, google_identity_id: str
    ) -> tuple[PlannedProvisioningChannel, ...]:
        identity = self._identity(google_identity_id)
        adapter = self.adapter_factory()
        adapter.open(identity, self.profile_path(google_identity_id))
        try:
            if not adapter.verify_session():
                self._session(google_identity_id, "NEEDS_LOGIN")
                raise NeedsHuman("AUTH_REQUIRED")
            self._session(google_identity_id, "LOGGED_IN")
            existing = adapter.inspect_channels()
            protected = {
                normalize(x)
                for x in self.protected_channels.get(google_identity_id, ())
            }
            if any(normalize(item.name) in protected for item in existing):
                pass  # Explicitly observed, never represented as a planned creation.
            with StateStore(self.state_db_path) as store:
                persisted = {
                    item.brand_plan_id: item
                    for item in store.youtube_provisioning_records(google_identity_id)
                }
                planned = self.list_planned_channels(google_identity_id)
                for item in planned:
                    result = classify_existing(
                        item.request,
                        existing,
                        persisted.get(item.request.brand_plan_id),
                    )
                    self._save(store, result)
            return self.list_planned_channels(google_identity_id)
        finally:
            adapter.close()

    def verify_session(self, google_identity_id: str) -> WebSessionStatus:
        identity = self._identity(google_identity_id)
        adapter = self.adapter_factory()
        path = self.profile_path(google_identity_id)
        adapter.open(identity, path)
        try:
            logged_in = adapter.verify_session()
            status = "LOGGED_IN" if logged_in else "NEEDS_LOGIN"
            self._session(google_identity_id, status)
            return WebSessionStatus(google_identity_id, status, path)
        finally:
            adapter.close()

    def provision_channel(self, brand_plan_id: str) -> ProvisioningResult:
        planned = next(
            (
                x
                for x in self.list_planned_channels()
                if x.request.brand_plan_id == brand_plan_id
            ),
            None,
        )
        if planned is None:
            raise KeyError(f"Unknown BrandPlan: {brand_plan_id}")
        if not planned.request.logo_path.is_file():
            result = ProvisioningResult(
                ProvisioningStatus.MISSING_ASSET,
                planned.request,
                error="Approved logo missing",
            )
            with StateStore(self.state_db_path) as store:
                self._save(store, result)
            return result
        identity = self._identity(planned.request.google_identity_id)
        adapter = self.adapter_factory()
        adapter.open(identity, self.profile_path(identity.identity_id))
        try:
            if not adapter.verify_session():
                result = ProvisioningResult(
                    ProvisioningStatus.AUTH_REQUIRED,
                    planned.request,
                    error="AUTH_REQUIRED",
                )
                self._session(identity.identity_id, "NEEDS_LOGIN")
            else:
                existing = adapter.inspect_channels()
                with StateStore(self.state_db_path) as store:
                    old = next(
                        (
                            x
                            for x in store.youtube_provisioning_records(
                                identity.identity_id
                            )
                            if x.brand_plan_id == brand_plan_id
                        ),
                        None,
                    )
                result = classify_existing(planned.request, existing, old)
                if result.status is ProvisioningStatus.MISSING:
                    result = ProvisioningResult(
                        ProvisioningStatus.CREATING, planned.request
                    )
                    with StateStore(self.state_db_path) as store:
                        self._save(store, result)
                    try:
                        created = adapter.create_channel(planned.request)
                        result = ProvisioningResult(
                            ProvisioningStatus.CREATED,
                            planned.request,
                            created.handle,
                            created.channel_id,
                        )
                    except HandleConflict as exc:
                        result = ProvisioningResult(
                            ProvisioningStatus.HANDLE_CONFLICT,
                            planned.request,
                            error=str(exc),
                        )
                    except (NavigationTimeout, TimeoutError) as exc:
                        result = ProvisioningResult(
                            ProvisioningStatus.FAILED_RECOVERABLE,
                            planned.request,
                            error=str(exc),
                        )
                    except NeedsHuman as exc:
                        reason = str(exc)
                        status = (
                            ProvisioningStatus.AUTH_REQUIRED
                            if reason == "AUTH_REQUIRED"
                            else ProvisioningStatus.NEEDS_HUMAN
                        )
                        result = ProvisioningResult(
                            status, planned.request, error=reason
                        )
                    except Exception as exc:
                        recoverable = exc.__class__.__name__ == "TimeoutError"
                        result = ProvisioningResult(
                            ProvisioningStatus.FAILED_RECOVERABLE
                            if recoverable
                            else ProvisioningStatus.FAILED,
                            planned.request,
                            error=str(exc),
                        )
            with StateStore(self.state_db_path) as store:
                self._save(store, result)
            return result
        finally:
            adapter.close()

    def provision_pending(
        self, google_identity_id: str, *, limit: int | None = None
    ) -> tuple[ProvisioningResult, ...]:
        results = []
        eligible = {
            ProvisioningStatus.PENDING,
            ProvisioningStatus.MISSING,
            ProvisioningStatus.FAILED_RECOVERABLE,
        }
        for item in self.list_planned_channels(google_identity_id):
            if self.cancel_event.is_set() or (
                limit is not None and len(results) >= limit
            ):
                break
            if item.status not in eligible:
                continue
            result = self.provision_channel(item.request.brand_plan_id)
            results.append(result)
            if (
                result.error in ACCOUNT_BLOCKING
                or result.status is ProvisioningStatus.AUTH_REQUIRED
            ):
                break
            if self.cancel_after_current.is_set():
                break
            time.sleep(0.5)
        return tuple(results)

    def resume(self, google_identity_id: str, *, limit: int | None = None):
        return self.provision_pending(google_identity_id, limit=limit)

    def cancel(self, *, after_current: bool = False) -> None:
        (self.cancel_after_current if after_current else self.cancel_event).set()

    def get_status(self, google_identity_id: str | None = None):
        return self.list_planned_channels(google_identity_id)

    def _identity(self, identity_id: str) -> GoogleIdentity:
        return next(
            item
            for item in self.config.google_identities
            if item.identity_id == identity_id
        )

    @staticmethod
    def _logo(store: StateStore, plan: PlannedChannelBrand) -> Path | None:
        asset = store.latest_valid_brand_asset(plan.brand_plan_id, BrandAssetType.LOGO)
        return asset.file_path if asset and asset.file_path.is_file() else None

    def _session(self, identity_id: str, status: str, error: str | None = None) -> None:
        with StateStore(self.state_db_path) as store:
            store.set_youtube_web_session_status(identity_id, status, error)

    @staticmethod
    def _save(store: StateStore, result: ProvisioningResult) -> None:
        store.save_youtube_provisioning_record(
            brand_plan_id=result.request.brand_plan_id,
            google_identity_id=result.request.google_identity_id,
            expected_name=result.request.expected_name,
            expected_handle=result.request.handle_candidates[0],
            actual_handle=result.actual_handle,
            youtube_channel_id=result.youtube_channel_id,
            status=result.status,
            logo_path=result.request.logo_path,
            last_error=result.error,
        )
