import sys

from autoshorts.desktop import desktop_quote, main


def test_shortcut_uses_active_interpreter_and_xdg_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "app data"))
    monkeypatch.setattr(sys, "executable", "/home/user/My Project/.venv/bin/python")
    assert main() == 0
    content = (tmp_path / "app data/applications/autoshorts.desktop").read_text()
    assert (
        'Exec="/home/user/My Project/.venv/bin/python" -m autoshorts.gui\n' in content
    )
    assert "Terminal=false" in content


def test_desktop_escapes_reserved_path_characters():
    # Two independent escaping passes: Desktop Entry string, then Exec quoting.
    value = '/tmp/a$"`\\%b/python'
    quoted = desktop_quote(value)
    assert "%%b" in quoted
    assert r"\\$" in quoted
    assert r'\\"' in quoted
    assert r"\\`" in quoted
    assert r"\\\\" in quoted
