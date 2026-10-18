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
    """Map clip 1→account 1, 2→2 and 3→3, preserving input order."""
    if len(clips) != 3:
        raise ValueError("Exactly 3 shorts are required for distribution.")
    enabled = tuple(account for account in accounts if account.enabled)
    if len(enabled) != 3:
        raise ValueError("Exactly 3 enabled accounts are required for distribution.")
    return tuple(
        ShortAssignment(index, index, clip, account)
        for index, (clip, account) in enumerate(
            zip(clips, enabled, strict=True), start=1
        )
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
    for assignment in assignments:
        account_directory = _safe_component(assignment.account.name)
        destination = (
            root
            / account_directory
            / "queue"
            / f"{youtube_id}_{assignment.clip_index:02d}.mp4"
        )
        method = link_or_copy(assignment.clip.file_path, destination)
        distributed.append(DistributedShort(assignment, destination, method))
    return tuple(distributed)


def _safe_component(value: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in "-_" else "_" for char in value)
    return cleaned.strip("._") or "account"
