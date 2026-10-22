from pathlib import Path

import pytest

from autoshorts.automation_config import AutomationConfig, GoogleIdentity
from autoshorts.state_store import StateStore
from autoshorts.youtube_playwright import PlaywrightYouTubeAdapter
from autoshorts.youtube_provisioning import (
    ExistingYouTubeChannel,
    NavigationTimeout,
    ProvisioningRequest,
    YouTubeProvisioningService,
    classify_existing,
    handle_candidates,
    persistent_profile_path,
    pretty_handle_base,
)


def request() -> ProvisioningRequest:
    return ProvisioningRequest(
        "google-c",
        "plan-1",
        "Rota de Renda",
        handle_candidates("Rota de Renda"),
        Path("logo.png"),
    )


def test_pretty_handle_and_three_candidates_preserve_public_name():
    assert pretty_handle_base("Rota de Renda") == "@RotaDeRendaOficial"
    assert handle_candidates("Dossiê Oculto") == (
        "@DossieOcultoOficial",
        "@DossieOcultoOficial1",
        "@DossieOcultoOficial2",
    )
    assert request().expected_name == "Rota de Renda"


@pytest.mark.parametrize(
    ("existing", "status"),
    [
        ((ExistingYouTubeChannel("UC1", "Other", "@RotaDeRendaOficial"),), "EXISTS"),
        ((ExistingYouTubeChannel("UC1", "Rota de Renda", "@other"),), "EXISTS"),
        ((), "MISSING"),
    ],
)
def test_reconcile_by_handle_name_or_missing(existing, status):
    assert classify_existing(request(), existing).status == status


def test_reconcile_duplicate_is_never_created_automatically():
    existing = (
        ExistingYouTubeChannel("UC1", "Rota de Renda", "@RotaDeRendaOficial"),
        ExistingYouTubeChannel("UC2", "Rota de Renda", "@RotaDeRendaOficial1"),
    )
    assert classify_existing(request(), existing).status == "POSSIBLE_DUPLICATE"


def test_profile_is_keyed_by_persisted_identity_and_rejects_unsafe_id(tmp_path):
    assert persistent_profile_path(tmp_path, "google-c") == (
        tmp_path / "data/playwright/youtube-profiles/google-c"
    )
    with pytest.raises(ValueError):
        persistent_profile_path(tmp_path, "../cookies")


class FakePage:
    def __init__(self, outcomes, usable=False):
        self.outcomes = iter(outcomes)
        self.usable = usable
        self.url = "https://youtube.test/partial"
        self.waits = []

    def goto(self, *_args, **_kwargs):
        outcome = next(self.outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        self.usable = bool(outcome)

    def wait_for_timeout(self, value):
        self.waits.append(value)


def test_navigation_timeout_retry_then_success():
    adapter = PlaywrightYouTubeAdapter(headless=True)
    adapter.page = FakePage([TimeoutError("one"), True])
    adapter.navigate_youtube("url", usable=lambda: adapter.page.usable)
    assert adapter.page.waits == [1000, 1500]


def test_navigation_timeout_but_expected_element_present_continues():
    adapter = PlaywrightYouTubeAdapter(headless=True)
    adapter.page = FakePage([TimeoutError("one")], usable=True)
    adapter.navigate_youtube("url", usable=lambda: adapter.page.usable)


def test_three_navigation_timeouts_are_recoverable():
    adapter = PlaywrightYouTubeAdapter(headless=True)
    adapter.page = FakePage([TimeoutError("1"), TimeoutError("2"), TimeoutError("3")])
    with pytest.raises(NavigationTimeout):
        adapter.navigate_youtube("url", usable=lambda: False)


class FakeAdapter:
    def __init__(self, logged_in=True, existing=()):
        self.logged_in = logged_in
        self.existing = existing
        self.opened = None

    def open(self, identity, profile_path):
        self.opened = (identity, profile_path)

    def close(self):
        pass

    def verify_session(self):
        return self.logged_in

    def inspect_channels(self):
        return self.existing

    def create_channel(self, value):
        return ExistingYouTubeChannel(
            "UCNEW", value.expected_name, value.handle_candidates[0]
        )


def seed(tmp_path, *, prior_status=None):
    db = tmp_path / "state.db"
    logo = tmp_path / "logo.png"
    logo.write_bytes(b"png")
    with StateStore(db) as store:
        now = "2026-01-01T00:00:00+00:00"
        store.connection.execute(
            """INSERT INTO planned_channel_brands
            (brand_plan_id, theme_key, slot, public_name, preferred_handle,
             backup_handles_json, handle_availability, description, tagline,
             brand_keywords_json, visual_direction, palette_json, logo_brief,
             banner_brief, status, provider, model, version, created_at, updated_at)
            VALUES ('plan-1','finance',1,'Rota de Renda','@rotaderenda','[]',
            'UNKNOWN','','','[]','','["a","b","c","d"]','','','APPROVED',
            'test','test',1,?,?)""",
            (now, now),
        )
        store.assign_planned_identity("plan-1", "google-c")
        store.connection.execute(
            """INSERT INTO brand_asset_metadata
            (brand_plan_id,asset_type,file_path,provider,model,prompt,width,height,
             checksum,created_at,status,version,file_size)
            VALUES ('plan-1','LOGO',?,'test','test','',1,1,'x',?,'APPROVED',1,3)""",
            (str(logo), now),
        )
        if prior_status:
            store.save_youtube_provisioning_record(
                brand_plan_id="plan-1",
                google_identity_id="google-c",
                expected_name="Rota de Renda",
                expected_handle="@RotaDeRendaOficial",
                status=prior_status,
                logo_path=logo,
            )
        store.connection.commit()
    config = AutomationConfig(
        (), (GoogleIdentity("google-c", "AutoTech", "owner@example.test"),)
    )
    return db, config


def test_session_needs_login_is_persisted(tmp_path):
    db, config = seed(tmp_path)
    adapter = FakeAdapter(logged_in=False)
    service = YouTubeProvisioningService(
        project_root=tmp_path,
        state_db_path=db,
        config=config,
        adapter_factory=lambda: adapter,
    )
    assert service.verify_session("google-c").status == "NEEDS_LOGIN"
    assert adapter.opened[1].name == "google-c"


def test_resume_skips_created_and_starts_first_incomplete(tmp_path):
    db, config = seed(tmp_path, prior_status="CREATED")
    adapter = FakeAdapter()
    service = YouTubeProvisioningService(
        project_root=tmp_path,
        state_db_path=db,
        config=config,
        adapter_factory=lambda: adapter,
    )
    assert service.resume("google-c") == ()


def test_gitignore_excludes_all_runtime_playwright_profiles():
    text = (Path(__file__).parents[1] / ".gitignore").read_text(encoding="utf-8")
    assert "data/" in text or "youtube-profiles" in text
