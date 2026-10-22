from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

from autoshorts import browser_runtime
from autoshorts.browser_runtime import resolve_browser_executable
from autoshorts.youtube_oauth_playwright import PlaywrightOAuthBrowser
from autoshorts.youtube_playwright import PlaywrightYouTubeAdapter


def test_env_browser_executable_has_priority(tmp_path, monkeypatch):
    configured = tmp_path / "custom-browser"
    configured.write_text("browser", encoding="utf-8")
    configured.chmod(0o755)
    monkeypatch.setenv("AUTOSHORTS_BROWSER_EXECUTABLE", str(configured))
    monkeypatch.setattr(browser_runtime.shutil, "which", lambda name: "/bin/brave")
    assert resolve_browser_executable() == configured.resolve()


def test_brave_is_detected_in_priority_order(monkeypatch):
    monkeypatch.delenv("AUTOSHORTS_BROWSER_EXECUTABLE", raising=False)
    checked = []

    def which(name):
        checked.append(name)
        return "/usr/bin/brave" if name == "brave" else None

    monkeypatch.setattr(browser_runtime.shutil, "which", which)
    assert resolve_browser_executable() == Path("/usr/bin/brave")
    assert checked == ["brave-browser", "brave"]


def test_missing_brave_uses_bundled_chromium(monkeypatch):
    monkeypatch.delenv("AUTOSHORTS_BROWSER_EXECUTABLE", raising=False)
    monkeypatch.setattr(browser_runtime.shutil, "which", lambda name: None)
    assert resolve_browser_executable() is None
    assert browser_runtime.persistent_context_browser_options() == {}


def test_provisioning_context_receives_resolved_executable(tmp_path, monkeypatch):
    captured = {}

    class Page:
        closed = False

        def close(self):
            self.closed = True

        def set_default_timeout(self, value):
            pass

    class Context:
        def __init__(self):
            self._pages = [Page()]

        @property
        def pages(self):
            return [page for page in self._pages if not page.closed]

        def new_page(self):
            page = Page()
            self._pages.append(page)
            return page

    class Chromium:
        def launch_persistent_context(self, **kwargs):
            captured.update(kwargs)
            return Context()

    class Playwright:
        chromium = Chromium()

        def stop(self):
            pass

    monkeypatch.setitem(
        sys.modules,
        "playwright.sync_api",
        SimpleNamespace(sync_playwright=lambda: SimpleNamespace(start=Playwright)),
    )
    monkeypatch.setattr(
        "autoshorts.youtube_playwright.persistent_context_browser_options",
        lambda: {"executable_path": "/usr/bin/brave-browser"},
    )
    monkeypatch.setattr(
        "autoshorts.youtube_playwright.resolve_browser_executable",
        lambda: Path("/usr/bin/brave-browser"),
    )
    adapter = PlaywrightYouTubeAdapter(headless=True)
    adapter.open(SimpleNamespace(identity_id="google-b"), tmp_path / "profile")
    assert captured["executable_path"] == "/usr/bin/brave-browser"
    assert captured["user_data_dir"] == str(tmp_path / "profile")


def test_all_persistent_adapters_use_shared_resolution():
    helper = "persistent_context_browser_options"
    assert helper in PlaywrightYouTubeAdapter.open.__code__.co_names
    assert helper in PlaywrightOAuthBrowser.open.__code__.co_names


def test_oauth_context_receives_resolved_executable(tmp_path, monkeypatch):
    captured = {}

    class Page:
        closed = False

        def goto(self, *args, **kwargs):
            pass

        def wait_for_timeout(self, value):
            pass

        def close(self):
            self.closed = True

    class Context:
        def __init__(self):
            self._pages = [Page()]

        @property
        def pages(self):
            return [page for page in self._pages if not page.closed]

        def new_page(self):
            page = Page()
            self._pages.append(page)
            return page

    class Chromium:
        def launch_persistent_context(self, **kwargs):
            captured.update(kwargs)
            return Context()

    class Playwright:
        chromium = Chromium()

        def stop(self):
            pass

    monkeypatch.setitem(
        sys.modules,
        "playwright.sync_api",
        SimpleNamespace(sync_playwright=lambda: SimpleNamespace(start=Playwright)),
    )
    monkeypatch.setattr(
        "autoshorts.youtube_oauth_playwright.persistent_context_browser_options",
        lambda: {"executable_path": "/usr/bin/brave-browser"},
    )
    browser = PlaywrightOAuthBrowser(tmp_path / "google-b", "Sopro Leve")
    monkeypatch.setattr(browser, "_assist", lambda: None)
    assert browser.open("http://127.0.0.1/oauth")
    assert captured["executable_path"] == "/usr/bin/brave-browser"
    assert captured["user_data_dir"] == str(tmp_path / "google-b")
