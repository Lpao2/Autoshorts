"""Run with a real or virtual display; remain optional on headless systems."""

import time
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from test_render_integration import make_settings, synthetic_video  # noqa: F401

pytest.importorskip("tkinter")
from autoshorts.automation_config import (  # noqa: E402
    AutomationConfig,
    Creator,
    EditorialProfile,
    GoogleIdentity,
    PublishingChannel,
    ThemeProfile,
)
from autoshorts.automation_store import AutomationStore  # noqa: E402
from autoshorts.brand_assets import BrandAssetStatus, BrandAssetType  # noqa: E402
from autoshorts.gui import (  # noqa: E402
    AutoShortsWindow,
    _asset_version_history,
    _brand_generation_error_message,
    _identity_display,
    _latest_asset_statuses,
    _youtube_action_labels,
)
from autoshorts.state_store import ChannelCredentialBinding, StateStore  # noqa: E402


@pytest.fixture
def window(tmp_path):
    import tkinter as tk

    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("A display is required for Tk window tests")
    app = AutoShortsWindow(root, make_settings(tmp_path))
    errors = []
    root.report_callback_exception = lambda *args: errors.append(args)
    yield app, root, errors
    app.controller.cancel()
    if app.controller.thread:
        app.controller.thread.join(35)
    try:
        app.destroy()
    except tk.TclError:
        pass
    assert not errors


def drive_until_idle(app, root):
    beats = []
    deadline = time.monotonic() + 15

    def tick():
        beats.append(1)
        if time.monotonic() > deadline:
            root.quit()
        elif app.running:
            root.after(5, tick)
        else:
            root.quit()

    root.after(5, tick)
    root.mainloop()
    assert not app.running
    assert not app.controller.busy
    assert len(beats) >= 1
    return len(beats)


def test_brand_503_message_preserves_existing_data():
    message = _brand_generation_error_message(
        RuntimeError("503 UNAVAILABLE: model high demand")
    )
    assert "temporariamente indisponível" in message
    assert "Nenhuma proposta foi salva" in message


def test_wizard_uses_human_identity_and_connection_actions():
    identity = GoogleIdentity("google-a", "Google Principal", "owner@example.test")
    assert _identity_display(identity) == "Google Principal — owner@example.test"
    assert "google-a" not in _identity_display(identity)
    assert _youtube_action_labels(None) == ("Conectar YouTube",)
    assert _youtube_action_labels(object()) == (
        "Verificar conexão",
        "Reconectar",
        "Desconectar",
    )


def test_theme_library_shows_complete_blueprints(window):
    app, _root, _errors = window
    assert len(app.catalog_tree.get_children()) == 20
    assert app.catalog_tree["columns"] == (
        "status",
        "name",
        "origin",
        "readiness",
        "prompt",
        "versions",
        "channels",
        "logos",
        "banners",
        "approved_assets",
    )
    rows = [
        app.catalog_tree.item(row, "values") for row in app.catalog_tree.get_children()
    ]
    assert all(row[3] == "Completo" for row in rows)
    assert all(int(row[6]) == 3 for row in rows)
    for row in rows:
        logos, plan_count = (int(value) for value in row[7].split("/"))
        banners, banner_plan_count = (int(value) for value in row[8].split("/"))
        approved, asset_count = (int(value) for value in row[9].split("/"))
        assert plan_count == banner_plan_count
        assert 0 <= logos <= plan_count
        assert 0 <= banners <= plan_count
        assert asset_count == plan_count * 2
        assert 0 <= approved <= asset_count


def test_publication_tab_exposes_review_dry_run_and_explicit_upload(window):
    app, root, _errors = window
    assert app.publish_privacy.get() == "private"
    assert app.publication_tree["columns"] == (
        "id",
        "channel",
        "identity",
        "youtube",
        "video",
        "title",
        "privacy",
        "status",
        "remote",
        "error",
    )

    def button_labels(widget):
        labels = []
        for child in widget.winfo_children():
            if child.winfo_class() == "TButton":
                labels.append(str(child["text"]))
            labels.extend(button_labels(child))
        return labels

    labels = set(button_labels(root))
    assert {
        "Vincular Brand Plan",
        "Criar para revisão",
        "Salvar revisão",
        "Aprovar selecionada",
        "Dry-run",
        "Upload real...",
    } <= labels


