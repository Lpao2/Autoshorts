from autoshorts.automation_config import (
    GoogleIdentity,
    PostingPolicy,
    PublishingChannel,
    ThemeProfile,
)
from autoshorts.brand_identity import BrandKitStatus, ChannelBrandKit
from autoshorts.channel_setup import (
    ChannelSetupStatus,
    resolve_channel_setup_status,
    resolve_theme_channel_setups,
)
from autoshorts.state_store import ChannelCredentialBinding, StateStore


def kit(channel_id="channel"):
    return ChannelBrandKit(
        channel_id,
        "Nome",
        "@nome",
        "Descrição",
        "Tagline",
        ("gaming",),
        "visual",
        ("#000", "#fff", "#f00", "#111"),
        "logo",
        "banner",
        BrandKitStatus.APPROVED,
        1,
    )


def binding(channel_id="channel"):
    return ChannelCredentialBinding(
        channel_id,
        "google-a",
        "youtube",
        "ref",
        "UC123",
        "Canal",
        "created",
        "verified",
        "CONNECTED",
    )


def test_setup_status_keeps_branding_and_youtube_independent():
    identity = GoogleIdentity("google-a", "Google", "owner@example.test")
    policy = PostingPolicy()
    assert (
        resolve_channel_setup_status(
            identity=identity, brand_kit=None, binding=binding(), posting_policy=policy
        )
        == ChannelSetupStatus.BRANDING_PENDING
    )
    assert (
        resolve_channel_setup_status(
            identity=identity, brand_kit=kit(), binding=None, posting_policy=policy
        )
        == ChannelSetupStatus.YOUTUBE_PENDING
    )
    assert (
        resolve_channel_setup_status(
            identity=identity, brand_kit=kit(), binding=binding(), posting_policy=policy
        )
        == ChannelSetupStatus.READY_FOR_SCHEDULING
    )
    assert (
        resolve_channel_setup_status(
            identity=identity,
            brand_kit=kit(),
            binding=binding(),
            posting_policy=PostingPolicy(enabled=True),
        )
        == ChannelSetupStatus.ACTIVE
    )


def test_resolved_setup_joins_config_catalog_brand_and_binding(tmp_path):
    identity = GoogleIdentity("google-a", "Google Principal", "owner@example.test")
    channels = tuple(
        PublishingChannel(
            f"channel-{slot}", f"LoL {slot}", slot, google_identity_id="google-a"
        )
        for slot in (1, 2, 3)
    )
    theme = ThemeProfile("LoL", "", (), "lol", channels=channels)
    with StateStore(tmp_path / "state.db") as store:
        template = store.get_theme_template("lol")
        store.save_channel_binding(binding("channel-1"))
        resolved = resolve_theme_channel_setups(theme, template, (identity,), store)
    assert len(resolved) == 3
    assert all(item.google_identity == identity for item in resolved)
    assert resolved[0].channel_binding.youtube_channel_id == "UC123"
    assert resolved[0].archetype.name == "Caos na Rift"
    assert resolved[0].setup_status == ChannelSetupStatus.BRANDING_PENDING


def test_approved_brand_survives_missing_youtube(tmp_path):
    with StateStore(tmp_path / "state.db") as store:
        approved = store.approve_brand_kit(kit())
        assert store.get_approved_brand_kit("channel") == approved
        assert store.get_channel_binding("channel") is None
