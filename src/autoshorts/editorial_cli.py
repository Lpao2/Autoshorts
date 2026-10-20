"""Small administration CLI for market-aware editorial source configuration."""

from __future__ import annotations

import argparse
from pathlib import Path

from autoshorts.editorial_seed import import_editorial_seed
from autoshorts.state_store import StateStore, default_state_db_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-db", type=Path, default=default_state_db_path())
    commands = parser.add_subparsers(dest="command", required=True)
    seed = commands.add_parser("import-seed")
    seed.add_argument("path", type=Path)
    commands.add_parser("summary")
    arguments = parser.parse_args(argv)
    with StateStore(arguments.state_db) as state:
        if arguments.command == "import-seed":
            profiles, associations = import_editorial_seed(arguments.path, state)
            print(f"profiles={profiles} associations={associations}")
            return 0
        profiles = state.theme_market_profiles()
        sources = state.content_sources()
        associations = sum(
            len(state.theme_content_sources(p.profile_id)) for p in profiles
        )
        validated = sum(s.validation_status == "VALIDATED" for s in sources)
        print(
            f"profiles={len(profiles)} sources={len(sources)} "
            f"associations={associations} validated={validated}"
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
