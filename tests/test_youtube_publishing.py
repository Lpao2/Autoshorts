from __future__ import annotations

from dataclasses import replace
from types import ModuleType, SimpleNamespace

import pytest
from test_brand_assets import setup_brand

from autoshorts.automation_config import (
    AutomationConfig,
    GoogleIdentity,
    PublishingChannel,
    ThemeProfile,
)
from autoshorts.brand_assets import (
    BrandAssetService,
    BrandAssetStatus,
    BrandAssetType,
    FakeBrandImageProvider,
)
from autoshorts.channel_setup import link_approved_brand_plan
from autoshorts.credential_store import FilesystemCredentialStore
from autoshorts.media_probe import MediaInfo
from autoshorts.mock_uploader import MockYouTubeUploadBackend
from autoshorts.state_store import (
    ChannelCredentialBinding,
    PublicationStatus,
    StateStore,
)
from autoshorts.youtube_publishing import (
    GoogleYouTubeUploadBackend,
    PublicationMetadata,
    YouTubePublicationService,
    YouTubeUploadFailure,
)


class Connection:
    def __init__(self, binding):
        self.binding = binding
        self.verify_calls = 0

    def verify_binding(self, channel_id):
        assert channel_id == self.binding.publishing_channel_id
        self.verify_calls += 1
        return self.binding

    def load_verified_credentials(self, channel_id):
        self.verify_binding(channel_id)
        return object()


class Uploader:
    def __init__(self, remote_id="remote-123", failure=None):
        self.remote_id = remote_id
        self.failure = failure
        self.calls = 0

    def upload(self, credentials, video_path, metadata):
        self.calls += 1
        if self.failure:
            raise self.failure
        assert metadata.privacy_status == "private"
        return self.remote_id


def _probe(path):
    return MediaInfo(path.resolve(), 30.0, 1080, 1920, 30.0, True, 0)


def configured_service(tmp_path, *, uploader=None):
    store = StateStore(tmp_path / "state.db")
    theme_template, archetype, plan = setup_brand(store)
    provider = FakeBrandImageProvider()
    asset_service = BrandAssetService(tmp_path, store, provider)
    for asset_type in BrandAssetType:
        asset = asset_service.generate(plan, theme_template, archetype, asset_type)
        store.set_brand_asset_status(
            plan.brand_plan_id,
            asset_type,
            asset.version,
            BrandAssetStatus.APPROVED,
        )
    identity = GoogleIdentity("google-a", "Google A", "owner@example.test")
    channel = PublishingChannel(
        "channel-a", "Canal A", plan.slot, google_identity_id=identity.identity_id
    )
    theme = ThemeProfile(
        theme_template.display_name,
        "",
        (),
        plan.theme_key,
        channels=(channel,),
        youtube_category_id="20",
        youtube_category_name="Gaming",
    )
    config = AutomationConfig((theme,), (identity,))
    link_approved_brand_plan(
        theme=theme,
        publishing_channel=channel,
        planned_brand=plan,
        state_store=store,
    )
    binding = ChannelCredentialBinding(
        channel.channel_id,
        identity.identity_id,
        "youtube",
        "channel-a.json",
        "UC-A",
        "YouTube A",
        "created",
        "verified",
        "CONNECTED",
    )
    store.save_channel_binding(binding)
    connection = Connection(binding)
    uploader = uploader or Uploader()
    service = YouTubePublicationService(
        config,
        store,
        FilesystemCredentialStore(tmp_path / "credentials"),
        connection_service=connection,
        upload_backend=uploader,
        media_probe=_probe,
    )
    video = tmp_path / "short.mp4"
    video.write_bytes(b"final-video")
    return service, store, channel, plan, video, uploader, connection


def test_review_approval_dry_run_and_idempotent_private_upload(tmp_path):
    service, store, channel, _plan, video, uploader, connection = configured_service(
        tmp_path
    )
    metadata = PublicationMetadata("Primeiro Short", "Descrição", ("tag",))
    publication = service.create_for_review(channel.channel_id, video, metadata)
    assert publication.status == PublicationStatus.READY_FOR_REVIEW
    assert publication.privacy_status == "private"
    assert service.create_for_review(channel.channel_id, video, metadata) == publication
    approved = service.approve(publication.publication_id)
    assert approved.status == PublicationStatus.APPROVED_FOR_PUBLISH
    preflight = service.preflight(publication.publication_id)
    assert preflight.oauth_verified
    assert preflight.youtube_channel_title == "YouTube A"
    published = service.upload_approved(publication.publication_id)
    assert published.status == PublicationStatus.PUBLISHED
    assert published.remote_video_id == "remote-123"
    assert uploader.calls == 1
    assert len(store.publication_attempts(publication.publication_id)) == 1
    assert service.upload_approved(publication.publication_id) == published
    assert uploader.calls == 1
    assert connection.verify_calls == 2
    store.close()


