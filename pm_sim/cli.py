from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .actions import (
    list_tasks,
    read_doc,
    schedule_meeting,
    send_chat,
    send_email,
    update_task,
)
from .agents.llm import LlmAgentError, run_llm_agent
from .agents.scripted import run_scripted_agent
from .evaluator import evaluate
from .formatters import format_output
from .paths import DEFAULT_DB_PATH, DEFAULT_SCENARIO_PATH
from .scenario import ScenarioError
from .state import action_log, event_log, observe, reset
from .time import advance_time
from .timeline import timeline


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        result = args.func(args)
    except ScenarioError as error:
        print(f"scenario error: {error}", file=sys.stderr)
        return 2
    except LlmAgentError as error:
        print(f"agent error: {error}", file=sys.stderr)
        return 2
    except sqlite_missing_reset_error() as error:
        print(f"state error: {error}", file=sys.stderr)
        return 2

    _print_result(args.command, result, as_json=args.as_json)
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
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Print machine-readable JSON instead of human-readable output.",
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

    tasks_parser = subparsers.add_parser("list-tasks", help="List project tasks.")
    tasks_parser.set_defaults(func=lambda args: list_tasks(args.db))

    read_doc_parser = subparsers.add_parser("read-doc", help="Read a visible document.")
    read_doc_parser.add_argument("doc_id")
    read_doc_parser.set_defaults(func=lambda args: read_doc(args.db, args.doc_id))

    chat_parser = subparsers.add_parser("send-chat", help="Send a chat to a coworker.")
    chat_parser.add_argument("person_id")
    chat_parser.add_argument("body")
    chat_parser.set_defaults(func=lambda args: send_chat(args.db, args.person_id, args.body))

    email_parser = subparsers.add_parser("send-email", help="Send an email to a coworker.")
    email_parser.add_argument("person_id")
    email_parser.add_argument("subject")
    email_parser.add_argument("body")
    email_parser.set_defaults(
        func=lambda args: send_email(args.db, args.person_id, args.subject, args.body)
    )

    update_task_parser = subparsers.add_parser("update-task", help="Update task status or priority.")
    update_task_parser.add_argument("task_id")
    update_task_parser.add_argument("--status")
    update_task_parser.add_argument("--priority")
    update_task_parser.set_defaults(
        func=lambda args: update_task(args.db, args.task_id, args.status, args.priority)
    )

    meeting_parser = subparsers.add_parser("schedule-meeting", help="Schedule a meeting.")
    meeting_parser.add_argument("title")
    meeting_parser.add_argument("start_at")
    meeting_parser.add_argument("end_at")
    meeting_parser.add_argument("attendees", nargs="+")
    meeting_parser.set_defaults(
        func=lambda args: schedule_meeting(
            args.db, args.title, args.start_at, args.end_at, args.attendees
        )
    )

    log_parser = subparsers.add_parser("log", help="Print recent action log.")
    log_parser.add_argument("--limit", type=int, default=20)
    log_parser.set_defaults(func=lambda args: action_log(args.db, args.limit))

    events_parser = subparsers.add_parser("events", help="Print scheduled/delivered events.")
    events_parser.add_argument("--limit", type=int, default=20)
    events_parser.set_defaults(func=lambda args: event_log(args.db, args.limit))

    timeline_parser = subparsers.add_parser("timeline", help="Print the simulation timeline.")
    timeline_parser.add_argument("--limit", type=int, default=0)
    timeline_parser.set_defaults(func=lambda args: timeline(args.db, args.limit))

    evaluate_parser = subparsers.add_parser("evaluate", help="Score the current simulation state.")
    evaluate_parser.add_argument(
        "--explain",
        action="store_true",
        help="Print a component-by-component scoring explanation.",
    )
    evaluate_parser.add_argument(
        "--scenario",
        type=Path,
        default=DEFAULT_SCENARIO_PATH,
        help=f"Scenario JSON path. Default: {DEFAULT_SCENARIO_PATH}",
    )
    evaluate_parser.set_defaults(func=_evaluate_command)

    advance_parser = subparsers.add_parser("advance-time", help="Advance simulated time.")
    advance_parser.add_argument(
        "target",
        help="Duration like 30m/2h/1d, 'until_next_event', or 'to:<iso time>'.",
    )
    advance_parser.set_defaults(func=lambda args: advance_time(args.db, args.target))

    agent_parser = subparsers.add_parser("run-agent", help="Run an agent policy through tools.")
    agent_parser.add_argument(
        "--policy",
        choices=["scripted", "llm"],
        default="scripted",
        help="Agent policy to run. Default: scripted.",
    )
    agent_parser.add_argument("--model", help="Model for --policy llm. Defaults to OPENAI_MODEL.")
    agent_parser.add_argument(
        "--max-turns",
        type=int,
        default=20,
        help="Maximum model/tool loop turns for --policy llm.",
    )
    agent_parser.add_argument(
        "--reset",
        action="store_true",
        dest="reset_first",
        help="Reset the DB from the scenario before running the agent.",
    )
    agent_parser.add_argument(
        "--scenario",
        type=Path,
        default=DEFAULT_SCENARIO_PATH,
        help=f"Scenario JSON path. Default: {DEFAULT_SCENARIO_PATH}",
    )
    agent_parser.set_defaults(func=_run_agent_command)

    return parser


def _print_result(command: str | None, value: Any, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, indent=2, sort_keys=True))
    else:
        print(format_output(command, value))


def _evaluate_command(args: argparse.Namespace) -> dict[str, Any]:
    result = evaluate(args.db, args.scenario)
    if args.explain:
        result = {**result, "explain": True}
    return result


def _run_agent_command(args: argparse.Namespace) -> dict[str, Any]:
    if args.policy == "scripted":
        return run_scripted_agent(args.db, args.scenario, reset_first=args.reset_first)
    if args.policy == "llm":
        return run_llm_agent(
            args.db,
            args.scenario,
            reset_first=args.reset_first,
            model=args.model,
            max_turns=args.max_turns,
        )
    raise ValueError(f"Unsupported policy: {args.policy}")


def sqlite_missing_reset_error() -> tuple[type[Exception], ...]:
    # Import lazily so argparse help does not need sqlite setup.
    import sqlite3

    return (sqlite3.OperationalError, RuntimeError)


if __name__ == "__main__":
    raise SystemExit(main())
