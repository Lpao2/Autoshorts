"""Resilient YouTube collection through the yt-dlp Python API."""

from __future__ import annotations

import os
import shutil
import socket
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from pathlib import Path
from threading import Event
from typing import Any, Literal
from urllib.parse import parse_qs, urlparse

from autoshorts.automation_events import AutomationCancelled, check_automation_cancelled

BrowserName = Literal["brave", "firefox"]


@dataclass(frozen=True, slots=True)
class YouTubeAccess:
    """Conservative yt-dlp access policy; it never contains cookie data."""

    browser: BrowserName | None = None
    cookie_file: Path | None = None
    timeout_seconds: int = 20
    retries: int = 1
    fragment_retries: int = 1
    pacing_seconds: float = 2.0

    def __post_init__(self) -> None:
        if self.browser not in {None, "brave", "firefox"}:
            raise ValueError("browser must be none, brave or firefox")
        if self.browser and self.cookie_file:
            raise ValueError("Configure browser cookies or cookie_file, not both.")
        if self.timeout_seconds < 1 or self.retries < 0 or self.fragment_retries < 0:
            raise ValueError("YouTube timeout/retry settings are invalid.")
        if self.pacing_seconds < 0:
            raise ValueError("YouTube pacing cannot be negative.")
        if self.cookie_file is not None:
            path = Path(self.cookie_file).expanduser().resolve()
            if not path.is_file():
                raise ValueError("Configured YouTube cookie file does not exist.")
            if path.stat().st_mode & 0o077:
                raise ValueError("YouTube cookie file permissions must be 0600.")
            object.__setattr__(self, "cookie_file", path)

    @classmethod
    def from_environment(cls) -> YouTubeAccess:
        browser = os.environ.get("AUTOSHORTS_YOUTUBE_BROWSER") or None
        cookie = os.environ.get("AUTOSHORTS_YOUTUBE_COOKIE_FILE") or None
        return cls(
            browser=browser,  # type: ignore[arg-type]
            cookie_file=Path(cookie) if cookie else None,
            timeout_seconds=int(os.environ.get("AUTOSHORTS_YOUTUBE_TIMEOUT", "20")),
            retries=int(os.environ.get("AUTOSHORTS_YOUTUBE_RETRIES", "1")),
            fragment_retries=int(
                os.environ.get("AUTOSHORTS_YOUTUBE_FRAGMENT_RETRIES", "1")
            ),
            pacing_seconds=float(
                os.environ.get("AUTOSHORTS_YOUTUBE_PACING_SECONDS", "2")
            ),
        )

    @property
    def mode(self) -> str:
        if self.browser:
            return f"cookies-from-browser:{self.browser}"
        if self.cookie_file:
            return "cookie-file"
        return "anonymous"


class YouTubeErrorCategory(StrEnum):
    RATE_LIMITED = "RATE_LIMITED"
    ANTI_BOT_CHALLENGE = "ANTI_BOT_CHALLENGE"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    VIDEO_UNAVAILABLE = "VIDEO_UNAVAILABLE"
    NETWORK_ERROR = "NETWORK_ERROR"
    DOWNLOAD_ERROR = "DOWNLOAD_ERROR"


class YouTubeCollectionError(RuntimeError):
    def __init__(self, category: YouTubeErrorCategory, message: str) -> None:
        super().__init__(message)
        self.category = category


@dataclass(frozen=True, slots=True)
class VideoCandidate:
    creator: str
    url: str
    title: str
    upload_date: date | None
    duration_seconds: int | None
    youtube_id: str | None = None


@dataclass(frozen=True, slots=True)
class CandidateSkip:
    creator: str
    title: str
    url: str
    reason: str
    detail: str


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
SkipHandler = Callable[[CandidateSkip], None]


