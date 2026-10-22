from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from autoshorts.automation_config import (
    AutomationConfig,
    GoogleIdentity,
    PublishingChannel,
    ThemeProfile,
)
from autoshorts.oauth_onboarding import authorize_provisioned_channel
from autoshorts.youtube_oauth import OAuthCredentialError
from autoshorts.youtube_oauth_playwright import OAuthUIError, PlaywrightOAuthBrowser


class Element:
    def __init__(self, label="", *, visible=True):
        self.label = label
        self.visible = visible
        self.clicked = False

    def is_visible(self):
        return self.visible

    def click(self):
        self.clicked = True

    def inner_text(self):
        return self.label

    def get_attribute(self, name):
        return self.label if name == "aria-label" else None


class Locator:
    def __init__(self, elements=(), text=""):
        self.elements = tuple(elements)
        self.text = text

    def count(self):
        return len(self.elements)

    def nth(self, index):
        return self.elements[index]

    def inner_text(self, **kwargs):
        return self.text

    def all_inner_texts(self):
        return [item.label for item in self.elements]


class Page:
    url = "https://accounts.google.com/oauth"

    def __init__(self, *, body="", texts=(), roles=(), identifiers=()):
        self.body = body
        self.texts = {text: Element(text) for text in texts}
        self.roles = {
            (role, label): Element(label) for role, label in roles
        }
        self.identifiers = {
            email: Element(email) for email in identifiers
        }
        self.waits = []

    @staticmethod
    def _matches(value, pattern, exact=False):
        if isinstance(pattern, re.Pattern):
            return bool(pattern.search(value))
        return value == pattern if exact else pattern in value

    def locator(self, selector):
        if selector == "body":
            return Locator(text=self.body)
        match = re.fullmatch(r'\[data-identifier="(.+)"\]', selector)
        if match and match.group(1) in self.identifiers:
            return Locator((self.identifiers[match.group(1)],))
        return Locator()

    def get_by_text(self, pattern, exact=False):
        return Locator(
            element
            for text, element in self.texts.items()
            if self._matches(text, pattern, exact)
        )

    def get_by_role(self, role, name=None):
        return Locator(
            element
            for (item_role, label), element in self.roles.items()
            if item_role == role
            and (name is None or self._matches(label, name))
        )

    def wait_for_timeout(self, milliseconds):
        self.waits.append(milliseconds)

    def title(self):
        return "Google OAuth"


def browser(tmp_path, page, **kwargs):
    value = PlaywrightOAuthBrowser(
        tmp_path / "profile",
        kwargs.pop("channel", "Força Constante"),
        account_key="google-b",
        expected_email="clicmarcelo@gmail.com",
        expected_channel_id="UC-EXPECTED",
        protected_channel_names=("Marcelo Clic", "Lucas Lp"),
        **kwargs,
    )
    value.page = page
    return value


def test_expected_google_account_is_selected(tmp_path):
    page = Page(identifiers=("clicmarcelo@gmail.com",))
    target = browser(tmp_path, page)
    assert target._select_expected_account()
    assert page.identifiers["clicmarcelo@gmail.com"].clicked


def test_missing_google_account_is_not_selected(tmp_path):
    page = Page(identifiers=("other@example.test",))
    assert not browser(tmp_path, page)._select_expected_account()
    assert not page.identifiers["other@example.test"].clicked


def test_expected_channel_is_selected(tmp_path):
    page = Page(texts=("Força Constante", "Marcelo Clic"))
    assert browser(tmp_path, page)._select_expected_channel()
    assert page.texts["Força Constante"].clicked
    assert not page.texts["Marcelo Clic"].clicked


def test_state_machine_prefers_visible_target_channel_over_personal_account(
    tmp_path, monkeypatch
):
    target = browser(tmp_path, Page())
    events = []
    monkeypatch.setattr(target, "_raise_for_security_challenge", lambda: None)
    monkeypatch.setattr(
        target, "_select_expected_channel", lambda: events.append("channel") or True
    )
    monkeypatch.setattr(
        target,
        "_select_expected_account",
        lambda: (_ for _ in ()).throw(AssertionError("personal account selected")),
    )
    monkeypatch.setattr(target, "_click_advanced", lambda: False)
    monkeypatch.setattr(target, "_click_autoshorts_link", lambda: False)
    monkeypatch.setattr(
        target,
        "_click_consent_action",
        lambda **kwargs: (
            events.append("consent") or "CONTINUE" if events else None
        ),
    )
    target._assist(timeout=1)
    assert events == ["channel", "consent"]
    assert target.status == "CONSENT_CONTINUED"


