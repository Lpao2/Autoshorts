"""Run with a real or virtual display; remain optional on headless systems."""

import time

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
from autoshorts.gui import AutoShortsWindow  # noqa: E402


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
        ("1", "✓", "LoL A", "Google A", "—", "Não conectado"),
        ("2", "—", "LoL B", "não vinculada", "—", "Não conectado"),
    ]
    identity_rows = app.auto_identities.get_children()
    assert [app.auto_identities.item(row, "values") for row in identity_rows] == [
        ("✓", "Google A", "a@example.test", "1")
    ]
