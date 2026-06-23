from __future__ import annotations

import sqlite3
from typing import Any


COMPLETED_STATUSES = {"complete", "completed", "done", "resolved"}


def apply_task_dependency_updates(
    conn: sqlite3.Connection,
    upstream_task_id: str,
) -> list[dict[str, Any]]:
    upstream = conn.execute(
        "SELECT status FROM tasks WHERE id = ?",
        (upstream_task_id,),
    ).fetchone()
    if upstream is None or str(upstream["status"]).lower() not in COMPLETED_STATUSES:
        return []

    rows = conn.execute(
        """
        SELECT DISTINCT downstream_task_id
        FROM dependencies
        WHERE upstream_task_id = ?
        ORDER BY downstream_task_id
        """,
        (upstream_task_id,),
    ).fetchall()
    updates = []
    for row in rows:
        downstream_task_id = row["downstream_task_id"]
        downstream = conn.execute(
            """
            SELECT id, status, blocked_by
            FROM tasks
            WHERE id = ?
            """,
            (downstream_task_id,),
        ).fetchone()
        if downstream is None or downstream["status"] != "blocked":
            continue
        if not _all_upstreams_completed(conn, downstream_task_id):
            continue
        if not _blocking_reason_resolved(conn, downstream["blocked_by"]):
            continue

        conn.execute(
            """
            UPDATE tasks
            SET status = 'in_progress',
                blocked_by = ''
            WHERE id = ?
            """,
            (downstream_task_id,),
        )
        updates.append(
            {
                "task_id": downstream_task_id,
                "from": downstream["status"],
                "to": "in_progress",
                "reason": "upstream_dependencies_complete",
            }
        )
    return updates


def _all_upstreams_completed(conn: sqlite3.Connection, downstream_task_id: str) -> bool:
    rows = conn.execute(
        """
        SELECT t.status
        FROM dependencies d
        JOIN tasks t ON t.id = d.upstream_task_id
        WHERE d.downstream_task_id = ?
        """,
        (downstream_task_id,),
    ).fetchall()
    if not rows:
        return False
    return all(str(row["status"]).lower() in COMPLETED_STATUSES for row in rows)


def _blocking_reason_resolved(conn: sqlite3.Connection, blocker_id: str | None) -> bool:
    if not blocker_id:
        return True
    row = conn.execute(
        "SELECT status FROM blockers WHERE id = ?",
        (blocker_id,),
    ).fetchone()
    if row is None:
        return False
    return row["status"] == "resolved"
