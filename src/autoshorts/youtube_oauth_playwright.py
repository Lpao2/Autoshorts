"""Playwright controller for the existing localhost YouTube OAuth flow."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from pathlib import Path

from autoshorts.browser_runtime import persistent_context_browser_options

ADVANCED = re.compile(r"^(Avançado|Advanced)$", re.I)
AUTOSHORTS_LINK = re.compile(r"^(Ir para|Go to).*AutoShorts", re.I)
ALLOW = re.compile(r"^(Permitir|Allow)$", re.I)
CONTINUE = re.compile(r"^(Continuar|Continue)$", re.I)
SECURITY_CHALLENGES = (
    "enter your password",
    "digite sua senha",
    "2-step verification",
    "verificação em duas etapas",
    "captcha",
    "recovery email",
    "e-mail de recuperação",
    "verify it's you",
    "confirme que é você",
)


class OAuthUIError(RuntimeError):
    """Safe UI automation failure without OAuth secrets."""


class PlaywrightOAuthBrowser:
    """Automate only explicit, semantically matched OAuth consent actions."""

    def __init__(
        self,
        profile_path: Path,
        expected_channel_name: str,
        *,
        account_key: str | None = None,
        expected_email: str | None = None,
        expected_channel_id: str | None = None,
        protected_channel_names: tuple[str, ...] = (),
        require_confirmations: bool = False,
        confirm_callback: Callable[[str], object] | None = None,
    ) -> None:
        self.profile_path = profile_path
        self.expected_channel_name = expected_channel_name
        self.account_key = account_key
        self.expected_email = expected_email
        self.expected_channel_id = expected_channel_id
        self.protected_channel_names = protected_channel_names
        self.require_confirmations = require_confirmations
        self.confirm_callback = confirm_callback or input
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
            **persistent_context_browser_options(),
        )
        restored = list(self.context.pages)
        self.page = self.context.new_page()
        for page in restored:
            if page != self.page:
                page.close()
        self.page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        self._assist()
        return True

    def _assist(self, *, timeout: int = 90) -> None:
        assert self.page is not None
        deadline = time.monotonic() + timeout
        account_selected = False
        channel_selected = False
        while time.monotonic() < deadline:
            self._raise_for_security_challenge()
            if not account_selected and self._select_expected_account():
                account_selected = True
                deadline = time.monotonic() + timeout
                continue
            if not channel_selected and self._select_expected_channel():
                channel_selected = True
                deadline = time.monotonic() + timeout
                continue
            if self._click_advanced():
                deadline = time.monotonic() + timeout
                continue
            if self._click_autoshorts_link():
                deadline = time.monotonic() + timeout
                continue
            if self._click_final_consent():
                self.status = "CONSENT_CONTINUED"
                return
            self.page.wait_for_timeout(300)
        self._fail("OAUTH_UI_TIMEOUT")

    def _select_expected_account(self) -> bool:
        if not self.expected_email:
            return False
        assert self.page is not None
        escaped = self.expected_email.replace('"', r'\"')
        locator = self.page.locator(f'[data-identifier="{escaped}"]')
        match = self._unique_visible(locator)
        if match is None:
            locator = self.page.get_by_text(self.expected_email, exact=False)
            match = self._unique_visible(locator)
        if match is None:
            return False
        self._checkpoint(
            "[OAUTH] Conta Google encontrada:\n"
            f"{self.expected_email}\n\n"
            "A automação vai selecionar esta conta.\n"
            "Confira o navegador e pressione ENTER para continuar..."
        )
        match.click()
        self.page.wait_for_timeout(750)
        self.status = "ACCOUNT_SELECTED"
        return True

    def _select_expected_channel(self) -> bool:
        assert self.page is not None
        if self.expected_channel_name.casefold() in {
            item.casefold() for item in self.protected_channel_names
        }:
            self._fail("OAUTH_PROTECTED_CHANNEL")
        locator = self.page.get_by_text(
            re.compile(rf"^{re.escape(self.expected_channel_name)}$", re.I),
            exact=True,
        )
        match = self._unique_visible(locator)
        if match is None:
            return False
        self._checkpoint(
            "[OAUTH] Canal/Brand Account alvo encontrado:\n"
            f"{self.expected_channel_name}\n"
            f"Expected Channel ID: {self.expected_channel_id or 'indisponível'}\n\n"
            "Confira visualmente se este é o canal correto.\n"
            "Pressione ENTER para selecionar..."
        )
        match.click()
        self.page.wait_for_timeout(750)
        self.status = "CHANNEL_SELECTED"
        return True

    def _click_advanced(self) -> bool:
        match = self._unique_action("button", ADVANCED)
        if match is None:
            return False
        self._checkpoint(
            "[OAUTH] Tela de aplicativo não verificado detectada.\n"
            'Próxima ação: clicar em "Avançado".\n\n'
            "Pressione ENTER para continuar..."
        )
        match.click()
        self.page.wait_for_timeout(500)
        self.status = "ADVANCED_OPENED"
        return True

    def _click_autoshorts_link(self) -> bool:
        match = self._unique_action("link", AUTOSHORTS_LINK)
        if match is None:
            return False
        label = self._label(match)
        self._checkpoint(
            f'[OAUTH] Link detectado:\n"{label}"\n\n'
            "Pressione ENTER para continuar..."
        )
        match.click()
        self.page.wait_for_timeout(750)
        self.status = "UNVERIFIED_APP_ACCEPTED"
        return True

    def _click_final_consent(self) -> bool:
        match = self._unique_action("button", ALLOW)
        if match is None:
            match = self._unique_action("button", CONTINUE)
        if match is None:
            return False
        label = self._label(match)
        assert self.page is not None
        self._checkpoint(
            "[OAUTH] Tela de consentimento encontrada.\n"
            "Aplicativo: AutoShorts\n"
            f"Account key: {self.account_key or 'indisponível'}\n"
            f"Google: {self.expected_email or 'indisponível'}\n"
            f"Canal alvo: {self.expected_channel_name}\n"
            f"Expected Channel ID: {self.expected_channel_id or 'indisponível'}\n"
            f"URL atual: {self.page.url}\n"
            f"Título: {self.page.title()}\n"
            f"Botão: {label}\n\n"
            f'Pressione ENTER para clicar em "{label}"...'
        )
        match.click()
        return True

    def _raise_for_security_challenge(self) -> None:
        assert self.page is not None
        body = self.page.locator("body").inner_text().casefold()
        if any(text in body for text in SECURITY_CHALLENGES):
            self._fail("NEEDS_HUMAN")

    def _unique_role(self, role: str, name: re.Pattern[str]):
        assert self.page is not None
        return self._unique_visible(self.page.get_by_role(role, name=name))

    def _unique_action(self, role: str, name: re.Pattern[str]):
        match = self._unique_role(role, name)
        if match is not None:
            return match
        assert self.page is not None
        return self._unique_visible(self.page.get_by_text(name, exact=False))

    @staticmethod
    def _unique_visible(locator):
        visible = [
            locator.nth(index)
            for index in range(locator.count())
            if locator.nth(index).is_visible()
        ]
        return visible[0] if len(visible) == 1 else None

    @staticmethod
    def _label(element) -> str:
        try:
            return element.inner_text().strip()
        except Exception:
            return element.get_attribute("aria-label") or "action"

    def _checkpoint(self, message: str) -> None:
        if self.require_confirmations:
            self.confirm_callback(message)

    def _fail(self, reason: str) -> None:
        self.status = reason
        raise OAuthUIError(self._diagnostic(reason))

    def _diagnostic(self, reason: str) -> str:
        assert self.page is not None
        try:
            title = self.page.title()
        except Exception:
            title = "<unavailable>"
        try:
            headings = " | ".join(
                self.page.get_by_role("heading").all_inner_texts()[:5]
            )
        except Exception:
            headings = "<unavailable>"
        return (
            f"{reason}; url={self.page.url}; title={title}; headings={headings}"
        )

    def close(self) -> None:
        if self.context is not None:
            self.context.close()
        if self._playwright is not None:
            self._playwright.stop()
        self.context = self.page = self._playwright = None
