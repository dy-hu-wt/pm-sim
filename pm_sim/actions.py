from __future__ import annotations

import sqlite3
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any

from .coworkers import CoworkerReply, replies_for_chat
from .db import connect, row_to_dict, rows_to_dicts
from .effects import apply_effects
from .conditions import all_conditions_match
from .jsonutil import dumps, loads
from .paths import DEFAULT_DB_PATH
from .state import AGENT_ID, get_current_time, get_state_value, log_action
from .time import consume_action_time

COMPLETED_STATUSES = {"complete", "completed", "done", "resolved"}
ACTION_TIME_COST_MINUTES = {
    "read_doc": 15,
    "update_doc": 20,
    "send_chat": 5,
    "send_email": 10,
    "schedule_meeting": 5,
    "update_task": 1,
}


# Read-only tool: returns visible task state without mutating time, logs, or events. Cost: 0m.
def list_tasks(db_path: Path | str = DEFAULT_DB_PATH) -> list[dict[str, Any]]:
    conn = connect(db_path)
    try:
        return rows_to_dicts(
            conn.execute(
                """
                SELECT id, project_id, title, description, owner_id, status,
                       priority, due_at, blocked_by
                FROM tasks
                ORDER BY due_at, priority DESC, id
                """
            ).fetchall()
        )
    finally:
        conn.close()


# Doc tool: returns a visible doc body. Cost: 15m reading time.
def read_doc(db_path: Path | str, doc_id: str) -> dict[str, Any]:
    conn = connect(db_path)
    try:
        current_time = get_current_time(conn)
        doc = row_to_dict(
            conn.execute(
                """
                SELECT id, title, kind, body, visible_at, updated_at
                FROM docs
                WHERE id = ?
                """,
                (doc_id,),
            ).fetchone()
        )
        if doc is None:
            return {"ok": False, "error": f"Doc not found: {doc_id}"}
        if doc["visible_at"] is None:
            return {"ok": False, "error": f"Doc is not visible: {doc_id}"}
        time_cost = consume_action_time(
            conn,
            current_time=current_time,
            minutes=ACTION_TIME_COST_MINUTES["read_doc"],
        )
        log_action(
            conn,
            action_id=_next_id(conn, "action_log", "action_read_doc"),
            actor=AGENT_ID,
            action_type="read_doc",
            created_at=current_time,
            payload={"doc_id": doc_id},
            result={"doc_id": doc_id, "time_cost": time_cost},
        )
        conn.commit()
        return {"ok": True, "doc": doc, "time_cost": time_cost}
    finally:
        conn.close()


# Doc tool: updates a visible existing doc and records a revision. Cost: 20m writing time.
def update_doc(db_path: Path | str, doc_id: str, body: str) -> dict[str, Any]:
    body = body.strip()
    if not body:
        return {"ok": False, "error": "Doc body is required."}

    conn = connect(db_path)
    try:
        current_time = get_current_time(conn)
        doc = row_to_dict(
            conn.execute(
                """
                SELECT id, title, kind, body, visible_at
                FROM docs
                WHERE id = ?
                """,
                (doc_id,),
            ).fetchone()
        )
        if doc is None:
            return {"ok": False, "error": f"Doc not found: {doc_id}"}
        if doc["visible_at"] is None:
            return {"ok": False, "error": f"Doc is not visible: {doc_id}"}

        revision_id = _next_id(conn, "doc_revisions", "doc_revision")
        conn.execute(
            """
            INSERT INTO doc_revisions
              (id, doc_id, actor, previous_body, new_body, created_at, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, '{}')
            """,
            (revision_id, doc_id, AGENT_ID, doc["body"], body, current_time),
        )
        conn.execute(
            """
            UPDATE docs
            SET body = ?, updated_at = ?
            WHERE id = ?
            """,
            (body, current_time, doc_id),
        )
        doc_effects = _effects_for_action(
            conn,
            "update_doc",
            {
                "doc_id": doc_id,
                "body": body,
                "text": body,
            },
        )
        applied_effects = apply_effects(
            conn,
            doc_effects,
            now=current_time,
            source=f"action:{revision_id}",
        )
        time_cost = consume_action_time(
            conn,
            current_time=current_time,
            minutes=ACTION_TIME_COST_MINUTES["update_doc"],
        )
        log_action(
            conn,
            action_id=_next_id(conn, "action_log", "action_update_doc"),
            actor=AGENT_ID,
            action_type="update_doc",
            created_at=current_time,
            payload={"doc_id": doc_id, "body": body},
            result={
                "doc_id": doc_id,
                "revision_id": revision_id,
                "applied_effects": applied_effects,
                "time_cost": time_cost,
            },
        )
        conn.commit()
        return {
            "ok": True,
            "doc_id": doc_id,
            "revision_id": revision_id,
            "applied_effects": applied_effects,
            "time_cost": time_cost,
        }
    finally:
        conn.close()


