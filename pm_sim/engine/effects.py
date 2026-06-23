from __future__ import annotations

import sqlite3
from typing import Any

from ..dependencies import apply_task_dependency_updates
from ..jsonutil import dumps, loads


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
        elif effect_type == "create_doc":
            result = _apply_create_doc(conn, effect, now=now, source=source, index=index)
        elif effect_type == "update_calendar_event":
            result = _apply_update_calendar_event(conn, effect)
        elif effect_type == "discover_fact":
            result = _apply_discover_fact(conn, effect, now=now, source=source)
        elif effect_type == "reveal_doc":
            result = _apply_reveal_doc(conn, effect, now=now)
        elif effect_type == "update_blocker":
            result = _apply_update_blocker(conn, effect, now=now)
        elif effect_type == "update_task":
            result = _apply_update_task(conn, effect)
        elif effect_type == "update_project":
            result = _apply_update_project(conn, effect)
        elif effect_type == "increase_pressure":
            result = _apply_increase_pressure(conn, effect, now=now)
        elif effect_type == "lower_pressure":
            result = _apply_lower_pressure(conn, effect, now=now)
        elif effect_type == "update_coworker_state":
            result = _apply_update_coworker_state(conn, effect, now=now, source=source)
        elif effect_type == "update_actor_workload":
            result = _apply_update_actor_workload(conn, effect, now=now)
        elif effect_type == "add_actor_commitment":
            result = _apply_add_actor_commitment(conn, effect, now=now, source=source, index=index)
        elif effect_type == "update_actor_commitment":
            result = _apply_update_actor_commitment(conn, effect, now=now)
        elif effect_type == "update_actor_goal":
            result = _apply_update_actor_goal(conn, effect)
        elif effect_type == "update_metric":
            result = _apply_update_metric(conn, effect)
        elif effect_type == "record_milestone":
            result = _apply_record_milestone(
                conn, effect, now=now, source=source, index=index
            )
        elif effect_type == "record_action_evidence":
            result = _apply_record_action_evidence(
                conn, effect, now=now, source=source, index=index
            )
        elif effect_type == "mark_action_evidence_promoted":
            result = _apply_mark_action_evidence_promoted(conn, effect, now=now)
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


def _apply_create_doc(
    conn: sqlite3.Connection,
    effect: dict[str, Any],
    *,
    now: str,
    source: str,
    index: int,
) -> dict[str, Any]:
    doc_id = effect.get("id") or _generated_id(conn, "docs", f"doc_{_source_slug(source)}", index)
    conn.execute(
        """
        INSERT INTO docs
          (id, title, kind, body, visibility_scope, visible_at, updated_at, metadata_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            doc_id,
            _required(effect, "title"),
            effect.get("kind", "doc"),
            effect.get("body", ""),
            effect.get("visibility_scope", "generated"),
            effect.get("visible_at", now),
            effect.get("updated_at", now),
            dumps({"source": source, **effect.get("metadata", {})}),
        ),
    )
    return {"id": doc_id}


def _apply_update_calendar_event(conn: sqlite3.Connection, effect: dict[str, Any]) -> dict[str, Any]:
    event_id = _required(effect, "calendar_event_id")
    updates = []
    values: list[Any] = []
    for key in ("status", "transcript_doc_id"):
        if key in effect:
            updates.append(f"{key} = ?")
            values.append(effect[key])

    if not updates:
        raise ValueError("update_calendar_event effect must include a mutable field.")

    values.append(event_id)
    cursor = conn.execute(
        f"UPDATE calendar_events SET {', '.join(updates)} WHERE id = ?",
        values,
    )
    if cursor.rowcount == 0:
        raise ValueError(f"Cannot update unknown calendar event: {event_id}")
    return {"calendar_event_id": event_id}


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
        SET visible_at = COALESCE(visible_at, ?),
            source = COALESCE(source, ?)
        WHERE id = ?
        """,
        (now, fact_source, fact_id),
    )
    if cursor.rowcount == 0:
        raise ValueError(f"Cannot discover unknown fact: {fact_id}")
    return {"fact_id": fact_id}


def _apply_reveal_doc(
    conn: sqlite3.Connection,
    effect: dict[str, Any],
    *,
    now: str,
) -> dict[str, Any]:
    doc_id = _required(effect, "doc_id")
    cursor = conn.execute(
        """
        UPDATE docs
        SET visible_at = COALESCE(visible_at, ?),
            updated_at = ?
        WHERE id = ?
        """,
        (now, now, doc_id),
    )
    if cursor.rowcount == 0:
        raise ValueError(f"Cannot reveal unknown doc: {doc_id}")
    return {"doc_id": doc_id}


