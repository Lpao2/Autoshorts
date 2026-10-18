"""Optional YouTube collection through yt-dlp.

The adapter is deliberately independent of browser sessions and AI providers.
Authenticated cookies, if ever needed, must be supplied by the caller and kept
outside the repository.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from threading import Event
from typing import Any

from autoshorts.automation_events import (
    AutomationCancelled,
    check_automation_cancelled,
)


@dataclass(frozen=True, slots=True)
class VideoCandidate:
    creator: str
    url: str
    title: str
    upload_date: date | None
    duration_seconds: int | None


@dataclass(frozen=True, slots=True)
class DownloadProgress:
    status: str
    filename: str | None
    downloaded_bytes: int | None
    total_bytes: int | None
    total_is_estimate: bool
    speed: float | None
    eta: int | None
    percent: float | None


ProgressHandler = Callable[[DownloadProgress], None]


def _require_yt_dlp() -> Any:
    try:
        import yt_dlp
    except ImportError as exc:
        raise RuntimeError(
            "A coleta automática exige yt-dlp. Instale com: "
            ".venv/bin/python -m pip install -e '.[automation]'"
        ) from exc
    return yt_dlp


def discover_creator_videos(
    creator_name: str,
    videos_url: str,
    *,
    quantity: int,
    age_days: int,
) -> list[VideoCandidate]:
    """Discover recent non-live videos from a creator's videos page."""
    if quantity < 1 or age_days < 0:
        raise ValueError("quantity must be positive and age_days cannot be negative")
    yt_dlp = _require_yt_dlp()
    with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True}) as ydl:
        info = ydl.extract_info(videos_url, download=False)
    cutoff = date.today() - timedelta(days=age_days)
    candidates: list[VideoCandidate] = []
    for entry in info.get("entries") or []:
        if (
            not entry
            or entry.get("is_live")
            or entry.get("live_status")
            in {
                "is_live",
                "is_upcoming",
            }
        ):
            continue
        upload_date = _parse_upload_date(entry.get("upload_date"))
        if upload_date is not None and upload_date < cutoff:
            continue
        url = entry.get("webpage_url") or entry.get("url")
        if not url:
            continue
        candidates.append(
            VideoCandidate(
                creator=creator_name,
                url=str(url),
                title=str(entry.get("title") or "untitled"),
                upload_date=upload_date,
                duration_seconds=(
                    int(entry["duration"]) if entry.get("duration") else None
                ),
            )
        )
        if len(candidates) >= quantity:
            break
    return candidates


def download_video(
    candidate: VideoCandidate,
    destination: str | Path,
    *,
    cookie_file: str | Path | None = None,
    progress_handler: ProgressHandler | None = None,
    cancel: Event | None = None,
) -> Path:
    """Download one candidate to a deterministic MP4 path."""
    yt_dlp = _require_yt_dlp()
    output_dir = Path(destination)
    output_dir.mkdir(parents=True, exist_ok=True)

    def progress_hook(raw: dict[str, object]) -> None:
        check_automation_cancelled(cancel)
        if progress_handler is not None:
            progress_handler(download_progress_from_hook(raw))

    options: dict[str, object] = {
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/mp4",
        "merge_output_format": "mp4",
        "outtmpl": str(output_dir / "%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [progress_hook],
    }
    if cookie_file is not None:
        options["cookiefile"] = str(cookie_file)
    check_automation_cancelled(cancel)
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(candidate.url, download=False)
            prepared = Path(ydl.prepare_filename(info)).with_suffix(".mp4")
            if prepared.is_file():
                if progress_handler is not None:
                    progress_handler(
                        DownloadProgress(
                            "reused", str(prepared), None, None, False, None, None, 1.0
                        )
                    )
                return prepared
            info = ydl.extract_info(candidate.url, download=True)
            prepared = Path(ydl.prepare_filename(info)).with_suffix(".mp4")
    except Exception as exc:
        if isinstance(exc, AutomationCancelled) or (
            cancel is not None and cancel.is_set()
        ):
            _cleanup_partials(output_dir)
            raise AutomationCancelled("Download cancelado pelo usuário.") from exc
        raise
    if not prepared.is_file():
        raise RuntimeError(f"yt-dlp did not create the expected file: {prepared}")
    return prepared


def download_progress_from_hook(raw: dict[str, object]) -> DownloadProgress:
    downloaded = _optional_int(raw.get("downloaded_bytes"))
    exact_total = _optional_int(raw.get("total_bytes"))
    estimated_total = _optional_int(raw.get("total_bytes_estimate"))
    total = exact_total or estimated_total
    percent = None
    if downloaded is not None and total:
        percent = min(1.0, max(0.0, downloaded / total))
    if raw.get("status") == "finished":
        percent = 1.0
    return DownloadProgress(
        status=str(raw.get("status") or "unknown"),
        filename=str(raw["filename"]) if raw.get("filename") else None,
        downloaded_bytes=downloaded,
        total_bytes=total,
        total_is_estimate=exact_total is None and estimated_total is not None,
        speed=_optional_float(raw.get("speed")),
        eta=_optional_int(raw.get("eta")),
        percent=percent,
    )


def _optional_int(value: object) -> int | None:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: object) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _cleanup_partials(directory: Path) -> None:
    for pattern in ("*.part", "*.part-*", "*.ytdl"):
        for path in directory.glob(pattern):
            path.unlink(missing_ok=True)


def _parse_upload_date(value: object) -> date | None:
    if not value:
        return None
    text = str(value)
    if len(text) != 8 or not text.isdigit():
        return None
    return date(int(text[:4]), int(text[4:6]), int(text[6:]))
