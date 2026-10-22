from __future__ import annotations

from types import SimpleNamespace

import pytest

from autoshorts.automation_config import (
    AutomationConfig,
    GoogleIdentity,
    PublishingChannel,
    ThemeProfile,
)
from autoshorts.oauth_onboarding import authorize_provisioned_channel
from autoshorts.youtube_oauth_playwright import PlaywrightOAuthBrowser


class Element:
    def __init__(self, *, visible=True):
        self.visible = visible
        self.clicked = False

    def is_visible(self):
        return self.visible

    def click(self):
        self.clicked = True


class Locator:
    def __init__(self, elements=(), text=""):
        self.elements = tuple(elements)
        self.text = text

    def count(self):
        return len(self.elements)

    def nth(self, index):
        return self.elements[index]

    def inner_text(self):
        return self.text


class Page:
    def __init__(self, *, body="", matches=0, buttons=0):
        self.body = body
        self.matches = Locator(Element() for _ in range(matches))
        self.buttons = Locator(Element() for _ in range(buttons))
        self.waits = []

    def locator(self, selector):
        assert selector == "body"
        return Locator(text=self.body)

    def get_by_text(self, pattern, exact=False):
        assert exact
        return self.matches

    def get_by_role(self, role, name):
        assert role == "button"
        return self.buttons

    def wait_for_timeout(self, milliseconds):
        self.waits.append(milliseconds)


@pytest.mark.parametrize(
    "body",
    ("Enter your password", "2-Step Verification", "CAPTCHA"),
)
def test_oauth_challenges_require_human(tmp_path, body):
    browser = PlaywrightOAuthBrowser(tmp_path / "profile", "Expected")
    browser.page = Page(body=body, matches=1)
    browser._assist()
    assert browser.status == "NEEDS_HUMAN"
    assert not browser.page.matches.nth(0).clicked


@pytest.mark.parametrize("matches", (0, 2))
def test_zero_or_ambiguous_channel_matches_require_human(tmp_path, matches):
    browser = PlaywrightOAuthBrowser(tmp_path / "profile", "Expected")
    browser.page = Page(matches=matches)
    browser._assist()
    assert browser.status == "NEEDS_HUMAN"


def test_unique_channel_match_is_selected_and_consent_continues(tmp_path):
    browser = PlaywrightOAuthBrowser(tmp_path / "profile", "Expected")
    browser.page = Page(matches=1, buttons=1)
    browser._assist()
    assert browser.page.matches.nth(0).clicked
    assert browser.page.buttons.nth(0).clicked
    assert browser.status == "CONSENT_CONTINUED"


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


def test_provisioned_oauth_uses_identity_profile_and_expected_channel(tmp_path):
    captured = {}

    class Browser:
        def __init__(self, profile, name):
            captured["profile"] = profile
            captured["name"] = name

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
    assert result.status == "READY"


def test_ready_oauth_is_not_repeated(tmp_path):
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
