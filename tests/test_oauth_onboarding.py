from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from autoshorts.automation_config import (
    AutomationConfig,
    GoogleIdentity,
    PublishingChannel,
    ThemeProfile,
)
from autoshorts.oauth_onboarding import (
    OAuthQueueItem,
    oauth_queue,
    reconcile_publishing_channels,
    run_guided_oauth,
)
from autoshorts.youtube_oauth import OAuthCredentialError


@dataclass
class Binding:
    publishing_channel_id: str
    youtube_channel_id: str
    status: str = "CONNECTED"


class Store:
    def __init__(self, bindings=()):
        self.bindings = {x.publishing_channel_id: x for x in bindings}

    def get_channel_binding(self, channel_id):
        return self.bindings.get(channel_id)


def _config():
    channels = (
        PublishingChannel(
            "a-ready",
            "Ready",
            1,
            google_identity_id="google-a",
            youtube_channel_id="UC-READY",
            brand_plan_id="plan-ready",
        ),
        PublishingChannel(
            "a-missing",
            "Missing",
            2,
            google_identity_id="google-a",
            youtube_channel_id="UC-MISSING",
            brand_plan_id="plan-missing",
        ),
        PublishingChannel(
            "protected",
            "Lucas Lp",
            3,
            False,
            google_identity_id="google-a",
            youtube_channel_id="UC-PERSONAL",
            brand_plan_id="plan-protected",
        ),
        PublishingChannel(
            "no-id",
            "Fora do Script",
            1,
            False,
            google_identity_id="google-c",
            brand_plan_id="plan-no-id",
        ),
        PublishingChannel(
            "excluded",
            "Google B",
            1,
            False,
            google_identity_id="google-b",
            youtube_channel_id="UC-B",
            brand_plan_id="plan-b",
        ),
    )
    return AutomationConfig(
        (ThemeProfile("Theme", "", (), "theme", channels=channels),),
        (
            GoogleIdentity("google-a", "A", "a@example.test"),
            GoogleIdentity("google-b", "B", "b@example.test"),
            GoogleIdentity("google-c", "C", "c@example.test"),
        ),
    )


def test_queue_excludes_protected_other_identities_and_missing_channel_id():
    items = oauth_queue(
        _config(), Store((Binding("a-ready", "UC-READY"),))
    )
    assert [(x.channel_name, x.status) for x in items] == [
        ("Missing", "MISSING"),
        ("Ready", "READY"),
    ]


def test_queue_marks_channel_id_mismatch():
    items = oauth_queue(
        _config(), Store((Binding("a-ready", "UC-WRONG"),))
    )
    assert next(x for x in items if x.channel_name == "Ready").status == "MISMATCH"


class Service:
    def __init__(self, *, mismatch=False):
        self.verified = []
        self.started = []
        self.mismatch = mismatch

    def verify_binding(self, channel_id):
        self.verified.append(channel_id)

    def begin_connection(self, channel_id):
        self.started.append(channel_id)
        return channel_id

    def confirm_expected_connection(self, pending, expected):
        if self.mismatch:
            raise OAuthCredentialError("OAUTH_CHANNEL_MISMATCH")
        return Binding(pending, expected)


def test_guided_queue_skips_ready_and_resumes_from_missing(monkeypatch):
    service = Service()
    items = (
        OAuthQueueItem("ready", "Ready", "google-a", "UC-R", "READY"),
        OAuthQueueItem("missing", "Missing", "google-a", "UC-M", "MISSING"),
    )
    monkeypatch.setattr("builtins.input", lambda prompt: "")
    assert run_guided_oauth(service, items) == (2, 0, 0)
    assert service.verified == ["ready"]
    assert service.started == ["missing"]


def test_guided_queue_mismatch_does_not_continue(monkeypatch):
    service = Service(mismatch=True)
    item = OAuthQueueItem("missing", "Missing", "google-a", "UC-M", "MISSING")
    monkeypatch.setattr("builtins.input", lambda prompt: "")
    with pytest.raises(OAuthCredentialError, match="OAUTH_CHANNEL_MISMATCH"):
        run_guided_oauth(service, (item,))


def test_reconcile_reuses_local_slot_and_is_idempotent():
    plan = SimpleNamespace(
        brand_plan_id="plan-a",
        theme_key="theme",
        slot=1,
        public_name="Planned Channel",
    )

    class MappingStore(Store):
        def planned_identity_assignments(self):
            return {"plan-a": "google-a"}

        def youtube_provisioning_records(self):
            return (SimpleNamespace(brand_plan_id="plan-a", youtube_channel_id="UC-A"),)

        def planned_channel_brands(self):
            return (plan,)

        def get_approved_brand_kit(self, channel_id):
            return None

    placeholder = PublishingChannel("local", "Placeholder", 1)
    config = AutomationConfig(
        (ThemeProfile("Theme", "", (), "theme", channels=(placeholder,)),),
        (GoogleIdentity("google-a", "A", "a@example.test"),),
    )
    store = MappingStore()
    first = reconcile_publishing_channels(config, store)
    second = reconcile_publishing_channels(first.config, store)
    channels = first.config.themes[0].channels
    assert len(channels) == 1
    assert channels[0].channel_id == "local"
    assert channels[0].brand_plan_id == "plan-a"
    assert channels[0].youtube_channel_id == "UC-A"
    assert second.config == first.config
    assert second.created == 0


def test_reconcile_can_limit_mapping_to_requested_brand_plan():
    plans = (
        SimpleNamespace(
            brand_plan_id="plan-a", theme_key="theme", slot=1, public_name="A"
        ),
        SimpleNamespace(
            brand_plan_id="plan-b", theme_key="theme", slot=2, public_name="B"
        ),
    )

    class MappingStore(Store):
        def planned_identity_assignments(self):
            return {"plan-a": "google-b", "plan-b": "google-b"}

        def youtube_provisioning_records(self):
            return (
                SimpleNamespace(brand_plan_id="plan-a", youtube_channel_id="UC-A"),
                SimpleNamespace(brand_plan_id="plan-b", youtube_channel_id="UC-B"),
            )

        def planned_channel_brands(self):
            return plans

        def get_approved_brand_kit(self, channel_id):
            return None

        def get_theme_template(self, theme_key):
            raise AssertionError("existing theme should be reused")

    config = AutomationConfig(
        (ThemeProfile("Theme", "", (), "theme"),),
        (GoogleIdentity("google-b", "B", "b@example.test"),),
    )
    result = reconcile_publishing_channels(
        config,
        MappingStore(),
        frozenset({"google-b"}),
        frozenset({"plan-a"}),
    )
    assert result.planned == 1
    assert [item.brand_plan_id for item in result.config.themes[0].channels] == [
        "plan-a"
    ]
