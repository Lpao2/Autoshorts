import json
from types import SimpleNamespace

import pytest

from autoshorts.automation_config import (
    AutomationConfig,
    GoogleIdentity,
    PublishingChannel,
    ThemeProfile,
)
from autoshorts.metadata_pipeline import (
    GeneratedMetadata,
    approve_and_enqueue_metadata,
    generate_metadata_draft,
)
from autoshorts.state_store import (
    MetadataDraftStatus,
    ReviewPolicy,
    StateStore,
    VideoStatus,
)


class FakeProvider:
    provider_name = "fake"
    model = "fake-v1"

    def __init__(self, *, error=None):
        self.calls = 0
        self.error = error

    def generate(self, prompt):
        self.calls += 1
        assert "SHORT TRANSCRIPT\nminha vida não desce" in prompt
        if self.error:
            raise self.error
        return GeneratedMetadata(
            (
                "A cura da Ambessa não para",
                "Essa build não deixa morrer",
                "Roubo de vida absurdo",
            ),
            "A cura da Ambessa não para",
            "Mylon testa a cura da Ambessa e se surpreende com o resultado.",
            ("League of Legends", "LoL", "Ambessa", "gameplay"),
            ("LoL", "Shorts"),
            "Título direto e fiel ao trecho.",
            self.provider_name,
            self.model,
        )


def prepared(tmp_path):
    channel = PublishingChannel("tiltado", "Tiltado", 1, google_identity_id="google-a")
    theme = ThemeProfile("LoL", "Momentos de LoL", (), "lol", channels=(channel,))
    config = AutomationConfig((theme,), (GoogleIdentity("google-a", "A", "a@test"),))
    store = StateStore(tmp_path / "state.db")
    source = tmp_path / "source.mp4"
    source.touch()
    store.register_video(
        youtube_id="video-1",
        source_url="https://youtube.test/video-1",
        creator_name="Mylon",
        theme_name="lol",
        title="Vídeo longo",
    )
    store.connection.execute(
        "UPDATE videos SET status=? WHERE youtube_id='video-1'", (VideoStatus.READY,)
    )
    transcript = tmp_path / "transcript.json"
    transcript.write_text("{}", encoding="utf-8")
    analysis = tmp_path / "analysis.json"
    analysis.write_text("{}", encoding="utf-8")
    (tmp_path / "selection.json").write_text(
        json.dumps({"context": "minha vida não desce", "reason": "cura inesperada"}),
        encoding="utf-8",
    )
    store.connection.execute(
        "INSERT INTO transcripts VALUES ('video-1', ?, 'fake', 'v1', 'pt', 'now')",
        (str(transcript),),
    )
    store.connection.execute(
        "INSERT INTO analyses VALUES ('video-1', ?, 'fake', 'v1', 'hash', 'now')",
        (str(analysis),),
    )
    store.connection.execute(
        """INSERT INTO ready_artifacts VALUES
        ('video-1', ?, 'ready-sha', ?, ?, 'caption', 'now', 'now', 'now')""",
        (str(source), str(tmp_path / "manifest.json"), str(tmp_path / "caption.ass")),
    )
    market = SimpleNamespace(
        enabled=True, youtube_category_id="20", youtube_category_name="Gaming"
    )
    kit = SimpleNamespace(
        source_plan_id="brand-plan", public_name="Tiltado", description="LoL"
    )
    store.theme_market_profile = lambda *args: market
    store.get_approved_brand_kit = lambda *args: kit
    return store, config, channel


def test_ready_metadata_is_persisted_idempotent_and_manual_does_not_enqueue(tmp_path):
    store, config, channel = prepared(tmp_path)
    provider = FakeProvider()
    with store:
        first = generate_metadata_draft(
            youtube_id="video-1",
            publishing_channel_id=channel.channel_id,
            config=config,
            state=store,
            provider=provider,
        )
        second = generate_metadata_draft(
            youtube_id="video-1",
            publishing_channel_id=channel.channel_id,
            config=config,
            state=store,
            provider=provider,
        )
        assert first == second
        assert provider.calls == 1
        assert first.status == MetadataDraftStatus.REVIEW_REQUIRED
        assert first.review_policy == ReviewPolicy.MANUAL_REVIEW
        assert first.category_id == "20"
        assert first.default_language == "pt-BR"
        assert first.publishing_channel_id == "tiltado"
        publications = store.connection.execute(
            "SELECT count(*) FROM publications"
        ).fetchone()[0]
        jobs = store.connection.execute(
            "SELECT count(*) FROM publication_jobs"
        ).fetchone()[0]
        assert publications == jobs == 0


def test_metadata_failure_preserves_ready_and_is_recoverable(tmp_path):
    store, config, channel = prepared(tmp_path)
    with store, pytest.raises(RuntimeError, match="offline"):
        generate_metadata_draft(
            youtube_id="video-1",
            publishing_channel_id=channel.channel_id,
            config=config,
            state=store,
            provider=FakeProvider(error=RuntimeError("offline")),
        )
    with StateStore(tmp_path / "state.db") as reopened:
        assert reopened.get_video("video-1").status == VideoStatus.READY
        row = reopened.connection.execute(
            "SELECT status FROM metadata_drafts"
        ).fetchone()
        assert row[0] == MetadataDraftStatus.FAILED_RECOVERABLE


def test_auto_policy_can_approve_and_enqueue_without_human():
    draft = SimpleNamespace(
        draft_id=1,
        youtube_id="video-1",
        ready_checksum="sha",
        publishing_channel_id="tiltado",
        title="Title",
        description="Description",
        tags=("tag",),
        privacy_status="private",
        made_for_kids=False,
        contains_synthetic_media=False,
        has_paid_product_placement=False,
        category_id="20",
        category_name="Gaming",
        default_language="pt-BR",
        license="youtube",
        embeddable=True,
        public_stats_viewable=True,
    )
    ready = SimpleNamespace(checksum="sha", file_path="video.mp4")
    state = SimpleNamespace(
        metadata_draft=lambda _id: draft,
        get_ready_artifact=lambda _id: ready,
        approve_metadata_draft=lambda draft_id, publication_id: SimpleNamespace(
            draft_id=draft_id, publication_id=publication_id
        ),
    )
    publication = SimpleNamespace(publication_id=12)
    service = SimpleNamespace(
        create_for_review=lambda *args: publication,
        approve=lambda _id: publication,
    )
    job = SimpleNamespace(job_id=7)
    scheduler = SimpleNamespace(enqueue=lambda *args: job)
    approved, queued = approve_and_enqueue_metadata(
        1,
        state=state,
        publication_service=service,
        scheduler=scheduler,
        now=SimpleNamespace(),
    )
    assert approved.publication_id == 12
    assert queued.job_id == 7
