from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .db import connect, reset_db_file, row_to_dict, rows_to_dicts
from .jsonutil import dumps
from .paths import DEFAULT_DB_PATH, DEFAULT_SCENARIO_PATH
from .scenario import load_scenario


AGENT_ID = "agent"


def reset(
    db_path: Path | str = DEFAULT_DB_PATH,
    scenario_path: Path | str = DEFAULT_SCENARIO_PATH,
) -> dict[str, Any]:
    scenario = load_scenario(scenario_path)
    conn = reset_db_file(db_path)
    try:
        _load_scenario(conn, scenario)
        conn.commit()
    finally:
        conn.close()

    return {
        "ok": True,
        "db_path": str(db_path),
        "scenario_id": scenario["id"],
        "current_time": scenario["start_time"],
    }


def observe(db_path: Path | str = DEFAULT_DB_PATH) -> dict[str, Any]:
    from .calendar import visible_calendar_obligations

    conn = connect(db_path)
    try:
        return {
            "current_time": get_current_time(conn),
            "scenario_id": get_state_value(conn, "scenario_id"),
            "projects": rows_to_dicts(
                conn.execute(
                    """
                    SELECT id, name, description, status, risk_level,
                           stakeholder_pressure, deadline, metadata_json
                    FROM projects
                    ORDER BY id
                    """
                ).fetchall()
            ),
            "discovered_facts": rows_to_dicts(
                conn.execute(
                    """
                    SELECT id, owner_id, summary, visible_at, source
                    FROM facts
                    WHERE visible_at IS NOT NULL
                    ORDER BY visible_at, id
                    """
                ).fetchall()
            ),
            "people": rows_to_dicts(
                conn.execute(
                    """
                    SELECT id, name, role
                    FROM people
                    ORDER BY id
                    """
                ).fetchall()
            ),
            "coworker_state": rows_to_dicts(
                conn.execute(
                    """
                    SELECT cs.person_id, p.name, cs.key, cs.value_json, cs.updated_at
                    FROM coworker_state cs
                    JOIN people p ON p.id = cs.person_id
                    ORDER BY cs.person_id, cs.key
                    """
                ).fetchall()
            ),
            "tasks": rows_to_dicts(
                conn.execute(
                    """
                    SELECT id, project_id, title, description, owner_id, status,
                           priority, due_at, blocked_by
                    FROM tasks
                    ORDER BY priority DESC, due_at, id
                    """
                ).fetchall()
            ),
            "known_blockers": rows_to_dicts(
                conn.execute(
                    """
                    SELECT id, project_id, title, description, severity, status,
                           owner_id, visible_at, resolved_at
                    FROM blockers
                    WHERE visible_at IS NOT NULL
                    ORDER BY severity DESC, id
                    """
                ).fetchall()
            ),
            "recent_messages": rows_to_dicts(
                conn.execute(
                    """
                    SELECT id, channel, sender_id, recipient_id, subject, body, sent_at
                    FROM messages
                    ORDER BY sent_at DESC, id DESC
                    LIMIT 10
                    """
                ).fetchall()
            ),
            "upcoming_calendar": rows_to_dicts(
                conn.execute(
                    """
                    SELECT id, title, start_at, end_at, status
                    FROM calendar_events
                    WHERE status = 'scheduled'
                    ORDER BY start_at, id
                    LIMIT 10
                    """
                ).fetchall()
            ),
            "calendar_obligations": visible_calendar_obligations(db_path),
            "visible_docs": rows_to_dicts(
                conn.execute(
                    """
                    SELECT id, title, kind, updated_at
                    FROM docs
                    WHERE visible_at IS NOT NULL
                    ORDER BY updated_at DESC, id
                    """
                ).fetchall()
            ),
            "pending_events": rows_to_dicts(
                conn.execute(
                    """
                    SELECT id, event_type, scheduled_at, status, priority
                    FROM events
                    WHERE status = 'pending'
                    ORDER BY scheduled_at, priority, id
                    LIMIT 10
                    """
                ).fetchall()
            ),
        }
    finally:
        conn.close()


