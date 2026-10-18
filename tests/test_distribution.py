import errno
import os

import pytest

from autoshorts.automation_config import AccountProfile
from autoshorts.distribution import (
    LinkMethod,
    ShortArtifact,
    assign_shorts_to_accounts,
    distribute_short_files,
    link_or_copy,
)


def accounts():
    return tuple(
        AccountProfile(f"Account {index}", f"a{index}@example.test", index)
        for index in range(1, 4)
    )


def test_assigns_three_shorts_to_matching_account_slots():
    assignments = assign_shorts_to_accounts(
        ("one", "two", "three"), tuple(reversed(accounts()))
    )
    assert [(item.clip, item.account.name) for item in assignments] == [
        ("one", "Account 1"),
        ("two", "Account 2"),
        ("three", "Account 3"),
    ]
    assert [item.account_slot for item in assignments] == [1, 2, 3]


@pytest.mark.parametrize("clips", [(), (1,), (1, 2), (1, 2, 3, 4)])
def test_distribution_requires_exactly_three_shorts(clips):
    with pytest.raises(ValueError, match="Exactly 3 shorts"):
        assign_shorts_to_accounts(clips, accounts())


def test_distribution_requires_three_enabled_accounts():
    disabled = (*accounts()[:2], AccountProfile("Off", "off@example.test", 3, False))
    with pytest.raises(ValueError, match="slots 1, 2 and 3"):
        assign_shorts_to_accounts((1, 2, 3), disabled)


def test_link_or_copy_prefers_hardlink(tmp_path):
    source = tmp_path / "source.mp4"
    destination = tmp_path / "queue" / "short.mp4"
    source.write_bytes(b"video")
    assert link_or_copy(source, destination) == LinkMethod.HARDLINK
    assert os.stat(source).st_ino == os.stat(destination).st_ino


def test_link_or_copy_falls_back_to_copy(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    destination = tmp_path / "queue" / "short.mp4"
    source.write_bytes(b"video")
    monkeypatch.setattr(
        "autoshorts.distribution.os.link",
        lambda *_args: (_ for _ in ()).throw(OSError(errno.EXDEV, "cross-device")),
    )
    assert link_or_copy(source, destination) == LinkMethod.COPY
    assert destination.read_bytes() == b"video"
    assert os.stat(source).st_ino != os.stat(destination).st_ino


def test_distributes_files_into_account_queues(tmp_path):
    clips = []
    for index in range(1, 4):
        path = tmp_path / "masters" / f"{index:02d}.mp4"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(str(index).encode())
        clips.append(ShortArtifact(index, path))
    assignments = assign_shorts_to_accounts(clips, accounts())
    distributed = distribute_short_files(assignments, tmp_path / "accounts", "video")
    assert [item.queue_path.name for item in distributed] == [
        "video_01.mp4",
        "video_02.mp4",
        "video_03.mp4",
    ]
    assert all(item.queue_path.is_file() for item in distributed)
    assert [item.queue_path.parent.parent.name for item in distributed] == [
        "slot-1-Account_1",
        "slot-2-Account_2",
        "slot-3-Account_3",
    ]


def test_partial_distribution_cleans_created_queue_files(tmp_path, monkeypatch):
    clips = []
    for index in range(1, 4):
        path = tmp_path / "masters" / f"{index:02d}.mp4"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"video")
        clips.append(ShortArtifact(index, path))
    assignments = assign_shorts_to_accounts(clips, accounts())
    real = link_or_copy

    def fail_third(source, destination):
        if str(destination).endswith("_03.mp4"):
            raise OSError("disk full")
        return real(source, destination)

    monkeypatch.setattr("autoshorts.distribution.link_or_copy", fail_third)
    with pytest.raises(OSError, match="disk full"):
        distribute_short_files(assignments, tmp_path / "accounts", "video")
    assert not list((tmp_path / "accounts").rglob("*.mp4"))
