"""Cross-platform naming helpers."""

from __future__ import annotations

import re


def sanitize_filename(value: str) -> str:
    """Convert a title into a safe, bounded filename component."""

    normalized = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "", value.strip())
    normalized = re.sub(r"\s+", "_", normalized).strip(" ._")
    return normalized[:80].rstrip(" .") or "untitled_clip"
