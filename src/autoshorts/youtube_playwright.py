"""Visible Playwright adapter for administrative YouTube provisioning."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse

from autoshorts.automation_config import GoogleIdentity
from autoshorts.browser_runtime import (
    browser_display_name,
    persistent_context_browser_options,
    resolve_browser_executable,
)
from autoshorts.youtube_provisioning import (
    ExistingYouTubeChannel,
    HandleConflict,
    NavigationTimeout,
    NeedsHuman,
    ProvisioningRequest,
)

CHANNEL_SWITCHER = (
    "https://www.youtube.com/channel_switcher?next=%2Faccount&feature=settings"
)
ADD_OR_MANAGE = re.compile(
    r"^(Adicionar ou gerenciar canais|Add or manage (?:your )?channels)$", re.I
)
CREATE_CHANNEL = re.compile(r"^(Crie um canal|Criar um canal|Create a channel)$", re.I)
CREATE_CHANNEL_BUTTON = re.compile(r"^(Criar canal|Create channel)$", re.I)


class PlaywrightYouTubeAdapter:
    def __init__(self, *, headless: bool = False) -> None:
        self.headless = headless
        self._playwright = None
        self.context = None
        self.page = None

    def open(self, identity: GoogleIdentity, profile_path: Path) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Playwright is required for YouTube channel provisioning."
            ) from exc
        profile_path.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        self.context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_path),
            headless=self.headless,
            no_viewport=True,
            args=["--start-maximized", "--disable-blink-features=AutomationControlled"],
            **persistent_context_browser_options(),
        )
        restored_pages = list(self.context.pages)
        self.page = self.context.new_page()
        for restored_page in restored_pages:
            if restored_page != self.page:
                restored_page.close()
        executable = resolve_browser_executable()
        print(f"Browser: {browser_display_name(executable)}")
        print(f"Profile: {identity.identity_id}")
        print(f"Restored tabs: {len(restored_pages)}")
        print(f"Provisioning tabs after cleanup: {len(self.context.pages)}")
        self.page.set_default_timeout(20_000)

    def close(self) -> None:
        if self.context:
            self.context.close()
        if self._playwright:
            self._playwright.stop()
        self.context = self.page = self._playwright = None

    def navigate_youtube(
        self,
        url: str,
        *,
        usable: Callable[[], bool],
        attempts: int = 3,
    ) -> None:
        assert self.page is not None
        errors = []
        for attempt in range(1, attempts + 1):
            if self._page_closed():
                self.page = self.context.new_page()
                self.page.set_default_timeout(20_000)
            navigation_completed = False
            print(f"→ Navegando para {url}")
            try:
                self.page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                navigation_completed = True
            except Exception as exc:
                errors.append(f"attempt {attempt}: {exc}")
                current_url = self._current_url()
                usable_now = self._youtube_page_usable(usable)
                print("Navigation timeout")
                print(f"Current URL: {current_url}")
                print(f"Page closed: {str(self._page_closed()).lower()}")
                print(f"YouTube usable: {str(usable_now).lower()}")
                print(f"Attempt: {attempt}/{attempts}")
                if usable_now:
                    return
            if navigation_completed:
                self.page.wait_for_timeout(1_500)
            if not self._page_closed() and usable():
                return
            if attempt < attempts:
                self._prepare_navigation_retry()
                self.page.wait_for_timeout(attempt * 1_000)
        raise NavigationTimeout(
            f"NAVIGATION_TIMEOUT after {attempts} attempts; "
            f"url={self._current_url()}; "
            + " | ".join(errors)
        )

    def _page_closed(self) -> bool:
        if self.page is None:
            return True
        is_closed = getattr(self.page, "is_closed", None)
        return bool(is_closed()) if is_closed else False

    def _current_url(self) -> str:
        return "" if self.page is None else str(getattr(self.page, "url", ""))

    def _youtube_page_usable(self, usable: Callable[[], bool]) -> bool:
        if self._page_closed():
            return False
        parsed = urlparse(self._current_url())
        host = (parsed.hostname or "").casefold()
        if host == "accounts.google.com":
            return False
        if host != "youtube.com" and not host.endswith(".youtube.com"):
            return False
        return usable()

    def _prepare_navigation_retry(self) -> None:
        if self._page_closed():
            assert self.context is not None
            self.page = self.context.new_page()
            self.page.set_default_timeout(20_000)
            return
        try:
            self.page.goto("about:blank", wait_until="commit", timeout=5_000)
        except Exception:
            if self._page_closed():
                assert self.context is not None
                self.page = self.context.new_page()
                self.page.set_default_timeout(20_000)

    def verify_session(self) -> bool:
        assert self.page is not None
        self.navigate_youtube(
            CHANNEL_SWITCHER,
            usable=lambda: bool(self._body()) or "accounts.google.com" in self.page.url,
        )
        text = self._body().casefold()
        return not (
            "accounts.google.com" in self.page.url
            or "fazer login" in text
            or "sign in" in text
        )

    def inspect_channels(self) -> tuple[ExistingYouTubeChannel, ...]:
        assert self.page is not None
        self._open_channel_management()
        cards = self.page.locator("ytd-account-item-renderer")
        found: dict[tuple[str | None, str, str | None], ExistingYouTubeChannel] = {}
        for index in range(cards.count()):
            data = cards.nth(index).evaluate("e => e.data || e.__data || {}")
            name = data.get("accountName", {}).get("simpleText", "").strip()
            handle = data.get("channelHandle", {}).get("simpleText")
            tokens = (
                data.get("serviceEndpoint", {})
                .get("selectActiveIdentityEndpoint", {})
                .get("supportedTokens", [])
            )
            cache_key = next(
                (
                    item["offlineCacheKeyToken"]["clientCacheKey"]
                    for item in tokens
                    if item.get("offlineCacheKeyToken", {}).get("clientCacheKey")
                ),
                None,
            )
            channel_id = f"UC{cache_key}" if cache_key else None
            if name:
                item = ExistingYouTubeChannel(channel_id, name, handle, None)
                found[(item.channel_id, item.name, item.handle)] = item
        return tuple(found.values())

    def create_channel(self, request: ProvisioningRequest) -> ExistingYouTubeChannel:
        assert self.page is not None
        self._open_channel_management()
        self._raise_human()
        create_link = self.page.locator('a[href*="/create_channel"]').first
        if not create_link.count():
            create_link = self.page.get_by_text(CREATE_CHANNEL).first
        if not create_link.count():
            raise RuntimeError("Create-channel action not found.")
        create_link.evaluate("element => element.click()")
        self._wait_for_creation_form()
        name_input = self._input("Nome", "Name")
        handle_input = self._input("Identificador", "Handle")
        selected = None
        for candidate in request.handle_candidates:
            handle_input.fill(candidate.removeprefix("@"))
            handle_input.press("Tab")
            self.page.wait_for_timeout(1_800)
            if not self._handle_conflict():
                selected = candidate
                break
        if selected is None:
            raise HandleConflict(
                "No handle available among " + ", ".join(request.handle_candidates)
            )
        name_input.fill(request.expected_name)
        name_input.press("Tab")
        self.page.wait_for_timeout(1_500)
        handle_input = self._input("Identificador", "Handle")
        handle_input.fill(selected.removeprefix("@"))
        handle_input.press("Tab")
        self.page.wait_for_timeout(1_800)
        self._upload_logo(request.logo_path)
        name_input = self._input("Nome", "Name")
        handle_input = self._input("Identificador", "Handle")
        if name_input.input_value().strip() != request.expected_name:
            name_input.fill(request.expected_name)
            name_input.press("Tab")
        if (
            handle_input.input_value().strip().lstrip("@").casefold()
            != selected.lstrip("@").casefold()
        ):
            handle_input.fill(selected.removeprefix("@"))
            handle_input.press("Tab")
        self.page.wait_for_timeout(1_800)
        actual_name = name_input.input_value().strip()
        actual_handle = "@" + handle_input.input_value().strip().lstrip("@")
        if (
            actual_name != request.expected_name
            or actual_handle.casefold() != selected.casefold()
        ):
            raise RuntimeError(
                f"DOM validation failed: {actual_name!r}/{actual_handle!r}"
            )
        button = self.page.get_by_role(
            "button", name=re.compile(r"^(Criar canal|Create channel)$", re.I)
        ).first
        button.click()
        self.page.wait_for_timeout(7_000)
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            channels = self.inspect_channels()
            matches = [
                item
                for item in channels
                if item.name == request.expected_name
                or (item.handle and item.handle.casefold() == selected.casefold())
            ]
            if matches:
                return matches[0]
            self.page.wait_for_timeout(2_000)
        raise NavigationTimeout("Channel submitted but not confirmed in switcher.")

    def _open_channel_management(self) -> None:
        """Enter channel management by DOM state; YouTube may keep /account."""
        assert self.page is not None
        self.navigate_youtube(CHANNEL_SWITCHER, usable=self._account_page_usable)
        if self._channel_management_usable():
            return
        action = self.page.get_by_text(ADD_OR_MANAGE).first
        if not action.count() or not action.is_visible():
            raise NavigationTimeout(
                "YouTube account loaded without an Add/manage channels action."
            )
        action.click()
        self._wait_for_dom(
            self._channel_management_usable,
            "Channel management did not become usable after Add/manage channels.",
        )

    def _account_page_usable(self) -> bool:
        assert self.page is not None
        action = self.page.get_by_text(ADD_OR_MANAGE).first
        return self._channel_management_usable() or bool(
            action.count() and action.is_visible()
        )

    def _channel_management_usable(self) -> bool:
        assert self.page is not None
        if self.page.locator("ytd-account-item-renderer").count() > 0:
            return True
        create_link = self.page.locator('a[href*="/create_channel"]').first
        if create_link.count() and create_link.is_visible():
            return True
        create_text = self.page.get_by_text(CREATE_CHANNEL).first
        return bool(create_text.count() and create_text.is_visible())

    def _wait_for_creation_form(self) -> None:
        self._wait_for_dom(
            self._creation_form_usable,
            "Channel creation form did not become usable.",
        )

    def _creation_form_usable(self) -> bool:
        assert self.page is not None
        name = self.page.get_by_label(re.compile(r"^(Nome|Name)$", re.I)).first
        handle = self.page.get_by_label(
            re.compile(r"^(Identificador|Handle)$", re.I)
        ).first
        create = self.page.get_by_role("button", name=CREATE_CHANNEL_BUTTON).first
        return all(
            item.count() and item.is_visible() for item in (name, handle, create)
        )

    def _wait_for_dom(
        self, predicate: Callable[[], bool], error: str, *, timeout: int = 30
    ) -> None:
        assert self.page is not None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self._page_closed() and predicate():
                return
            self.page.wait_for_timeout(300)
        raise NavigationTimeout(error)

    def _input(self, *labels: str):
        assert self.page is not None
        for label in labels:
            locator = self.page.get_by_label(label, exact=True)
            if locator.count() and locator.first.is_visible():
                return locator.first
        fields = self.page.locator("input.tp-yt-paper-input")
        if fields.count() == 2:
            requested = {item.casefold() for item in labels}
            return fields.nth(0 if requested & {"nome", "name"} else 1)
        raise RuntimeError(f"Input not found: {labels}")

    def _upload_logo(self, logo: Path) -> None:
        assert self.page is not None
        if not logo.is_file():
            raise FileNotFoundError(f"Logo not found: {logo}")

        before_images = self._visible_image_sources()
        print("→ Abrindo seletor de imagem")
        opener = self.page.get_by_text(
            re.compile(
                r"^(Selecione uma imagem|Selecionar imagem|Escolher imagem|"
                r"Select an image|Choose an image)$",
                re.I,
            )
        ).first
        if not opener.count() or not opener.is_visible():
            raise TimeoutError("'Selecione uma imagem' não está disponível.")
        opener.click()

        computer = self._find_in_frames(
            (
                re.compile(r"^Do computador$", re.I),
                re.compile(r"^Computador$", re.I),
                re.compile(r"^From computer$", re.I),
                re.compile(r"^Computer$", re.I),
            ),
            timeout=20,
            roles=("tab", "button"),
            allow_text=True,
        )
        print("→ Popup de imagem aberto")
        print('→ Abrindo "Do computador"')
        computer.click()
        self.page.wait_for_timeout(1_200)

        upload = self._find_in_frames(
            (
                re.compile(r"^Carregar a partir do computador$", re.I),
                re.compile(r"carregar.*computador", re.I),
                re.compile(r"^Fazer upload do computador$", re.I),
                re.compile(r"^Upload from computer$", re.I),
                re.compile(r"upload.*computer", re.I),
            ),
            timeout=15,
            roles=("button",),
            allow_text=True,
        )
        print('→ Botão "Carregar a partir do computador" encontrado')

        try:
            with self.page.expect_file_chooser(timeout=15_000) as chooser_info:
                upload.click()
            print("→ File chooser aberto")
            chooser_info.value.set_files(str(logo))
        except Exception as exc:
            raise TimeoutError("O botão de upload não abriu o file chooser.") from exc

        print("→ master.png enviado")
        print("→ Aguardando processamento da imagem")
        self.page.wait_for_timeout(4_500)

        try:
            done = self._find_in_frames(
                (
                    re.compile(r"^Concluído$", re.I),
                    re.compile(r"^Concluir$", re.I),
                    re.compile(r"^Salvar$", re.I),
                    re.compile(r"^Definir como foto do perfil$", re.I),
                    re.compile(r"^Done$", re.I),
                    re.compile(r"^Save$", re.I),
                ),
                timeout=15,
                roles=("button",),
                allow_text=True,
                enabled=True,
            )
        except TimeoutError:
            done = None
        if done is not None:
            print('→ Confirmando "Concluído"')
            done.click()

        self._wait_for_logo_applied(before_images)
        print("→ Logo aplicada ao formulário")

    def _find_in_frames(
        self,
        patterns,
        *,
        timeout: int,
        roles: tuple[str, ...] = (),
        allow_text: bool = True,
        enabled: bool = False,
    ):
        assert self.page is not None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for frame in self.page.frames:
                for pattern in patterns:
                    for role in roles:
                        locator = frame.get_by_role(role, name=pattern)
                        if self._usable(locator, enabled=enabled):
                            return locator.first
                    if allow_text:
                        locator = frame.get_by_text(pattern)
                        if self._usable(locator, enabled=enabled):
                            return locator.first
            self.page.wait_for_timeout(300)
        expected = " | ".join(pattern.pattern for pattern in patterns)
        raise TimeoutError(f"Element not found in frames: {expected}")

    @staticmethod
    def _usable(locator, *, enabled: bool) -> bool:
        try:
            return bool(
                locator.count()
                and locator.first.is_visible()
                and (not enabled or locator.first.is_enabled())
            )
        except Exception:
            return False

    def _wait_for_logo_applied(self, before_images: set[str]) -> None:
        assert self.page is not None
        deadline = time.monotonic() + 20
        popup_pattern = re.compile(r"^(Escolha a sua imagem|Choose your image)$", re.I)
        create_pattern = re.compile(r"^(Criar canal|Create channel)$", re.I)
        while time.monotonic() < deadline:
            popup_open = self._frame_visible(popup_pattern)
            create = self.page.get_by_role("button", name=create_pattern)
            form_returned = create.count() and create.first.is_visible()
            images_changed = bool(self._visible_image_sources() - before_images)
            if not popup_open and form_returned and images_changed:
                self.page.wait_for_timeout(1_000)
                return
            self.page.wait_for_timeout(400)
        raise TimeoutError(
            "A imagem foi confirmada, mas a logo não apareceu no formulário."
        )

    def _frame_visible(self, pattern) -> bool:
        assert self.page is not None
        for frame in self.page.frames:
            locator = frame.get_by_text(pattern)
            if locator.count() and locator.first.is_visible():
                return True
        return False

    def _visible_image_sources(self) -> set[str]:
        assert self.page is not None
        return set(
            self.page.locator("img").evaluate_all(
                """images => images
                .filter(image => image.offsetParent !== null && image.currentSrc)
                .map(image => image.currentSrc)"""
            )
        )

    def _body(self) -> str:
        assert self.page is not None
        try:
            return self.page.locator("body").inner_text(timeout=5_000)
        except Exception:
            return ""

    def _handle_conflict(self) -> bool:
        text = self._body().casefold()
        return any(
            phrase in text
            for phrase in (
                "identificador não está disponível",
                "identificador indisponível",
                "handle is not available",
                "handle isn't available",
                "already taken",
            )
        )

    def _raise_human(self) -> None:
        assert self.page is not None
        text = self._body().casefold()
        if "accounts.google.com" in self.page.url or "fazer login" in text:
            raise NeedsHuman("AUTH_REQUIRED")
        checks = {
            "CAPTCHA": ("captcha", "não sou um robô", "not a robot"),
            "2FA": ("verificação em duas etapas", "2-step verification"),
            "SECURITY_CHECK": ("verificação de segurança", "security check"),
            "CHANNEL_CREATION_LIMIT": ("limite de canais", "channel limit"),
        }
        for reason, phrases in checks.items():
            if any(phrase in text for phrase in phrases):
                raise NeedsHuman(reason)
