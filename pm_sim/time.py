from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .coworkers import effects_for_event, effects_for_meeting
from .db import connect, rows_to_dicts
from .effects import apply_effects
from .jsonutil import dumps, loads
from .paths import DEFAULT_DB_PATH
from .state import get_current_time, log_action, set_state_value


_DURATION_RE = re.compile(r"^(?P<amount>\d+)(?P<unit>m|h|d)$")


def advance_time(db_path: Path | str = DEFAULT_DB_PATH, target: str = "until_next_event") -> dict[str, Any]:
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
            action_id=f"action_{_next_action_number(conn)}_advance_time",
            actor="agent",
            action_type="advance_time",
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
        return effects_for_meeting(payload)

    if event_type == "friday_nimbus_deadline":
        return _effects_for_friday_deadline(conn)

    return effects_for_event(event_type, payload)


def _effects_for_friday_deadline(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    facts = _discovered_fact_ids(conn)
    draft_approved = "fact_draft_mode_approved" in facts
    scope_confirmed = "fact_draft_mode_scope_confirmed" in facts
    daisy_aligned = "fact_nimbus_values_reliability" in facts or _has_evidence(
        conn, "stakeholder_alignment"
    )

    if draft_approved and scope_confirmed and daisy_aligned:
        outcome = {
            "status": "shipped",
            "risk_level": "low",
            "final_outcome": "draft_mode_beta_shipped",
            "summary": (
                "The team shipped the reliable draft-mode beta for Nimbus Labs. "
                "Auto-commenting remains follow-up work."
            ),
        }
    elif draft_approved:
        outcome = {
            "status": "partial",
            "risk_level": "medium",
            "final_outcome": "draft_mode_approved_with_execution_gaps",
            "summary": (
                "Draft mode was approved, but scope or stakeholder alignment was "
                "not fully closed before the Friday deadline."
            ),
        }
    else:
        outcome = {
            "status": "missed",
            "risk_level": "high",
            "final_outcome": "no_approved_friday_plan",
            "summary": (
                "Friday arrived without an approved reliable launch plan. "
                "Auto-commenting remains risky because repo sync can review stale code."
            ),
        }

    return [
        {
            "type": "update_project",
            "project_id": "project_pr_review_agent",
            "status": outcome["status"],
            "risk_level": outcome["risk_level"],
            "deadline_reached": True,
            "deadline_id": "deadline_nimbus_beta",
            "final_outcome": outcome["final_outcome"],
            "final_outcome_summary": outcome["summary"],
        },
        {
            "type": "create_doc",
            "id": "doc_friday_outcome",
            "title": "Friday Outcome",
            "kind": "outcome_report",
            "visible": True,
            "body": outcome["summary"],
            "metadata": {"final_outcome": outcome["final_outcome"]},
        },
    ]


def _discovered_fact_ids(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        """
        SELECT id
        FROM facts
        WHERE discovered_at IS NOT NULL
        """
    ).fetchall()
    return {row["id"] for row in rows}


def _has_evidence(conn: sqlite3.Connection, evidence_key: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM evaluation_evidence
        WHERE evidence_key = ?
        LIMIT 1
        """,
        (evidence_key,),
    ).fetchone()
    return row is not None


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
