from pathlib import Path

import pytest

from autoshorts.config import load_settings


def test_load_settings_reads_validated_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_WORKERS", "4")
    monkeypatch.setenv("VIDEO_CRF", "18")
    monkeypatch.setenv("OVERWRITE_POLICY", "skip")

    settings = load_settings()

    assert settings.max_workers == 4
    assert settings.video_crf == 18
    assert settings.overwrite_policy == "skip"
    assert isinstance(settings.output_dir, Path)


def test_load_settings_rejects_invalid_integer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_WORKERS", "many")
    with pytest.raises(ValueError, match="MAX_WORKERS"):
        load_settings()


def test_load_settings_rejects_invalid_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OVERWRITE_POLICY", "always")
    with pytest.raises(ValueError, match="OVERWRITE_POLICY"):
        load_settings()
