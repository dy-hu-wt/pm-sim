from __future__ import annotations

import sqlite3
from typing import Any

from ..jsonutil import loads


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
    if "project_id" in condition:
        return project_id == condition["project_id"]
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
    if "coworker_state" in condition:
        return _coworker_state_matches(conn, condition["coworker_state"])
    if "message_exists" in condition:
        return _message_exists(conn, condition["message_exists"])
    if "first_time_at_or_after" in condition:
        spec = condition["first_time_at_or_after"]
        first_time = _first_condition_time(conn, spec)
        return first_time is not None and first_time >= spec["at"]
    if "first_time_before" in condition:
        spec = condition["first_time_before"]
        first_time = _first_condition_time(conn, spec)
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


def failed_condition_descriptions(
    conn: sqlite3.Connection,
    conditions: list[dict[str, Any]],
    *,
    prefix: str,
) -> list[str]:
    failed = []
    for condition in conditions:
        if condition_matches(conn, condition):
            continue
        failed.append(f"{prefix}: {condition_description(conn, condition)}")
    return failed


def condition_description(conn: sqlite3.Connection, condition: dict[str, Any]) -> str:
    if "all" in condition:
        failed = failed_condition_descriptions(conn, condition.get("all", []), prefix="all")
        return "all of: " + ("; ".join(failed) if failed else "unknown nested gate")
    if "any" in condition:
        failed = failed_condition_descriptions(conn, condition.get("any", []), prefix="any")
        return "any of: " + ("; ".join(failed) if failed else "unknown nested gate")
    if "not" in condition:
        return "not " + condition_description(conn, condition["not"])
    if "fact_discovered" in condition:
        fact_id = condition["fact_discovered"]
        visible_at = _single_value(
            conn,
            "SELECT visible_at FROM facts WHERE id = ?",
            (fact_id,),
        )
        return f"fact {fact_id} discovered (current visible_at={visible_at!r})"
    if "evidence_exists" in condition:
        key = condition["evidence_exists"]
        created_at = _single_value(
            conn,
            """
            SELECT created_at
            FROM evaluation_evidence
            WHERE evidence_key = ?
            ORDER BY created_at, id
            LIMIT 1
            """,
            (key,),
        )
        return f"evidence {key} exists (current created_at={created_at!r})"
    if "coworker_state" in condition:
        spec = condition["coworker_state"]
        person_id = spec["person_id"]
        key = spec["key"]
        value = _single_value(
            conn,
            """
            SELECT value_json
            FROM coworker_state
            WHERE person_id = ? AND key = ?
            """,
            (person_id, key),
        )
        expected = spec.get("equals", "truthy" if spec.get("truthy") else "configured condition")
        return f"{person_id}.{key} == {expected!r} (current={value})"
    if "project_decision" in condition:
        spec = condition["project_decision"]
        project_id = spec.get("project_id")
        metadata = loads(
            _single_value(
                conn,
                "SELECT metadata_json FROM projects WHERE id = ?",
                (project_id,),
            )
            or "{}",
            {},
        )
        return (
            f"project {project_id} decision == {spec.get('equals')!r} "
            f"(current={metadata.get('decision')!r})"
        )
    if "message_exists" in condition:
        spec = condition["message_exists"]
        count = _message_match_count(conn, spec)
        return f"message exists matching {spec} (current count={count})"
    if "blocker_status" in condition:
        spec = condition["blocker_status"]
        status = _single_value(
            conn,
            "SELECT status FROM blockers WHERE id = ?",
            (spec.get("id"),),
        )
        return f"blocker {spec.get('id')} status matches {spec} (current={status!r})"
    if "task_status" in condition:
        spec = condition["task_status"]
        status = _single_value(
            conn,
            "SELECT status FROM tasks WHERE id = ?",
            (spec.get("id"),),
        )
        return f"task {spec.get('id')} status matches {spec} (current={status!r})"
    if "current_time_at_or_after" in condition:
        return (
            f"current time >= {condition['current_time_at_or_after']} "
            f"(current={_state_value(conn, 'current_time')})"
        )
    if "current_time_before" in condition:
        return (
            f"current time < {condition['current_time_before']} "
            f"(current={_state_value(conn, 'current_time')})"
        )
    return f"unsupported diagnostic for {condition}"