def test_same_video_and_channel_cannot_create_competing_metadata(tmp_path):
    service, store, channel, _plan, video, _uploader, _connection = configured_service(
        tmp_path
    )
    service.create_for_review(channel.channel_id, video, PublicationMetadata("One"))
    with pytest.raises(ValueError, match="já possui"):
        service.create_for_review(
            channel.channel_id, video, PublicationMetadata("Changed")
        )
    assert len(store.publications()) == 1
    store.close()


def test_revision_revokes_prior_approval_and_keeps_same_idempotency_key(tmp_path):
    service, store, channel, _plan, video, _uploader, _connection = configured_service(
        tmp_path
    )
    publication = service.create_for_review(
        channel.channel_id, video, PublicationMetadata("Original")
    )
    service.approve(publication.publication_id)
    revised = service.revise(
        publication.publication_id,
        PublicationMetadata("Revisado", "Nova descrição", ("nova",), "unlisted"),
    )
    assert revised.publication_id == publication.publication_id
    assert revised.idempotency_key == publication.idempotency_key
    assert revised.status == PublicationStatus.READY_FOR_REVIEW
    assert revised.approved_at is None
    assert revised.title == "Revisado"
    assert revised.privacy_status == "unlisted"
    store.close()


def test_upload_requires_explicit_approval_and_unchanged_file(tmp_path):
    service, store, channel, _plan, video, uploader, _connection = configured_service(
        tmp_path
    )
    publication = service.create_for_review(
        channel.channel_id, video, PublicationMetadata("Title")
    )
    with pytest.raises(ValueError, match="explicitly approved"):
        service.upload_approved(publication.publication_id)
    service.approve(publication.publication_id)
    video.write_bytes(b"changed")
    with pytest.raises(ValueError, match="mudou"):
        service.preflight(publication.publication_id)
    assert uploader.calls == 0
    store.close()


def test_uncertain_remote_failure_is_persisted_and_blocks_retry(tmp_path):
    failure = YouTubeUploadFailure(
        "check Studio",
        retryable=False,
        outcome_uncertain=True,
        category="REMOTE_OUTCOME_UNCERTAIN",
    )
    uploader = Uploader(failure=failure)
    service, store, channel, _plan, video, _uploader, _connection = configured_service(
        tmp_path, uploader=uploader
    )
    publication = service.create_for_review(
        channel.channel_id, video, PublicationMetadata("Title")
    )
    service.approve(publication.publication_id)
    with pytest.raises(YouTubeUploadFailure, match="Studio"):
        service.upload_approved(publication.publication_id)
    failed = store.publication(publication.publication_id)
    assert failed.status == PublicationStatus.UPLOAD_UNCERTAIN
    assert failed.error_category == "REMOTE_OUTCOME_UNCERTAIN"
    with pytest.raises(ValueError, match="explicitly approved"):
        service.upload_approved(publication.publication_id)
    assert uploader.calls == 1
    store.close()


def test_published_package_marks_linked_queue_short_posted(tmp_path):
    service, store, channel, _plan, video, _uploader, _connection = configured_service(
        tmp_path
    )
    now = "2026-10-19T00:00:00+00:00"
    store.connection.execute(
        """INSERT INTO videos
        (youtube_id,source_url,creator_name,theme_name,title,status,
         discovered_at,updated_at)
        VALUES ('source','url','creator','theme','title','QUEUED',?,?)""",
        (now, now),
    )
    store.connection.execute(
        """INSERT INTO shorts
        (youtube_id,clip_index,account_slot,account_identifier,
         publishing_channel_id,status,file_path,queue_path,created_at,updated_at)
        VALUES ('source',1,1,?,?, 'QUEUED',?,?,?,?)""",
        (channel.channel_id, channel.channel_id, str(video), str(video), now, now),
    )
    store.connection.commit()
    publication = service.create_for_review(
        channel.channel_id, video, PublicationMetadata("Title")
    )
    assert publication.source_short_id is not None
    service.approve(publication.publication_id)
    service.upload_approved(publication.publication_id)
    short = store.shorts_for_video("source")[0]
    assert short.status.value == "POSTED"
    assert short.posted_at is not None
    store.close()


