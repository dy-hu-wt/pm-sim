from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .coworkers import CoworkerReply, replies_for_chat
from .db import connect, row_to_dict, rows_to_dicts
from .effects import apply_effects
from .jsonutil import dumps, loads
from .paths import DEFAULT_DB_PATH
from .state import AGENT_ID, get_current_time, log_action

EMAIL_RISK_TERMS = frozenset(
    {"risk", "blocker", "blocked", "repo", "sync", "stale", "commit", "webhook"}
)
EMAIL_DRAFT_TERMS = frozenset(
    {"fallback", "draft", "draft-mode", "reliable", "de-scope", "descope", "human approval"}
)
EMAIL_CUSTOMER_TERMS = frozenset({"nimbus", "friday", "beta", "pilot", "customer", "confidence"})
EMAIL_APPROVAL_TERMS = frozenset({"human approval", "approve", "approval", "review before posting"})
COMPLETED_STATUSES = {"complete", "completed", "done", "resolved"}
UNRESOLVED_BLOCKER_STATUSES = {"open", "surfaced", "blocked"}


# Read-only tool: returns visible task state without mutating time, logs, or events.
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


# Read-only tool: returns a visible doc body; private docs stay inaccessible.
def read_doc(db_path: Path | str, doc_id: str) -> dict[str, Any]:
    conn = connect(db_path)
    try:
        doc = row_to_dict(
            conn.execute(
                """
                SELECT id, title, kind, body, visible, updated_at
                FROM docs
                WHERE id = ?
                """,
                (doc_id,),
            ).fetchone()
        )
        if doc is None:
            return {"ok": False, "error": f"Doc not found: {doc_id}"}
        if doc["visible"] != 1:
            return {"ok": False, "error": f"Doc is not visible: {doc_id}"}
        return {"ok": True, "doc": doc}
    finally:
        conn.close()


# Chat tool: records the agent message and schedules deterministic coworker replies.
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

        log_action(
            conn,
            action_id=_next_id(conn, "action_log", "action_send_chat"),
            actor=AGENT_ID,
            action_type="send_chat",
            created_at=current_time,
            payload={"person_id": person_id, "body": body},
            result={"message_id": message_id, "scheduled_reply_ids": scheduled_reply_ids},
        )
        conn.commit()

        return {
            "ok": True,
            "message_id": message_id,
            "scheduled_reply_ids": scheduled_reply_ids,
        }
    finally:
        conn.close()


# Email tool: records outreach and applies deterministic communication evidence when matched.
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
        email_effects = _effects_for_email(person_id, subject, body)
        applied_effects = apply_effects(
            conn,
            email_effects,
            now=current_time,
            source=f"action:{message_id}",
        )
        log_action(
            conn,
            action_id=_next_id(conn, "action_log", "action_send_email"),
            actor=AGENT_ID,
            action_type="send_email",
            created_at=current_time,
            payload={"person_id": person_id, "subject": subject, "body": body},
            result={"message_id": message_id, "applied_effects": applied_effects},
        )
        conn.commit()

        return {"ok": True, "message_id": message_id, "applied_effects": applied_effects}
    finally:
        conn.close()


# Task tool: updates explicit task fields only when required world state supports completion.
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
            },
        )
        conn.commit()

        return {"ok": True, "task_id": task_id, "status": new_status, "priority": new_priority}
    finally:
        conn.close()


def _validate_task_update(conn: sqlite3.Connection, task_id: str, new_status: str) -> str | None:
    if not _is_completion_status(new_status):
        return None

    if task_id == "task_repo_sync" and not _blocker_resolved(conn, "blocker_repo_sync_stale"):
        return (
            "task_repo_sync cannot be marked complete while "
            "blocker_repo_sync_stale is unresolved."
        )

    if task_id == "task_draft_mode_docs":
        missing = []
        if not _fact_discovered(conn, "fact_draft_mode_scope_confirmed"):
            missing.append("draft-mode scope confirmation")
        if not _blocker_resolved(conn, "blocker_scope_unclear"):
            missing.append("resolved scope blocker")
        if not _draft_mode_selected(conn):
            missing.append("draft-mode approval or selection")
        if missing:
            return "task_draft_mode_docs cannot be marked complete without " + ", ".join(missing) + "."

    if task_id == "task_customer_talk_track":
        missing = []
        if not _daisy_aligned(conn):
            missing.append("Daisy alignment")
        if not _evidence_exists(conn, "customer_message_ready"):
            missing.append("customer-ready Daisy email")
        if not _launch_mode_decided(conn):
            missing.append("launch mode decision")
        if missing:
            return "task_customer_talk_track cannot be marked complete without " + ", ".join(missing) + "."

    if task_id == "task_launch_decision" and not _toad_approval_exists(conn):
        return "task_launch_decision cannot be marked complete without Toad approval."

    return None


