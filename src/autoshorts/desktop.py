"""Install a personal Linux application-menu entry for the active interpreter."""

import os
import sys
from pathlib import Path


def desktop_quote(value: str) -> str:
    """Quote a Desktop Entry Exec argument (not a shell command)."""
    if "\n" in value or "\r" in value:
        raise ValueError("Desktop paths cannot contain newlines.")
    value = value.replace("%", "%%")
    for character in ("\\", '"', "`", "$"):
        value = value.replace(character, "\\" + character)
    return ('"' + value + '"').replace("\\", "\\\\")


def main() -> int:
    directory = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    target = directory / "applications/autoshorts.desktop"
    content = (
        "[Desktop Entry]\nType=Application\nName=AutoShorts\n"
        "Comment=Produção local de vídeos curtos\n"
        f"Exec={desktop_quote(sys.executable)} -m autoshorts.gui\n"
        "Icon=video-x-generic\nTerminal=false\nCategories=AudioVideo;Video;\n"
        "StartupNotify=true\n"
    )
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    except OSError as exc:
        print(f"Não foi possível criar o atalho: {exc}", file=sys.stderr)
        return 2
    print(f"Atalho instalado: {target}\nProcure AutoShorts no menu de aplicativos.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
