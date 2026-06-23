from __future__ import annotations

from pathlib import Path
from typing import Any

from .db import connect
from .evaluator import _load_state_milestones
from .jsonutil import loads
from .paths import DEFAULT_DB_PATH, DEFAULT_SCENARIO_PATH
from .scenario import load_scenario


TIMELINE_KINDS = {"action", "event", "event_scheduled", "event_delivered", "message", "milestone"}


def timeline(
    db_path: Path | str = DEFAULT_DB_PATH,
    limit: int = 0,
    kind: str | None = None,
) -> list[dict[str, Any]]:
    scenario = load_scenario(DEFAULT_SCENARIO_PATH)
    conn = connect(db_path)
    try:
        entries = []
        entries.extend(_action_entries(conn))
        entries.extend(_event_entries(conn))
        entries.extend(_message_entries(conn))
        entries.extend(_milestone_entries(conn, scenario))
    finally:
        conn.close()

    if kind:
        entries = _filter_entries(entries, kind)
    entries.sort(key=lambda entry: (entry["time"], _kind_rank(entry["kind"]), entry["id"]))
    if limit <= 0:
        return entries
    return entries[:limit]


def _filter_entries(entries: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    if kind == "event":
        return [entry for entry in entries if entry["kind"].startswith("event_")]
    return [entry for entry in entries if entry["kind"] == kind]


def _action_entries(conn) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, actor, action_type, created_at, payload_json, result_json
        FROM action_log
        """
    ).fetchall()
    entries = []
    for row in rows:
        entries.append(
            {
                "time": row["created_at"],
                "kind": "action",
                "id": row["id"],
                "title": f"{row['actor']} ran {row['action_type']}",
                "actor": row["actor"],
                "action_type": row["action_type"],
                "payload": loads(row["payload_json"], {}),
                "result": loads(row["result_json"], {}),
            }
        )
    return entries


def _event_entries(conn) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, event_type, scheduled_at, created_at, delivered_at,
               status, priority, payload_json, result_json
        FROM events
        """
    ).fetchall()
    entries = []
    for row in rows:
        payload = loads(row["payload_json"], {})
        result = loads(row["result_json"], {})
        entries.append(
            {
                "time": row["created_at"],
                "kind": "event_scheduled",
                "id": row["id"],
                "title": f"scheduled {row['event_type']} for {row['scheduled_at']}",
                "event_type": row["event_type"],
                "scheduled_at": row["scheduled_at"],
                "status": row["status"],
                "priority": row["priority"],
                "payload": payload,
            }
        )
        if row["delivered_at"]:
            entries.append(
                {
                    "time": row["delivered_at"],
                    "kind": "event_delivered",
                    "id": row["id"],
                    "title": f"delivered {row['event_type']}",
                    "event_type": row["event_type"],
                    "scheduled_at": row["scheduled_at"],
                    "status": row["status"],
                    "payload": payload,
                    "result": result,
                }
            )
    return entries


def _message_entries(conn) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, channel, sender_id, recipient_id, subject, body, sent_at
        FROM messages
        """
    ).fetchall()
    entries = []
    for row in rows:
        recipient = row["recipient_id"] or "all"
        subject = f" [{row['subject']}]" if row["subject"] else ""
        entries.append(
            {
                "time": row["sent_at"],
                "kind": "message",
                "id": row["id"],
                "title": f"{row['channel']} {row['sender_id']} -> {recipient}{subject}",
                "channel": row["channel"],
                "sender_id": row["sender_id"],
                "recipient_id": row["recipient_id"],
                "subject": row["subject"],
                "body": row["body"],
            }
        )
    return entries


def _milestone_entries(conn, scenario: dict[str, Any]) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, milestone_id, note, created_at, source
        FROM milestones
        """
    ).fetchall()
    entries = []
    milestone_rows = [dict(row) for row in rows]
    milestone_rows.extend(_load_state_milestones(conn, scenario))
    seen = set()
    for row in milestone_rows:
        dedupe_key = (row["milestone_id"], row["note"], row["created_at"], row["source"])
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        entries.append(
            {
                "time": row["created_at"],
                "kind": "milestone",
                "id": row["id"],
                "title": f"recorded milestone {row['milestone_id']}",
                "milestone_id": row["milestone_id"],
                "note": row["note"],
                "source": row["source"],
            }
        )
    return entries


def _kind_rank(kind: str) -> int:
    ranks = {
        "action": 0,
        "event_delivered": 1,
        "message": 2,
        "milestone": 3,
        "event_scheduled": 4,
    }
    return ranks.get(kind, 99)
