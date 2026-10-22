import json
import stat
from dataclasses import replace

import pytest

from autoshorts.automation_config import (
    AutomationConfig,
    GoogleIdentity,
    PublishingChannel,
    ThemeProfile,
)
from autoshorts.credential_store import FilesystemCredentialStore
from autoshorts.state_store import StateStore
from autoshorts.youtube_oauth import (
    YOUTUBE_MANAGE_SCOPE,
    YOUTUBE_SCOPES,
    OAuthCredentialError,
    PendingChannelConnection,
    YouTubeChannelIdentity,
    YouTubeConnectionService,
    _raise_identity_error,
)


class Credentials:
    def __init__(self, token, *, refreshed=False):
        self.token = token
        self.refreshed = refreshed


class Backend:
    def __init__(self, channel_id="UC-A", title="Channel A"):
        self.channel = YouTubeChannelIdentity(channel_id, title)
        self.authorize_calls = 0
        self.load_calls = 0
        self.authorize_scopes = None
        self.load_scopes = None

    def authorize(self, client_secret_path, scopes, browser=None):
        self.authorize_calls += 1
        self.authorize_scopes = scopes
        return Credentials("secret-access-token")

    def load(self, payload, scopes):
        self.load_calls += 1
        self.load_scopes = scopes
        return Credentials(json.loads(payload)["token"], refreshed=True)

    def serialize(self, credentials):
        return json.dumps(
            {"token": credentials.token, "refreshed": credentials.refreshed}
        )

    def identify_channel(self, credentials):
        return self.channel


def config(*, same_identity=False):
    identity_a = GoogleIdentity("google-a", "Google A", "a@example.test")
    identity_b = GoogleIdentity("google-b", "Google B", "b@example.test")
    channels = (
        PublishingChannel("lol-a", "LoL A", 1, google_identity_id="google-a"),
        PublishingChannel(
            "lol-b",
            "LoL B",
            2,
            google_identity_id="google-a" if same_identity else "google-b",
        ),
    )
    return AutomationConfig(
        (ThemeProfile("LoL", "", (), "lol", channels=channels),),
        (identity_a, identity_b),
    )


def service(tmp_path, monkeypatch, *, backend=None, app_config=None):
    client = tmp_path / "client-secret.json"
    client.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("AUTOSHORTS_GOOGLE_CLIENT_SECRET", str(client))
    state = StateStore(tmp_path / "state.db")
    secrets = FilesystemCredentialStore(tmp_path / "credentials")
    return (
        YouTubeConnectionService(
            app_config or config(), state, secrets, backend or Backend()
        ),
        state,
        secrets,
    )


def connect(target, channel_id="lol-a"):
    pending = target.begin_connection(channel_id)
    return target.confirm_connection(pending)


def test_oauth_requests_manage_scope_needed_for_complete_onboarding(
    tmp_path, monkeypatch
):
    backend = Backend()
    target, state, _ = service(tmp_path, monkeypatch, backend=backend)
    pending = target.begin_connection("lol-a")
    assert backend.authorize_scopes == YOUTUBE_SCOPES
    assert YOUTUBE_SCOPES == (YOUTUBE_MANAGE_SCOPE,)
    target.confirm_connection(pending)
    target.verify_binding("lol-a")
    assert backend.load_scopes == YOUTUBE_SCOPES
    state.close()


def test_credential_store_permissions_and_path_safety(tmp_path):
    store = FilesystemCredentialStore(tmp_path / "credentials")
    ref = store.write("channel-a", '{"refresh_token":"never-log-me"}')
    path = store.root / ref
    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert store.read(ref) == '{"refresh_token":"never-log-me"}'
    with pytest.raises(ValueError):
        store.write("../escape", "secret")


def test_connect_requires_confirmation_before_persisting(tmp_path, monkeypatch):
    target, state, secrets = service(tmp_path, monkeypatch)
    pending = target.begin_connection("lol-a")
    assert state.get_channel_binding("lol-a") is None
    assert not secrets.root.exists()
    binding = target.confirm_connection(pending)
    assert binding.youtube_channel_id == "UC-A"
    assert state.get_channel_binding("lol-a") == binding
    assert "secret-access-token" not in repr(binding)
    state.close()


def test_guided_connection_requires_exact_expected_channel_id(
    tmp_path, monkeypatch
):
    target, state, secrets = service(tmp_path, monkeypatch)
    pending = target.begin_connection("lol-a")
    with pytest.raises(OAuthCredentialError, match="OAUTH_CHANNEL_MISMATCH"):
        target.confirm_expected_connection(pending, "UC-EXPECTED")
    assert state.get_channel_binding("lol-a") is None
    assert not secrets.root.exists()
    binding = target.confirm_expected_connection(pending, "UC-A")
    assert binding.youtube_channel_id == "UC-A"
    state.close()