def test_brand_asset_status_summary_and_library_dialog(window):
    app, root, _errors = window
    assert _latest_asset_statuses(()) == {"LOGO": "PENDING", "BANNER": "PENDING"}
    assert _asset_version_history(()) == {
        "LOGO": "PENDING",
        "BANNER": "PENDING",
    }
    history = _asset_version_history(
        (
            SimpleNamespace(
                asset_type=BrandAssetType.BANNER,
                version=1,
                status=BrandAssetStatus.VALIDATED,
            ),
            SimpleNamespace(
                asset_type=BrandAssetType.BANNER,
                version=2,
                status=BrandAssetStatus.VALIDATED,
            ),
        )
    )
    assert history["BANNER"] == "v1 VALIDATED · v2 VALIDATED"
    first = app.catalog_tree.get_children()[0]
    with StateStore(app.state_db_path) as store:
        if not store.planned_channel_brands(first):
            pytest.skip("Brand asset dialog requires materialized Brand Plans")
    app.catalog_tree.selection_set(first)
    app.view_brand_assets()
    root.update_idletasks()
    dialogs = [
        item for item in root.winfo_children() if item.winfo_class() == "Toplevel"
    ]
    assert dialogs

    def labels(widget):
        result = []
        for child in widget.winfo_children():
            if child.winfo_class() == "TButton":
                result.append(str(child["text"]))
            result.extend(labels(child))
        return result

    buttons = set(labels(dialogs[-1]))
    assert {
        "Gerar logo",
        "Gerar banner",
        "Gerar ambos",
        "Visualizar",
        "Importar logo",
        "Importar banner",
        "Importar ambos",
        "Aprovar",
        "Rejeitar",
        "Regenerar",
        "Gerar assets pendentes — tema atual",
        "Gerar assets pendentes — todos",
        "Importar pasta/lote",
        "Importar manifest JSON",
    } <= buttons
    dialogs[-1].destroy()


def test_catalog_exposes_triads_visual_review(window):
    app, root, _errors = window
    first = app.catalog_tree.get_children()[0]
    with StateStore(app.state_db_path) as store:
        if not store.planned_channel_brands(first):
            pytest.skip("Visual review requires materialized Brand Plans")
    app.review_brand_asset_triads()
    root.update_idletasks()
    dialogs = [
        item for item in root.winfo_children() if item.winfo_class() == "Toplevel"
    ]
    assert dialogs

    def labels(widget):
        result = []
        for child in widget.winfo_children():
            if child.winfo_class() in {"TButton", "TLabel", "TLabelframe"}:
                result.append(str(child["text"]))
            result.extend(labels(child))
        return result

    text = labels(dialogs[-1])
    assert "Tema anterior" in text
    assert "Próximo tema" in text
    assert "Aprovar logo" in text
    assert "Aprovar banner" in text
    assert "Aprovar ambos" in text
    assert "Importar logo" in text
    assert "Importar banner" in text
    assert "Importar ambos" in text
    assert "Rejeitar logo" in text
    assert "Rejeitar banner" in text
    assert "Visualizar histórico" in text
    assert "Aprovar todos deste tema" in text
    assert "Regenerar logo" in text
    assert "Regenerar banner" in text
    assert not any("catalog:" in item for item in text)
    dialogs[-1].destroy()


def test_window_validates_renders_and_reuses_without_blocking(window, synthetic_video):  # noqa: F811
    app, root, _ = window
    app.source.set(str(synthetic_video))
    app.cuts.delete("1.0", "end")
    app.cuts.insert("1.0", "[00:00 - 00:01] - First\n[00:01 - 00:02] - Second")
    app.start(True)
    assert str(app.run_button["state"]) == "disabled"
    drive_until_idle(app, root)
    assert "Validação concluída" in app.status.get()
    assert len(app.tree.get_children()) == 2
    assert not app.controller.defaults.output_dir.exists()
    app.start(False)
    assert drive_until_idle(app, root) > 1
    assert "Sucesso" in app.status.get()
    assert float(app.progress["value"]) == 100
    assert str(app.folder_button["state"]) == "normal"
    app.policy.set("Reutilizar válidos")
    app.start(False)
    drive_until_idle(app, root)
    assert [app.tree.item(row, "values")[2] for row in app.tree.get_children()] == [
        "Reutilizado",
        "Reutilizado",
    ]