def probe_video_metadata(video_id: str, access: YouTubeAccess) -> VideoCandidate:
    """Perform exactly one no-download metadata request for access preflight."""
    video_id = video_id.strip()
    if not video_id:
        raise ValueError("video_id cannot be empty.")
    url = f"https://www.youtube.com/watch?v={video_id}"
    options = {
        **youtube_dl_options(access),
        "quiet": True,
        "skip_download": True,
        "noplaylist": True,
    }
    try:
        with _require_yt_dlp().YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        raise YouTubeCollectionError(
            classify_youtube_error(exc), friendly_youtube_error(exc)
        ) from exc
    if not info:
        raise YouTubeCollectionError(
            YouTubeErrorCategory.DOWNLOAD_ERROR,
            "yt-dlp did not return metadata.",
        )
    return VideoCandidate(
        str(info.get("uploader") or "YouTube"),
        _entry_url(info) or url,
        str(info.get("title") or "Sem título"),
        _parse_upload_date(info.get("upload_date")),
        _optional_int(info.get("duration")),
        str(info.get("id") or video_id),
    )


def _require_yt_dlp() -> Any:
    try:
        import yt_dlp
    except ImportError as exc:
        raise RuntimeError(
            "A coleta automática exige yt-dlp. Instale com: "
            ".venv/bin/python -m pip install -e '.[automation]'"
        ) from exc
    return yt_dlp


def resolve_deno() -> Path | None:
    """Find Deno both in PATH and in its standard per-user installation."""
    found = shutil.which("deno")
    if found:
        return Path(found).resolve()
    fallback = Path.home() / ".deno" / "bin" / "deno"
    return fallback.resolve() if fallback.is_file() else None


def youtube_dl_options(
    access: YouTubeAccess | None = None, *, deno_path: Path | None = None
) -> dict[str, object]:
    """Build shared authentication/runtime options for every yt-dlp phase."""
    options: dict[str, object] = {}
    access = access or YouTubeAccess()
    if access.browser:
        options["cookiesfrombrowser"] = (access.browser, None, None, None)
    if access.cookie_file:
        options["cookiefile"] = str(access.cookie_file)
    options.update(
        {
            "socket_timeout": access.timeout_seconds,
            "retries": access.retries,
            "extractor_retries": access.retries,
            "fragment_retries": access.fragment_retries,
            "sleep_interval_requests": access.pacing_seconds,
        }
    )
    deno = deno_path if deno_path is not None else resolve_deno()
    if deno is not None:
        options["js_runtimes"] = {"deno": {"path": str(deno)}}
    return options


def candidate_scan_limit(quantity: int) -> int:
    return min(max(quantity * 5, 20), 100)


