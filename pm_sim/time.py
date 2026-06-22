from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .conditions import all_conditions_match
from .coworkers import effects_for_event, effects_for_meeting
from .db import connect, rows_to_dicts
from .effects import apply_effects
from .jsonutil import dumps, loads
from .paths import DEFAULT_DB_PATH
from .state import get_current_time, get_state_value, log_action, set_state_value


_DURATION_RE = re.compile(r"^(?P<amount>\d+)(?P<unit>m|h|d)$")


def advance_time(
    db_path: Path | str = DEFAULT_DB_PATH,
    target: str = "until_next_event",
    *,
    actor: str = "agent",
    action_type: str = "advance_time",
) -> dict[str, Any]:
    conn = connect(db_path)
    try:
        current_time = get_current_time(conn)
        new_time = _resolve_target_time(conn, current_time, target)

        if new_time < current_time:
            raise ValueError("Cannot move simulated time backwards.")

        due_events = _due_events(conn, new_time)
        delivered = [_deliver_event(conn, event, new_time) for event in due_events]

        set_state_value(conn, "current_time", new_time)
        log_action(
            conn,
            action_id=f"action_{_next_action_number(conn)}_{action_type}",
            actor=actor,
            action_type=action_type,
            created_at=new_time,
            payload={"target": target, "from": current_time, "to": new_time},
            result={"delivered_event_ids": [event["id"] for event in delivered]},
        )
        conn.commit()

        return {
            "ok": True,
            "from": current_time,
            "to": new_time,
            "delivered_events": delivered,
        }
    finally:
        conn.close()


def consume_action_time(
    conn: sqlite3.Connection,
    *,
    current_time: str,
    minutes: int,
) -> dict[str, Any]:
    new_time = _format_time(_parse_time(current_time) + timedelta(minutes=minutes))
    due_events = _due_events(conn, new_time)
    delivered = [_deliver_event(conn, event, new_time) for event in due_events]
    set_state_value(conn, "current_time", new_time)
    return {
        "minutes": minutes,
        "from": current_time,
        "to": new_time,
        "delivered_events": delivered,
        "delivered_event_ids": [event["id"] for event in delivered],
    }


def _resolve_target_time(conn: sqlite3.Connection, current_time: str, target: str) -> str:
    if target == "until_next_event":
        row = conn.execute(
            """
            SELECT scheduled_at
            FROM events
            WHERE status = 'pending' AND scheduled_at >= ?
            ORDER BY scheduled_at, priority, id
            LIMIT 1
            """,
            (current_time,),
        ).fetchone()
        if row is None:
            return current_time
        return row["scheduled_at"]

    if target.startswith("to:"):
        return target.removeprefix("to:").strip()

    match = _DURATION_RE.match(target)
    if match is None:
        raise ValueError("Time target must be a duration like 30m/2h/1d, 'until_next_event', or 'to:<iso time>'.")

    amount = int(match.group("amount"))
    unit = match.group("unit")
    delta = {
        "m": timedelta(minutes=amount),
        "h": timedelta(hours=amount),
        "d": timedelta(days=amount),
    }[unit]
    return _format_time(_parse_time(current_time) + delta)


def _due_events(conn: sqlite3.Connection, new_time: str) -> list[dict[str, Any]]:
    return rows_to_dicts(
        conn.execute(
            """
            SELECT id, event_type, scheduled_at, created_at, status,
                   priority, payload_json, result_json
            FROM events
            WHERE status = 'pending' AND scheduled_at <= ?
            ORDER BY scheduled_at, priority, id
            """,
            (new_time,),
        ).fetchall()
    )