def test_window_recovers_from_invalid_cuts_and_closes_safely(window, synthetic_video):  # noqa: F811
    app, root, _ = window
    app.source.set(str(synthetic_video))
    app.cuts.delete("1.0", "end")
    app.cuts.insert("1.0", "invalid")
    app.start(True)
    drive_until_idle(app, root)
    assert "Erro" in app.status.get()
    assert str(app.run_button["state"]) == "normal"
    app.cuts.delete("1.0", "end")
    app.cuts.insert("1.0", "[00:00 - 00:01] - First")
    app.start(False)
    app.cancel()
    drive_until_idle(app, root)
    assert "Cancelado" in app.status.get()
    assert not list(app.controller.defaults.output_dir.glob("*.part.*"))


def test_close_waits_for_worker_after_confirmation(window, monkeypatch):
    from threading import Event

    from autoshorts import gui_jobs

    app, root, _ = window
    entered, release = Event(), Event()

    def delayed_validation(*args):
        entered.set()
        assert release.wait(5)
        return None, [], None, None

    monkeypatch.setattr(gui_jobs, "prepare_job", delayed_validation)
    app.start(True)
    assert entered.wait(5)
    monkeypatch.setattr("autoshorts.gui.messagebox.askyesno", lambda *args: False)
    app.close()
    assert app.running and not app.closing
    monkeypatch.setattr("autoshorts.gui.messagebox.askyesno", lambda *args: True)
    app.close()
    assert app.closing and app.running
    release.set()
    root.after(5000, root.quit)
    root.mainloop()
    assert not app.running
    assert not app.controller.busy
    assert "Cancelado" in app.status.get()


def test_automatic_tab_reflects_saved_creators_and_disabled_state(window, tmp_path):
    app, root, _ = window
    path = tmp_path / "automation.json"
    AutomationStore(path).save(
        AutomationConfig(
            (
                ThemeProfile(
                    "LoL",
                    "",
                    (
                        Creator("Arthur", "https://example.com/arthur"),
                        Creator("Inactive", "https://example.com/inactive", False),
                    ),
                    "lol",
                    channels=(
                        PublishingChannel(
                            "lol-a",
                            "LoL A",
                            1,
                            google_identity_id="google-a",
                            editorial_profile=EditorialProfile(tone="Humor"),
                        ),
                        PublishingChannel("lol-b", "LoL B", 2, False),
                    ),
                ),
            ),
            (GoogleIdentity("google-a", "Google A", "a@example.test"),),
        )
    )
    app.auto_config_path.set(str(path))
    app.load_automation_config_ui(silent=True)
    root.update_idletasks()
    rows = app.auto_creators.get_children()
    assert [app.auto_creators.item(row, "values")[1] for row in rows] == [
        "Arthur",
        "Inactive",
    ]
    assert app.auto_creators.selection() == (rows[0],)
    account_rows = app.auto_accounts.get_children()
    assert [app.auto_accounts.item(row, "values") for row in account_rows] == [
        (
            "1",
            "LoL A",
            "Caos na Rift",
            "Google A",
            "Pendente",
            "—",
            "Branding pendente",
        ),
        ("2", "LoL B", "Clutch Mecânico", "—", "Pendente", "—", "Não preparado"),
    ]
    identity_rows = app.auto_identities.get_children()
    assert [app.auto_identities.item(row, "values") for row in identity_rows] == [
        ("✓", "Google A", "a@example.test", "1")
    ]


