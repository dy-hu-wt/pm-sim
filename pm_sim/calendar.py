from __future__ import annotations

from pathlib import Path
from typing import Any

from .db import connect, rows_to_dicts
from .jsonutil import loads
from .paths import DEFAULT_DB_PATH
from .state import get_current_time


def visible_calendar_obligations(db_path: Path | str = DEFAULT_DB_PATH) -> list[dict[str, Any]]:
    conn = connect(db_path)
    try:
        current_time = get_current_time(conn)
        obligations = rows_to_dicts(
            conn.execute(
                """
                SELECT id, title, start_at, end_at, status, metadata_json
                FROM calendar_events
                WHERE status = 'scheduled'
                  AND end_at > ?
                ORDER BY start_at, id
                """,
                (current_time,),
            ).fetchall()
        )
        for obligation in obligations:
            obligation["kind"] = "calendar_event"
            obligation["metadata"] = loads(obligation.pop("metadata_json"), {})

        event_rows = rows_to_dicts(
            conn.execute(
                """
                SELECT id, event_type, scheduled_at, payload_json
                FROM events
                WHERE status = 'pending'
                  AND scheduled_at > ?
                ORDER BY scheduled_at, priority, id
                """,
                (current_time,),
            ).fetchall()
        )
        for row in event_rows:
            payload = loads(row["payload_json"], {})
            obligation = payload.get("calendar_obligation")
            if not isinstance(obligation, dict):
                continue
            obligations.append(
                {
                    "id": row["id"],
                    "kind": "scheduled_event",
                    "title": obligation.get("title") or row["event_type"],
                    "start_at": row["scheduled_at"],
                    "end_at": obligation.get("end_at") or row["scheduled_at"],
                    "status": "scheduled",
                    "event_type": row["event_type"],
                    "metadata": {
                        "attendees": obligation.get("attendees", []),
                        "project_id": payload.get("project_id"),
                    },
                }
            )

        deadline_rows = rows_to_dicts(
            conn.execute(
                """
                SELECT id, name, deadline, metadata_json
                FROM projects
                WHERE deadline IS NOT NULL
                  AND deadline > ?
                ORDER BY deadline, id
                """,
                (current_time,),
            ).fetchall()
        )
        for row in deadline_rows:
            metadata = loads(row["metadata_json"], {})
            if metadata.get("deadline_reached"):
                continue
            obligations.append(
                {
                    "id": f"deadline_{row['id']}",
                    "kind": "project_deadline",
                    "title": f"{row['name']} deadline",
                    "start_at": row["deadline"],
                    "end_at": row["deadline"],
                    "status": "scheduled",
                    "project_id": row["id"],
                    "metadata": metadata,
                }
            )

        return sorted(obligations, key=lambda item: (item["start_at"], item["id"]))
    finally:
        conn.close()


def validate_finish(db_path: Path | str = DEFAULT_DB_PATH) -> dict[str, Any]:
    obligations = visible_calendar_obligations(db_path)
    if obligations:
        return {
            "ok": False,
            "error": "Cannot finish yet. Visible calendar obligations remain.",
            "remaining_obligations": obligations,
        }
    return {"ok": True, "remaining_obligations": []}
