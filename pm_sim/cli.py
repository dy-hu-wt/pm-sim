from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .paths import DEFAULT_DB_PATH, DEFAULT_SCENARIO_PATH
from .scenario import ScenarioError
from .state import action_log, event_log, observe, reset
from .time import advance_time


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        result = args.func(args)
    except ScenarioError as error:
        print(f"scenario error: {error}", file=sys.stderr)
        return 2
    except sqlite_missing_reset_error() as error:
        print(f"state error: {error}", file=sys.stderr)
        return 2

    _print_json(result)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pm-sim")
    parser.set_defaults(func=lambda _args: parser.print_help() or {})

    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"SQLite DB path. Default: {DEFAULT_DB_PATH}",
    )

    subparsers = parser.add_subparsers(dest="command")

    reset_parser = subparsers.add_parser("reset", help="Reset DB from scenario JSON.")
    reset_parser.add_argument(
        "--scenario",
        type=Path,
        default=DEFAULT_SCENARIO_PATH,
        help=f"Scenario JSON path. Default: {DEFAULT_SCENARIO_PATH}",
    )
    reset_parser.set_defaults(func=lambda args: reset(args.db, args.scenario))

    observe_parser = subparsers.add_parser("observe", help="Print current observation.")
    observe_parser.set_defaults(func=lambda args: observe(args.db))

    log_parser = subparsers.add_parser("log", help="Print recent action log.")
    log_parser.add_argument("--limit", type=int, default=20)
    log_parser.set_defaults(func=lambda args: action_log(args.db, args.limit))

    events_parser = subparsers.add_parser("events", help="Print scheduled/delivered events.")
    events_parser.add_argument("--limit", type=int, default=20)
    events_parser.set_defaults(func=lambda args: event_log(args.db, args.limit))

    advance_parser = subparsers.add_parser("advance-time", help="Advance simulated time.")
    advance_parser.add_argument(
        "target",
        help="Duration like 30m/2h/1d, 'until_next_event', or 'to:<iso time>'.",
    )
    advance_parser.set_defaults(func=lambda args: advance_time(args.db, args.target))

    return parser


def _print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def sqlite_missing_reset_error() -> tuple[type[Exception], ...]:
    # Import lazily so argparse help does not need sqlite setup.
    import sqlite3

    return (sqlite3.OperationalError, RuntimeError)


if __name__ == "__main__":
    raise SystemExit(main())
