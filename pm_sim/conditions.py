from __future__ import annotations

import sqlite3
from typing import Any

from .jsonutil import loads


UNRESOLVED_BLOCKER_STATUSES = {"open", "surfaced", "blocked"}


def condition_matches(
    conn: sqlite3.Connection,
    condition: dict[str, Any],
    *,
    project_id: str | None = None,
) -> bool:
    if not condition:
        return True
    if "all" in condition:
        return all(
            condition_matches(conn, item, project_id=project_id)
            for item in condition.get("all", [])
        )
    if "any" in condition:
        return any(
            condition_matches(conn, item, project_id=project_id)
            for item in condition.get("any", [])
        )
    if "not" in condition:
        return not condition_matches(conn, condition["not"], project_id=project_id)
    if "fact_discovered" in condition:
        return fact_discovered_at(conn, condition["fact_discovered"]) is not None
    if "evidence_exists" in condition:
        return first_evidence_time(conn, condition["evidence_exists"]) is not None
    if "blocker_status" in condition:
        return _blocker_status_matches(conn, condition["blocker_status"])
    if "task_status" in condition:
        return _task_status_matches(conn, condition["task_status"])
    if "project_decision" in condition:
        return _project_decision_matches(conn, condition["project_decision"], project_id)
    if "first_time_at_or_after" in condition:
        spec = condition["first_time_at_or_after"]
        first_time = first_fact_or_evidence_time(
            conn,
            fact_id=spec.get("fact_id"),
            evidence_key=spec.get("evidence_key"),
        )
        return first_time is not None and first_time >= spec["at"]
    if "first_time_before" in condition:
        spec = condition["first_time_before"]
        first_time = first_fact_or_evidence_time(
            conn,
            fact_id=spec.get("fact_id"),
            evidence_key=spec.get("evidence_key"),
        )
        return first_time is not None and first_time < spec["before"]
    if "outreach_before" in condition:
        return _outreach_before(conn, condition["outreach_before"])
    if "current_time_at_or_after" in condition:
        current_time = _state_value(conn, "current_time") or ""
        return current_time >= condition["current_time_at_or_after"]
    if "current_time_before" in condition:
        current_time = _state_value(conn, "current_time") or ""
        return bool(current_time) and current_time < condition["current_time_before"]
    raise ValueError(f"Unsupported condition: {condition}")


def all_conditions_match(
    conn: sqlite3.Connection,
    conditions: list[dict[str, Any]],
    *,
    project_id: str | None = None,
) -> bool:
    return all(condition_matches(conn, condition, project_id=project_id) for condition in conditions)


def condition_time(conn: sqlite3.Connection, source: dict[str, Any]) -> str | None:
    if "fact_discovered" in source:
        return fact_discovered_at(conn, source["fact_discovered"])
    if "evidence" in source:
        return first_evidence_time(conn, source["evidence"])
    if "first_fact_or_evidence" in source:
        spec = source["first_fact_or_evidence"]
        return first_fact_or_evidence_time(
            conn,
            fact_id=spec.get("fact_id"),
            evidence_key=spec.get("evidence_key"),
        )
    raise ValueError(f"Unsupported time source: {source}")


def fact_discovered_at(conn: sqlite3.Connection, fact_id: str) -> str | None:
    row = conn.execute(
        """
        SELECT discovered_at
        FROM facts
        WHERE id = ?
          AND discovered_at IS NOT NULL
        """,
        (fact_id,),
    ).fetchone()
    return None if row is None else row["discovered_at"]


def first_evidence_time(conn: sqlite3.Connection, evidence_key: str) -> str | None:
    row = conn.execute(
        """
        SELECT created_at
        FROM evaluation_evidence
        WHERE evidence_key = ?
        ORDER BY created_at, id
        LIMIT 1
        """,
        (evidence_key,),
    ).fetchone()
    return None if row is None else row["created_at"]


