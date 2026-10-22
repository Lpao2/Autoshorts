"""Visible Playwright adapter for administrative YouTube provisioning."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from pathlib import Path

from autoshorts.automation_config import GoogleIdentity
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
        )
        self.page = (
            self.context.pages[0] if self.context.pages else self.context.new_page()
        )
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
            navigation_completed = False
            try:
                self.page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                navigation_completed = True
            except Exception as exc:
                errors.append(f"attempt {attempt}: {exc}")
            if navigation_completed:
                self.page.wait_for_timeout(1_500)
            if usable():
                return
            if attempt < attempts:
                self.page.wait_for_timeout(attempt * 1_000)
        raise NavigationTimeout(
            f"NAVIGATION_TIMEOUT after {attempts} attempts; url={self.page.url}; "
            + " | ".join(errors)
        )

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
        self.navigate_youtube(
            CHANNEL_SWITCHER,
            usable=lambda: (
                self.page.locator("ytd-account-item-renderer").count() > 0
                or "criar um canal" in self._body().casefold()
                or "create a channel" in self._body().casefold()
            ),
        )
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
        self.navigate_youtube(
            CHANNEL_SWITCHER,
            usable=lambda: (
                "criar um canal" in self._body().casefold()
                or "create a channel" in self._body().casefold()
            ),
        )
        self._raise_human()
        create_link = self.page.locator('a[href*="/create_channel"]').first
        if not create_link.count():
            create_link = self.page.get_by_text(
                re.compile(r"^(Criar um canal|Create a channel)$", re.I)
            ).first
        if not create_link.count():
            raise RuntimeError("Create-channel action not found.")
        create_link.evaluate("element => element.click()")
        self.page.wait_for_timeout(1_500)
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

        self._frame_text(
            re.compile(r"^(Escolha a sua imagem|Choose your image)$", re.I),
            timeout=20,
        )
        print('→ Popup "Escolha a sua imagem" aberto')

        computer = self._frame_text(
            re.compile(r"^(Do computador|From computer)$", re.I),
            timeout=20,
            role="tab",
        )
        print('→ Abrindo aba "Do computador"')
        computer.click()
        self.page.wait_for_timeout(1_200)

        upload = self._frame_text(
            re.compile(
                r"^(Carregar a partir do computador|Upload from computer)$", re.I
            ),
            timeout=15,
            role="button",
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

        done = self._frame_text(
            re.compile(r"^(Concluído|Done)$", re.I),
            timeout=15,
            role="button",
            enabled=True,
        )
        print('→ Confirmando "Concluído"')
        done.click()

        self._wait_for_logo_applied(before_images)
        print("→ Logo aplicada ao formulário")

    def _frame_text(
        self,
        pattern,
        *,
        timeout: int,
        role: str | None = None,
        enabled: bool = False,
    ):
        assert self.page is not None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for frame in self.page.frames:
                locator = (
                    frame.get_by_role(role, name=pattern)
                    if role
                    else frame.get_by_text(pattern)
                )
                if (
                    locator.count()
                    and locator.first.is_visible()
                    and (not enabled or locator.first.is_enabled())
                ):
                    return locator.first
            self.page.wait_for_timeout(300)
        raise TimeoutError(f"Element not found in frames: {pattern.pattern}")

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
