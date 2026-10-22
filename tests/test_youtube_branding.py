from __future__ import annotations

import json
from dataclasses import replace

import pytest
from test_youtube_publishing import configured_service

from autoshorts.credential_store import FilesystemCredentialStore
from autoshorts.youtube_branding import (
    GoogleYouTubeBrandingBackend,
    YouTubeBrandingService,
    _conservative_branding,
    _immutable_channel_identity,
    _language_matches,
    _youtube_banner_asset,
)
from autoshorts.youtube_oauth import YOUTUBE_MANAGE_SCOPE


class Connection:
    def load_verified_credentials(self, channel_id):
        return object()


class Backend:
    def __init__(self, channel_id="UC-A"):
        self.channel_id = channel_id
        self.uploads = 0
        self.watermarks = []
        self.updates = []
        self.remote = {
            "id": channel_id,
            "snippet": {
                "title": "YouTube A",
                "customUrl": "@youtube-a",
                "thumbnails": {"default": {"url": "https://avatar"}},
            },
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

    def set_watermark(self, credentials, channel_id, path):
        assert channel_id == self.channel_id
        self.watermarks.append(path)
        return {}


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
    assert plan.country == "BR"
    assert plan.banner.width == 2560
    assert plan.logo.width == 1024
    assert backend.uploads == 0
    assert backend.updates == []
    assert backend.watermarks == []
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
    assert backend.watermarks == []
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
    assert backend.watermarks == [result.plan.watermark_path]
    assert result.watermark_set
    store.close()


def test_watermark_only_does_not_reapply_other_branding(tmp_path):
    backend = Backend()
    target, store, channel = _service(tmp_path, backend=backend)
    result = target.apply_watermark(channel.channel_id)
    assert result.http_result == "HTTP 204 No Content"
    assert backend.watermarks == [result.plan.watermark_path]
    assert backend.uploads == 0
    assert backend.updates == []
    store.close()


def test_banner_only_preserves_channel_settings_and_skips_watermark(tmp_path):
    backend = Backend()
    backend.remote["brandingSettings"]["image"] = {}
    target, store, channel = _service(tmp_path, backend=backend)
    result = target.apply_banner_only(channel.channel_id)
    assert result.banner_active
    assert result.youtube_channel_id == "UC-A"
    assert backend.uploads == 1
    assert backend.watermarks == []
    assert backend.updates[0]["channel"] == {
        "description": "old",
        "keywords": "old-keyword",
        "country": "BR",
        "trackingAnalyticsAccountId": "UA-preserve",
        "title": "YouTube A",
    }
    assert backend.updates[0]["image"]["bannerExternalUrl"].endswith(
        "new-banner"
    )
    store.close()


def test_banner_only_accepts_insert_that_is_immediately_active(tmp_path):
    class ImmediateBackend(Backend):
        def upload_banner(self, credentials, path):
            url = super().upload_banner(credentials, path)
            self.remote["brandingSettings"]["image"] = {
                "bannerImageUrl": "https://cdn.example/active-banner"
            }
            return url

    backend = ImmediateBackend()
    backend.remote["brandingSettings"]["image"] = {}
    target, store, channel = _service(tmp_path, backend=backend)
    result = target.apply_banner_only(channel.channel_id)
    assert result.banner_active
    assert backend.uploads == 1
    assert backend.updates == []
    store.close()


def test_banner_only_wrong_remote_channel_stops_before_upload(tmp_path):
    backend = Backend("UC-WRONG")
    target, store, channel = _service(tmp_path, backend=backend)
    with pytest.raises(ValueError, match="channel ID"):
        target.apply_banner_only(channel.channel_id)
    assert backend.uploads == 0
    assert backend.updates == []
    store.close()


def test_wrong_remote_channel_stops_before_banner_upload(tmp_path):
    backend = Backend("UC-WRONG")
    target, store, channel = _service(tmp_path, backend=backend)
    with pytest.raises(ValueError, match="channel ID"):
        target.apply(channel.channel_id)
    assert backend.uploads == 0
    assert backend.updates == []
    assert backend.watermarks == []
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


def test_remote_identity_ignores_only_thumbnail_query_strings():
    before = {
        "snippet": {
            "title": "A",
            "customUrl": "@a",
            "thumbnails": {"default": {"url": "https://cdn/x.jpg?a=1"}},
        }
    }
    after = {
        "snippet": {
            "title": "A",
            "customUrl": "@a",
            "thumbnails": {"default": {"url": "https://cdn/x.jpg?a=2"}},
        }
    }
    changed = {
        "snippet": {
            "title": "A",
            "customUrl": "@a",
            "thumbnails": {"default": {"url": "https://cdn/other.jpg"}},
        }
    }
    assert _immutable_channel_identity(before) == _immutable_channel_identity(after)
    assert _immutable_channel_identity(before) != _immutable_channel_identity(changed)


def test_watermark_disables_gzip_for_empty_204_response(tmp_path, monkeypatch):
    logo = tmp_path / "logo.png"
    logo.write_bytes(b"png")

    class Request:
        def __init__(self):
            self.headers = {}

        def execute(self):
            assert self.headers["accept-encoding"] == "identity"
            return None

    request = Request()

    class Watermarks:
        def set(self, **kwargs):
            assert kwargs["channelId"] == "UC-A"
            assert kwargs["body"]["timing"] == {
                "type": "offsetFromStart",
                "offsetMs": 0,
            }
            return request

    class Api:
        def watermarks(self):
            return Watermarks()

    backend = GoogleYouTubeBrandingBackend()
    monkeypatch.setattr(backend, "_api", lambda credentials: Api())
    assert backend.set_watermark(object(), "UC-A", logo) is None


def test_oversize_banner_uses_persisted_delivery_asset(tmp_path):
    master = tmp_path / "master.png"
    delivery = tmp_path / "youtube.jpg"
    master.write_bytes(b"x")
    delivery.write_bytes(b"delivery")
    target, store, channel = _service(tmp_path / "service")
    asset = store.latest_valid_brand_asset(
        store.get_approved_brand_kit(channel.channel_id).source_plan_id,
        "BANNER",
    )
    oversized = replace(
        asset,
        file_size=7 * 1024 * 1024,
        file_path=master,
        delivery_path=delivery,
    )
    selected = _youtube_banner_asset(oversized)
    assert selected.file_path == delivery
    assert selected.file_size == len(b"delivery")
    store.close()
