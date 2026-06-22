from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .coworkers import effects_for_event, effects_for_meeting
from .db import connect, rows_to_dicts
from .effects import apply_effects
from .jsonutil import dumps, loads
from .paths import DEFAULT_DB_PATH
from .state import get_current_time, log_action, set_state_value


_DURATION_RE = re.compile(r"^(?P<amount>\d+)(?P<unit>m|h|d)$")
DAISY_READY_BY = "2026-06-25T10:00:00"
PEACH_READY_BY = "2026-06-25T10:00:00"
DRAFT_APPROVAL_BY = "2026-06-25T15:00:00"
COMPLETED_STATUSES = {"complete", "completed", "done", "resolved"}
UNRESOLVED_BLOCKER_STATUSES = {"open", "surfaced", "blocked"}


def advance_time(db_path: Path | str = DEFAULT_DB_PATH, target: str = "until_next_event") -> dict[str, Any]:
    conn = connect(db_path)
    try:
        current_time = get_current_time(conn)
        new_time = _resolve_target_time(conn, current_time, target)

        if new_time < current_time:
            raise ValueError("Cannot move simulated time backwards.")

        due_events = _due_events(conn, new_time)
        delivered = [_deliver_event(conn, event, new_time) for event in due_events]

        set_state_value(conn, "current_time", new_time)
        log_action(
            conn,
            action_id=f"action_{_next_action_number(conn)}_advance_time",
            actor="agent",
            action_type="advance_time",
            created_at=new_time,
            payload={"target": target, "from": current_time, "to": new_time},
            result={"delivered_event_ids": [event["id"] for event in delivered]},
        )
        conn.commit()

        return {
            "ok": True,
            "from": current_time,
            "to": new_time,
            "delivered_events": delivered,
        }
    finally:
        conn.close()


def consume_action_time(
    conn: sqlite3.Connection,
    *,
    current_time: str,
    minutes: int,
) -> dict[str, Any]:
    new_time = _format_time(_parse_time(current_time) + timedelta(minutes=minutes))
    due_events = _due_events(conn, new_time)
    delivered = [_deliver_event(conn, event, new_time) for event in due_events]
    set_state_value(conn, "current_time", new_time)
    return {
        "minutes": minutes,
        "from": current_time,
        "to": new_time,
        "delivered_events": delivered,
        "delivered_event_ids": [event["id"] for event in delivered],
    }


def _resolve_target_time(conn: sqlite3.Connection, current_time: str, target: str) -> str:
    if target == "until_next_event":
        row = conn.execute(
            """
            SELECT scheduled_at
            FROM events
            WHERE status = 'pending' AND scheduled_at >= ?
            ORDER BY scheduled_at, priority, id
            LIMIT 1
            """,
            (current_time,),
        ).fetchone()
        if row is None:
            return current_time
        return row["scheduled_at"]

    if target.startswith("to:"):
        return target.removeprefix("to:").strip()

    match = _DURATION_RE.match(target)
    if match is None:
        raise ValueError("Time target must be a duration like 30m/2h/1d, 'until_next_event', or 'to:<iso time>'.")

    amount = int(match.group("amount"))
    unit = match.group("unit")
    delta = {
        "m": timedelta(minutes=amount),
        "h": timedelta(hours=amount),
        "d": timedelta(days=amount),
    }[unit]
    return _format_time(_parse_time(current_time) + delta)


def _due_events(conn: sqlite3.Connection, new_time: str) -> list[dict[str, Any]]:
    return rows_to_dicts(
        conn.execute(
            """
            SELECT id, event_type, scheduled_at, created_at, status,
                   priority, payload_json, result_json
            FROM events
            WHERE status = 'pending' AND scheduled_at <= ?
            ORDER BY scheduled_at, priority, id
            """,
            (new_time,),
        ).fetchall()
    )


