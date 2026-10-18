"""Resilient YouTube collection through the yt-dlp Python API."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from threading import Event
from typing import Any, Literal
from urllib.parse import parse_qs, urlparse

from autoshorts.automation_events import AutomationCancelled, check_automation_cancelled

BrowserName = Literal["brave", "firefox"]


@dataclass(frozen=True, slots=True)
class YouTubeAccess:
    """Selects a local browser profile; it never contains cookie data."""

    browser: BrowserName | None = None

    def __post_init__(self) -> None:
        if self.browser not in {None, "brave", "firefox"}:
            raise ValueError("browser must be none, brave or firefox")


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
    access: YouTubeAccess | None = None,
    skip_handler: SkipHandler | None = None,
    cancel: Event | None = None,
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
        raise RuntimeError(friendly_youtube_error(exc)) from exc
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
            _skip(
                skip_handler,
                creator_name,
                title,
                url,
                classify_youtube_error(exc),
                str(exc),
            )
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
        candidates.append(
            VideoCandidate(
                creator_name,
                final_url,
                title,
                upload_date,
                int(hydrated["duration"]) if hydrated.get("duration") else None,
                str(hydrated["id"]) if hydrated.get("id") else None,
            )
        )
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
        raise RuntimeError(friendly_youtube_error(exc)) from exc
    if not prepared.is_file():
        raise RuntimeError(f"yt-dlp não criou o arquivo esperado: {prepared}")
    return prepared


def classify_youtube_error(error: BaseException | str) -> str:
    text = str(error).lower()
    if "sign in to confirm" in text or "authentication" in text:
        return "requires_authentication"
    if "unavailable" in text or "private video" in text or "removed" in text:
        return "unavailable"
    return "metadata_error"


def candidate_youtube_id(candidate: VideoCandidate) -> str | None:
    if candidate.youtube_id:
        return candidate.youtube_id
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