def test_link_requires_current_approved_assets(tmp_path):
    with StateStore(tmp_path / "state.db") as store:
        template, _archetype, plan = setup_brand(store)
        identity = GoogleIdentity("google-a", "A", "a@example.test")
        channel = PublishingChannel(
            "channel", "Channel", plan.slot, google_identity_id=identity.identity_id
        )
        theme = ThemeProfile(
            template.display_name,
            "",
            (),
            plan.theme_key,
            channels=(channel,),
        )
        with pytest.raises(ValueError, match="logo"):
            link_approved_brand_plan(
                theme=theme,
                publishing_channel=channel,
                planned_brand=plan,
                state_store=store,
            )


def test_metadata_validation_rejects_publication_api_limits():
    with pytest.raises(ValueError, match="100"):
        PublicationMetadata("x" * 101).validated()
    with pytest.raises(ValueError, match="Privacy"):
        replace(PublicationMetadata("ok"), privacy_status="secret").validated()
    with pytest.raises(ValueError, match="Made for kids"):
        replace(
            PublicationMetadata("ok", category_id="20"), made_for_kids=None
        ).validated()
    with pytest.raises(ValueError, match="Synthetic"):
        replace(
            PublicationMetadata("ok", category_id="20"),
            contains_synthetic_media=None,
        ).validated()
    with pytest.raises(ValueError, match="Paid product"):
        replace(
            PublicationMetadata("ok", category_id="20"),
            has_paid_product_placement=None,
        ).validated()


def test_theme_category_and_explicit_declarations_are_persisted(tmp_path):
    service, store, channel, _plan, video, _uploader, _connection = configured_service(
        tmp_path
    )
    publication = service.create_for_review(
        channel.channel_id, video, PublicationMetadata("Title")
    )
    assert publication.category_id == "20"
    assert publication.category_name == "Gaming"
    assert publication.made_for_kids is False
    assert publication.contains_synthetic_media is False
    assert publication.has_paid_product_placement is False
    assert publication.default_language == "pt-BR"
    assert publication.license == "youtube"
    assert publication.embeddable is True
    assert publication.public_stats_viewable is True
    store.close()


def test_official_backend_uses_resumable_videos_insert(tmp_path, monkeypatch):
    calls = {}

    class Request:
        def next_chunk(self, *, num_retries):
            calls["retries"] = num_retries
            return None, {"id": "youtube-video-id"}

    class Videos:
        def insert(self, **kwargs):
            calls["insert"] = kwargs
            return Request()

    discovery = ModuleType("googleapiclient.discovery")
    discovery.build = lambda *args, **kwargs: SimpleNamespace(videos=lambda: Videos())
    http = ModuleType("googleapiclient.http")

    def media(path, *, chunksize, resumable):
        calls["media"] = (path, chunksize, resumable)
        return object()

    http.MediaFileUpload = media
    package = ModuleType("googleapiclient")
    monkeypatch.setitem(__import__("sys").modules, "googleapiclient", package)
    monkeypatch.setitem(
        __import__("sys").modules, "googleapiclient.discovery", discovery
    )
    monkeypatch.setitem(__import__("sys").modules, "googleapiclient.http", http)
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    remote_id = GoogleYouTubeUploadBackend().upload(
        object(),
        video,
        PublicationMetadata(
            "Title",
            "Description",
            ("tag",),
            category_id="20",
            category_name="Gaming",
        ),
    )
    assert remote_id == "youtube-video-id"
    assert calls["media"] == (str(video), 8 * 1024 * 1024, True)
    assert calls["insert"]["part"] == "snippet,status,paidProductPlacementDetails"
    assert calls["insert"]["body"]["snippet"]["categoryId"] == "20"
    assert calls["insert"]["body"]["status"] == {
        "privacyStatus": "private",
        "selfDeclaredMadeForKids": False,
        "containsSyntheticMedia": False,
        "license": "youtube",
        "embeddable": True,
        "publicStatsViewable": True,
    }
    assert calls["insert"]["body"]["paidProductPlacementDetails"] == {
        "hasPaidProductPlacement": False
    }
    assert calls["retries"] == 3


def test_reusable_mock_backend_never_needs_network(tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    backend = MockYouTubeUploadBackend("fake-id")
    metadata = PublicationMetadata("Title")
    assert backend.upload(object(), video, metadata) == "fake-id"
    assert backend.calls == [(video, metadata)]
