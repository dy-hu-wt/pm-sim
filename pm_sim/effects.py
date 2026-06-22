from __future__ import annotations

import sqlite3
from typing import Any

from .jsonutil import dumps, loads


def apply_effects(
    conn: sqlite3.Connection,
    effects: list[dict[str, Any]],
    *,
    now: str,
    source: str,
) -> list[dict[str, Any]]:
    applied = []
    for index, effect in enumerate(effects, start=1):
        effect_type = effect.get("type")
        if effect_type == "create_message":
            result = _apply_create_message(conn, effect, now=now, source=source, index=index)
        elif effect_type == "discover_fact":
            result = _apply_discover_fact(conn, effect, now=now, source=source)
        elif effect_type == "update_blocker":
            result = _apply_update_blocker(conn, effect, now=now)
        elif effect_type == "update_task":
            result = _apply_update_task(conn, effect)
        elif effect_type == "update_project":
            result = _apply_update_project(conn, effect)
        elif effect_type == "update_metric":
            result = _apply_update_metric(conn, effect)
        elif effect_type == "add_evaluation_evidence":
            result = _apply_add_evaluation_evidence(
                conn, effect, now=now, source=source, index=index
            )
        else:
            raise ValueError(f"Unknown effect type: {effect_type!r}")

        applied.append({"type": effect_type, **result})
    return applied


def _apply_create_message(
    conn: sqlite3.Connection,
    effect: dict[str, Any],
    *,
    now: str,
    source: str,
    index: int,
) -> dict[str, Any]:
    message_id = effect.get("id") or _generated_id(
        conn, "messages", f"msg_{_source_slug(source)}", index
    )
    conn.execute(
        """
        INSERT INTO messages
          (id, channel, sender_id, recipient_id, subject, body, sent_at, metadata_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            message_id,
            effect.get("channel", "chat"),
            _required(effect, "sender_id"),
            effect.get("recipient_id", "agent"),
            effect.get("subject"),
            effect.get("body", ""),
            effect.get("sent_at", now),
            dumps({"source": source, **effect.get("metadata", {})}),
        ),
    )
    return {"id": message_id}


def _apply_discover_fact(
    conn: sqlite3.Connection,
    effect: dict[str, Any],
    *,
    now: str,
    source: str,
) -> dict[str, Any]:
    fact_id = _required(effect, "fact_id")
    fact_source = effect.get("source", source)
    cursor = conn.execute(
        """
        UPDATE facts
        SET discovered_at = COALESCE(discovered_at, ?),
            source = COALESCE(source, ?)
        WHERE id = ?
        """,
        (now, fact_source, fact_id),
    )
    if cursor.rowcount == 0:
        raise ValueError(f"Cannot discover unknown fact: {fact_id}")
    return {"fact_id": fact_id}


def _apply_update_blocker(
    conn: sqlite3.Connection,
    effect: dict[str, Any],
    *,
    now: str,
) -> dict[str, Any]:
    blocker_id = _required(effect, "blocker_id")
    status = _required(effect, "status")
    discovered_at = now if status in {"surfaced", "open", "resolved"} else None
    resolved_at = now if status == "resolved" else None

    cursor = conn.execute(
        """
        UPDATE blockers
        SET status = ?,
            discovered_at = COALESCE(discovered_at, ?),
            resolved_at = CASE WHEN ? IS NULL THEN resolved_at ELSE ? END
        WHERE id = ?
        """,
        (status, discovered_at, resolved_at, resolved_at, blocker_id),
    )
    if cursor.rowcount == 0:
        raise ValueError(f"Cannot update unknown blocker: {blocker_id}")
    return {"blocker_id": blocker_id, "status": status}


def _apply_update_task(conn: sqlite3.Connection, effect: dict[str, Any]) -> dict[str, Any]:
    task_id = _required(effect, "task_id")
    updates = []
    values: list[Any] = []
    for key in ("status", "priority", "owner_id", "blocked_by"):
        if key in effect:
            updates.append(f"{key} = ?")
            values.append(effect[key])

    if not updates:
        raise ValueError("update_task effect must include at least one mutable field.")

    values.append(task_id)
    cursor = conn.execute(
        f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?",
        values,
    )
    if cursor.rowcount == 0:
        raise ValueError(f"Cannot update unknown task: {task_id}")
    return {"task_id": task_id, "updated": sorted(key for key in effect if key != "type")}


def _apply_update_project(conn: sqlite3.Connection, effect: dict[str, Any]) -> dict[str, Any]:
    project_id = _required(effect, "project_id")
    row = conn.execute(
        "SELECT metadata_json FROM projects WHERE id = ?",
        (project_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Cannot update unknown project: {project_id}")

    direct_updates = []
    values: list[Any] = []
    for key in ("status", "risk_level", "stakeholder_pressure", "deadline"):
        if key in effect:
            direct_updates.append(f"{key} = ?")
            values.append(effect[key])

    metadata = loads(row["metadata_json"], {}) or {}
    for key, value in effect.items():
        if key not in {"type", "project_id", "status", "risk_level", "stakeholder_pressure", "deadline"}:
            metadata[key] = value

    direct_updates.append("metadata_json = ?")
    values.append(dumps(metadata))
    values.append(project_id)
    conn.execute(
        f"UPDATE projects SET {', '.join(direct_updates)} WHERE id = ?",
        values,
    )
    return {"project_id": project_id}


def _apply_update_metric(conn: sqlite3.Connection, effect: dict[str, Any]) -> dict[str, Any]:
    metric = _required(effect, "metric")
    delta = int(effect.get("delta", 0))
    key = f"metric:{metric}"
    row = conn.execute("SELECT value FROM sim_state WHERE key = ?", (key,)).fetchone()
    current = 0 if row is None else int(row["value"])
    updated = current + delta
    conn.execute(
        """
        INSERT INTO sim_state (key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, str(updated)),
    )
    return {"metric": metric, "value": updated}


def _apply_add_evaluation_evidence(
    conn: sqlite3.Connection,
    effect: dict[str, Any],
    *,
    now: str,
    source: str,
    index: int,
) -> dict[str, Any]:
    evidence_id = effect.get("id") or _generated_id(
        conn, "evaluation_evidence", f"evidence_{_source_slug(source)}", index
    )
    conn.execute(
        """
        INSERT INTO evaluation_evidence
          (id, evidence_key, note, created_at, source, metadata_json)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            evidence_id,
            _required(effect, "key"),
            effect.get("note", ""),
            effect.get("created_at", now),
            source,
            dumps(effect.get("metadata", {})),
        ),
    )
    return {"id": evidence_id, "key": effect["key"]}


def _required(effect: dict[str, Any], key: str) -> Any:
    value = effect.get(key)
    if value is None:
        raise ValueError(f"Effect {effect.get('type')!r} is missing required key {key!r}.")
    return value


def _generated_id(
    conn: sqlite3.Connection,
    table: str,
    prefix: str,
    index: int,
) -> str:
    row = conn.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
    return f"{prefix}_{int(row['count']) + index}"


def _source_slug(source: str) -> str:
    return "".join(char if char.isalnum() else "_" for char in source.lower()).strip("_")
