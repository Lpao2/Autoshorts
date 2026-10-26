from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from autoshorts.collection import (
    VideoCandidate,
    YouTubeAccess,
    YouTubeCollectionError,
    YouTubeErrorCategory,
    candidate_scan_limit,
    classify_youtube_error,
    discover_creator_videos,
    download_video,
    friendly_youtube_error,
    resolve_deno,
    youtube_dl_options,
)


def test_youtube_options_without_authentication(monkeypatch):
    monkeypatch.setattr("autoshorts.collection.resolve_deno", lambda: None)
    options = youtube_dl_options(YouTubeAccess())
    assert "cookiesfrombrowser" not in options
    assert options["socket_timeout"] == 20
    assert options["retries"] == 1
    assert options["sleep_interval_requests"] == 2.0


@pytest.mark.parametrize("browser", ["brave", "firefox"])
def test_youtube_options_use_selected_browser(browser, monkeypatch):
    monkeypatch.setattr("autoshorts.collection.resolve_deno", lambda: None)
    assert youtube_dl_options(YouTubeAccess(browser))["cookiesfrombrowser"] == (
        browser,
        None,
        None,
        None,
    )


def test_youtube_options_pass_explicit_deno_path(tmp_path):
    deno = tmp_path / "deno"
    assert youtube_dl_options(deno_path=deno)["js_runtimes"] == {
        "deno": {"path": str(deno)}
    }


def test_resolve_deno_uses_user_install_when_not_on_path(tmp_path, monkeypatch):
    deno = tmp_path / ".deno" / "bin" / "deno"
    deno.parent.mkdir(parents=True)
    deno.touch()
    monkeypatch.setattr("autoshorts.collection.shutil.which", lambda _name: None)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    assert resolve_deno() == deno.resolve()


def test_resolve_deno_returns_none_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr("autoshorts.collection.shutil.which", lambda _name: None)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    assert resolve_deno() is None


class FakeYoutubeDL:
    responses = []
    options = []

    def __init__(self, options):
        self.current_options = options
        self.options.append(options)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def extract_info(self, _url, download=False):
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def install_fake_ytdlp(monkeypatch, responses):
    FakeYoutubeDL.responses = list(responses)
    FakeYoutubeDL.options = []
    monkeypatch.setitem(sys.modules, "yt_dlp", SimpleNamespace(YoutubeDL=FakeYoutubeDL))
    monkeypatch.setattr("autoshorts.collection.resolve_deno", lambda: None)


def test_flat_discovery_and_failed_candidate_do_not_abort(monkeypatch):
    flat = {"entries": [{"id": "bad", "title": "Bad"}, {"id": "ok", "title": "Good"}]}
    hydrated = {
        "id": "ok",
        "title": "Good",
        "upload_date": date.today().strftime("%Y%m%d"),
        "duration": 10,
    }
    install_fake_ytdlp(monkeypatch, [flat, RuntimeError("Video unavailable"), hydrated])
    skipped = []
    result = discover_creator_videos(
        "Creator",
        "https://youtube.example/videos",
        quantity=1,
        age_days=30,
        skip_handler=skipped.append,
    )
    assert [item.title for item in result] == ["Good"]
    assert skipped[0].reason == "VIDEO_UNAVAILABLE"
    assert FakeYoutubeDL.options[0]["extract_flat"] == "in_playlist"
    assert FakeYoutubeDL.options[0]["playlistend"] == 20


def test_age_filter_runs_after_hydration(monkeypatch):
    old = (date.today() - timedelta(days=100)).strftime("%Y%m%d")
    flat = {"entries": [{"id": "old", "title": "Old"}]}
    install_fake_ytdlp(monkeypatch, [flat, {"id": "old", "upload_date": old}])
    skipped = []
    assert not discover_creator_videos(
        "Creator",
        "https://youtube.example/videos",
        quantity=1,
        age_days=5,
        skip_handler=skipped.append,
    )
    assert skipped[0].reason == "too_old"


def test_authentication_reaches_discovery_hydration(monkeypatch):
    flat = {"entries": [{"id": "ok", "title": "Good"}]}
    install_fake_ytdlp(monkeypatch, [flat, {"id": "ok", "title": "Good"}])
    discover_creator_videos(
        "Creator",
        "https://youtube.example/videos",
        quantity=1,
        age_days=1,
        access=YouTubeAccess("brave"),
    )
    assert all(
        options["cookiesfrombrowser"] == ("brave", None, None, None)
        for options in FakeYoutubeDL.options
    )


def test_authentication_reaches_download(tmp_path, monkeypatch):
    target = tmp_path / "video.mp4"

    class DownloadYDL(FakeYoutubeDL):
        def extract_info(self, _url, download=False):
            if download:
                target.touch()
            return {"id": "video"}

        def prepare_filename(self, _info):
            return str(target)

    DownloadYDL.options = []
    monkeypatch.setitem(sys.modules, "yt_dlp", SimpleNamespace(YoutubeDL=DownloadYDL))
    monkeypatch.setattr("autoshorts.collection.resolve_deno", lambda: None)
    candidate = SimpleNamespace(url="https://youtube.example/watch?v=video")
    result = download_video(candidate, tmp_path, access=YouTubeAccess("firefox"))
    assert result == target
    assert DownloadYDL.options[0]["cookiesfrombrowser"] == (
        "firefox",
        None,
        None,
        None,
    )


