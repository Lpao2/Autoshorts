import re
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


class LogoLocator:
    def __init__(self, name):
        self.name = name
        self.clicked = False

    def count(self):
        return 1

    def is_visible(self):
        return True

    def is_enabled(self):
        return True

    def click(self):
        self.clicked = True

    @property
    def first(self):
        return self


class MissingLocator(LogoLocator):
    def count(self):
        return 0


class LogoChooser:
    def __init__(self):
        self.path = None

    def set_files(self, path):
        self.path = path


class LogoChooserContext:
    def __init__(self, chooser, *, fail=False):
        self.value = chooser
        self.fail = fail

    def __enter__(self):
        if self.fail:
            raise TimeoutError("chooser did not open")
        return self

    def __exit__(self, *_args):
        return False


class LogoFrame:
    def __init__(self, *, roles=(), texts=()):
        self.roles = {(role, text): LogoLocator(text) for role, text in roles}
        self.texts = {text: LogoLocator(text) for text in texts}

    @staticmethod
    def _matching(items, pattern):
        return next((locator for text, locator in items if pattern.search(text)), None)

    def get_by_role(self, role, *, name):
        locator = self._matching(
            (
                (text, value)
                for (item_role, text), value in self.roles.items()
                if item_role == role
            ),
            name,
        )
        return locator or MissingLocator("missing")

    def get_by_text(self, pattern):
        locator = self._matching(self.texts.items(), pattern)
        return locator or MissingLocator("missing")


class LogoPage:
    def __init__(self, *, frame, chooser_fails=False):
        self.opener = LogoLocator("opener")
        self.chooser = LogoChooser()
        self.chooser_fails = chooser_fails
        self.waits = []
        self.frames = [frame]

    def get_by_text(self, _pattern):
        return type("First", (), {"first": self.opener})()

    def expect_file_chooser(self, **_kwargs):
        return LogoChooserContext(self.chooser, fail=self.chooser_fails)

    def wait_for_timeout(self, value):
        self.waits.append(value)


class LogoFlowAdapter(PlaywrightYouTubeAdapter):
    def __init__(
        self,
        *,
        computer_role="tab",
        computer_as_text=False,
        upload_as_text=False,
        missing_upload=False,
        chooser_fails=False,
        apply_fails=False,
    ):
        super().__init__(headless=True)
        roles = [("button", "Done")]
        texts = []
        if computer_as_text:
            texts.append("Do computador")
        else:
            roles.append((computer_role, "Do computador"))
        if not missing_upload:
            if upload_as_text:
                texts.append("Carregar a partir do computador")
            else:
                roles.append(("button", "Carregar a partir do computador"))
        frame = LogoFrame(roles=roles, texts=texts)
        self.page = LogoPage(frame=frame, chooser_fails=chooser_fails)
        self.apply_fails = apply_fails

    def _visible_image_sources(self):
        return {"before.png"}

    def _wait_for_logo_applied(self, _before_images):
        if self.apply_fails:
            raise TimeoutError("logo not applied")

    def role_locator(self, role, text):
        return self.page.frames[0].roles[(role, text)]

    def text_locator(self, text):
        return self.page.frames[0].texts[text]


def logo_file(tmp_path):
    path = tmp_path / "master.png"
    path.write_bytes(b"png")
    return path


def test_logo_upload_opens_popup_and_computer_tab(tmp_path, capsys):
    adapter = LogoFlowAdapter()
    adapter._upload_logo(logo_file(tmp_path))
    assert adapter.page.opener.clicked
    assert adapter.role_locator("tab", "Do computador").clicked
    output = capsys.readouterr().out
    assert "Popup de imagem aberto" in output
    assert 'Abrindo "Do computador"' in output


def test_logo_upload_opens_file_chooser(tmp_path):
    adapter = LogoFlowAdapter()
    logo = logo_file(tmp_path)
    adapter._upload_logo(logo)
    assert adapter.role_locator("button", "Carregar a partir do computador").clicked
    assert adapter.page.chooser.path == str(logo)


def test_logo_upload_clicks_done(tmp_path):
    adapter = LogoFlowAdapter()
    adapter._upload_logo(logo_file(tmp_path))
    assert adapter.role_locator("button", "Done").clicked


def test_logo_upload_fails_when_upload_button_is_missing(tmp_path):
    adapter = LogoFlowAdapter(missing_upload=True)
    with pytest.raises(TimeoutError, match="Element not found in frames"):
        adapter._upload_logo(logo_file(tmp_path))


def test_logo_upload_fails_when_chooser_does_not_open(tmp_path):
    adapter = LogoFlowAdapter(chooser_fails=True)
    with pytest.raises(TimeoutError, match="não abriu o file chooser"):
        adapter._upload_logo(logo_file(tmp_path))


def test_logo_upload_fails_when_image_is_not_confirmed(tmp_path):
    adapter = LogoFlowAdapter(apply_fails=True)
    with pytest.raises(TimeoutError, match="logo not applied"):
        adapter._upload_logo(logo_file(tmp_path))


def test_logo_upload_complete_success(tmp_path, capsys):
    adapter = LogoFlowAdapter()
    adapter._upload_logo(logo_file(tmp_path))
    output = capsys.readouterr().out
    assert "→ File chooser aberto" in output
    assert "→ master.png enviado" in output
    assert '→ Confirmando "Concluído"' in output
    assert "→ Logo aplicada ao formulário" in output


def test_logo_upload_does_not_require_popup_title(tmp_path):
    adapter = LogoFlowAdapter()
    assert (
        not adapter.page.frames[0]
        .get_by_text(re.compile(r"Escolha a sua imagem", re.I))
        .count()
    )
    adapter._upload_logo(logo_file(tmp_path))
    assert adapter.page.chooser.path == str(tmp_path / "master.png")


def test_computer_tab_falls_back_to_text(tmp_path):
    adapter = LogoFlowAdapter(computer_as_text=True)
    adapter._upload_logo(logo_file(tmp_path))
    assert adapter.text_locator("Do computador").clicked


def test_upload_button_falls_back_to_text(tmp_path):
    adapter = LogoFlowAdapter(upload_as_text=True)
    adapter._upload_logo(logo_file(tmp_path))
    assert adapter.text_locator("Carregar a partir do computador").clicked