def test_state_machine_waits_for_brand_account_before_email_fallback(
    tmp_path, monkeypatch
):
    target = browser(tmp_path, Page())
    events = []
    clock = {"value": -2}

    def monotonic():
        clock["value"] += 2
        return clock["value"]

    monkeypatch.setattr(
        "autoshorts.youtube_oauth_playwright.time.monotonic", monotonic
    )
    monkeypatch.setattr(target, "_raise_for_security_challenge", lambda: None)
    monkeypatch.setattr(
        target,
        "_select_expected_channel",
        lambda: (
            events.append("channel") or True if "account" in events else False
        ),
    )
    monkeypatch.setattr(
        target,
        "_select_expected_account",
        lambda: (
            (_ for _ in ()).throw(AssertionError("email fallback was too early"))
            if clock["value"] < 10
            else events.append("account") or True
        ),
    )
    monkeypatch.setattr(target, "_click_advanced", lambda: False)
    monkeypatch.setattr(target, "_click_autoshorts_link", lambda: False)
    monkeypatch.setattr(
        target,
        "_click_consent_action",
        lambda **kwargs: (
            events.append("consent") or "CONTINUE"
            if "channel" in events
            else None
        ),
    )
    target._assist(timeout=90)
    assert events == ["account", "channel", "consent"]


def test_wrong_channel_present_but_target_absent_is_not_selected(tmp_path):
    page = Page(texts=("Marcelo Clic",))
    assert not browser(tmp_path, page)._select_expected_channel()
    assert not page.texts["Marcelo Clic"].clicked


def test_protected_channel_can_never_be_the_target(tmp_path):
    page = Page(texts=("Marcelo Clic",))
    with pytest.raises(OAuthUIError, match="OAUTH_PROTECTED_CHANNEL"):
        browser(tmp_path, page, channel="Marcelo Clic")._select_expected_channel()
    assert not page.texts["Marcelo Clic"].clicked


@pytest.mark.parametrize("label", ("Avançado", "Avançadas", "Advanced"))
def test_advanced_portuguese_and_english(tmp_path, label):
    page = Page(roles=(("button", label),))
    assert browser(tmp_path, page)._click_advanced()
    assert page.roles[("button", label)].clicked


def test_advanced_falls_back_to_semantic_text(tmp_path):
    page = Page(texts=("Avançado",))
    assert browser(tmp_path, page)._click_advanced()
    assert page.texts["Avançado"].clicked


@pytest.mark.parametrize(
    "label",
    (
        "Ir para AutoShorts (não seguro)",
        "Acessar AutoShorts (não seguro)",
        "Aceder a AutoShorts (não seguro)",
        "Go to AutoShorts (unsafe)",
    ),
)
def test_autoshorts_link_portuguese_and_english(tmp_path, label):
    page = Page(roles=(("link", label),))
    assert browser(tmp_path, page)._click_autoshorts_link()
    assert page.roles[("link", label)].clicked


@pytest.mark.parametrize("label", ("Permitir", "Allow"))
def test_final_consent_portuguese_and_english(tmp_path, label):
    page = Page(roles=(("button", label),))
    assert browser(tmp_path, page)._click_final_consent()
    assert page.roles[("button", label)].clicked


def test_continue_can_advance_to_channel_chooser_before_target_selected(tmp_path):
    page = Page(roles=(("button", "Continuar"),))
    assert (
        browser(tmp_path, page)._click_consent_action(channel_selected=False)
        == "CONTINUE"
    )
    assert page.roles[("button", "Continuar")].clicked


def test_allow_never_clicks_before_target_channel_is_selected(tmp_path):
    page = Page(roles=(("button", "Permitir"),))
    with pytest.raises(OAuthUIError, match="OAUTH_TARGET_CHANNEL_NOT_FOUND"):
        browser(tmp_path, page)._click_consent_action(channel_selected=False)
    assert not page.roles[("button", "Permitir")].clicked