# Chat tool: records an agent message and schedules deterministic coworker replies. Cost: 5m.
# Reply delays consume only the recipient coworker's authored working hours.
def send_chat(db_path: Path | str, person_id: str, body: str) -> dict[str, Any]:
    body = body.strip()
    if not body:
        return {"ok": False, "error": "Chat body is required."}

    conn = connect(db_path)
    try:
        current_time = get_current_time(conn)
        person = _get_person(conn, person_id)
        if person is None:
            return {"ok": False, "error": f"Person not found: {person_id}"}

        message_id = _next_id(conn, "messages", "msg_agent_chat")
        conn.execute(
            """
            INSERT INTO messages
              (id, channel, sender_id, recipient_id, subject, body, sent_at, metadata_json)
            VALUES (?, 'chat', ?, ?, NULL, ?, ?, '{}')
            """,
            (message_id, AGENT_ID, person_id, body, current_time),
        )

        replies = replies_for_chat(person_id, body, _behavior_state(conn))
        scheduled_reply_ids = [
            _schedule_coworker_reply(conn, reply, current_time) for reply in replies
        ]
        chat_effects = _effects_for_action(
            conn,
            "send_chat",
            {
                "recipient_id": person_id,
                "person_id": person_id,
                "body": body,
                "text": body,
            },
        )
        applied_effects = apply_effects(
            conn,
            chat_effects,
            now=current_time,
            source=f"action:{message_id}",
        )
        time_cost = consume_action_time(
            conn,
            current_time=current_time,
            minutes=ACTION_TIME_COST_MINUTES["send_chat"],
        )

        log_action(
            conn,
            action_id=_next_id(conn, "action_log", "action_send_chat"),
            actor=AGENT_ID,
            action_type="send_chat",
            created_at=current_time,
            payload={"person_id": person_id, "body": body},
            result={
                "message_id": message_id,
                "scheduled_reply_ids": scheduled_reply_ids,
                "applied_effects": applied_effects,
                "time_cost": time_cost,
            },
        )
        conn.commit()

        return {
            "ok": True,
            "message_id": message_id,
            "scheduled_reply_ids": scheduled_reply_ids,
            "applied_effects": applied_effects,
            "time_cost": time_cost,
        }
    finally:
        conn.close()