def action_log(db_path: Path | str = DEFAULT_DB_PATH, limit: int = 20) -> list[dict[str, Any]]:
    conn = connect(db_path)
    try:
        return rows_to_dicts(
            conn.execute(
                """
                SELECT id, actor, action_type, created_at, payload_json, result_json
                FROM action_log
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        )
    finally:
        conn.close()


def event_log(db_path: Path | str = DEFAULT_DB_PATH, limit: int = 20) -> list[dict[str, Any]]:
    conn = connect(db_path)
    try:
        return rows_to_dicts(
            conn.execute(
                """
                SELECT id, event_type, scheduled_at, delivered_at, status,
                       priority, payload_json, result_json
                FROM events
                ORDER BY scheduled_at DESC, priority, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        )
    finally:
        conn.close()


def get_state_value(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM sim_state WHERE key = ?", (key,)).fetchone()
    return None if row is None else row["value"]


def set_state_value(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO sim_state (key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )


def get_current_time(conn: sqlite3.Connection) -> str:
    current_time = get_state_value(conn, "current_time")
    if current_time is None:
        raise RuntimeError("Simulation state is missing current_time. Run reset first.")
    return current_time


def log_action(
    conn: sqlite3.Connection,
    *,
    action_id: str,
    actor: str,
    action_type: str,
    created_at: str,
    payload: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO action_log
          (id, actor, action_type, created_at, payload_json, result_json)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            action_id,
            actor,
            action_type,
            created_at,
            dumps(payload or {}),
            dumps(result or {}),
        ),
    )


def _load_scenario(conn: sqlite3.Connection, scenario: dict[str, Any]) -> None:
    set_state_value(conn, "scenario_id", scenario["id"])
    set_state_value(conn, "current_time", scenario["start_time"])
    set_state_value(conn, "coworker_rules_json", dumps(scenario.get("coworker_rules", [])))
    set_state_value(conn, "event_rules_json", dumps(scenario.get("event_rules", [])))
    set_state_value(conn, "meeting_rules_json", dumps(scenario.get("meeting_rules", [])))
    set_state_value(conn, "action_rules_json", dumps(scenario.get("action_rules", [])))
    set_state_value(conn, "response_delays_json", dumps(_response_delays(scenario)))
    set_state_value(conn, "state_evidence_rules_json", dumps(scenario.get("state_evidence_rules", [])))
    set_state_value(conn, "task_gate_rules_json", dumps(scenario.get("task_gate_rules", [])))
    set_state_value(conn, "outcome_rules_json", dumps(scenario.get("outcome_rules", [])))

    _insert_people(conn, scenario.get("people", []))
    _insert_coworker_state(conn, scenario.get("coworker_state", []), scenario["start_time"])
    _insert_facts(conn, scenario.get("facts", []))
    _insert_projects(conn, scenario.get("projects", []))
    _insert_tasks(conn, scenario.get("tasks", []))
    _insert_dependencies(conn, scenario.get("dependencies", []))
    _insert_blockers(conn, scenario.get("blockers", []))
    _insert_docs(conn, scenario.get("docs", []), scenario["start_time"])
    _insert_messages(conn, scenario.get("messages", []), scenario["start_time"])
    _insert_calendar_events(conn, scenario.get("calendar_events", []))
    _insert_events(conn, scenario.get("events", []), scenario["start_time"])

    log_action(
        conn,
        action_id="action_reset",
        actor="operator",
        action_type="reset",
        created_at=scenario["start_time"],
        payload={"scenario_id": scenario["id"]},
        result={"ok": True},
    )


def _insert_people(conn: sqlite3.Connection, people: list[dict[str, Any]]) -> None:
    for person in people:
        conn.execute(
            """
            INSERT INTO people
              (id, name, role, goals, constraints_json, availability_json,
               private_knowledge_json, behavior_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                person["id"],
                person["name"],
                person["role"],
                dumps(person.get("goals", [])),
                dumps(person.get("constraints", {})),
                dumps(person.get("availability", {})),
                dumps(person.get("private_knowledge", {})),
                dumps(person.get("behavior", {})),
            ),
        )


def _insert_coworker_state(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
    start_time: str,
) -> None:
    for row in rows:
        conn.execute(
            """
            INSERT INTO coworker_state (person_id, key, value_json, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                row["person_id"],
                row["key"],
                dumps(row.get("value")),
                row.get("updated_at", start_time),
            ),
        )


def _response_delays(scenario: dict[str, Any]) -> dict[str, int]:
    return {
        person["id"]: person["response_delay_minutes"]
        for person in scenario.get("people", [])
        if isinstance(person.get("response_delay_minutes"), int)
    }


def _insert_projects(conn: sqlite3.Connection, projects: list[dict[str, Any]]) -> None:
    for project in projects:
        conn.execute(
            """
            INSERT INTO projects
              (id, name, description, status, risk_level, stakeholder_pressure,
               deadline, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project["id"],
                project["name"],
                project.get("description", ""),
                project.get("status", "active"),
                project.get("risk_level", "unknown"),
                project.get("stakeholder_pressure", ""),
                project.get("deadline"),
                dumps(
                    {
                        key: value
                        for key, value in project.items()
                        if key
                        not in {
                            "id",
                            "name",
                            "description",
                            "status",
                            "risk_level",
                            "stakeholder_pressure",
                            "deadline",
                        }
                    }
                ),
            ),
        )


def _insert_facts(conn: sqlite3.Connection, facts: list[dict[str, Any]]) -> None:
    for fact in facts:
        visibility_scope = fact.get("visibility_scope", "hidden")
        visible_at = (
            fact.get("visible_at")
            or (get_current_time(conn) if visibility_scope == "public" else None)
        )
        conn.execute(
            """
            INSERT INTO facts
              (id, visibility_scope, owner_id, summary, visible_at, source, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fact["id"],
                visibility_scope,
                fact.get("owner_id"),
                fact["summary"],
                visible_at,
                fact.get("source"),
                dumps(
                    {
                        key: value
                        for key, value in fact.items()
                        if key
                        not in {
                            "id",
                            "visibility_scope",
                            "owner_id",
                            "summary",
                            "visible_at",
                            "source",
                        }
                    }
                ),
            ),
        )


def _insert_tasks(conn: sqlite3.Connection, tasks: list[dict[str, Any]]) -> None:
    for task in tasks:
        conn.execute(
            """
            INSERT INTO tasks
              (id, project_id, title, description, owner_id, status, priority,
               due_at, blocked_by, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task["id"],
                task["project_id"],
                task["title"],
                task.get("description", ""),
                task.get("owner_id"),
                task.get("status", "todo"),
                task.get("priority", "medium"),
                task.get("due_at"),
                task.get("blocked_by"),
                dumps(task.get("metadata", {})),
            ),
        )


def _insert_dependencies(conn: sqlite3.Connection, dependencies: list[dict[str, Any]]) -> None:
    for dependency in dependencies:
        conn.execute(
            """
            INSERT INTO dependencies
              (id, project_id, upstream_task_id, downstream_task_id, description)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                dependency["id"],
                dependency["project_id"],
                dependency["upstream_task_id"],
                dependency["downstream_task_id"],
                dependency.get("description", ""),
            ),
        )


def _insert_blockers(conn: sqlite3.Connection, blockers: list[dict[str, Any]]) -> None:
    for blocker in blockers:
        visibility_scope = blocker.get("visibility_scope", "hidden")
        visible_at = blocker.get("visible_at")
        conn.execute(
            """
            INSERT INTO blockers
              (id, project_id, title, description, severity, status, owner_id,
               visibility_scope, visible_at, resolved_at, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                blocker["id"],
                blocker["project_id"],
                blocker["title"],
                blocker.get("description", ""),
                blocker.get("severity", "medium"),
                blocker.get("status", "open"),
                blocker.get("owner_id"),
                visibility_scope,
                visible_at,
                blocker.get("resolved_at"),
                dumps(
                    {
                        key: value
                        for key, value in blocker.items()
                        if key
                        not in {
                            "id",
                            "project_id",
                            "title",
                            "description",
                            "severity",
                            "status",
                            "owner_id",
                            "visibility_scope",
                            "visible_at",
                            "resolved_at",
                        }
                    }
                ),
            ),
        )


def _insert_docs(
    conn: sqlite3.Connection,
    docs: list[dict[str, Any]],
    default_time: str,
) -> None:
    for doc in docs:
        visibility_scope = doc.get("visibility_scope", "hidden")
        visible_at = doc.get("visible_at")
        conn.execute(
            """
            INSERT INTO docs
              (id, title, kind, body, visibility_scope, visible_at, updated_at, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                doc["id"],
                doc["title"],
                doc.get("kind", "doc"),
                doc.get("body", ""),
                visibility_scope,
                visible_at,
                doc.get("updated_at", default_time),
                dumps(
                    {
                        key: value
                        for key, value in doc.items()
                        if key
                        not in {
                            "id",
                            "title",
                            "kind",
                            "body",
                            "visibility_scope",
                            "visible_at",
                            "updated_at",
                        }
                    }
                ),
            ),
        )


def _insert_messages(
    conn: sqlite3.Connection,
    messages: list[dict[str, Any]],
    default_time: str,
) -> None:
    for message in messages:
        conn.execute(
            """
            INSERT INTO messages
              (id, channel, sender_id, recipient_id, subject, body, sent_at, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message["id"],
                message.get("channel", "chat"),
                message["sender_id"],
                message.get("recipient_id"),
                message.get("subject"),
                message.get("body", ""),
                message.get("sent_at", default_time),
                dumps(message.get("metadata", {})),
            ),
        )


def _insert_calendar_events(
    conn: sqlite3.Connection,
    calendar_events: list[dict[str, Any]],
) -> None:
    for event in calendar_events:
        conn.execute(
            """
            INSERT INTO calendar_events
              (id, title, start_at, end_at, attendees_json, status,
               transcript_doc_id, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event["id"],
                event["title"],
                event["start_at"],
                event["end_at"],
                dumps(event.get("attendees", [])),
                event.get("status", "scheduled"),
                event.get("transcript_doc_id"),
                dumps(event.get("metadata", {})),
            ),
        )


def _insert_events(
    conn: sqlite3.Connection,
    events: list[dict[str, Any]],
    default_time: str,
) -> None:
    for event in events:
        conn.execute(
            """
            INSERT INTO events
              (id, event_type, scheduled_at, created_at, delivered_at,
               status, priority, payload_json, result_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event["id"],
                event["event_type"],
                event["scheduled_at"],
                event.get("created_at", default_time),
                event.get("delivered_at"),
                event.get("status", "pending"),
                event.get("priority", 100),
                dumps(event.get("payload", {})),
                dumps(event.get("result", {})),
            ),
        )
