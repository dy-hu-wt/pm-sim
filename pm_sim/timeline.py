from __future__ import annotations

from pathlib import Path
from typing import Any

from .db import connect
from .jsonutil import loads
from .paths import DEFAULT_DB_PATH


TIMELINE_KINDS = {"action", "event", "event_scheduled", "event_delivered", "message", "evidence"}


def timeline(
    db_path: Path | str = DEFAULT_DB_PATH,
    limit: int = 0,
    kind: str | None = None,
) -> list[dict[str, Any]]:
    conn = connect(db_path)
    try:
        entries = []
        entries.extend(_action_entries(conn))
        entries.extend(_event_entries(conn))
        entries.extend(_message_entries(conn))
        entries.extend(_evidence_entries(conn))
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


def _evidence_entries(conn) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, evidence_key, note, created_at, source
        FROM evaluation_evidence
        """
    ).fetchall()
    entries = []
    for row in rows:
        entries.append(
            {
                "time": row["created_at"],
                "kind": "evidence",
                "id": row["id"],
                "title": f"recorded evidence {row['evidence_key']}",
                "evidence_key": row["evidence_key"],
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
        "evidence": 3,
        "event_scheduled": 4,
    }
    return ranks.get(kind, 99)
