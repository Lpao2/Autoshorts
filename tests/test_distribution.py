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
        AccountProfile(f"Account {index}", f"a{index}@example.test")
        for index in range(1, 4)
    )


def test_assigns_three_shorts_to_matching_account_slots():
    assignments = assign_shorts_to_accounts(("one", "two", "three"), accounts())
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
    disabled = (*accounts()[:2], AccountProfile("Off", "off@example.test", False))
    with pytest.raises(ValueError, match="3 enabled accounts"):
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
