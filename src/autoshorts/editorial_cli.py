"""Small administration CLI for market-aware editorial source configuration."""

from __future__ import annotations

import argparse
from pathlib import Path

from autoshorts.editorial_seed import import_editorial_seed, import_posting_seed
from autoshorts.state_store import StateStore, default_state_db_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-db", type=Path, default=default_state_db_path())
    commands = parser.add_subparsers(dest="command", required=True)
    seed = commands.add_parser("import-seed")
    seed.add_argument("path", type=Path)
    posting_seed = commands.add_parser("import-schedules")
    posting_seed.add_argument("path", type=Path)
    commands.add_parser("summary")
    commands.add_parser("schedules")
    arguments = parser.parse_args(argv)
    with StateStore(arguments.state_db) as state:
        if arguments.command == "import-seed":
            profiles, associations = import_editorial_seed(arguments.path, state)
            print(f"profiles={profiles} associations={associations}")
            return 0
        if arguments.command == "import-schedules":
            profiles, windows = import_posting_seed(arguments.path, state)
            print(f"posting_profiles={profiles} windows={windows}")
            return 0
        if arguments.command == "schedules":
            templates = {item.theme_key: item for item in state.list_theme_templates()}
            for market in state.theme_market_profiles():
                posting = state.theme_posting_profile(market.profile_id)
                if posting is None:
                    continue
                windows = state.posting_windows(posting.posting_profile_id)
                principal = next(item for item in windows if item.priority == 1)
                test = next(item for item in windows if item.priority == 2)
                days = ",".join(principal.days_of_week)
                theme = templates[market.theme_key].display_name
                print(
                    f"{theme} | {market.region_code}/{market.language_code} | "
                    f"{posting.timezone} | {posting.timing_policy} | "
                    f"{principal.start_local_time}-{principal.end_local_time} | "
                    f"{test.start_local_time}-{test.end_local_time} | {days} | "
                    f"{posting.strategy}"
                )
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
