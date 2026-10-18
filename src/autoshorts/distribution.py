"""Deterministic, headless assignment and filesystem distribution of Shorts."""

from __future__ import annotations

import os
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Generic, TypeVar
from uuid import uuid4

from autoshorts.automation_config import AccountProfile

ClipT = TypeVar("ClipT")


@dataclass(frozen=True, slots=True)
class ShortArtifact:
    clip_index: int
    file_path: Path


@dataclass(frozen=True, slots=True)
class ShortAssignment(Generic[ClipT]):
    clip_index: int
    account_slot: int
    clip: ClipT
    account: AccountProfile


class LinkMethod(StrEnum):
    HARDLINK = "hardlink"
    COPY = "copy"


@dataclass(frozen=True, slots=True)
class DistributedShort:
    assignment: ShortAssignment[ShortArtifact]
    queue_path: Path
    method: LinkMethod


def assign_shorts_to_accounts(
    clips: Sequence[ClipT], accounts: Sequence[AccountProfile]
) -> tuple[ShortAssignment[ClipT], ...]:
    """Map each clip index to the enabled account with the same persistent slot."""
    if len(clips) != 3:
        raise ValueError("Exactly 3 shorts are required for distribution.")
    enabled_by_slot = {account.slot: account for account in accounts if account.enabled}
    if set(enabled_by_slot) != {1, 2, 3}:
        missing = sorted({1, 2, 3} - set(enabled_by_slot))
        detail = (
            f" Missing enabled slot(s): {', '.join(map(str, missing))}."
            if missing
            else ""
        )
        raise ValueError(f"Enabled account slots 1, 2 and 3 are required.{detail}")
    if sum(account.enabled for account in accounts) != 3:
        raise ValueError("Enabled account slots must be unique.")
    return tuple(
        ShortAssignment(index, index, clip, enabled_by_slot[index])
        for index, clip in enumerate(clips, start=1)
    )


def link_or_copy(source: str | Path, destination: str | Path) -> LinkMethod:
    """Atomically hard-link a file, falling back to a byte copy when necessary."""
    source_path = Path(source)
    destination_path = Path(destination)
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination_path.with_name(
        f".{destination_path.name}.{uuid4().hex}.tmp"
    )
    try:
        try:
            os.link(source_path, temporary)
            method = LinkMethod.HARDLINK
        except OSError:
            shutil.copy2(source_path, temporary)
            method = LinkMethod.COPY
        temporary.replace(destination_path)
        return method
    finally:
        temporary.unlink(missing_ok=True)


def distribute_short_files(
    assignments: Sequence[ShortAssignment[ShortArtifact]],
    accounts_root: str | Path,
    youtube_id: str,
) -> tuple[DistributedShort, ...]:
    if len(assignments) != 3:
        raise ValueError("Exactly 3 assignments are required.")
    root = Path(accounts_root)
    distributed: list[DistributedShort] = []
    try:
        for assignment in assignments:
            account_directory = (
                f"slot-{assignment.account_slot}-"
                f"{_safe_component(assignment.account.name)}"
            )
            destination = (
                root
                / account_directory
                / "queue"
                / f"{youtube_id}_{assignment.clip_index:02d}.mp4"
            )
            method = link_or_copy(assignment.clip.file_path, destination)
            distributed.append(DistributedShort(assignment, destination, method))
    except Exception:
        for item in distributed:
            item.queue_path.unlink(missing_ok=True)
        raise
    return tuple(distributed)


def _safe_component(value: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in "-_" else "_" for char in value)
    return cleaned.strip("._") or "account"
