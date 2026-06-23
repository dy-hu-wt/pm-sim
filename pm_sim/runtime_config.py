from __future__ import annotations

import sqlite3
from typing import Any

from .jsonutil import loads
from .state import get_state_value


def state_json(conn: sqlite3.Connection, key: str, default: Any) -> Any:
    return loads(get_state_value(conn, key) or "", default)


def actor_behaviors(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = state_json(conn, "actor_behaviors_json", [])
    return rows if isinstance(rows, list) else []


def reply_behaviors(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        behavior
        for behavior in actor_behaviors(conn)
        if isinstance(behavior, dict) and behavior.get("kind") == "reply"
    ]


def policy_behaviors(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        behavior
        for behavior in actor_behaviors(conn)
        if isinstance(behavior, dict) and behavior.get("kind") == "policy"
    ]


def action_rules(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rules = state_json(conn, "action_rules_json", [])
    if not isinstance(rules, list):
        return []
    return sorted(rules, key=lambda rule: int(rule.get("priority", 0)), reverse=True)


def event_rules(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rules = state_json(conn, "event_rules_json", [])
    return rules if isinstance(rules, list) else []


def meeting_rules(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rules = state_json(conn, "meeting_rules_json", [])
    return rules if isinstance(rules, list) else []


def outcome_rules(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rules = state_json(conn, "outcome_rules_json", [])
    return rules if isinstance(rules, list) else []


def task_gate_rules(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rules = state_json(conn, "task_gate_rules_json", [])
    return rules if isinstance(rules, list) else []


def response_delays(conn: sqlite3.Connection) -> dict[str, Any]:
    delays = state_json(conn, "response_delays_json", {})
    return delays if isinstance(delays, dict) else {}
