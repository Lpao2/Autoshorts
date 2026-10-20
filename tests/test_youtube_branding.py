from __future__ import annotations

import json

import pytest
from test_youtube_publishing import configured_service

from autoshorts.credential_store import FilesystemCredentialStore
from autoshorts.youtube_branding import (
    YouTubeBrandingService,
    _conservative_branding,
    _language_matches,
)
from autoshorts.youtube_oauth import YOUTUBE_MANAGE_SCOPE


class Connection:
    def load_verified_credentials(self, channel_id):
        return object()


class Backend:
    def __init__(self, channel_id="UC-A"):
        self.channel_id = channel_id
        self.uploads = 0
        self.updates = []
        self.remote = {
            "id": channel_id,
            "snippet": {"title": "YouTube A"},
            "brandingSettings": {
                "channel": {
                    "description": "old",
                    "keywords": "old-keyword",
                    "country": "BR",
                    "trackingAnalyticsAccountId": "UA-preserve",
                },
                "image": {"bannerImageUrl": "https://old.example/banner"},
            },
        }

    def get_channel(self, credentials):
        return self.remote

    def upload_banner(self, credentials, path):
        self.uploads += 1
        return "https://upload.example/new-banner"

    def update_branding(self, credentials, channel_id, branding):
        self.updates.append(branding)
        assert channel_id == self.channel_id
        self.remote = {
            **self.remote,
            "brandingSettings": {
                "channel": branding["channel"],
                "image": {
                    "bannerImageUrl": "https://cdn.example/active-banner",
                    **branding["image"],
                },
            },
        }
        return self.remote


def _service(tmp_path, *, scopes=(YOUTUBE_MANAGE_SCOPE,), backend=None):
    publishing, store, channel, _plan, _video, _uploader, _connection = (
        configured_service(tmp_path)
    )
    credentials = FilesystemCredentialStore(tmp_path / "credentials")
    credentials.write(
        channel.channel_id,
        json.dumps({"scopes": list(scopes), "refresh_token": "secret"}),
    )
    target = YouTubeBrandingService(
        publishing.config,
        store,
        credentials,
        connection_service=Connection(),
        backend=backend or Backend(),
    )
    return target, store, channel


def test_branding_dry_run_is_local_and_marks_manual_properties(tmp_path):
    backend = Backend()
    target, store, channel = _service(tmp_path, backend=backend)
    plan = target.dry_run(channel.channel_id)
    assert plan.oauth_ready
    assert plan.description == "Descrição editorial"
    assert plan.keywords == ("teste", "original")
    assert plan.default_language == "pt-BR"
    assert plan.banner.width == 2560
    assert plan.logo.width == 1024
    assert backend.uploads == 0
    assert backend.updates == []
    store.close()


def test_scope_insufficient_stops_before_any_remote_call(tmp_path):
    backend = Backend()
    target, store, channel = _service(
        tmp_path,
        scopes=("https://www.googleapis.com/auth/youtube.upload",),
        backend=backend,
    )
    assert not target.dry_run(channel.channel_id).oauth_ready
    with pytest.raises(ValueError, match="scope insufficient"):
        target.apply(channel.channel_id)
    assert backend.uploads == 0
    assert backend.updates == []
    store.close()


def test_apply_preserves_writable_settings_and_validates_banner(tmp_path):
    backend = Backend()
    target, store, channel = _service(tmp_path, backend=backend)
    result = target.apply(channel.channel_id)
    assert result.banner_active
    assert backend.uploads == 1
    sent = backend.updates[0]
    assert sent["channel"]["country"] == "BR"
    assert sent["channel"]["trackingAnalyticsAccountId"] == "UA-preserve"
    assert sent["channel"]["description"] == "Descrição editorial"
    assert sent["channel"]["defaultLanguage"] == "pt-BR"
    assert sent["image"]["bannerExternalUrl"].endswith("new-banner")
    store.close()


def test_wrong_remote_channel_stops_before_banner_upload(tmp_path):
    backend = Backend("UC-WRONG")
    target, store, channel = _service(tmp_path, backend=backend)
    with pytest.raises(ValueError, match="channel ID"):
        target.apply(channel.channel_id)
    assert backend.uploads == 0
    assert backend.updates == []
    store.close()


def test_read_modify_write_excludes_read_only_image_urls():
    result = _conservative_branding(
        {
            "channel": {"country": "BR", "description": "keep", "title": "No"},
            "image": {"bannerImageUrl": "read-only"},
        }
    )
    assert result == {"channel": {"country": "BR", "description": "keep"}}


def test_language_validation_accepts_youtube_region_normalization():
    assert _language_matches("pt-BR", "pt")
    assert _language_matches("pt-BR", "pt-BR")
    assert not _language_matches("pt-BR", "en")
    assert not _language_matches("pt-BR", None)
