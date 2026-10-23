import pytest

from autoshorts.automation_config import (
    AutomationConfig,
    ColorPalette,
    EditorialProfile,
    GoogleIdentity,
    PostingPolicy,
    PublishingChannel,
    ThemeProfile,
    VisualProfile,
    automation_config_from_dict,
    automation_config_to_dict,
    resolve_editorial_context,
    resolve_publishing_profile,
)


def channel(channel_id="channel-a", slot=1, **kwargs):
    return PublishingChannel(channel_id, f"Channel {slot}", slot, **kwargs)


def test_legacy_theme_and_channel_ids_are_stable_across_loads():
    legacy = {
        "themes": [
            {
                "name": "Any Theme",
                "output_subdirectory": "any-theme",
                "accounts": [
                    {"name": "Destination", "email": "x@example.test", "slot": 1}
                ],
            }
        ]
    }
    first = automation_config_from_dict(legacy)
    second = automation_config_from_dict(legacy)
    assert first.themes[0].theme_id == second.themes[0].theme_id
    assert (
        first.themes[0].channels[0].channel_id
        == second.themes[0].channels[0].channel_id
    )
    assert first.google_identities[0].email == "x@example.test"


def test_unknown_identity_is_rejected_but_disabled_identity_is_valid():
    theme = ThemeProfile(
        "Topic", "", (), "topic", channels=(channel(google_identity_id="missing"),)
    )
    with pytest.raises(ValueError, match="unknown Google identity"):
        AutomationConfig((theme,))
    config = AutomationConfig(
        (theme,), (GoogleIdentity("missing", "Disabled", "x@example.test", False),)
    )
    assert not config.google_identities[0].enabled


def test_editorial_resolution_composes_base_theme_and_channel_without_hardcode():
    selected = channel(
        editorial_profile=EditorialProfile(
            "Priorize explicações.", "curta", "didático"
        )
    )
    theme = ThemeProfile(
        "Astronomia", "Procure descobertas.", (), "space", channels=(selected,)
    )
    context = resolve_editorial_context(theme, selected, base_prompt="Base")
    assert context == (
        "Base\n\nProcure descobertas.\n\nPriorize explicações.\n\n"
        "Estilo de headline: curta\n\nTom: didático"
    )
    resolved = resolve_publishing_profile(theme, selected, base_prompt="Base")
    assert resolved.channel.channel_id == "channel-a"
    assert resolved.visual_profile == selected.visual_profile


def test_theme_visual_profile_is_inherited_with_channel_palette():
    theme_visual = VisualProfile(
        crop_preset="lol-gameplay-v1",
        focal_y=0.38,
        safe_bottom=450,
    )
    selected = channel(
        visual_profile=VisualProfile(
            palette=ColorPalette("red", "black", "white", "#111111")
        )
    )
    theme = ThemeProfile(
        "LoL", "", (), "lol", channels=(selected,), visual_profile=theme_visual
    )
    resolved = resolve_publishing_profile(theme, selected)
    assert resolved.visual_profile.crop_preset == "lol-gameplay-v1"
    assert resolved.visual_profile.focal_y == 0.38
    assert resolved.visual_profile.palette == selected.visual_profile.palette

    other = ThemeProfile("Other", "", (), "other")
    assert other.visual_profile.crop_preset == "default"


def test_visual_and_posting_profiles_round_trip_with_defaults():
    visual = VisualProfile(
        ColorPalette("red", "black", "white", "#111111"),
        "face",
        "bold",
        "logo-a",
    )
    posting = PostingPolicy(True, 7, "UTC", ("09:00-11:00",))
    selected = channel(visual_profile=visual, posting_policy=posting)
    config = AutomationConfig(
        (ThemeProfile("Topic", "", (), "topic", channels=(selected,)),)
    )
    loaded = automation_config_from_dict(automation_config_to_dict(config))
    restored = loaded.themes[0].channels[0]
    assert restored.visual_profile == visual
    assert restored.posting_policy == posting
    assert loaded.themes[0].visual_profile == VisualProfile()
    assert PostingPolicy().enabled is False
    with pytest.raises(ValueError, match="positive"):
        PostingPolicy(daily_limit=0)


def test_theme_visual_profile_round_trip():
    profile = VisualProfile(
        crop_preset="gameplay",
        caption_preset="gameplay-safe",
        focal_x=0.45,
        focal_y=0.38,
        foreground_scale=1.1,
        blur_strength=24,
        safe_top=100,
        safe_bottom=420,
    )
    config = AutomationConfig(
        (ThemeProfile("LoL", "", (), "lol", visual_profile=profile),)
    )
    restored = automation_config_from_dict(automation_config_to_dict(config))
    assert restored.themes[0].visual_profile == profile


def test_maximum_three_enabled_channels_and_persistent_slots():
    with pytest.raises(ValueError, match="at most 3"):
        ThemeProfile(
            "Topic",
            "",
            (),
            "topic",
            channels=tuple(channel(f"c{x}", x if x < 4 else 3) for x in range(1, 5)),
        )