def discover_creator_videos(
    creator_name: str,
    videos_url: str,
    *,
    quantity: int,
    age_days: int,
    min_duration_seconds: int | None = None,
    max_duration_seconds: int | None = None,
    access: YouTubeAccess | None = None,
    skip_handler: SkipHandler | None = None,
    cancel: Event | None = None,
    candidate_allowed: Callable[[str], bool] | None = None,
    failure_handler: Callable[[str, YouTubeErrorCategory, str], None] | None = None,
    metadata_lookup: Callable[[str], VideoCandidate | None] | None = None,
    metadata_save: Callable[[VideoCandidate], None] | None = None,
) -> list[VideoCandidate]:
    """Flat-discover a channel, then hydrate candidates independently."""
    if quantity < 1 or age_days < 0:
        raise ValueError("quantity must be positive and age_days cannot be negative")
    yt_dlp = _require_yt_dlp()
    limit = candidate_scan_limit(quantity)
    shared = youtube_dl_options(access)
    flat_options = {
        **shared,
        "quiet": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "playlistend": limit,
        "ignoreerrors": True,
    }
    try:
        with yt_dlp.YoutubeDL(flat_options) as ydl:
            info = ydl.extract_info(videos_url, download=False)
    except Exception as exc:
        raise YouTubeCollectionError(
            classify_youtube_error(exc), safe_youtube_error(exc)
        ) from exc
    cutoff = date.today() - timedelta(days=age_days)
    candidates: list[VideoCandidate] = []
    for entry in (info or {}).get("entries") or []:
        if len(candidates) >= quantity:
            break
        check_automation_cancelled(cancel)
        if not entry:
            continue
        title = str(entry.get("title") or "Sem título")
        url = _entry_url(entry)
        if not url:
            _skip(skip_handler, creator_name, title, "", "unavailable", "URL ausente")
            continue
        entry_id = str(
            entry.get("id")
            or candidate_youtube_id(
                VideoCandidate(creator_name, url, title, None, None)
            )
            or ""
        )
        if entry_id and candidate_allowed and not candidate_allowed(entry_id):
            _skip(skip_handler, creator_name, title, url, "cooldown", entry_id)
            continue
        flat_duration = _optional_int(entry.get("duration"))
        if (
            flat_duration is not None
            and min_duration_seconds is not None
            and flat_duration < min_duration_seconds
        ):
            reason = "shorts" if flat_duration <= 180 else "duration"
            _skip(
                skip_handler,
                creator_name,
                title,
                url,
                reason,
                f"{flat_duration}s",
            )
            continue
        if (
            flat_duration is not None
            and max_duration_seconds is not None
            and flat_duration > max_duration_seconds
        ):
            _skip(
                skip_handler,
                creator_name,
                title,
                url,
                "duration",
                f"{flat_duration}s",
            )
            continue
        cached = metadata_lookup(entry_id) if entry_id and metadata_lookup else None
        if cached is not None:
            if (
                cached.duration_seconds is not None
                and min_duration_seconds is not None
                and cached.duration_seconds < min_duration_seconds
            ):
                continue
            if (
                cached.duration_seconds is not None
                and max_duration_seconds is not None
                and cached.duration_seconds > max_duration_seconds
            ):
                continue
            if cached.upload_date is not None and cached.upload_date < cutoff:
                _skip(
                    skip_handler,
                    creator_name,
                    cached.title,
                    cached.url,
                    "too_old",
                    str(cached.upload_date),
                )
                continue
            candidates.append(cached)
            continue
        try:
            hydrate_options = {
                **shared,
                "quiet": True,
                "skip_download": True,
                "noplaylist": True,
            }
            with yt_dlp.YoutubeDL(hydrate_options) as ydl:
                hydrated = ydl.extract_info(url, download=False)
            if not hydrated:
                raise RuntimeError("yt-dlp não retornou metadados")
        except Exception as exc:
            category = classify_youtube_error(exc)
            if entry_id and failure_handler:
                failure_handler(entry_id, category, safe_youtube_error(exc))
            _skip(
                skip_handler,
                creator_name,
                title,
                url,
                category.value,
                safe_youtube_error(exc),
            )
            if category in {
                YouTubeErrorCategory.RATE_LIMITED,
                YouTubeErrorCategory.ANTI_BOT_CHALLENGE,
                YouTubeErrorCategory.NETWORK_ERROR,
            }:
                raise YouTubeCollectionError(
                    category, friendly_youtube_error(exc)
                ) from exc
            continue
        title = str(hydrated.get("title") or title)
        final_url = _entry_url(hydrated) or url
        live_status = hydrated.get("live_status")
        if hydrated.get("is_live") or live_status == "is_live":
            _skip(skip_handler, creator_name, title, final_url, "live", "live")
            continue
        if live_status == "is_upcoming":
            _skip(skip_handler, creator_name, title, final_url, "upcoming", "upcoming")
            continue
        upload_date = _parse_upload_date(hydrated.get("upload_date"))
        if upload_date is not None and upload_date < cutoff:
            _skip(
                skip_handler,
                creator_name,
                title,
                final_url,
                "too_old",
                str(upload_date),
            )
            continue
        candidate = VideoCandidate(
            creator_name,
            final_url,
            title,
            upload_date,
            int(hydrated["duration"]) if hydrated.get("duration") else None,
            str(hydrated["id"]) if hydrated.get("id") else entry_id or None,
        )
        candidates.append(candidate)
        if metadata_save:
            metadata_save(candidate)
    return candidates


