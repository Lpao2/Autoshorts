import json

import pytest

from autoshorts.ai_analysis import parse_analysis_response
from autoshorts.automation_config import (
    AutomationConfig,
    PublishingChannel,
    ThemeProfile,
    load_automation_config,
)
from autoshorts.automation_pipeline import AutomationRequest, _safe_creator_directory


def test_load_automation_config(tmp_path):
    path = tmp_path / "automation.json"
    path.write_text(
        json.dumps(
            {
                "themes": [
                    {
                        "name": "tech",
                        "prompt": "select moments",
                        "creators": [
                            {"name": "Creator", "videos_url": "https://example/videos"}
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    config = load_automation_config(path)
    assert config.theme("tech").creators[0].name == "Creator"


def test_parse_legacy_ai_response():
    result = parse_analysis_response(
        "INICIODARESPOSTA\n"
        "corte1: [00:10 - 00:20] - Momento forte\n"
        "corte2: [01:00 - 01:12] - Segundo momento\n"
        "FIMDARESPOSTA"
    )
    assert [clip.title for clip in result.clips] == [
        "Momento forte",
        "Segundo momento",
    ]


def test_creator_directory_is_safe():
    assert _safe_creator_directory("Creator / One") == "Creator___One"
    assert _safe_creator_directory("...") == "creator"


def test_invalid_automation_config_is_rejected(tmp_path):
    path = tmp_path / "invalid.json"
    path.write_text(
        '{"themes": [{"name": "bad", "creators": '
        '[{"name": "creator", "videos_url": "not-a-url"}]}]}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="HTTP"):
        load_automation_config(path)


def test_missing_theme_is_clear(tmp_path):
    path = tmp_path / "valid.json"
    path.write_text(
        '{"themes": [{"name": "tech", "creators": '
        '[{"name": "Creator", "videos_url": "https://example.com/videos"}]}]}',
        encoding="utf-8",
    )
    config = load_automation_config(path)
    with pytest.raises(KeyError, match="missing"):
        config.theme("missing")


def test_automation_request_rejects_invalid_limits(tmp_path, monkeypatch):
    from autoshorts import automation_pipeline

    monkeypatch.setattr(
        automation_pipeline, "discover_creator_videos", lambda *args, **kwargs: []
    )

    class FakeConfig:
        def theme(self, name):
            return type("Theme", (), {"creators": (), "output_subdirectory": "tech"})()

    with pytest.raises(ValueError, match="positive"):
        automation_pipeline.run_automation(
            AutomationRequest("tech", 0, 30, tmp_path, download_only=True),
            FakeConfig(),
            None,
        )


def test_channel_and_youtube_ids_are_globally_unique():
    with pytest.raises(ValueError, match="globally unique"):
        AutomationConfig(
            (
                ThemeProfile(
                    "A",
                    "",
                    (),
                    "a",
                    channels=(PublishingChannel("same", "A", 1),),
                ),
                ThemeProfile(
                    "B",
                    "",
                    (),
                    "b",
                    channels=(PublishingChannel("same", "B", 1),),
                ),
            )
        )
    with pytest.raises(ValueError, match="YouTube channel IDs"):
        AutomationConfig(
            (
                ThemeProfile(
                    "A",
                    "",
                    (),
                    "a",
                    channels=(
                        PublishingChannel("a", "A", 1, youtube_channel_id="UC"),
                    ),
                ),
                ThemeProfile(
                    "B",
                    "",
                    (),
                    "b",
                    channels=(
                        PublishingChannel("b", "B", 1, youtube_channel_id="UC"),
                    ),
                ),
            )
        )