def condition_time(conn: sqlite3.Connection, source: dict[str, Any]) -> str | None:
    if "fact_discovered" in source:
        return fact_discovered_at(conn, source["fact_discovered"])
    if "evidence" in source:
        return first_evidence_time(conn, source["evidence"])
    if "coworker_state" in source:
        spec = source["coworker_state"]
        return coworker_state_updated_at(conn, spec["person_id"], spec["key"])
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
        SELECT visible_at
        FROM facts
        WHERE id = ?
          AND visible_at IS NOT NULL
        """,
        (fact_id,),
    ).fetchone()
    return None if row is None else row["visible_at"]


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


def _first_condition_time(conn: sqlite3.Connection, spec: dict[str, Any]) -> str | None:
    times = [
        value
        for value in (
            fact_discovered_at(conn, spec.get("fact_id")) if spec.get("fact_id") else None,
            first_evidence_time(conn, spec.get("evidence_key"))
            if spec.get("evidence_key")
            else None,
            condition_time(conn, {"coworker_state": spec["coworker_state"]})
            if "coworker_state" in spec
            else None,
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


def task_status(conn: sqlite3.Connection, task_id: str) -> str | None:
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return None if row is None else row["status"]


def coworker_state(conn: sqlite3.Connection, person_id: str, key: str) -> Any:
    row = conn.execute(
        """
        SELECT value_json
        FROM coworker_state
        WHERE person_id = ? AND key = ?
        """,
        (person_id, key),
    ).fetchone()
    return None if row is None else loads(row["value_json"], None)


def coworker_state_updated_at(conn: sqlite3.Connection, person_id: str, key: str) -> str | None:
    row = conn.execute(
        """
        SELECT updated_at
        FROM coworker_state
        WHERE person_id = ? AND key = ?
        """,
        (person_id, key),
    ).fetchone()
    return None if row is None else row["updated_at"]


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


def _coworker_state_matches(conn: sqlite3.Connection, spec: dict[str, Any]) -> bool:
    value = coworker_state(conn, spec["person_id"], spec["key"])
    if "exists" in spec:
        return (value is not None) is bool(spec["exists"])
    if "equals" in spec:
        return value == spec["equals"]
    if "not_equals" in spec:
        return value != spec["not_equals"]
    if "in" in spec:
        return value in spec["in"]
    if "not_in" in spec:
        return value not in spec["not_in"]
    raise ValueError(f"Unsupported coworker_state condition: {spec}")


def _message_exists(conn: sqlite3.Connection, spec: dict[str, Any]) -> bool:
    clauses = []
    values = []
    for key in ("channel", "sender_id", "recipient_id"):
        if key in spec:
            clauses.append(f"{key} = ?")
            values.append(spec[key])
    if "before" in spec:
        clauses.append("sent_at < ?")
        values.append(spec["before"])
    if "at_or_after" in spec:
        clauses.append("sent_at >= ?")
        values.append(spec["at_or_after"])

    query = "SELECT subject, body FROM messages"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY sent_at, id"
    rows = conn.execute(query, values).fetchall()

    match = spec.get("match", {})
    for row in rows:
        text = _normalize(f"{row['subject'] or ''} {row['body'] or ''}")
        if not _match_text(match, text):
            continue
        return True
    return False


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


def _single_value(conn: sqlite3.Connection, query: str, params: tuple[Any, ...]) -> Any:
    row = conn.execute(query, params).fetchone()
    if row is None:
        return None
    return row[0]


def _message_match_count(conn: sqlite3.Connection, spec: dict[str, Any]) -> int:
    clauses = []
    values = []
    for key in ("channel", "sender_id", "recipient_id"):
        if key in spec:
            clauses.append(f"{key} = ?")
            values.append(spec[key])
    if "before" in spec:
        clauses.append("sent_at < ?")
        values.append(spec["before"])
    if "at_or_after" in spec:
        clauses.append("sent_at >= ?")
        values.append(spec["at_or_after"])

    query = "SELECT subject, body FROM messages"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    rows = conn.execute(query, values).fetchall()

    match = spec.get("match", {})
    count = 0
    for row in rows:
        text = _normalize(f"{row['subject'] or ''} {row['body'] or ''}")
        if not _match_text(match, text):
            continue
        count += 1
    return count


def _match_text(match: dict[str, Any], text: str) -> bool:
    if not match:
        return True
    intents = {
        intent["id"]: intent
        for intent in match.get("intents", [])
        if isinstance(intent, dict) and isinstance(intent.get("id"), str)
    }
    matched = {
        intent_id
        for intent_id, intent in intents.items()
        if _intent_matches(text, intent)
    }
    require_all = set(match.get("require_all", intents))
    if require_all and not require_all.issubset(matched):
        return False
    require_any = set(match.get("require_any", []))
    if require_any and not require_any.intersection(matched):
        return False
    forbid = set(match.get("forbid", []))
    if forbid and forbid.intersection(matched):
        return False
    return True


def _intent_matches(text: str, intent: dict[str, Any]) -> bool:
    signals = [_normalize(signal) for signal in intent.get("signals", [])]
    if signals:
        return any(signal and signal in text for signal in signals)
    description = _normalize(str(intent.get("description", "")))
    return bool(description and description in text)


def _normalize(value: str) -> str:
    return " ".join(value.lower().split())
