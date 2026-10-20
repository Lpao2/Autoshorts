from autoshorts.automation_config import AutomationConfig
from autoshorts.automation_store import AutomationStore
from autoshorts.publishing_cli import create_parser, main
from autoshorts.state_store import SCHEMA_VERSION


def test_upload_cli_requires_explicit_confirmation_flag():
    arguments = create_parser().parse_args(["upload", "7"])
    assert arguments.publication_id == 7
    assert arguments.confirm_upload is False
    assert arguments.allow_public is False


def test_audit_cli_uses_shared_config_and_state(tmp_path, capsys):
    config = tmp_path / "automation.json"
    state = tmp_path / "state.db"
    AutomationStore(config).save(AutomationConfig(()))
    assert (
        main(
            [
                "--config",
                str(config),
                "--state-db",
                str(state),
                "audit",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert f"schema={SCHEMA_VERSION}" in output
    assert "publishing_channels=0" in output
    assert "publications=0" in output