def _deliver_event(
    conn: sqlite3.Connection,
    event: dict[str, Any],
    delivered_at: str,
) -> dict[str, Any]:
    payload = loads(event["payload_json"], {})
    source = f"event:{event['id']}"
    effects = _effects_for_delivery(conn, event["event_type"], payload)
    event_time = event["scheduled_at"]
    applied_effects = apply_effects(conn, effects, now=event_time, source=source)
    result = {
        "handled": True,
        "payload": payload,
        "applied_effects": applied_effects,
    }
    conn.execute(
        """
        UPDATE events
        SET status = 'delivered', delivered_at = ?, result_json = ?
        WHERE id = ?
        """,
        (event_time, dumps(result), event["id"]),
    )
    return {
        "id": event["id"],
        "event_type": event["event_type"],
        "scheduled_at": event["scheduled_at"],
        "delivered_at": event_time,
        "result": result,
    }


def _effects_for_delivery(
    conn: sqlite3.Connection,
    event_type: str,
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    if event_type == "coworker_reply":
        return [
            {
                "type": "create_message",
                "channel": payload.get("channel", "chat"),
                "sender_id": payload["person_id"],
                "recipient_id": payload.get("recipient_id", "agent"),
                "subject": payload.get("subject"),
                "body": payload.get("body", ""),
            },
            *payload.get("effects", []),
        ]

    if event_type == "meeting_occurs":
        return effects_for_meeting(payload, _meeting_state(conn))

    if event_type == "friday_nimbus_deadline":
        return _effects_for_friday_deadline(conn)

    return effects_for_event(event_type, payload)


def _effects_for_friday_deadline(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    outcome = _classify_friday_outcome(conn)

    return [
        {
            "type": "update_project",
            "project_id": "project_pr_review_agent",
            "status": outcome["status"],
            "risk_level": outcome["risk_level"],
            "deadline_reached": True,
            "deadline_id": "deadline_nimbus_beta",
            "final_outcome": outcome["final_outcome"],
            "final_outcome_summary": outcome["summary"],
        },
        {
            "type": "create_doc",
            "id": "doc_friday_outcome",
            "title": "Friday Outcome",
            "kind": "outcome_report",
            "visible": True,
            "body": outcome["summary"],
            "metadata": {"final_outcome": outcome["final_outcome"]},
        },
    ]


def _classify_friday_outcome(conn: sqlite3.Connection) -> dict[str, str]:
    facts = _discovered_fact_ids(conn)
    decision = _project_decision(conn)
    draft_approved_at = _first_fact_or_evidence_time(
        conn,
        "fact_draft_mode_approved",
        "draft_mode_approved",
    )
    stakeholder_aligned_at = _first_fact_or_evidence_time(
        conn,
        "fact_nimbus_values_reliability",
        "stakeholder_alignment",
    )
    customer_message_ready_at = _first_evidence_time(conn, "customer_message_ready")
    peach_unblocked_at = _first_evidence_time(conn, "peach_unblocked")

    draft_approved = draft_approved_at is not None or decision == "draft_mode_approved"
    stale_repo_risk_unresolved = not _blocker_resolved(conn, "blocker_repo_sync_stale")
    auto_commenting_committed = decision in {
        "auto_commenting_approved",
        "auto_commenting_selected",
        "auto_commenting_committed",
    }
    launch_mode_chosen = decision is not None and decision != "undecided"

    if auto_commenting_committed and stale_repo_risk_unresolved:
        return {
            "status": "shipped",
            "risk_level": "high",
            "final_outcome": "risky_auto_commenting",
            "summary": (
                "The project shipped with an auto-commenting commitment while repo sync "
                "risk remained unresolved, leaving Nimbus exposed to stale-code comments."
            ),
        }

    if not draft_approved and not launch_mode_chosen:
        return {
            "status": "missed",
            "risk_level": "high",
            "final_outcome": "no_approved_friday_plan",
            "summary": (
                "Friday arrived without an approved reliable launch plan. "
                "Auto-commenting remains risky because repo sync can review stale code."
            ),
        }

    customer_ready = stakeholder_aligned_at is not None and customer_message_ready_at is not None
    onboarding_ready = _draft_mode_onboarding_ready(conn, facts)
    if not (customer_ready and onboarding_ready):
        return {
            "status": "missed",
            "risk_level": "high",
            "final_outcome": "missed_due_to_blockers",
            "summary": (
                "A launch mode was chosen, but Friday execution was still blocked: "
                "customer messaging or draft-mode onboarding was not ready."
            ),
        }

    late_reasons = []
    if draft_approved_at and draft_approved_at >= DRAFT_APPROVAL_BY:
        late_reasons.append("draft approval landed after Thursday afternoon")
    if stakeholder_aligned_at and stakeholder_aligned_at >= DAISY_READY_BY:
        late_reasons.append("Daisy alignment landed after Thursday morning")
    if customer_message_ready_at and customer_message_ready_at >= DAISY_READY_BY:
        late_reasons.append("customer-ready email landed after Thursday morning")
    if peach_unblocked_at and peach_unblocked_at >= PEACH_READY_BY:
        late_reasons.append("Peach onboarding was unblocked after Thursday morning")
    if late_reasons:
        return {
            "status": "partial",
            "risk_level": "medium",
            "final_outcome": "late_draft_mode",
            "summary": (
                "Draft mode was approved, but it landed late for a confident Friday launch: "
                + "; ".join(late_reasons)
                + "."
            ),
        }

    return {
        "status": "shipped",
        "risk_level": "low",
        "final_outcome": "draft_mode_beta_shipped",
        "summary": (
            "The team shipped the reliable draft-mode beta for Nimbus Labs. "
            "Customer messaging was aligned, onboarding was unblocked, and "
            "auto-commenting remained follow-up work."
        ),
    }


def _discovered_fact_ids(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        """
        SELECT id
        FROM facts
        WHERE discovered_at IS NOT NULL
        """
    ).fetchall()
    return {row["id"] for row in rows}


def _meeting_state(conn: sqlite3.Connection) -> dict[str, Any]:
    return {
        "discovered_facts": sorted(_discovered_fact_ids(conn)),
        "evidence_keys": sorted(_evidence_keys(conn)),
    }


def _evidence_keys(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT evidence_key
        FROM evaluation_evidence
        """
    ).fetchall()
    return {row["evidence_key"] for row in rows}


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
    metadata = loads(row["metadata_json"], {}) or {}
    decision = metadata.get("decision")
    return decision if isinstance(decision, str) else None


def _blocker_resolved(conn: sqlite3.Connection, blocker_id: str) -> bool:
    row = conn.execute("SELECT status FROM blockers WHERE id = ?", (blocker_id,)).fetchone()
    if row is None:
        return False
    return row["status"].lower() not in UNRESOLVED_BLOCKER_STATUSES


def _task_status(conn: sqlite3.Connection, task_id: str) -> str | None:
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return None if row is None else row["status"]


def _draft_mode_onboarding_ready(conn: sqlite3.Connection, facts: set[str]) -> bool:
    return (
        "fact_draft_mode_scope_confirmed" in facts
        and _blocker_resolved(conn, "blocker_scope_unclear")
        and _task_status(conn, "task_draft_mode_docs") in {"in_progress", *COMPLETED_STATUSES}
    )


def _first_fact_or_evidence_time(
    conn: sqlite3.Connection,
    fact_id: str,
    evidence_key: str,
) -> str | None:
    times = [
        time
        for time in (
            _fact_discovered_at(conn, fact_id),
            _first_evidence_time(conn, evidence_key),
        )
        if time is not None
    ]
    return min(times) if times else None


def _fact_discovered_at(conn: sqlite3.Connection, fact_id: str) -> str | None:
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


def _first_evidence_time(conn: sqlite3.Connection, evidence_key: str) -> str | None:
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


def _next_action_number(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS count FROM action_log").fetchone()
    return int(row["count"]) + 1


def _parse_time(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"Invalid ISO simulated time: {value}") from error


def _format_time(value: datetime) -> str:
    return value.isoformat(timespec="seconds")