@pytest.mark.parametrize(
    "body",
    (
        "Enter your password",
        "2-Step Verification",
        "CAPTCHA",
        "Recovery email",
    ),
)
def test_oauth_challenges_require_human(tmp_path, body):
    target = browser(tmp_path, Page(body=body))
    with pytest.raises(OAuthUIError, match="NEEDS_HUMAN"):
        target._raise_for_security_challenge()
    assert target.status == "NEEDS_HUMAN"


def test_checkpoints_are_injectable_and_can_be_disabled(tmp_path):
    messages = []
    page = Page(identifiers=("clicmarcelo@gmail.com",))
    enabled = browser(
        tmp_path,
        page,
        require_confirmations=True,
        confirm_callback=messages.append,
    )
    assert enabled._select_expected_account()
    assert "clicmarcelo@gmail.com" in messages[0]

    disabled_messages = []
    disabled = browser(
        tmp_path,
        Page(identifiers=("clicmarcelo@gmail.com",)),
        confirm_callback=disabled_messages.append,
    )
    assert disabled._select_expected_account()
    assert disabled_messages == []


def _config():
    channel = PublishingChannel(
        "local",
        "Expected",
        1,
        google_identity_id="google-c",
        youtube_channel_id="UC-EXPECTED",
        brand_plan_id="plan",
    )
    return AutomationConfig(
        (ThemeProfile("Theme", "", (), "theme", channels=(channel,)),),
        (GoogleIdentity("google-c", "C", "c@example.test"),),
    )


def test_provisioned_oauth_passes_expected_identity_to_browser(tmp_path):
    captured = {}

    class Browser:
        def __init__(self, profile, name, **kwargs):
            captured.update(profile=profile, name=name, **kwargs)

    class Connection:
        def verify_binding(self, channel_id):
            raise KeyError(channel_id)

        def begin_connection(self, channel_id, *, authorization_browser):
            captured["browser"] = authorization_browser
            return "pending"

        def confirm_expected_connection(self, pending, expected):
            assert pending == "pending"
            assert expected == "UC-EXPECTED"
            return SimpleNamespace(youtube_channel_id=expected)

    result = authorize_provisioned_channel(
        project_root=tmp_path,
        config=_config(),
        connection_service=Connection(),
        brand_plan_id="plan",
        browser_factory=Browser,
    )
    assert captured["profile"] == (
        tmp_path / "data/playwright/youtube-profiles/google-c"
    )
    assert captured["name"] == "Expected"
    assert captured["account_key"] == "google-c"
    assert captured["expected_email"] == "c@example.test"
    assert captured["expected_channel_id"] == "UC-EXPECTED"
    assert result.status == "READY"


def test_ready_oauth_is_not_repeated_without_force(tmp_path):
    class Connection:
        def verify_binding(self, channel_id):
            return SimpleNamespace(youtube_channel_id="UC-EXPECTED")

        def begin_connection(self, *args, **kwargs):
            raise AssertionError("OAuth must not repeat")

    result = authorize_provisioned_channel(
        project_root=tmp_path,
        config=_config(),
        connection_service=Connection(),
        brand_plan_id="plan",
    )
    assert result.status == "READY"


def test_channel_id_mismatch_is_not_persisted(tmp_path):
    class Browser:
        def __init__(self, *args, **kwargs):
            pass

    class Connection:
        def verify_binding(self, channel_id):
            return SimpleNamespace(youtube_channel_id="UC-EXPECTED")

        def begin_connection(self, *args, **kwargs):
            return "pending"

        def confirm_expected_connection(self, pending, expected):
            raise OAuthCredentialError("OAUTH_CHANNEL_MISMATCH")

    with pytest.raises(OAuthCredentialError, match="OAUTH_CHANNEL_MISMATCH"):
        authorize_provisioned_channel(
            project_root=tmp_path,
            config=_config(),
            connection_service=Connection(),
            brand_plan_id="plan",
            browser_factory=Browser,
            force_reauthorize=True,
        )