def test_verify_refreshes_and_rejects_channel_mismatch(tmp_path, monkeypatch):
    backend = Backend()
    target, state, secrets = service(tmp_path, monkeypatch, backend=backend)
    connect(target)
    verified = target.verify_binding("lol-a")
    assert verified.youtube_channel_id == "UC-A"
    assert json.loads(secrets.read(verified.credential_ref))["refreshed"] is True
    backend.channel = YouTubeChannelIdentity("UC-WRONG", "Wrong")
    with pytest.raises(ValueError, match="não corresponde"):
        target.verify_binding("lol-a")
    assert state.get_channel_binding("lol-a").youtube_channel_id == "UC-A"
    state.close()


def test_reconnect_replaces_credential_and_disconnect_preserves_config(
    tmp_path, monkeypatch
):
    backend = Backend()
    app_config = config()
    target, state, secrets = service(
        tmp_path, monkeypatch, backend=backend, app_config=app_config
    )
    first = connect(target)
    backend.channel = YouTubeChannelIdentity("UC-NEW", "New")
    second = connect(target)
    assert second.youtube_channel_id == "UC-NEW"
    assert first.credential_ref == second.credential_ref
    assert target.disconnect("lol-a")
    assert state.get_channel_binding("lol-a") is None
    assert not (secrets.root / first.credential_ref).exists()
    assert app_config.themes[0].channels[0].name == "LoL A"
    state.close()


def test_same_google_identity_still_has_isolated_channel_credentials(
    tmp_path, monkeypatch
):
    backend = Backend("UC-A", "A")
    target, state, secrets = service(
        tmp_path, monkeypatch, backend=backend, app_config=config(same_identity=True)
    )
    first = connect(target, "lol-a")
    backend.channel = YouTubeChannelIdentity("UC-B", "B")
    second = connect(target, "lol-b")
    assert first.google_identity_id == second.google_identity_id == "google-a"
    assert first.youtube_channel_id != second.youtube_channel_id
    assert first.credential_ref != second.credential_ref
    assert len(list(secrets.root.glob("*.json"))) == 2
    state.close()


def test_same_youtube_channel_cannot_bind_to_two_publishing_channels(
    tmp_path, monkeypatch
):
    backend = Backend("UC-SAME", "Same")
    target, state, secrets = service(
        tmp_path, monkeypatch, backend=backend, app_config=config(same_identity=True)
    )
    connect(target, "lol-a")
    pending = target.begin_connection("lol-b")
    with pytest.raises(ValueError, match="já está vinculado"):
        target.confirm_connection(pending)
    assert state.get_channel_binding("lol-b") is None
    assert len(list(secrets.root.glob("*.json"))) == 1
    state.close()


def test_missing_client_secret_disabled_identity_and_revoked_credential(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("AUTOSHORTS_GOOGLE_CLIENT_SECRET", raising=False)
    state = StateStore(tmp_path / "state.db")
    target = YouTubeConnectionService(
        config(), state, FilesystemCredentialStore(tmp_path / "credentials"), Backend()
    )
    with pytest.raises(ValueError, match="AUTOSHORTS_GOOGLE_CLIENT_SECRET"):
        target.begin_connection("lol-a")
    disabled = replace(
        config(),
        google_identities=(
            GoogleIdentity("google-a", "A", "a@example.test", False),
            GoogleIdentity("google-b", "B", "b@example.test"),
        ),
    )
    client = tmp_path / "client.json"
    client.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("AUTOSHORTS_GOOGLE_CLIENT_SECRET", str(client))
    target = YouTubeConnectionService(
        disabled, state, FilesystemCredentialStore(tmp_path / "credentials"), Backend()
    )
    with pytest.raises(ValueError, match="desativada"):
        target.begin_connection("lol-a")
    state.close()


def test_pending_payload_is_not_in_repr():
    pending = PendingChannelConnection(
        "channel", "identity", YouTubeChannelIdentity("UC", "Title"), "TOKEN"
    )
    assert "TOKEN" not in repr(pending)


def test_revoked_credential_error_does_not_expose_saved_token(tmp_path, monkeypatch):
    class RevokedBackend(Backend):
        def load(self, payload, scopes):
            raise RuntimeError("credential revoked")

    target, state, _ = service(tmp_path, monkeypatch, backend=RevokedBackend())
    connect(target)
    with pytest.raises(RuntimeError, match="revoked") as error:
        target.verify_binding("lol-a")
    assert "secret-access-token" not in str(error.value)
    assert state.get_channel_binding("lol-a").status == "REVOKED"
    state.close()


def test_outdated_scope_requires_reauth_without_destroying_binding(
    tmp_path, monkeypatch
):
    target, state, secrets = service(tmp_path, monkeypatch)
    binding = connect(target)
    secrets.write(
        "lol-a",
        json.dumps(
            {
                "token": "secret-access-token",
                "scopes": ["https://www.googleapis.com/auth/youtube.upload"],
            }
        ),
    )
    with pytest.raises(OAuthCredentialError, match="scope outdated"):
        target.verify_binding("lol-a")
    assert state.get_channel_binding("lol-a") == binding
    assert state.get_channel_binding("lol-a").status == "CONNECTED"
    state.close()


def test_youtube_signup_required_is_safe_non_persistable_error():
    with pytest.raises(
        OAuthCredentialError, match="OAUTH_IDENTITY_WITHOUT_CHANNEL"
    ):
        _raise_identity_error(RuntimeError("youtubeSignupRequired"))