def _deliver_event(
    conn: sqlite3.Connection,
    event: dict[str, Any],
    delivered_at: str,
) -> dict[str, Any]:
    payload = loads(event["payload_json"], {})
    source = f"event:{event['id']}"
    effects = _effects_for_delivery(conn, event["event_type"], payload)
    event_time = event["scheduled_at"]
    applied_effects = apply_effects(conn, effects, now=event_time, source=source)
    result = {
        "handled": True,
        "payload": payload,
        "applied_effects": applied_effects,
    }
    conn.execute(
        """
        UPDATE events
        SET status = 'delivered', delivered_at = ?, result_json = ?
        WHERE id = ?
        """,
        (event_time, dumps(result), event["id"]),
    )
    return {
        "id": event["id"],
        "event_type": event["event_type"],
        "scheduled_at": event["scheduled_at"],
        "delivered_at": event_time,
        "result": result,
    }


def _effects_for_delivery(
    conn: sqlite3.Connection,
    event_type: str,
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    if event_type == "coworker_reply":
        return [
            {
                "type": "create_message",
                "channel": payload.get("channel", "chat"),
                "sender_id": payload["person_id"],
                "recipient_id": payload.get("recipient_id", "agent"),
                "subject": payload.get("subject"),
                "body": payload.get("body", ""),
            },
            *payload.get("effects", []),
        ]

    if event_type == "meeting_occurs":
        return effects_for_meeting(payload, _meeting_state(conn))

    if event_type == "project_deadline":
        return _effects_for_project_deadline(conn, payload)

    return effects_for_event(conn, event_type, payload)


def _effects_for_project_deadline(
    conn: sqlite3.Connection,
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    project_id = payload.get("project_id")
    if not isinstance(project_id, str) or not project_id:
        raise RuntimeError("Project deadline event requires payload.project_id.")
    deadline_id = payload.get("deadline_id")
    if not isinstance(deadline_id, str) or not deadline_id:
        raise RuntimeError("Project deadline event requires payload.deadline_id.")
    outcome_doc_id = payload.get("outcome_doc_id")
    if not isinstance(outcome_doc_id, str) or not outcome_doc_id:
        outcome_doc_id = f"doc_{deadline_id}_outcome"
    outcome = _classify_project_outcome(conn, project_id)

    return [
        {
            "type": "update_project",
            "project_id": project_id,
            "status": outcome["status"],
            "risk_level": outcome["risk_level"],
            "deadline_reached": True,
            "deadline_id": deadline_id,
            "final_outcome": outcome["final_outcome"],
            "final_outcome_summary": outcome["summary"],
        },
        {
            "type": "create_doc",
            "id": outcome_doc_id,
            "title": payload.get("outcome_doc_title", "Project Outcome"),
            "kind": "outcome_report",
            "visible": True,
            "body": outcome["summary"],
            "metadata": {"final_outcome": outcome["final_outcome"]},
        },
    ]


def _classify_project_outcome(conn: sqlite3.Connection, project_id: str) -> dict[str, str]:
    rules = loads(get_state_value(conn, "outcome_rules_json") or "[]", [])
    for rule in rules:
        if not all_conditions_match(conn, rule.get("when", []), project_id=project_id):
            continue
        result = rule.get("result", {})
        return {
            "status": result["status"],
            "risk_level": result["risk_level"],
            "final_outcome": result.get("final_outcome", rule["id"]),
            "summary": result["summary"],
        }
    raise RuntimeError("No Friday outcome rule matched current scenario state.")


def _discovered_fact_ids(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        """
        SELECT id
        FROM facts
        WHERE discovered_at IS NOT NULL
        """
    ).fetchall()
    return {row["id"] for row in rows}


def _meeting_state(conn: sqlite3.Connection) -> dict[str, Any]:
    return {
        "discovered_facts": sorted(_discovered_fact_ids(conn)),
        "evidence_keys": sorted(_evidence_keys(conn)),
        "meeting_rules": loads(get_state_value(conn, "meeting_rules_json") or "[]", []),
    }


def _evidence_keys(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT evidence_key
        FROM evaluation_evidence
        """
    ).fetchall()
    return {row["evidence_key"] for row in rows}


def _next_action_number(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS count FROM action_log").fetchone()
    return int(row["count"]) + 1


def _parse_time(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"Invalid ISO simulated time: {value}") from error


def _format_time(value: datetime) -> str:
    return value.isoformat(timespec="seconds")
