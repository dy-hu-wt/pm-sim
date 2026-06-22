from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
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

EMAIL_RISK_TERMS = frozenset(
    {"risk", "blocker", "blocked", "repo", "sync", "stale", "commit", "webhook"}
)
EMAIL_DRAFT_TERMS = frozenset(
    {"fallback", "draft", "draft-mode", "reliable", "de-scope", "descope", "human approval"}
)
EMAIL_CUSTOMER_TERMS = frozenset({"nimbus", "friday", "beta", "pilot", "customer", "confidence"})
EMAIL_APPROVAL_TERMS = frozenset({"human approval", "approve", "approval", "review before posting"})
EMAIL_SECURITY_TERMS = frozenset(
    {
        "security",
        "private repo",
        "private repos",
        "source code",
        "raw source",
        "stores source",
        "stored",
        "retained",
        "retention",
    }
)
EMAIL_TRANSIENT_TERMS = frozenset(
    {"transient", "transiently", "not stored", "not retained", "no long-term", "raw source is not"}
)
EMAIL_AUDIT_TERMS = frozenset(
    {"metadata", "draft suggestions", "generated suggestions", "audit", "30 days", "beta audit"}
)
DOC_DRAFT_TERMS = frozenset({"draft", "draft-mode", "draft mode"})
DOC_HUMAN_APPROVAL_TERMS = frozenset({"human approval", "approval before posting", "review before posting"})
DOC_AUTO_FOLLOWUP_TERMS = frozenset(
    {
        "auto-commenting out",
        "auto-commenting is out",
        "auto-commenting follow-up",
        "auto-commenting as follow-up",
        "auto-commenting remains follow-up",
        "auto-commenting not in friday",
        "no auto-commenting",
        "do not ship auto-commenting",
    }
)
DOC_RISK_TERMS = frozenset({"repo sync", "stale", "stale-code", "stale code", "commit", "webhook"})
DOC_APPROVAL_TERMS = frozenset({"toad", "approved", "approval"})
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
                SELECT id, title, kind, body, visible
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
        doc_effects = _effects_for_doc_update(conn, doc_id, body)
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
                "time_cost": time_cost,
            },
        )
        conn.commit()

        return {
            "ok": True,
            "message_id": message_id,
            "scheduled_reply_ids": scheduled_reply_ids,
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
        email_effects = _effects_for_email(conn, person_id, subject, body)
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


def _effects_for_email(
    conn: sqlite3.Connection,
    person_id: str,
    subject: str,
    body: str,
) -> list[dict[str, Any]]:
    if person_id.lower() != "daisy":
        return []

    normalized = _normalize(f"{subject} {body}")
    effects = []
    has_risk = _mentions_any(normalized, EMAIL_RISK_TERMS)
    has_draft_plan = _mentions_any(normalized, EMAIL_DRAFT_TERMS)
    has_customer_context = _mentions_any(normalized, EMAIL_CUSTOMER_TERMS)
    has_human_approval = _mentions_any(normalized, EMAIL_APPROVAL_TERMS)
    if has_risk and has_draft_plan and has_customer_context:
        effects.extend(
            [
                {
                    "type": "add_evaluation_evidence",
                    "key": "stakeholder_alignment",
                    "note": "Agent sent Daisy a concrete Nimbus repo-sync risk and draft-mode status update.",
                },
                {
                    "type": "update_project",
                    "project_id": _primary_project_id(conn),
                    "launch_conflict": {
                        "status": "investigated",
                        "inputs": {"customer_constraint_known": True},
                    },
                },
                {
                    "type": "update_coworker_state",
                    "person_id": "daisy",
                    "values": {
                        "customer_update_received": True,
                        "draft_mode_message_aligned": True,
                    },
                },
            ]
        )
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

    has_security_question = _mentions_any(normalized, EMAIL_SECURITY_TERMS)
    has_transient_answer = _mentions_any(normalized, EMAIL_TRANSIENT_TERMS)
    has_audit_limits = _mentions_any(normalized, EMAIL_AUDIT_TERMS)
    if (
        has_customer_context
        and has_security_question
        and has_transient_answer
        and has_audit_limits
        and _daisy_security_question_visible(conn)
    ):
        effects.append(
            {
                "type": "add_evaluation_evidence",
                "key": "security_question_answered",
                "note": (
                    "Agent sent Daisy a doc-backed answer on private repo source handling "
                    "and beta retention limits."
                ),
            }
        )
        effects.append(
            {
                "type": "update_coworker_state",
                "person_id": "daisy",
                "key": "security_answer_received",
                "value": True,
            }
        )
    return effects


def _effects_for_doc_update(conn: sqlite3.Connection, doc_id: str, body: str) -> list[dict[str, Any]]:
    if doc_id != "doc_launch_decision_record":
        return []
    normalized = _normalize(body)
    if not (
        _mentions_any(normalized, DOC_DRAFT_TERMS)
        and _mentions_any(normalized, DOC_HUMAN_APPROVAL_TERMS)
        and _mentions_any(normalized, DOC_AUTO_FOLLOWUP_TERMS)
        and _mentions_any(normalized, DOC_RISK_TERMS)
        and _mentions_any(normalized, DOC_APPROVAL_TERMS)
        and _draft_mode_approved(conn)
    ):
        return []
    return [
        {
            "type": "add_evaluation_evidence",
            "key": "decision_record_written",
            "note": (
                "Agent documented the Friday draft-mode decision with Toad approval, "
                "repo-sync risk rationale, human approval, and auto-commenting as follow-up."
            ),
        }
    ]


def _primary_project_id(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        """
        SELECT id
        FROM projects
        ORDER BY deadline DESC, id
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("Simulation has no project.")
    return row["id"]


def _daisy_security_question_visible(conn: sqlite3.Connection) -> bool:
    return (
        conn.execute(
            """
            SELECT 1
            FROM messages
            WHERE sender_id = 'daisy'
              AND recipient_id = 'agent'
              AND (
                lower(coalesce(subject, '')) LIKE '%security%'
                OR lower(body) LIKE '%security%'
                OR lower(body) LIKE '%source code%'
                OR lower(body) LIKE '%private repo%'
              )
            LIMIT 1
            """
        ).fetchone()
        is not None
    )


def _draft_mode_approved(conn: sqlite3.Connection) -> bool:
    fact = conn.execute(
        """
        SELECT 1
        FROM facts
        WHERE id = 'fact_draft_mode_approved'
          AND discovered_at IS NOT NULL
        LIMIT 1
        """
    ).fetchone()
    if fact is not None:
        return True
    project = conn.execute(
        """
        SELECT metadata_json
        FROM projects
        WHERE id = ?
        """,
        (_primary_project_id(conn),),
    ).fetchone()
    metadata = loads(project["metadata_json"], {}) if project is not None else {}
    return metadata.get("decision") == "draft_mode_approved"


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
    rules = loads(get_state_value(conn, "coworker_rules_json") or "[]", [])
    response_delays = loads(get_state_value(conn, "response_delays_json") or "{}", {})
    return {
        "discovered_facts": [row["id"] for row in facts],
        "coworker_rules": rules,
        "response_delays": response_delays,
    }


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
