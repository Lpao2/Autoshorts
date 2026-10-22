"""Shared Playwright browser executable resolution."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

BROWSER_EXECUTABLE_ENV = "AUTOSHORTS_BROWSER_EXECUTABLE"
BRAVE_COMMANDS = ("brave-browser", "brave", "brave-browser-stable")


def resolve_browser_executable() -> Path | None:
    configured = os.environ.get(BROWSER_EXECUTABLE_ENV)
    if configured:
        path = Path(configured).expanduser().resolve()
        if not path.is_file() or not os.access(path, os.X_OK):
            raise ValueError(
                f"{BROWSER_EXECUTABLE_ENV} is not an executable file: {path}"
            )
        return path
    for command in BRAVE_COMMANDS:
        found = shutil.which(command)
        if found:
            return Path(found).resolve()
    return None


def browser_display_name(executable: Path | None) -> str:
    if executable is None:
        return "Playwright Chromium"
    if "brave" in executable.name.casefold():
        return "Brave"
    return executable.name


def persistent_context_browser_options() -> dict[str, str]:
    executable = resolve_browser_executable()
    return {"executable_path": str(executable)} if executable else {}