def test_gui_loads_sqlite_binding_and_does_not_use_config_channel_id(
    window, tmp_path, monkeypatch
):
    app, root, _ = window
    path = tmp_path / "automation.json"
    config = AutomationConfig(
        (
            ThemeProfile(
                "LoL",
                "",
                (),
                "lol",
                channels=(
                    PublishingChannel(
                        "lol-a",
                        "LoL A",
                        1,
                        google_identity_id="google-a",
                        youtube_channel_id=None,
                    ),
                    PublishingChannel("lol-b", "LoL B", 2),
                ),
            ),
        ),
        (GoogleIdentity("google-a", "Google A", "a@example.test"),),
    )
    AutomationStore(path).save(config)
    app.state_db_path = tmp_path / "state.db"
    now = datetime.now(UTC).isoformat()
    with StateStore(app.state_db_path) as store:
        store.save_channel_binding(
            ChannelCredentialBinding(
                "lol-a",
                "google-a",
                "youtube",
                "lol-a.json",
                "UC-REAL",
                "Real Channel",
                now,
                now,
                "CONNECTED",
            )
        )
    monkeypatch.setattr(
        "autoshorts.gui.YouTubeConnectionService.verify_binding",
        lambda *_args: pytest.fail("GUI refresh must not call YouTube API"),
    )
    app.auto_config_path.set(str(path))
    app.load_automation_config_ui(silent=True)
    root.update_idletasks()
    rows = app.auto_accounts.get_children()
    assert app.auto_accounts.item(rows[0], "values")[5:] == (
        "Real Channel (UC-REAL)",
        "Branding pendente",
    )
    assert app.auto_accounts.item(rows[1], "values")[5:] == ("—", "Não preparado")
    app.auto_accounts.selection_set("lol-a")
    app._update_youtube_buttons()
    assert str(app.connect_youtube_button["state"]) == "disabled"
    assert str(app.verify_youtube_button["state"]) == "normal"
    assert str(app.reconnect_youtube_button["state"]) == "normal"
    assert str(app.disconnect_youtube_button["state"]) == "normal"


def test_successful_connect_and_disconnect_refresh_gui(window, tmp_path, monkeypatch):
    app, _, _ = window
    config = AutomationConfig(
        (
            ThemeProfile(
                "LoL",
                "",
                (),
                "lol",
                channels=(PublishingChannel("lol-a", "LoL A", 1),),
            ),
        )
    )
    app.automation_config = config
    app.state_db_path = tmp_path / "state.db"
    app.auto_accounts.insert("", "end", iid="lol-a", values=(1, "✓", "LoL A"))
    app.auto_accounts.selection_set("lol-a")
    refreshes = []
    monkeypatch.setattr(app, "refresh_theme_details", lambda: refreshes.append(True))
    monkeypatch.setattr("autoshorts.gui.messagebox.askyesno", lambda *_a, **_k: True)
    monkeypatch.setattr("autoshorts.gui.messagebox.showinfo", lambda *_a, **_k: None)

    class Service:
        def __init__(self, _config, store, _credentials):
            self.store = store

        def begin_connection(self, channel_id):
            return SimpleNamespace(
                publishing_channel_id=channel_id,
                google_identity_id="google-a",
                channel=SimpleNamespace(title="Real", channel_id="UC-REAL"),
            )

        def confirm_connection(self, pending):
            now = datetime.now(UTC).isoformat()
            self.store.save_channel_binding(
                ChannelCredentialBinding(
                    pending.publishing_channel_id,
                    pending.google_identity_id,
                    "youtube",
                    "lol-a.json",
                    pending.channel.channel_id,
                    pending.channel.title,
                    now,
                    now,
                    "CONNECTED",
                )
            )

        def disconnect(self, channel_id):
            return self.store.delete_channel_binding(channel_id)

    monkeypatch.setattr("autoshorts.gui.YouTubeConnectionService", Service)
    app.connect_youtube_ui()
    assert len(refreshes) == 1
    with StateStore(app.state_db_path) as store:
        assert store.get_channel_binding("lol-a") is not None
    app.connect_youtube_ui()
    assert len(refreshes) == 2
    app.disconnect_youtube_ui()
    assert len(refreshes) == 3