def _apply_update_blocker(
    conn: sqlite3.Connection,
    effect: dict[str, Any],
    *,
    now: str,
) -> dict[str, Any]:
    blocker_id = _required(effect, "blocker_id")
    status = _required(effect, "status")
    existing = conn.execute(
        "SELECT status FROM blockers WHERE id = ?",
        (blocker_id,),
    ).fetchone()
    if existing is None:
        raise ValueError(f"Cannot update unknown blocker: {blocker_id}")
    if existing["status"] == "resolved" and status != "resolved":
        return {"blocker_id": blocker_id, "status": existing["status"], "skipped": True}

    visible_at = now if status in {"surfaced", "open", "resolved"} else None
    resolved_at = now if status == "resolved" else None

    conn.execute(
        """
        UPDATE blockers
        SET status = ?,
            visible_at = COALESCE(visible_at, ?),
            resolved_at = CASE WHEN ? IS NULL THEN resolved_at ELSE ? END
        WHERE id = ?
        """,
        (status, visible_at, resolved_at, resolved_at, blocker_id),
    )
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
    dependency_updates = apply_task_dependency_updates(conn, task_id)
    return {
        "task_id": task_id,
        "updated": sorted(key for key in effect if key != "type"),
        "dependency_updates": dependency_updates,
    }


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
            if key == "launch_conflict" and isinstance(value, dict):
                existing = metadata.get(key, {})
                metadata[key] = _deep_merge(existing if isinstance(existing, dict) else {}, value)
            else:
                metadata[key] = value

    direct_updates.append("metadata_json = ?")
    values.append(dumps(metadata))
    values.append(project_id)
    conn.execute(
        f"UPDATE projects SET {', '.join(direct_updates)} WHERE id = ?",
        values,
    )
    return {"project_id": project_id}


def _apply_increase_pressure(
    conn: sqlite3.Connection,
    effect: dict[str, Any],
    *,
    now: str,
) -> dict[str, Any]:
    pressure_id = _required(effect, "pressure_id")
    by = int(_required(effect, "by"))
    if by < 0:
        raise ValueError("increase_pressure.by must be non-negative.")
    row = _pressure_row(conn, pressure_id)
    updated = min(int(row["max_intensity"]), int(row["intensity"]) + by)
    reason = effect.get("reason", row["reason"])
    conn.execute(
        """
        UPDATE pressures
        SET intensity = ?,
            reason = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (updated, reason, now, pressure_id),
    )
    return {
        "pressure_id": pressure_id,
        "previous_intensity": int(row["intensity"]),
        "intensity": updated,
    }


def _apply_lower_pressure(
    conn: sqlite3.Connection,
    effect: dict[str, Any],
    *,
    now: str,
) -> dict[str, Any]:
    pressure_id = _required(effect, "pressure_id")
    row = _pressure_row(conn, pressure_id)
    if "to" in effect:
        requested = int(effect["to"])
    elif "by" in effect:
        by = int(effect["by"])
        if by < 0:
            raise ValueError("lower_pressure.by must be non-negative.")
        requested = int(row["intensity"]) - by
    else:
        raise ValueError("lower_pressure effect must include to or by.")
    updated = max(int(row["min_intensity"]), min(int(row["max_intensity"]), requested))
    reason = effect.get("reason", row["reason"])
    conn.execute(
        """
        UPDATE pressures
        SET intensity = ?,
            reason = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (updated, reason, now, pressure_id),
    )
    return {
        "pressure_id": pressure_id,
        "previous_intensity": int(row["intensity"]),
        "intensity": updated,
    }