# Email tool: records outreach and applies deterministic communication evidence when matched. Cost: 10m.
def send_email(
    db_path: Path | str,
    person_id: str,
    subject: str,
    body: str,
) -> dict[str, Any]:
    subject = subject.strip()
    body = body.strip()
    if not subject:
        return {"ok": False, "error": "Email subject is required."}
    if not body:
        return {"ok": False, "error": "Email body is required."}

    conn = connect(db_path)
    try:
        current_time = get_current_time(conn)
        person = _get_person(conn, person_id)
        if person is None:
            return {"ok": False, "error": f"Person not found: {person_id}"}

        message_id = _next_id(conn, "messages", "msg_agent_email")
        conn.execute(
            """
            INSERT INTO messages
              (id, channel, sender_id, recipient_id, subject, body, sent_at, metadata_json)
            VALUES (?, 'email', ?, ?, ?, ?, ?, '{}')
            """,
            (message_id, AGENT_ID, person_id, subject, body, current_time),
        )
        email_effects = _effects_for_action(
            conn,
            "send_email",
            {
                "recipient_id": person_id,
                "person_id": person_id,
                "subject": subject,
                "body": body,
                "text": f"{subject} {body}",
            },
        )
        applied_effects = apply_effects(
            conn,
            email_effects,
            now=current_time,
            source=f"action:{message_id}",
        )
        time_cost = consume_action_time(
            conn,
            current_time=current_time,
            minutes=ACTION_TIME_COST_MINUTES["send_email"],
        )
        log_action(
            conn,
            action_id=_next_id(conn, "action_log", "action_send_email"),
            actor=AGENT_ID,
            action_type="send_email",
            created_at=current_time,
            payload={"person_id": person_id, "subject": subject, "body": body},
            result={
                "message_id": message_id,
                "applied_effects": applied_effects,
                "time_cost": time_cost,
            },
        )
        conn.commit()

        return {
            "ok": True,
            "message_id": message_id,
            "applied_effects": applied_effects,
            "time_cost": time_cost,
        }
    finally:
        conn.close()


# Task tool: updates explicit task fields when world state supports completion. Cost: 1m.
def update_task(
    db_path: Path | str,
    task_id: str,
    status: str | None = None,
    priority: str | None = None,
) -> dict[str, Any]:
    if status is None and priority is None:
        return {"ok": False, "error": "At least one of status or priority is required."}

    conn = connect(db_path)
    try:
        current_time = get_current_time(conn)
        task = row_to_dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())
        if task is None:
            return {"ok": False, "error": f"Task not found: {task_id}"}

        new_status = status if status is not None else task["status"]
        new_priority = priority if priority is not None else task["priority"]
        validation_error = _validate_task_update(conn, task_id, new_status)
        if validation_error:
            return {"ok": False, "error": validation_error}

        conn.execute(
            """
            UPDATE tasks
            SET status = ?, priority = ?
            WHERE id = ?
            """,
            (new_status, new_priority, task_id),
        )
        time_cost = consume_action_time(
            conn,
            current_time=current_time,
            minutes=ACTION_TIME_COST_MINUTES["update_task"],
        )
        log_action(
            conn,
            action_id=_next_id(conn, "action_log", "action_update_task"),
            actor=AGENT_ID,
            action_type="update_task",
            created_at=current_time,
            payload={"task_id": task_id, "status": status, "priority": priority},
            result={
                "previous": {"status": task["status"], "priority": task["priority"]},
                "current": {"status": new_status, "priority": new_priority},
                "time_cost": time_cost,
            },
        )
        conn.commit()

        return {
            "ok": True,
            "task_id": task_id,
            "status": new_status,
            "priority": new_priority,
            "time_cost": time_cost,
        }
    finally:
        conn.close()


def _validate_task_update(conn: sqlite3.Connection, task_id: str, new_status: str) -> str | None:
    if not _is_completion_status(new_status):
        return None

    for rule in _task_gate_rules(conn):
        if rule.get("task_id") != task_id:
            continue
        statuses = {status.lower() for status in rule.get("statuses", list(COMPLETED_STATUSES))}
        if new_status.lower() not in statuses:
            continue
        if not all_conditions_match(conn, rule.get("requires", [])):
            return rule.get("error") or f"{task_id} cannot be marked {new_status} yet."

    return None


