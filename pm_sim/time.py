from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .db import connect, rows_to_dicts
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
    # Specific event handlers will live next to coworker rules. Until then,
    # delivery is still explicit and inspectable.
    result = {
        "handled": False,
        "note": "No handler registered yet.",
        "payload": loads(event["payload_json"], {}),
    }
    conn.execute(
        """
        UPDATE events
        SET status = 'delivered', delivered_at = ?, result_json = ?
        WHERE id = ?
        """,
        (delivered_at, dumps(result), event["id"]),
    )
    return {
        "id": event["id"],
        "event_type": event["event_type"],
        "scheduled_at": event["scheduled_at"],
        "delivered_at": delivered_at,
        "result": result,
    }


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
