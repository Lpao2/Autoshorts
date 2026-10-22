"""Playwright browser controller for the existing localhost OAuth flow."""

from __future__ import annotations

import re
from pathlib import Path


class PlaywrightOAuthBrowser:
    """Open OAuth in a GoogleIdentity profile and assist only unambiguous UI."""

    def __init__(self, profile_path: Path, expected_channel_name: str) -> None:
        self.profile_path = profile_path
        self.expected_channel_name = expected_channel_name
        self.status = "PENDING"
        self._playwright = None
        self.context = None
        self.page = None

    def open(self, url: str, new: int = 0, autoraise: bool = True) -> bool:
        del new, autoraise
        from playwright.sync_api import sync_playwright

        self.profile_path.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        self.context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_path),
            headless=False,
            no_viewport=True,
            args=["--start-maximized"],
        )
        self.page = (
            self.context.pages[0] if self.context.pages else self.context.new_page()
        )
        self.page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        self.page.wait_for_timeout(1_000)
        self._assist()
        return True

    def _assist(self) -> None:
        assert self.page is not None
        body = self.page.locator("body").inner_text().casefold()
        challenges = (
            "enter your password",
            "digite sua senha",
            "2-step verification",
            "verificação em duas etapas",
            "captcha",
            "verify it's you",
            "confirme que é você",
        )
        if any(text in body for text in challenges):
            self.status = "NEEDS_HUMAN"
            return
        matches = self.page.get_by_text(
            re.compile(rf"^{re.escape(self.expected_channel_name)}$", re.I),
            exact=True,
        )
        visible = [
            matches.nth(index)
            for index in range(matches.count())
            if matches.nth(index).is_visible()
        ]
        if len(visible) != 1:
            self.status = "NEEDS_HUMAN"
            return
        visible[0].click()
        self.page.wait_for_timeout(750)
        self.status = "CHANNEL_SELECTED"
        self._continue_when_unambiguous()

    def _continue_when_unambiguous(self) -> None:
        assert self.page is not None
        button = self.page.get_by_role(
            "button",
            name=re.compile(r"^(Continue|Continuar|Allow|Permitir)$", re.I),
        )
        visible = [
            button.nth(index)
            for index in range(button.count())
            if button.nth(index).is_visible()
        ]
        if len(visible) == 1:
            visible[0].click()
            self.status = "CONSENT_CONTINUED"

    def close(self) -> None:
        if self.context is not None:
            self.context.close()
        if self._playwright is not None:
            self._playwright.stop()
        self.context = self.page = self._playwright = None