def download_video(
    candidate: VideoCandidate,
    destination: str | Path,
    *,
    access: YouTubeAccess | None = None,
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
        **youtube_dl_options(access),
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/mp4",
        "merge_output_format": "mp4",
        "outtmpl": str(output_dir / "%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [progress_hook],
    }
    check_automation_cancelled(cancel)
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            video_id = candidate_youtube_id(candidate)
            prepared = output_dir / f"{video_id}.mp4" if video_id else None
            if prepared is not None and prepared.is_file():
                return prepared
            info = ydl.extract_info(candidate.url, download=True)
            prepared = Path(ydl.prepare_filename(info)).with_suffix(".mp4")
            if prepared.is_file():
                if progress_handler is not None:
                    progress_handler(
                        DownloadProgress(
                            "reused", str(prepared), None, None, False, None, None, 1.0
                        )
                    )
                return prepared
    except Exception as exc:
        if isinstance(exc, AutomationCancelled) or (
            cancel is not None and cancel.is_set()
        ):
            _cleanup_partials(output_dir)
            raise AutomationCancelled("Download cancelado pelo usuário.") from exc
        raise YouTubeCollectionError(
            classify_youtube_error(exc), friendly_youtube_error(exc)
        ) from exc
    if not prepared.is_file():
        raise RuntimeError(f"yt-dlp não criou o arquivo esperado: {prepared}")
    return prepared


def classify_youtube_error(error: BaseException | str) -> YouTubeErrorCategory:
    text = str(error).lower()
    if "http error 429" in text or "http 429" in text or "too many requests" in text:
        return YouTubeErrorCategory.RATE_LIMITED
    if "not a bot" in text or "challenge" in text:
        return YouTubeErrorCategory.ANTI_BOT_CHALLENGE
    if any(
        marker in text
        for marker in (
            "sign in",
            "authentication",
            "login required",
            "age-restricted",
            "confirm your age",
            "members-only",
            "members only",
            "join this channel",
        )
    ):
        return YouTubeErrorCategory.AUTH_REQUIRED
    if "unavailable" in text or "private video" in text or "removed" in text:
        return YouTubeErrorCategory.VIDEO_UNAVAILABLE
    if isinstance(error, (TimeoutError, socket.timeout)) or any(
        item in text for item in ("timed out", "network", "connection", "dns")
    ):
        return YouTubeErrorCategory.NETWORK_ERROR
    return YouTubeErrorCategory.DOWNLOAD_ERROR


def safe_youtube_error(error: BaseException | str) -> str:
    """Return a bounded message without cookie paths or yt-dlp option values."""
    return friendly_youtube_error(error)[:500]


def candidate_youtube_id(candidate: VideoCandidate) -> str | None:
    youtube_id = getattr(candidate, "youtube_id", None)
    if youtube_id:
        return youtube_id
    parsed = urlparse(candidate.url)
    if parsed.hostname in {"youtu.be", "www.youtu.be"}:
        return parsed.path.strip("/") or None
    values = parse_qs(parsed.query).get("v")
    return values[0] if values else None


def friendly_youtube_error(error: BaseException | str) -> str:
    text = str(error)
    lowered = text.lower()
    if "sign in to confirm you're not a bot" in lowered:
        return (
            "YouTube exige autenticação. Selecione Brave ou Firefox em "
            "Autenticação YouTube."
        )
    if "sign in to confirm your age" in lowered or "age-restricted" in lowered:
        return "Este vídeo exige autenticação/validação de idade."
    if "http error 429" in lowered or "http 429" in lowered:
        return "YouTube limitou temporariamente as requisições."
    if "javascript runtime" in lowered or (
        "deno" in lowered and "not found" in lowered
    ):
        return "Runtime JavaScript Deno não encontrado."
    return text


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
        str(raw.get("status") or "unknown"),
        str(raw["filename"]) if raw.get("filename") else None,
        downloaded,
        total,
        exact_total is None and estimated_total is not None,
        _optional_float(raw.get("speed")),
        _optional_int(raw.get("eta")),
        percent,
    )


def _entry_url(entry: dict[str, Any]) -> str:
    value = entry.get("webpage_url") or entry.get("original_url") or entry.get("url")
    if value and str(value).startswith(("http://", "https://")):
        return str(value)
    video_id = entry.get("id") or value
    return f"https://www.youtube.com/watch?v={video_id}" if video_id else ""


def _skip(
    handler: SkipHandler | None,
    creator: str,
    title: str,
    url: str,
    reason: str,
    detail: str,
) -> None:
    if handler:
        handler(CandidateSkip(creator, title, url, reason, detail))


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