def _task_gate_rules(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rules = loads(get_state_value(conn, "task_gate_rules_json") or "[]", [])
    return rules if isinstance(rules, list) else []


def _is_completion_status(status: str | None) -> bool:
    return (status or "").lower() in COMPLETED_STATUSES


# Calendar tool: records a meeting and schedules meeting_occurs at its end time. Cost: 5m to schedule.
def schedule_meeting(
    db_path: Path | str,
    title: str,
    start_at: str,
    end_at: str,
    attendees: list[str],
) -> dict[str, Any]:
    title = title.strip()
    if not title:
        return {"ok": False, "error": "Meeting title is required."}
    if not attendees:
        return {"ok": False, "error": "At least one attendee is required."}

    conn = connect(db_path)
    try:
        current_time = get_current_time(conn)
        missing = [person_id for person_id in attendees if _get_person(conn, person_id) is None]
        if missing:
            return {"ok": False, "error": f"Unknown attendees: {', '.join(missing)}"}

        meeting_id = _next_id(conn, "calendar_events", "cal")
        transcript_doc_id = f"doc_transcript_{meeting_id}"
        conn.execute(
            """
            INSERT INTO calendar_events
              (id, title, start_at, end_at, attendees_json, status,
               transcript_doc_id, metadata_json)
            VALUES (?, ?, ?, ?, ?, 'scheduled', NULL, '{}')
            """,
            (meeting_id, title, start_at, end_at, dumps(attendees)),
        )
        meeting_event_id = _schedule_meeting_occurs(
            conn,
            meeting_id=meeting_id,
            transcript_doc_id=transcript_doc_id,
            title=title,
            start_at=start_at,
            end_at=end_at,
            attendees=attendees,
            current_time=current_time,
        )
        time_cost = consume_action_time(
            conn,
            current_time=current_time,
            minutes=ACTION_TIME_COST_MINUTES["schedule_meeting"],
        )
        log_action(
            conn,
            action_id=_next_id(conn, "action_log", "action_schedule_meeting"),
            actor=AGENT_ID,
            action_type="schedule_meeting",
            created_at=current_time,
            payload={
                "title": title,
                "start_at": start_at,
                "end_at": end_at,
                "attendees": attendees,
            },
            result={
                "meeting_id": meeting_id,
                "event_id": meeting_event_id,
                "time_cost": time_cost,
            },
        )
        conn.commit()

        return {
            "ok": True,
            "meeting_id": meeting_id,
            "event_id": meeting_event_id,
            "time_cost": time_cost,
        }
    finally:
        conn.close()


def _schedule_coworker_reply(
    conn: sqlite3.Connection,
    reply: CoworkerReply,
    current_time: str,
) -> str:
    event_id = _next_id(conn, "events", "event_coworker_reply")
    scheduled_at = _format_time(
        _add_available_minutes(
            _parse_time(current_time),
            reply.delay_minutes,
            _person_availability(conn, reply.person_id),
        )
    )
    conn.execute(
        """
        INSERT INTO events
          (id, event_type, scheduled_at, created_at, delivered_at,
           status, priority, payload_json, result_json)
        VALUES (?, 'coworker_reply', ?, ?, NULL, 'pending', 50, ?, '{}')
        """,
        (
            event_id,
            scheduled_at,
            current_time,
            dumps(
                {
                    "person_id": reply.person_id,
                    "body": reply.body,
                    "effects": list(reply.effects),
                }
            ),
        ),
    )
    return event_id


def _effects_for_action(
    conn: sqlite3.Connection,
    action_type: str,
    context: dict[str, Any],
) -> list[dict[str, Any]]:
    normalized = _normalize(str(context.get("text", "")))
    effects: list[dict[str, Any]] = []
    for rule in _action_rules(conn):
        if rule.get("action_type") != action_type:
            continue
        if not _action_rule_matches(rule, context, normalized, conn):
            continue
        effects.extend(dict(effect) for effect in rule.get("effects", []))
    return effects


def _action_rule_matches(
    rule: dict[str, Any],
    context: dict[str, Any],
    normalized: str,
    conn: sqlite3.Connection,
) -> bool:
    for key in ("person_id", "recipient_id", "doc_id"):
        expected = rule.get(key)
        if expected is not None and str(context.get(key, "")).lower() != str(expected).lower():
            return False

    terms_any = {_normalize(term) for term in rule.get("terms_any", [])}
    if terms_any and not _mentions_any(normalized, terms_any):
        return False

    terms_all = {_normalize(term) for term in rule.get("terms_all", [])}
    if terms_all and not all(term in normalized for term in terms_all):
        return False

    for group in rule.get("term_groups_all", []):
        terms = {_normalize(term) for term in group}
        if not terms or not _mentions_any(normalized, terms):
            return False

    return all_conditions_match(conn, rule.get("when", []))


def _action_rules(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rules = loads(get_state_value(conn, "action_rules_json") or "[]", [])
    return sorted(rules, key=lambda rule: int(rule.get("priority", 0)), reverse=True)


def _schedule_meeting_occurs(
    conn: sqlite3.Connection,
    *,
    meeting_id: str,
    transcript_doc_id: str,
    title: str,
    start_at: str,
    end_at: str,
    attendees: list[str],
    current_time: str,
) -> str:
    event_id = _next_id(conn, "events", "event_meeting_occurs")
    conn.execute(
        """
        INSERT INTO events
          (id, event_type, scheduled_at, created_at, delivered_at,
           status, priority, payload_json, result_json)
        VALUES (?, 'meeting_occurs', ?, ?, NULL, 'pending', 75, ?, '{}')
        """,
        (
            event_id,
            end_at,
            current_time,
            dumps(
                {
                    "calendar_event_id": meeting_id,
                    "transcript_doc_id": transcript_doc_id,
                    "title": title,
                    "start_at": start_at,
                    "end_at": end_at,
                    "attendees": attendees,
                }
            ),
        ),
    )
    return event_id


def _behavior_state(conn: sqlite3.Connection) -> dict[str, Any]:
    facts = conn.execute(
        """
        SELECT id
        FROM facts
        WHERE visible_at IS NOT NULL
        """
    ).fetchall()
    rules = loads(get_state_value(conn, "coworker_rules_json") or "[]", [])
    response_delays = loads(get_state_value(conn, "response_delays_json") or "{}", {})
    return {
        "discovered_facts": [row["id"] for row in facts],
        "coworker_rules": rules,
        "response_delays": response_delays,
    }


def _get_person(conn: sqlite3.Connection, person_id: str) -> dict[str, Any] | None:
    return row_to_dict(conn.execute("SELECT * FROM people WHERE id = ?", (person_id,)).fetchone())


def _person_availability(conn: sqlite3.Connection, person_id: str) -> list[dict[str, Any]]:
    row = conn.execute(
        "SELECT availability_json FROM people WHERE id = ?",
        (person_id,),
    ).fetchone()
    if row is None:
        return []
    availability = loads(row["availability_json"], [])
    return availability if isinstance(availability, list) else []


def _add_available_minutes(
    start: datetime,
    minutes: int,
    availability: list[dict[str, Any]],
) -> datetime:
    if not availability:
        return start + timedelta(minutes=minutes)

    remaining = minutes
    current = start
    for _ in range(21):
        windows = _availability_windows_for_day(current, availability)
        for window_start, window_end in windows:
            if current < window_start:
                current = window_start
            if current >= window_end:
                continue

            available_minutes = int((window_end - current).total_seconds() // 60)
            if remaining <= available_minutes:
                return current + timedelta(minutes=remaining)
            remaining -= available_minutes
            current = window_end

        current = datetime.combine((current + timedelta(days=1)).date(), time.min)

    raise ValueError("Could not schedule coworker reply within configured availability.")


def _availability_windows_for_day(
    current: datetime,
    availability: list[dict[str, Any]],
) -> list[tuple[datetime, datetime]]:
    day_name = current.strftime("%A").lower()
    windows = []
    for window in availability:
        if str(window.get("day", "")).lower() != day_name:
            continue
        start = _parse_clock_time(window.get("start"))
        end = _parse_clock_time(window.get("end"))
        if start is None or end is None or end <= start:
            continue
        windows.append(
            (
                datetime.combine(current.date(), start),
                datetime.combine(current.date(), end),
            )
        )
    return sorted(windows, key=lambda item: item[0])


def _parse_clock_time(value: Any) -> time | None:
    if not isinstance(value, str):
        return None
    try:
        return time.fromisoformat(value)
    except ValueError:
        return None


def _next_id(conn: sqlite3.Connection, table: str, prefix: str) -> str:
    row = conn.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
    return f"{prefix}_{int(row['count']) + 1}"


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _format_time(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _normalize(value: str) -> str:
    return " ".join(value.lower().split())


def _mentions_any(value: str, terms: frozenset[str]) -> bool:
    return any(term in value for term in terms)
