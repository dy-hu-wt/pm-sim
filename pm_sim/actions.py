from __future__ import annotations

import sqlite3
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any

from .coworkers import CoworkerReply, replies_for_chat, replies_for_email
from .db import connect, row_to_dict, rows_to_dicts
from .dependencies import apply_task_dependency_updates
from .engine.effects import apply_effects
from .engine.conditions import all_conditions_match
from .engine.runtime_config import (
    action_rules,
    actor_behaviors,
    evidence_promotion_rules,
    response_delays,
    task_gate_rules,
)
from .engine.rules import match_rule, normalize_text
from .engine.time import consume_action_time
from .jsonutil import dumps, loads
from .paths import DEFAULT_DB_PATH
from .concept_match import concept_match
from .state import AGENT_ID, get_current_time, log_action

COMPLETED_STATUSES = {"complete", "completed", "done", "resolved"}
WEEKDAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
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
        action_id = _next_id(conn, "action_log", "action_update_doc")
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
        action_context = {"doc_id": doc_id, "body": body, "text": body}
        doc_effects = _effects_for_action(conn, "update_doc", action_context)
        applied_effects = apply_effects(
            conn,
            doc_effects,
            now=current_time,
            source=f"action:{action_id}",
        )
        applied_effects.extend(_apply_evidence_promotions(conn, current_time, action_id))
        time_cost = consume_action_time(
            conn,
            current_time=current_time,
            minutes=ACTION_TIME_COST_MINUTES["update_doc"],
        )
        log_action(
            conn,
            action_id=action_id,
            actor=AGENT_ID,
            action_type="update_doc",
            created_at=current_time,
            payload={
                "doc_id": doc_id,
                "body": body,
                "concept_matches": action_context.get("concept_matches", []),
            },
            result={
                "doc_id": doc_id,
                "revision_id": revision_id,
                "applied_effects": applied_effects,
                "concept_matches": action_context.get("concept_matches", []),
                "time_cost": time_cost,
            },
        )
        conn.commit()
        return {
            "ok": True,
            "doc_id": doc_id,
            "revision_id": revision_id,
            "applied_effects": applied_effects,
            "concept_matches": action_context.get("concept_matches", []),
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
        action_id = _next_id(conn, "action_log", "action_send_chat")
        conn.execute(
            """
            INSERT INTO messages
              (id, channel, sender_id, recipient_id, subject, body, sent_at, metadata_json)
            VALUES (?, 'chat', ?, ?, NULL, ?, ?, '{}')
            """,
            (message_id, AGENT_ID, person_id, body, current_time),
        )

        replies = replies_for_chat(person_id, body, _behavior_state(conn), conn=conn)
        scheduled_reply_ids = [
            _schedule_coworker_reply(conn, reply, current_time) for reply in replies
        ]
        action_context = {
            "recipient_id": person_id,
            "person_id": person_id,
            "body": body,
            "text": body,
        }
        chat_effects = _effects_for_action(conn, "send_chat", action_context)
        applied_effects = apply_effects(
            conn,
            chat_effects,
            now=current_time,
            source=f"action:{action_id}",
        )
        applied_effects.extend(_apply_evidence_promotions(conn, current_time, action_id))
        time_cost = consume_action_time(
            conn,
            current_time=current_time,
            minutes=ACTION_TIME_COST_MINUTES["send_chat"],
        )

        log_action(
            conn,
            action_id=action_id,
            actor=AGENT_ID,
            action_type="send_chat",
            created_at=current_time,
            payload={
                "person_id": person_id,
                "body": body,
                "concept_matches": action_context.get("concept_matches", []),
            },
            result={
                "message_id": message_id,
                "scheduled_reply_ids": scheduled_reply_ids,
                "applied_effects": applied_effects,
                "concept_matches": action_context.get("concept_matches", []),
                "time_cost": time_cost,
            },
        )
        conn.commit()

        return {
            "ok": True,
            "message_id": message_id,
            "scheduled_reply_ids": scheduled_reply_ids,
            "applied_effects": applied_effects,
            "concept_matches": action_context.get("concept_matches", []),
            "time_cost": time_cost,
        }
    finally:
        conn.close()


# Email tool: records outreach and applies deterministic communication milestones when matched. Cost: 10m.
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
        action_id = _next_id(conn, "action_log", "action_send_email")
        conn.execute(
            """
            INSERT INTO messages
              (id, channel, sender_id, recipient_id, subject, body, sent_at, metadata_json)
            VALUES (?, 'email', ?, ?, ?, ?, ?, '{}')
            """,
            (message_id, AGENT_ID, person_id, subject, body, current_time),
        )
        replies = replies_for_email(person_id, subject, body, _behavior_state(conn), conn=conn)
        scheduled_reply_ids = [
            _schedule_coworker_reply(conn, reply, current_time) for reply in replies
        ]
        action_context = {
            "recipient_id": person_id,
            "person_id": person_id,
            "subject": subject,
            "body": body,
            "text": f"{subject} {body}",
        }
        email_effects = _effects_for_action(conn, "send_email", action_context)
        applied_effects = apply_effects(
            conn,
            email_effects,
            now=current_time,
            source=f"action:{action_id}",
        )
        applied_effects.extend(_apply_evidence_promotions(conn, current_time, action_id))
        time_cost = consume_action_time(
            conn,
            current_time=current_time,
            minutes=ACTION_TIME_COST_MINUTES["send_email"],
        )
        log_action(
            conn,
            action_id=action_id,
            actor=AGENT_ID,
            action_type="send_email",
            created_at=current_time,
            payload={
                "person_id": person_id,
                "subject": subject,
                "body": body,
                "concept_matches": action_context.get("concept_matches", []),
            },
            result={
                "message_id": message_id,
                "scheduled_reply_ids": scheduled_reply_ids,
                "applied_effects": applied_effects,
                "concept_matches": action_context.get("concept_matches", []),
                "time_cost": time_cost,
            },
        )
        conn.commit()

        return {
            "ok": True,
            "message_id": message_id,
            "scheduled_reply_ids": scheduled_reply_ids,
            "applied_effects": applied_effects,
            "concept_matches": action_context.get("concept_matches", []),
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
        dependency_updates = apply_task_dependency_updates(conn, task_id)
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
                "dependency_updates": dependency_updates,
                "time_cost": time_cost,
            },
        )
        conn.commit()

        return {
            "ok": True,
            "task_id": task_id,
            "status": new_status,
            "priority": new_priority,
            "dependency_updates": dependency_updates,
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
    return task_gate_rules(conn)


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
    try:
        start_time = _parse_time(start_at)
        end_time = _parse_time(end_at)
    except ValueError:
        return {"ok": False, "error": "Meeting start_at and end_at must be ISO timestamps."}
    if end_time - start_time < timedelta(minutes=10):
        return {"ok": False, "error": "Meetings must be at least 10 minutes long."}

    conn = connect(db_path)
    try:
        current_time = get_current_time(conn)
        missing = [person_id for person_id in attendees if _get_person(conn, person_id) is None]
        if missing:
            return {"ok": False, "error": f"Unknown attendees: {', '.join(missing)}"}
        availability_error = _validate_meeting_availability(
            conn,
            attendees,
            start_time=start_time,
            end_time=end_time,
        )
        if availability_error:
            return {"ok": False, "error": availability_error}

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
                    "channel": reply.channel,
                    "subject": reply.subject,
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
    normalized = normalize_text(str(context.get("text", "")))
    effects: list[dict[str, Any]] = []
    for rule in _action_rules(conn):
        if rule.get("action_type") != action_type:
            continue
        match_result = match_rule(
            rule,
            normalized_text=normalized,
            context=context,
            context_keys=("person_id", "recipient_id", "doc_id"),
            conn=conn,
            concept_matcher=lambda criteria, matched_rule: concept_match(
                conn,
                text=str(context.get("text", "")),
                criteria=criteria,
                rule_id=str(matched_rule.get("id", "")),
            ),
        )
        if not match_result.matches:
            continue
        concept_result = match_result.concept
        if concept_result is not None:
            context.setdefault("concept_matches", []).append(
                {
                    "rule_id": rule.get("id"),
                    "mode": concept_result.get("mode"),
                    "matcher": concept_result.get("matcher"),
                    "model": concept_result.get("model"),
                    "matches": concept_result.get("matches"),
                    "required": concept_result.get("required", []),
                    "forbidden": concept_result.get("forbidden", []),
                    "error": concept_result.get("error"),
                    "cache_key": concept_result.get("cache_key"),
                }
            )
        effects.extend(dict(effect) for effect in rule.get("effects", []))
    return effects


def _apply_evidence_promotions(
    conn: sqlite3.Connection,
    current_time: str,
    action_id: str,
) -> list[dict[str, Any]]:
    applied_effects: list[dict[str, Any]] = []
    for rule in _evidence_promotion_rules(conn):
        if not all_conditions_match(conn, rule.get("when", [])):
            continue
        effects = [dict(effect) for effect in rule.get("effects", [])]
        applied_effects.extend(
            apply_effects(
                conn,
                effects,
                now=current_time,
                source=f"evidence_promotion:{action_id}:{rule.get('id')}",
            )
        )
    return applied_effects


def _action_rules(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return action_rules(conn)


def _evidence_promotion_rules(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return evidence_promotion_rules(conn)


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
    return {
        "discovered_facts": [row["id"] for row in facts],
        "actor_behaviors": actor_behaviors(conn),
        "response_delays": response_delays(conn),
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
    return _normalize_availability(availability)


def _normalize_availability(availability: Any) -> list[dict[str, Any]]:
    if isinstance(availability, list):
        return [dict(window) for window in availability if isinstance(window, dict)]
    if not isinstance(availability, dict):
        return []

    start = availability.get("start")
    end = availability.get("end")
    workdays = availability.get("workdays", [])
    if not isinstance(workdays, list):
        return []

    windows = []
    for day in workdays:
        if not isinstance(day, int) or day < 0 or day >= len(WEEKDAY_NAMES):
            continue
        windows.append({"day": WEEKDAY_NAMES[day], "start": start, "end": end})
    return windows


def _validate_meeting_availability(
    conn: sqlite3.Connection,
    attendees: list[str],
    *,
    start_time: datetime,
    end_time: datetime,
) -> str | None:
    for person_id in attendees:
        availability = _person_availability(conn, person_id)
        if availability and not _time_range_inside_availability(start_time, end_time, availability):
            return f"{person_id} is not available for the full meeting window."
        conflict = _calendar_conflict(conn, person_id, start_time, end_time)
        if conflict:
            return f"{person_id} already has a meeting during that window: {conflict}."
    return None


def _time_range_inside_availability(
    start_time: datetime,
    end_time: datetime,
    availability: list[dict[str, Any]],
) -> bool:
    windows = _availability_windows_for_day(start_time, availability)
    return any(start_time >= window_start and end_time <= window_end for window_start, window_end in windows)


def _calendar_conflict(
    conn: sqlite3.Connection,
    person_id: str,
    start_time: datetime,
    end_time: datetime,
) -> str | None:
    rows = conn.execute(
        """
        SELECT title, start_at, end_at, attendees_json
        FROM calendar_events
        WHERE status IN ('scheduled', 'completed')
          AND start_at < ?
          AND end_at > ?
        ORDER BY start_at, id
        """,
        (_format_time(end_time), _format_time(start_time)),
    ).fetchall()
    for row in rows:
        attendees = loads(row["attendees_json"], [])
        if person_id in attendees:
            return row["title"]
    return None


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