def test_scan_limit_is_bounded():
    assert candidate_scan_limit(1) == 20
    assert candidate_scan_limit(10) == 50
    assert candidate_scan_limit(1000) == 100


def test_flat_duration_filter_avoids_unnecessary_hydration(monkeypatch):
    flat = {
        "entries": [
            {"id": "short", "title": "Short", "duration": 60},
            {"id": "valid", "title": "Valid", "duration": 1200},
        ]
    }
    hydrated = {
        "id": "valid",
        "title": "Valid",
        "duration": 1200,
        "upload_date": date.today().strftime("%Y%m%d"),
    }
    install_fake_ytdlp(monkeypatch, [flat, hydrated])
    skipped = []
    result = discover_creator_videos(
        "Creator",
        "https://youtube.example/videos",
        quantity=1,
        age_days=30,
        min_duration_seconds=900,
        max_duration_seconds=2400,
        skip_handler=skipped.append,
    )
    assert [item.youtube_id for item in result] == ["valid"]
    assert skipped[0].reason == "shorts"
    assert len(FakeYoutubeDL.options) == 2


@pytest.mark.parametrize(
    ("raw", "friendly"),
    [
        (
            "Sign in to confirm you're not a bot",
            "YouTube exige autenticação. Selecione Brave ou Firefox em "
            "Autenticação YouTube.",
        ),
        (
            "Sign in to confirm your age",
            "Este vídeo exige autenticação/validação de idade.",
        ),
        ("HTTP Error 429", "YouTube limitou temporariamente as requisições."),
        (
            "JavaScript runtime Deno not found",
            "Runtime JavaScript Deno não encontrado.",
        ),
    ],
)
def test_friendly_errors(raw, friendly):
    assert friendly_youtube_error(raw) == friendly


def test_rate_limit_stops_hydration_immediately(monkeypatch):
    flat = {"entries": [{"id": "one"}, {"id": "two"}]}
    install_fake_ytdlp(monkeypatch, [flat, RuntimeError("HTTP Error 429")])
    failures = []
    with pytest.raises(YouTubeCollectionError) as raised:
        discover_creator_videos(
            "Creator",
            "https://youtube.example/videos",
            quantity=1,
            age_days=30,
            failure_handler=lambda *args: failures.append(args),
        )
    assert raised.value.category is YouTubeErrorCategory.RATE_LIMITED
    assert len(FakeYoutubeDL.options) == 2
    assert failures[0][0] == "one"


@pytest.mark.parametrize(
    "message",
    [
        "Sign in to confirm your age",
        "Join this channel for members-only content",
    ],
)
def test_restricted_video_skipped_during_hydration(monkeypatch, message):
    flat = {"entries": [{"id": "blocked"}, {"id": "accessible"}]}
    install_fake_ytdlp(
        monkeypatch,
        [
            flat,
            RuntimeError(message),
            {
                "id": "accessible",
                "title": "Good",
                "duration": 1200,
                "upload_date": date.today().strftime("%Y%m%d"),
                "webpage_url": "https://youtube.com/watch?v=accessible",
            },
        ],
    )
    failures = []
    videos = discover_creator_videos(
        "Creator",
        "https://youtube.com/@creator/videos",
        quantity=1,
        age_days=30,
        failure_handler=lambda *args: failures.append(args),
    )
    assert videos[0].youtube_id == "accessible"
    assert failures[0][1] == YouTubeErrorCategory.AUTH_REQUIRED


@pytest.mark.parametrize(
    ("error", "category"),
    (
        (
            "Sign in to confirm you're not a bot",
            YouTubeErrorCategory.ANTI_BOT_CHALLENGE,
        ),
        ("login required", YouTubeErrorCategory.AUTH_REQUIRED),
        ("Video unavailable", YouTubeErrorCategory.VIDEO_UNAVAILABLE),
        (TimeoutError("timed out"), YouTubeErrorCategory.NETWORK_ERROR),
        ("other", YouTubeErrorCategory.DOWNLOAD_ERROR),
    ),
)
def test_youtube_error_classification(error, category):
    assert classify_youtube_error(error) is category


def test_cookie_file_requires_private_permissions(tmp_path):
    cookie = tmp_path / "cookies.txt"
    cookie.write_text("secret-cookie", encoding="utf-8")
    cookie.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        YouTubeAccess(cookie_file=cookie)
    cookie.chmod(0o600)
    access = YouTubeAccess(cookie_file=cookie)
    assert youtube_dl_options(access)["cookiefile"] == str(cookie.resolve())
    assert "secret-cookie" not in repr(access)


def test_download_uses_single_extract_request(tmp_path, monkeypatch):
    target = tmp_path / "video.mp4"

    class OneCallYDL(FakeYoutubeDL):
        calls = 0

        def extract_info(self, _url, download=False):
            self.__class__.calls += 1
            assert download is True
            target.touch()
            return {"id": "video"}

        def prepare_filename(self, _info):
            return str(target)

    monkeypatch.setitem(sys.modules, "yt_dlp", SimpleNamespace(YoutubeDL=OneCallYDL))
    monkeypatch.setattr("autoshorts.collection.resolve_deno", lambda: None)
    candidate = VideoCandidate(
        "Creator", "https://youtube.com/watch?v=video", "Video", None, 1200, "video"
    )
    assert download_video(candidate, tmp_path) == target
    assert OneCallYDL.calls == 1