def _pressure_row(conn: sqlite3.Connection, pressure_id: str) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT id, intensity, min_intensity, max_intensity, reason
        FROM pressures
        WHERE id = ?
        """,
        (pressure_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Cannot update unknown pressure: {pressure_id}")
    return row


def _apply_update_coworker_state(
    conn: sqlite3.Connection,
    effect: dict[str, Any],
    *,
    now: str,
    source: str,
) -> dict[str, Any]:
    person_id = _required(effect, "person_id")
    person = conn.execute("SELECT 1 FROM people WHERE id = ?", (person_id,)).fetchone()
    if person is None:
        raise ValueError(f"Cannot update state for unknown coworker: {person_id}")

    updates = effect.get("values")
    if updates is None:
        key = _required(effect, "key")
        updates = {key: effect.get("value")}
    if not isinstance(updates, dict) or not updates:
        raise ValueError("update_coworker_state effect must include key/value or values.")

    changed = []
    for key, value in updates.items():
        if not isinstance(key, str) or not key:
            raise ValueError("update_coworker_state keys must be non-empty strings.")
        value_json = dumps(value)
        existing = conn.execute(
            """
            SELECT value_json
            FROM coworker_state
            WHERE person_id = ? AND key = ?
            """,
            (person_id, key),
        ).fetchone()
        if existing is not None and existing["value_json"] == value_json:
            changed.append(key)
            continue
        if existing is None:
            conn.execute(
                """
                INSERT INTO coworker_state (person_id, key, value_json, updated_at, source)
                VALUES (?, ?, ?, ?, ?)
                """,
                (person_id, key, value_json, now, source),
            )
        else:
            conn.execute(
                """
                UPDATE coworker_state
                SET value_json = ?, updated_at = ?, source = ?
                WHERE person_id = ? AND key = ?
                """,
                (value_json, now, source, person_id, key),
            )
        changed.append(key)

    return {"person_id": person_id, "keys": sorted(changed)}


def _apply_update_actor_workload(
    conn: sqlite3.Connection,
    effect: dict[str, Any],
    *,
    now: str,
) -> dict[str, Any]:
    person_id = _required(effect, "person_id")
    if conn.execute("SELECT 1 FROM people WHERE id = ?", (person_id,)).fetchone() is None:
        raise ValueError(f"Cannot update workload for unknown actor: {person_id}")

    row = conn.execute(
        "SELECT metadata_json FROM actor_workload WHERE person_id = ?",
        (person_id,),
    ).fetchone()
    metadata = loads(row["metadata_json"], {}) if row is not None else {}
    if isinstance(effect.get("metadata"), dict):
        metadata = _deep_merge(metadata if isinstance(metadata, dict) else {}, effect["metadata"])

    updates = {
        key: effect[key]
        for key in ("current_focus", "capacity_minutes_remaining", "load_level")
        if key in effect
    }
    current_focus = updates.get("current_focus", "")
    capacity = int(updates.get("capacity_minutes_remaining", 0))
    load_level = updates.get("load_level", "normal")
    if row is not None:
        existing = conn.execute(
            """
            SELECT current_focus, capacity_minutes_remaining, load_level
            FROM actor_workload
            WHERE person_id = ?
            """,
            (person_id,),
        ).fetchone()
        current_focus = updates.get("current_focus", existing["current_focus"])
        capacity = int(updates.get("capacity_minutes_remaining", existing["capacity_minutes_remaining"]))
        load_level = updates.get("load_level", existing["load_level"])

    conn.execute(
        """
        INSERT INTO actor_workload
          (person_id, current_focus, capacity_minutes_remaining, load_level,
           updated_at, metadata_json)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(person_id) DO UPDATE SET
          current_focus = excluded.current_focus,
          capacity_minutes_remaining = excluded.capacity_minutes_remaining,
          load_level = excluded.load_level,
          updated_at = excluded.updated_at,
          metadata_json = excluded.metadata_json
        """,
        (person_id, current_focus, capacity, load_level, now, dumps(metadata)),
    )
    return {"person_id": person_id, "updated": sorted(updates)}


def _apply_add_actor_commitment(
    conn: sqlite3.Connection,
    effect: dict[str, Any],
    *,
    now: str,
    source: str,
    index: int,
) -> dict[str, Any]:
    person_id = _required(effect, "person_id")
    if conn.execute("SELECT 1 FROM people WHERE id = ?", (person_id,)).fetchone() is None:
        raise ValueError(f"Cannot add commitment for unknown actor: {person_id}")
    commitment_id = effect.get("id") or _generated_id(
        conn, "actor_commitments", f"commitment_{_source_slug(source)}", index
    )
    conn.execute(
        """
        INSERT INTO actor_commitments
          (id, person_id, project_id, commitment_type, description, due_at,
           status, created_at, updated_at, metadata_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            commitment_id,
            person_id,
            effect.get("project_id"),
            effect.get("commitment_type", "commitment"),
            _required(effect, "description"),
            effect.get("due_at"),
            effect.get("status", "open"),
            effect.get("created_at", now),
            effect.get("updated_at", now),
            dumps({"source": source, **effect.get("metadata", {})}),
        ),
    )
    return {"id": commitment_id, "person_id": person_id}


