from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from autoshorts.collection import (
    YouTubeAccess,
    candidate_scan_limit,
    discover_creator_videos,
    download_video,
    friendly_youtube_error,
    resolve_deno,
    youtube_dl_options,
)


def test_youtube_options_without_authentication(monkeypatch):
    monkeypatch.setattr("autoshorts.collection.resolve_deno", lambda: None)
    assert youtube_dl_options(YouTubeAccess()) == {}


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
    assert skipped[0].reason == "unavailable"
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
