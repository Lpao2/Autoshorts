"""Filesystem-backed secret storage kept outside the project repository."""

from __future__ import annotations

import os
import re
from pathlib import Path
from uuid import uuid4

CREDENTIALS_DIR_ENV = "AUTOSHORTS_CREDENTIALS_DIR"
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def default_credentials_dir() -> Path:
    configured = os.environ.get(CREDENTIALS_DIR_ENV)
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / ".config" / "autoshorts" / "youtube-credentials").resolve()


class FilesystemCredentialStore:
    def __init__(self, root: str | Path | None = None) -> None:
        self.root = (
            Path(root).expanduser().resolve() if root else default_credentials_dir()
        )

    def write(self, publishing_channel_id: str, payload: str) -> str:
        path = self._path(publishing_channel_id)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            temporary.replace(path)
            os.chmod(path, 0o600)
        finally:
            temporary.unlink(missing_ok=True)
        return path.name

    def read(self, credential_ref: str) -> str:
        return self._path_from_ref(credential_ref).read_text(encoding="utf-8")

    def delete(self, credential_ref: str) -> bool:
        path = self._path_from_ref(credential_ref)
        if not path.exists():
            return False
        path.unlink()
        return True

    def _path(self, publishing_channel_id: str) -> Path:
        self._validate_id(publishing_channel_id)
        return self.root / f"{publishing_channel_id}.json"

    def _path_from_ref(self, credential_ref: str) -> Path:
        if not credential_ref.endswith(".json"):
            raise ValueError("Invalid credential reference.")
        channel_id = credential_ref.removesuffix(".json")
        path = self._path(channel_id)
        if path.name != credential_ref:
            raise ValueError("Invalid credential reference.")
        return path

    @staticmethod
    def _validate_id(value: str) -> None:
        if not _SAFE_ID.fullmatch(value):
            raise ValueError("Invalid publishing channel ID for credential storage.")