def first_fact_or_evidence_time(
    conn: sqlite3.Connection,
    *,
    fact_id: str | None = None,
    evidence_key: str | None = None,
) -> str | None:
    times = [
        value
        for value in (
            fact_discovered_at(conn, fact_id) if fact_id else None,
            first_evidence_time(conn, evidence_key) if evidence_key else None,
        )
        if value is not None
    ]
    return min(times) if times else None


def project_decision(conn: sqlite3.Connection, project_id: str | None) -> str | None:
    if project_id is None:
        row = conn.execute(
            """
            SELECT metadata_json
            FROM projects
            ORDER BY deadline DESC, id
            LIMIT 1
            """
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT metadata_json
            FROM projects
            WHERE id = ?
            """,
            (project_id,),
        ).fetchone()
    if row is None:
        return None
    metadata = loads(row["metadata_json"], {}) or {}
    decision = metadata.get("decision")
    return decision if isinstance(decision, str) else None


def blocker_resolved(conn: sqlite3.Connection, blocker_id: str) -> bool:
    row = conn.execute("SELECT status FROM blockers WHERE id = ?", (blocker_id,)).fetchone()
    if row is None:
        return False
    return row["status"].lower() not in UNRESOLVED_BLOCKER_STATUSES


def task_status(conn: sqlite3.Connection, task_id: str) -> str | None:
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return None if row is None else row["status"]


def _blocker_status_matches(conn: sqlite3.Connection, spec: dict[str, Any]) -> bool:
    blocker_id = spec["id"]
    row = conn.execute("SELECT status FROM blockers WHERE id = ?", (blocker_id,)).fetchone()
    status = None if row is None else row["status"].lower()
    if spec.get("is") == "resolved":
        return status is not None and status not in UNRESOLVED_BLOCKER_STATUSES
    if spec.get("is") == "unresolved":
        return status is None or status in UNRESOLVED_BLOCKER_STATUSES
    if "in" in spec:
        return status in {value.lower() for value in spec["in"]}
    if "not_in" in spec:
        return status not in {value.lower() for value in spec["not_in"]}
    if "equals" in spec:
        return status == str(spec["equals"]).lower()
    raise ValueError(f"Unsupported blocker_status condition: {spec}")


def _task_status_matches(conn: sqlite3.Connection, spec: dict[str, Any]) -> bool:
    status = task_status(conn, spec["id"])
    normalized = None if status is None else status.lower()
    if "in" in spec:
        return normalized in {value.lower() for value in spec["in"]}
    if "not_in" in spec:
        return normalized not in {value.lower() for value in spec["not_in"]}
    if "equals" in spec:
        return normalized == str(spec["equals"]).lower()
    raise ValueError(f"Unsupported task_status condition: {spec}")


def _project_decision_matches(
    conn: sqlite3.Connection,
    spec: dict[str, Any],
    default_project_id: str | None,
) -> bool:
    decision = project_decision(conn, spec.get("project_id") or default_project_id)
    if "exists" in spec:
        exists = decision is not None and decision != "undecided"
        return exists is bool(spec["exists"])
    if "equals" in spec:
        return decision == spec["equals"]
    if "in" in spec:
        return decision in set(spec["in"])
    if "not_in" in spec:
        return decision not in set(spec["not_in"])
    raise ValueError(f"Unsupported project_decision condition: {spec}")


def _outreach_before(conn: sqlite3.Connection, spec: dict[str, Any]) -> bool:
    person_id = spec["person_id"]
    before = spec["before"]
    message = conn.execute(
        """
        SELECT 1
        FROM messages
        WHERE sender_id = 'agent'
          AND recipient_id = ?
          AND sent_at < ?
        LIMIT 1
        """,
        (person_id, before),
    ).fetchone()
    if message is not None:
        return True

    rows = conn.execute(
        """
        SELECT attendees_json
        FROM calendar_events
        WHERE start_at < ?
        """,
        (before,),
    ).fetchall()
    for row in rows:
        attendees = loads(row["attendees_json"], [])
        if person_id in attendees:
            return True
    return False


def _state_value(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM sim_state WHERE key = ?", (key,)).fetchone()
    return None if row is None else row["value"]