def _apply_update_actor_commitment(
    conn: sqlite3.Connection,
    effect: dict[str, Any],
    *,
    now: str,
) -> dict[str, Any]:
    commitment_id = _required(effect, "id")
    updates = []
    values: list[Any] = []
    for key in ("status", "due_at", "description", "commitment_type"):
        if key in effect:
            updates.append(f"{key} = ?")
            values.append(effect[key])
    if not updates:
        raise ValueError("update_actor_commitment effect must include a mutable field.")
    updates.append("updated_at = ?")
    values.append(now)
    values.append(commitment_id)
    cursor = conn.execute(
        f"UPDATE actor_commitments SET {', '.join(updates)} WHERE id = ?",
        values,
    )
    if cursor.rowcount == 0:
        raise ValueError(f"Cannot update unknown actor commitment: {commitment_id}")
    return {"id": commitment_id, "updated": sorted(key for key in effect if key not in {"type", "id"})}


def _apply_update_actor_goal(conn: sqlite3.Connection, effect: dict[str, Any]) -> dict[str, Any]:
    goal_id = _required(effect, "id")
    updates = []
    values: list[Any] = []
    for key in ("status", "priority", "description"):
        if key in effect:
            updates.append(f"{key} = ?")
            values.append(effect[key])
    if not updates:
        raise ValueError("update_actor_goal effect must include a mutable field.")
    values.append(goal_id)
    cursor = conn.execute(f"UPDATE actor_goals SET {', '.join(updates)} WHERE id = ?", values)
    if cursor.rowcount == 0:
        raise ValueError(f"Cannot update unknown actor goal: {goal_id}")
    return {"id": goal_id, "updated": sorted(key for key in effect if key not in {"type", "id"})}


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in patch.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(existing, value)
        else:
            merged[key] = value
    return merged


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


def _apply_record_milestone(
    conn: sqlite3.Connection,
    effect: dict[str, Any],
    *,
    now: str,
    source: str,
    index: int,
) -> dict[str, Any]:
    milestone_id = _required(effect, "key")
    note = effect.get("note", "")
    existing = conn.execute(
        """
        SELECT id
        FROM milestones
        WHERE milestone_id = ? AND note = ?
        LIMIT 1
        """,
        (milestone_id, note),
    ).fetchone()
    if existing is not None:
        return {"id": existing["id"], "key": milestone_id, "deduped": True}

    milestone_record_id = effect.get("id") or _generated_id(
        conn, "milestones", f"milestone_{_source_slug(source)}", index
    )
    conn.execute(
        """
        INSERT INTO milestones
          (id, milestone_id, note, created_at, source, metadata_json)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            milestone_record_id,
            milestone_id,
            note,
            effect.get("created_at", now),
            source,
            dumps(effect.get("metadata", {})),
        ),
    )
    return {"id": milestone_record_id, "key": milestone_id, "deduped": False}


def _apply_record_action_evidence(
    conn: sqlite3.Connection,
    effect: dict[str, Any],
    *,
    now: str,
    source: str,
    index: int,
) -> dict[str, Any]:
    evidence_key = _required(effect, "key")
    evidence_id = effect.get("id") or _generated_id(
        conn,
        "action_evidence",
        f"evidence_{_source_slug(source)}",
        index,
    )
    status = effect.get("status", "pending")
    metadata = {
        key: value
        for key, value in effect.items()
        if key not in {"type", "id", "key", "action_type", "status"}
    }
    conn.execute(
        """
        INSERT INTO action_evidence
          (id, key, action_type, created_at, source, status, metadata_json)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            evidence_id,
            evidence_key,
            effect.get("action_type", ""),
            now,
            source,
            status,
            dumps(metadata),
        ),
    )
    return {"id": evidence_id, "key": evidence_key, "status": status}


def _apply_mark_action_evidence_promoted(
    conn: sqlite3.Connection,
    effect: dict[str, Any],
    *,
    now: str,
) -> dict[str, Any]:
    evidence_key = _required(effect, "key")
    cursor = conn.execute(
        """
        UPDATE action_evidence
        SET status = 'promoted'
        WHERE key = ?
          AND status = 'pending'
        """,
        (evidence_key,),
    )
    return {"key": evidence_key, "promoted_count": cursor.rowcount}


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
