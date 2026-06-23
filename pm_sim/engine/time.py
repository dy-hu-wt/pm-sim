from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .conditions import all_conditions_match
from .effects import apply_effects
from .runtime_config import meeting_rules, outcome_rules, policy_behaviors
from ..coworkers import effects_for_event, effects_for_meeting
from ..db import connect, rows_to_dicts
from ..jsonutil import dumps, loads
from ..paths import DEFAULT_DB_PATH
from ..state import get_current_time, get_state_value, log_action, set_state_value


_DURATION_RE = re.compile(r"^(?P<amount>\d+)(?P<unit>m|h|d)$")


def advance_time(
    db_path: Path | str = DEFAULT_DB_PATH,
    target: str = "until_next_event",
    *,
    actor: str = "agent",
    action_type: str = "advance_time",
) -> dict[str, Any]:
    conn = connect(db_path)
    try:
        current_time = get_current_time(conn)
        new_time = _resolve_target_time(conn, current_time, target)

        if new_time < current_time:
            raise ValueError("Cannot move simulated time backwards.")

        delivered, applied_policies = _deliver_due_activity(conn, current_time, new_time)

        set_state_value(conn, "current_time", new_time)
        log_action(
            conn,
            action_id=f"action_{_next_action_number(conn)}_{action_type}",
            actor=actor,
            action_type=action_type,
            created_at=new_time,
            payload={"target": target, "from": current_time, "to": new_time},
            result={
                "delivered_event_ids": [event["id"] for event in delivered],
                "applied_actor_behavior_ids": [policy["id"] for policy in applied_policies],
            },
        )
        conn.commit()

        return {
            "ok": True,
            "from": current_time,
            "to": new_time,
            "delivered_events": delivered,
            "applied_actor_behaviors": applied_policies,
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
    delivered, applied_policies = _deliver_due_activity(conn, current_time, new_time)
    set_state_value(conn, "current_time", new_time)
    return {
        "minutes": minutes,
        "from": current_time,
        "to": new_time,
        "delivered_events": delivered,
        "delivered_event_ids": [event["id"] for event in delivered],
        "applied_actor_behaviors": applied_policies,
        "applied_actor_behavior_ids": [policy["id"] for policy in applied_policies],
    }


def _resolve_target_time(conn: sqlite3.Connection, current_time: str, target: str) -> str:
    if target == "until_next_event":
        event_row = conn.execute(
            """
            SELECT scheduled_at
            FROM events
            WHERE status = 'pending' AND scheduled_at >= ?
            ORDER BY scheduled_at, priority, id
            LIMIT 1
            """,
            (current_time,),
        ).fetchone()
        next_times = []
        if event_row is not None:
            next_times.append(event_row["scheduled_at"])
        policy_time = _next_actor_behavior_time(conn, current_time)
        if policy_time is not None:
            next_times.append(policy_time)
        if not next_times:
            return current_time
        return min(next_times)

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


def _deliver_due_activity(
    conn: sqlite3.Connection,
    current_time: str,
    new_time: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    delivered_events: list[dict[str, Any]] = []
    applied_policies: list[dict[str, Any]] = []
    while True:
        next_event = _next_due_event(conn, new_time)
        next_policy = _next_due_actor_behavior(conn, current_time, new_time)
        if next_event is None and next_policy is None:
            break
        if next_event is not None and (
            next_policy is None or next_event["scheduled_at"] <= next_policy["trigger_at"]
        ):
            delivered_events.append(_deliver_event(conn, next_event, new_time))
            continue
        if next_policy is not None:
            applied_policies.append(_apply_actor_behavior(conn, next_policy))
    return delivered_events, applied_policies


def _next_due_event(conn: sqlite3.Connection, new_time: str) -> dict[str, Any] | None:
    rows = _due_events(conn, new_time)
    return rows[0] if rows else None


def _next_actor_behavior_time(conn: sqlite3.Connection, current_time: str) -> str | None:
    times = [
        policy["trigger_at"]
        for policy in _actor_policy_candidates(conn)
        if policy["trigger_at"] > current_time and not _actor_behavior_fired(conn, policy["id"])
    ]
    return min(times) if times else None


def _next_due_actor_behavior(
    conn: sqlite3.Connection,
    current_time: str,
    new_time: str,
) -> dict[str, Any] | None:
    for policy in _actor_policy_candidates(conn):
        if policy["trigger_at"] <= current_time or policy["trigger_at"] > new_time:
            continue
        if _actor_behavior_fired(conn, policy["id"]):
            continue
        if not all_conditions_match(conn, policy.get("when", [])):
            continue
        return policy
    return None


def _actor_policy_candidates(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    policies = _policy_behaviors(conn)
    candidates = []
    for policy in policies if isinstance(policies, list) else []:
        trigger = policy.get("trigger", {})
        trigger_at = trigger.get("at") or trigger.get("at_or_after")
        if not isinstance(trigger_at, str) or not trigger_at:
            continue
        candidates.append({**policy, "trigger_at": trigger_at})
    return sorted(
        candidates,
        key=lambda policy: (
            policy["trigger_at"],
            int(policy.get("priority", 100)),
            policy["id"],
        ),
    )


def _policy_behaviors(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return policy_behaviors(conn)


def _apply_actor_behavior(conn: sqlite3.Connection, policy: dict[str, Any]) -> dict[str, Any]:
    policy_id = policy["id"]
    source = f"actor_behavior:{policy_id}"
    applied_effects = apply_effects(
        conn,
        [dict(effect) for effect in policy.get("effects", [])],
        now=policy["trigger_at"],
        source=source,
    )
    set_state_value(conn, _actor_behavior_fired_key(policy_id), policy["trigger_at"])
    return {
        "id": policy_id,
        "person_id": policy.get("person_id"),
        "trigger_at": policy["trigger_at"],
        "applied_effects": applied_effects,
    }


def _actor_behavior_fired(conn: sqlite3.Connection, policy_id: str) -> bool:
    return get_state_value(conn, _actor_behavior_fired_key(policy_id)) is not None


def _actor_behavior_fired_key(policy_id: str) -> str:
    return f"actor_behavior_fired:{policy_id}"


def _deliver_event(
    conn: sqlite3.Connection,
    event: dict[str, Any],
    delivered_at: str,
) -> dict[str, Any]:
    payload = loads(event["payload_json"], {})
    source = f"event:{event['id']}"
    event_time = event["scheduled_at"]
    effects = _effects_for_delivery(conn, event["event_type"], payload, event_time)
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
    event_time: str,
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

    if event_type == "project_deadline":
        return _effects_for_project_deadline(conn, payload, event_time)

    return effects_for_event(conn, event_type, payload)


def _effects_for_project_deadline(
    conn: sqlite3.Connection,
    payload: dict[str, Any],
    event_time: str,
) -> list[dict[str, Any]]:
    project_id = payload.get("project_id")
    if not isinstance(project_id, str) or not project_id:
        raise RuntimeError("Project deadline event requires payload.project_id.")
    deadline_id = payload.get("deadline_id")
    if not isinstance(deadline_id, str) or not deadline_id:
        raise RuntimeError("Project deadline event requires payload.deadline_id.")
    outcome_doc_id = payload.get("outcome_doc_id")
    if not isinstance(outcome_doc_id, str) or not outcome_doc_id:
        outcome_doc_id = f"doc_{deadline_id}_outcome"
    outcome = _classify_project_outcome(conn, project_id)

    return [
        {
            "type": "update_project",
            "project_id": project_id,
            "status": outcome["status"],
            "risk_level": outcome["risk_level"],
            "deadline_reached": True,
            "deadline_id": deadline_id,
            "final_outcome": outcome["final_outcome"],
            "final_outcome_summary": outcome["summary"],
        },
        {
            "type": "create_doc",
            "id": outcome_doc_id,
            "title": payload.get("outcome_doc_title", "Project Outcome"),
            "kind": "outcome_report",
            "visible_at": event_time,
            "body": outcome["summary"],
            "metadata": {"final_outcome": outcome["final_outcome"]},
        },
    ]


def _classify_project_outcome(conn: sqlite3.Connection, project_id: str) -> dict[str, str]:
    for rule in outcome_rules(conn):
        if not all_conditions_match(conn, rule.get("when", []), project_id=project_id):
            continue
        result = rule.get("result", {})
        return {
            "status": result["status"],
            "risk_level": result["risk_level"],
            "final_outcome": result.get("final_outcome", rule["id"]),
            "summary": result.get("summary", ""),
        }
    raise RuntimeError("No Friday outcome rule matched current scenario state.")


def _discovered_fact_ids(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        """
        SELECT id
        FROM facts
        WHERE visible_at IS NOT NULL
        """
    ).fetchall()
    return {row["id"] for row in rows}


def _meeting_state(conn: sqlite3.Connection) -> dict[str, Any]:
    return {
        "discovered_facts": sorted(_discovered_fact_ids(conn)),
        "milestone_ids": sorted(_milestone_ids(conn)),
        "meeting_rules": meeting_rules(conn),
    }


def _milestone_ids(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT milestone_id
        FROM milestones
        """
    ).fetchall()
    return {row["milestone_id"] for row in rows}


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