def _is_completion_status(status: str | None) -> bool:
    return (status or "").lower() in COMPLETED_STATUSES


def _blocker_resolved(conn: sqlite3.Connection, blocker_id: str) -> bool:
    row = conn.execute("SELECT status FROM blockers WHERE id = ?", (blocker_id,)).fetchone()
    if row is None:
        return False
    return row["status"].lower() not in UNRESOLVED_BLOCKER_STATUSES


def _fact_discovered(conn: sqlite3.Connection, fact_id: str) -> bool:
    return (
        conn.execute(
            """
            SELECT 1
            FROM facts
            WHERE id = ?
              AND discovered_at IS NOT NULL
            LIMIT 1
            """,
            (fact_id,),
        ).fetchone()
        is not None
    )


def _draft_mode_selected(conn: sqlite3.Connection) -> bool:
    decision = _project_decision(conn)
    return decision in {"draft_mode_selected", "draft_mode_approved"} or _fact_discovered(
        conn,
        "fact_draft_mode_approved",
    )


def _daisy_aligned(conn: sqlite3.Connection) -> bool:
    return _fact_discovered(conn, "fact_nimbus_values_reliability") or _evidence_exists(
        conn,
        "stakeholder_alignment",
    )


def _launch_mode_decided(conn: sqlite3.Connection) -> bool:
    decision = _project_decision(conn)
    return (
        decision is not None
        and decision != "undecided"
    ) or _fact_discovered(conn, "fact_draft_mode_approved")


def _toad_approval_exists(conn: sqlite3.Connection) -> bool:
    return _fact_discovered(conn, "fact_draft_mode_approved") or _evidence_exists(
        conn,
        "draft_mode_approved",
    )


def _evidence_exists(conn: sqlite3.Connection, evidence_key: str) -> bool:
    return (
        conn.execute(
            """
            SELECT 1
            FROM evaluation_evidence
            WHERE evidence_key = ?
            LIMIT 1
            """,
            (evidence_key,),
        ).fetchone()
        is not None
    )


def _project_decision(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        """
        SELECT metadata_json
        FROM projects
        WHERE id = 'project_pr_review_agent'
        """
    ).fetchone()
    if row is None:
        return None
    metadata = loads(row["metadata_json"], {})
    decision = metadata.get("decision")
    return decision if isinstance(decision, str) else None


# Calendar tool: records the meeting and schedules an async meeting_occurs event.
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
            result={"meeting_id": meeting_id, "event_id": meeting_event_id},
        )
        conn.commit()

        return {"ok": True, "meeting_id": meeting_id, "event_id": meeting_event_id}
    finally:
        conn.close()


def _schedule_coworker_reply(
    conn: sqlite3.Connection,
    reply: CoworkerReply,
    current_time: str,
) -> str:
    event_id = _next_id(conn, "events", "event_coworker_reply")
    scheduled_at = _format_time(_parse_time(current_time) + timedelta(minutes=reply.delay_minutes))
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


def _effects_for_email(person_id: str, subject: str, body: str) -> list[dict[str, Any]]:
    if person_id.lower() != "daisy":
        return []

    normalized = _normalize(f"{subject} {body}")
    has_risk = _mentions_any(normalized, EMAIL_RISK_TERMS)
    has_draft_plan = _mentions_any(normalized, EMAIL_DRAFT_TERMS)
    has_customer_context = _mentions_any(normalized, EMAIL_CUSTOMER_TERMS)
    has_human_approval = _mentions_any(normalized, EMAIL_APPROVAL_TERMS)
    if not (has_risk and has_draft_plan and has_customer_context):
        return []

    effects = [
        {
            "type": "add_evaluation_evidence",
            "key": "stakeholder_alignment",
            "note": "Agent sent Daisy a concrete Nimbus repo-sync risk and draft-mode status update.",
        },
        {
            "type": "update_project",
            "project_id": "project_pr_review_agent",
            "launch_conflict": {
                "status": "investigated",
                "inputs": {"customer_constraint_known": True},
            },
        }
    ]
    if has_human_approval:
        effects.append(
            {
                "type": "add_evaluation_evidence",
                "key": "customer_message_ready",
                "note": (
                    "Agent gave Daisy a Nimbus-ready Friday update: repo-sync risk, "
                    "draft mode, and human approval before posting."
                ),
            }
        )
    return effects


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
        WHERE discovered_at IS NOT NULL
        """
    ).fetchall()
    return {"discovered_facts": [row["id"] for row in facts]}


def _get_person(conn: sqlite3.Connection, person_id: str) -> dict[str, Any] | None:
    return row_to_dict(conn.execute("SELECT * FROM people WHERE id = ?", (person_id,)).fetchone())


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
