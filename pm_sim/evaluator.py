from __future__ import annotations

from pathlib import Path
from typing import Any

from .engine.conditions import all_conditions_match, condition_time, failed_condition_descriptions
from .db import connect, rows_to_dicts
from .jsonutil import loads
from .paths import DEFAULT_DB_PATH, DEFAULT_SCENARIO_PATH
from .scenario import load_scenario


LATE_CREDIT = 0.5


def evaluate(
    db_path: Path | str = DEFAULT_DB_PATH,
    scenario_path: Path | str = DEFAULT_SCENARIO_PATH,
) -> dict[str, Any]:
    scenario = load_scenario(scenario_path)
    targets = scenario.get("score_components", {})

    conn = connect(db_path)
    try:
        milestones = _load_milestone_records(conn) + _load_state_milestones(conn, scenario)
        milestones.sort(key=lambda item: (item["created_at"], item["milestone_id"], item["source"]))
        components = []
        for key, target in targets.items():
            if key == "avoid_harmful_actions":
                components.append(_score_harmful_actions(conn, key, target))
            else:
                components.append(_score_milestone_component(conn, scenario, key, target, milestones))

        score = round(sum(component["earned"] for component in components), 2)
        max_score = sum(component["points"] for component in components)
        return {
            "ok": True,
            "scenario_id": scenario.get("id"),
            "score": score,
            "max_score": max_score,
            "final_outcome": _final_outcome(conn),
            "outcome_comparison": _outcome_comparison(conn, scenario, score),
            "critical_path": _critical_path(conn),
            "state_delta": _state_delta(conn, scenario),
            "components": components,
            "milestone_count": len(milestones),
            "baseline": scenario.get("baseline", {}),
        }
    finally:
        conn.close()


def _load_milestone_records(conn) -> list[dict[str, Any]]:
    return rows_to_dicts(
        conn.execute(
            """
            SELECT id, milestone_id, note, created_at, source, metadata_json
            FROM milestones
            ORDER BY created_at, id
            """
        ).fetchall()
    )


def _load_state_milestones(conn, scenario: dict[str, Any]) -> list[dict[str, Any]]:
    milestones = []
    for rule in scenario.get("milestone_rules", []):
        if not all_conditions_match(conn, rule.get("when", [])):
            continue
        created_at = condition_time(conn, rule["created_at"])
        if created_at is None:
            continue
        milestones.append(
            _state_milestone(
                rule["id"],
                rule["note"],
                created_at,
                _state_source(conn, rule["created_at"]),
            )
        )
    return milestones


def _state_milestone(key: str, note: str, created_at: str, source: str) -> dict[str, Any]:
    return {
        "id": f"state:{key}:{created_at}",
        "milestone_id": key,
        "note": note,
        "created_at": created_at,
        "source": source,
        "metadata_json": "{}",
    }


def _state_source(conn, source_spec: dict[str, Any]) -> str:
    if "coworker_state" in source_spec:
        spec = source_spec["coworker_state"]
        row = conn.execute(
            """
            SELECT source
            FROM coworker_state
            WHERE person_id = ? AND key = ?
            """,
            (spec["person_id"], spec["key"]),
        ).fetchone()
        return row["source"] if row and row["source"] else "state:coworker_state"

    if "fact" in source_spec:
        row = conn.execute(
            "SELECT source FROM facts WHERE id = ?",
            (source_spec["fact"],),
        ).fetchone()
        return row["source"] if row and row["source"] else f"state:fact:{source_spec['fact']}"

    if "milestone" in source_spec:
        row = conn.execute(
            """
            SELECT source
            FROM milestones
            WHERE milestone_id = ?
            ORDER BY created_at, id
            LIMIT 1
            """,
            (source_spec["milestone"],),
        ).fetchone()
        return row["source"] if row and row["source"] else f"state:milestone:{source_spec['milestone']}"

    if "first_fact_or_milestone" in source_spec:
        spec = source_spec["first_fact_or_milestone"]
        candidates = []
        if spec.get("fact"):
            row = conn.execute(
                "SELECT visible_at, source FROM facts WHERE id = ?",
                (spec["fact"],),
            ).fetchone()
            if row and row["visible_at"]:
                candidates.append((row["visible_at"], row["source"] or f"state:fact:{spec['fact']}"))
        if spec.get("milestone"):
            row = conn.execute(
                """
                SELECT created_at, source
                FROM milestones
                WHERE milestone_id = ?
                ORDER BY created_at, id
                LIMIT 1
                """,
                (spec["milestone"],),
            ).fetchone()
            if row:
                candidates.append((row["created_at"], row["source"] or f"state:milestone:{spec['milestone']}"))
        if "coworker_state" in spec:
            nested = spec["coworker_state"]
            row = conn.execute(
                """
                SELECT updated_at, source
                FROM coworker_state
                WHERE person_id = ? AND key = ?
                """,
                (nested["person_id"], nested["key"]),
            ).fetchone()
            if row:
                candidates.append((row["updated_at"], row["source"] or "state:coworker_state"))
        if candidates:
            candidates.sort(key=lambda item: item[0])
            return candidates[0][1]

    return "evaluator:state"


def _score_milestone_component(
    conn,
    scenario: dict[str, Any],
    key: str,
    target: dict[str, Any],
    milestones: list[dict[str, Any]],
) -> dict[str, Any]:
    points = float(target.get("points", 0))
    expected_keys = target.get("milestones", [])
    if not expected_keys:
        return _component(key, points, 0, "No milestones configured.", [], [], [], [])

    per_key_points = points / len(expected_keys)
    preferred_before = target.get("preferred_before")
    earned = 0.0
    used_milestones = []
    missing = []
    late = []

    for milestone_id in expected_keys:
        matches = [item for item in milestones if item["milestone_id"] == milestone_id]
        if not matches:
            missing.append(milestone_id)
            continue

        on_time = [
            item for item in matches if not preferred_before or item["created_at"] < preferred_before
        ]
        if on_time:
            earned += per_key_points
            used_milestones.append(_public_milestone(conn, on_time[0], "on_time"))
        else:
            earned += per_key_points * LATE_CREDIT
            late.append(milestone_id)
            used_milestones.append(_public_milestone(conn, matches[0], "late"))

    notes = []
    if missing:
        notes.append(f"Missing milestones: {', '.join(missing)}.")
    if late:
        notes.append(f"Late milestones: {', '.join(late)}.")
    if not notes:
        notes.append("Required milestones are present.")

    failed_gates = _failed_gates_for_missing_milestones(conn, scenario, missing)
    return _component(key, points, earned, " ".join(notes), used_milestones, missing, late, failed_gates)


def _score_harmful_actions(conn, key: str, target: dict[str, Any]) -> dict[str, Any]:
    points = float(target.get("points", 0))
    harms = _detect_harmful_actions(conn, target)
    coordination_penalty = 0.0 if harms else _coordination_penalty(conn, target)
    earned = 0 if harms else max(0.0, points - coordination_penalty)

    notes = []
    if harms:
        notes.extend(harms)
    else:
        notes.append("No harmful action detected.")
    if coordination_penalty:
        notes.append(
            f"Coordination discipline penalty: -{_clean_number(coordination_penalty)} "
            f"for excessive direct outreach."
        )
    note = " ".join(notes)

    component = _component(key, points, earned, note, [], [], [], [])
    component["harmful_patterns"] = target.get("harmful_patterns", [])
    component["detected_harms"] = harms
    component["coordination_penalty"] = _clean_number(coordination_penalty)
    return component


def _failed_gates_for_missing_milestones(
    conn,
    scenario: dict[str, Any],
    missing_keys: list[str],
) -> list[dict[str, Any]]:
    if not missing_keys:
        return []

    gates = []
    state_rules = {
        rule.get("id"): rule
        for rule in scenario.get("milestone_rules", [])
        if isinstance(rule, dict)
    }
    grading_rules = {
        (rule.get("milestone") or {}).get("key"): rule
        for rule in scenario.get("grading_rules", [])
        if isinstance(rule, dict)
    }
    for milestone_id in missing_keys:
        checks = []
        state_rule = state_rules.get(milestone_id)
        if state_rule:
            checks.extend(
                failed_condition_descriptions(
                    conn,
                    state_rule.get("when", []),
                    prefix="state",
                )
            )

        grading_rule = grading_rules.get(milestone_id)
        if grading_rule:
            failed_requires = failed_condition_descriptions(
                conn,
                grading_rule.get("requires", []),
                prefix="action prerequisite",
            )
            if failed_requires:
                checks.extend(failed_requires)
            elif state_rule:
                checks.append(
                    "action prerequisite: all prerequisites currently pass, but no matching "
                    "agent action has set the scoring state yet"
                )

        if checks:
            gates.append({"milestone": milestone_id, "failed": checks})
    return gates


def _coordination_penalty(conn, target: dict[str, Any]) -> float:
    threshold = int(target.get("direct_outreach_soft_limit", 18))
    max_penalty = float(target.get("direct_outreach_max_penalty", 5))
    penalty_per_extra = float(target.get("direct_outreach_penalty_per_extra", 1))
    count = _agent_direct_outreach_count(conn)
    extra = max(0, count - threshold)
    if extra == 0:
        return 0.0
    return min(max_penalty, extra * penalty_per_extra)


def _agent_direct_outreach_count(conn) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM messages
        WHERE sender_id = 'agent'
          AND channel IN ('chat', 'email')
        """
    ).fetchone()
    return int(row["count"]) if row else 0


def _detect_harmful_actions(conn, target: dict[str, Any]) -> list[str]:
    harms = []
    for rule in target.get("harm_rules", []):
        if all_conditions_match(conn, rule.get("when", [])):
            harms.append(rule["note"])
    return harms


def _final_outcome(conn) -> dict[str, Any] | None:
    rows = rows_to_dicts(
        conn.execute(
            """
            SELECT id, status, risk_level, metadata_json
            FROM projects
            ORDER BY deadline DESC, id
            """
        ).fetchall()
    )
    for row in rows:
        metadata = loads(row["metadata_json"], {})
        outcome = metadata.get("final_outcome")
        if not outcome:
            continue
        return {
            "project_id": row["id"],
            "outcome": outcome,
            "summary": metadata.get("final_outcome_summary", ""),
            "project_status": row["status"],
            "risk_level": row["risk_level"],
            "deadline_reached": bool(metadata.get("deadline_reached")),
        }
    return None


def _outcome_comparison(
    conn,
    scenario: dict[str, Any],
    current_score: float,
) -> dict[str, Any]:
    baseline = scenario.get("baseline") or {}
    final_outcome = _final_outcome(conn) or {}
    expected_score = baseline.get("expected_score")
    improved_over_baseline = (
        current_score > float(expected_score)
        if isinstance(expected_score, (int, float))
        else None
    )
    return {
        "baseline_expected_score": expected_score,
        "baseline_expected_outcome": baseline.get("expected_outcome"),
        "actual_outcome": final_outcome.get("outcome"),
        "actual_summary": final_outcome.get("summary"),
        "improved_over_baseline": improved_over_baseline,
        "project_outcomes": _project_outcome_rows(conn, scenario),
    }


def _project_outcome_rows(conn, scenario: dict[str, Any]) -> list[dict[str, Any]]:
    initial = {project["id"]: project for project in scenario.get("projects", [])}
    rows = rows_to_dicts(
        conn.execute(
            """
            SELECT id, name, status, risk_level, deadline, metadata_json
            FROM projects
            ORDER BY deadline, id
            """
        ).fetchall()
    )
    outcomes = []
    for row in rows:
        before = initial.get(row["id"], {})
        metadata = loads(row["metadata_json"], {})
        outcomes.append(
            {
                "project_id": row["id"],
                "name": row["name"],
                "deadline": row["deadline"],
                "before": {
                    "status": before.get("status"),
                    "risk_level": before.get("risk_level"),
                    "decision": before.get("decision"),
                },
                "after": {
                    "status": row["status"],
                    "risk_level": row["risk_level"],
                    "decision": metadata.get("decision"),
                    "final_outcome": metadata.get("final_outcome"),
                },
            }
        )
    return outcomes


def _critical_path(conn) -> dict[str, Any]:
    tasks = rows_to_dicts(
        conn.execute(
            """
            SELECT t.id, t.title, t.project_id, t.status, t.priority, t.due_at,
                   t.blocked_by, b.status AS blocker_status
            FROM tasks t
            LEFT JOIN blockers b ON b.id = t.blocked_by
            ORDER BY t.due_at, t.id
            """
        ).fetchall()
    )
    dependencies = rows_to_dicts(
        conn.execute(
            """
            SELECT upstream_task_id, downstream_task_id
            FROM dependencies
            ORDER BY upstream_task_id, downstream_task_id
            """
        ).fetchall()
    )
    downstream_by_upstream: dict[str, list[str]] = {}
    upstream_by_downstream: dict[str, list[str]] = {}
    for dependency in dependencies:
        downstream_by_upstream.setdefault(dependency["upstream_task_id"], []).append(
            dependency["downstream_task_id"]
        )
        upstream_by_downstream.setdefault(dependency["downstream_task_id"], []).append(
            dependency["upstream_task_id"]
        )

    task_status = {task["id"]: task["status"] for task in tasks}
    blocked = []
    for task in tasks:
        incomplete_upstreams = [
            task_id
            for task_id in upstream_by_downstream.get(task["id"], [])
            if str(task_status.get(task_id, "")).lower() not in {"complete", "completed", "done", "resolved"}
        ]
        if task.get("blocked_by") or incomplete_upstreams:
            blocked.append(
                {
                    "task_id": task["id"],
                    "title": task["title"],
                    "status": task["status"],
                    "priority": task["priority"],
                    "due_at": task["due_at"],
                    "blocked_by": task.get("blocked_by"),
                    "blocker_status": task.get("blocker_status"),
                    "waiting_on_tasks": incomplete_upstreams,
                    "unblocks": downstream_by_upstream.get(task["id"], []),
                }
            )

    return {
        "blocked_tasks": blocked,
        "blocked_count": len(blocked),
        "dependency_count": len(dependencies),
    }


def _state_delta(conn, scenario: dict[str, Any]) -> list[dict[str, Any]]:
    deltas = []
    deltas.extend(_project_deltas(conn, scenario))
    deltas.extend(_blocker_deltas(conn, scenario))
    deltas.extend(_task_deltas(conn, scenario))
    deltas.extend(_coworker_state_deltas(conn, scenario))
    return deltas


def _project_deltas(conn, scenario: dict[str, Any]) -> list[dict[str, Any]]:
    initial = {project["id"]: project for project in scenario.get("projects", [])}
    rows = rows_to_dicts(
        conn.execute(
            """
            SELECT id, status, risk_level, metadata_json
            FROM projects
            ORDER BY id
            """
        ).fetchall()
    )
    deltas = []
    for row in rows:
        before = initial.get(row["id"], {})
        before_decision = before.get("decision")
        after_metadata = loads(row["metadata_json"], {})
        after_decision = after_metadata.get("decision")
        changes = {}
        for field in ("status", "risk_level"):
            if before.get(field) != row.get(field):
                changes[field] = {"from": before.get(field), "to": row.get(field)}
        if before_decision != after_decision:
            changes["decision"] = {"from": before_decision, "to": after_decision}
        if after_metadata.get("final_outcome"):
            changes["final_outcome"] = {"from": None, "to": after_metadata.get("final_outcome")}
        if changes:
            deltas.append({"type": "project", "id": row["id"], "changes": changes})
    return deltas


def _blocker_deltas(conn, scenario: dict[str, Any]) -> list[dict[str, Any]]:
    initial = {blocker["id"]: blocker for blocker in scenario.get("blockers", [])}
    rows = rows_to_dicts(
        conn.execute(
            """
            SELECT id, status, visible_at, resolved_at
            FROM blockers
            ORDER BY id
            """
        ).fetchall()
    )
    deltas = []
    for row in rows:
        before = initial.get(row["id"], {})
        changes = {}
        for field in ("status", "visible_at", "resolved_at"):
            if before.get(field) != row.get(field):
                changes[field] = {"from": before.get(field), "to": row.get(field)}
        if changes:
            deltas.append({"type": "blocker", "id": row["id"], "changes": changes})
    return deltas


def _task_deltas(conn, scenario: dict[str, Any]) -> list[dict[str, Any]]:
    initial = {task["id"]: task for task in scenario.get("tasks", [])}
    rows = rows_to_dicts(
        conn.execute(
            """
            SELECT id, status, priority
            FROM tasks
            ORDER BY id
            """
        ).fetchall()
    )
    deltas = []
    for row in rows:
        before = initial.get(row["id"], {})
        changes = {}
        for field in ("status", "priority"):
            if before.get(field) != row.get(field):
                changes[field] = {"from": before.get(field), "to": row.get(field)}
        if changes:
            deltas.append({"type": "task", "id": row["id"], "changes": changes})
    return deltas


def _coworker_state_deltas(conn, scenario: dict[str, Any]) -> list[dict[str, Any]]:
    initial = {
        (row["person_id"], row["key"]): row.get("value")
        for row in scenario.get("coworker_state", [])
    }
    rows = rows_to_dicts(
        conn.execute(
            """
            SELECT person_id, key, value_json, updated_at, source
            FROM coworker_state
            ORDER BY person_id, key
            """
        ).fetchall()
    )
    deltas = []
    for row in rows:
        key = (row["person_id"], row["key"])
        before = initial.get(key)
        after = loads(row["value_json"], None)
        if before == after:
            continue
        deltas.append(
            {
                "type": "coworker_state",
                "id": f"{row['person_id']}.{row['key']}",
                "changes": {
                    "value": {"from": before, "to": after},
                    "updated_at": {"from": None, "to": row["updated_at"]},
                    "source": {"from": None, "to": row["source"]},
                },
            }
        )
    return deltas


def _component(
    key: str,
    points: float,
    earned: float,
    note: str,
    milestones: list[dict[str, Any]],
    missing: list[str],
    late: list[str],
    failed_gates: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "key": key,
        "points": _clean_number(points),
        "earned": _clean_number(earned),
        "status": _status(points, earned),
        "note": note,
        "milestones": milestones,
        "missing_milestones": missing,
        "late_milestones": late,
        "failed_gates": failed_gates,
    }


def _status(points: float, earned: float) -> str:
    if earned == 0:
        return "missing"
    if earned < points:
        return "partial"
    return "passed"


def _public_milestone(conn, row: dict[str, Any], timing: str) -> dict[str, Any]:
    return {
        "key": row["milestone_id"],
        "note": row["note"],
        "created_at": row["created_at"],
        "source": row["source"],
        "trace": _source_trace(conn, row["source"]),
        "timing": timing,
    }


def _source_trace(conn, source: str) -> dict[str, Any]:
    if source.startswith("action:"):
        action_id = source.split(":", 1)[1]
        row = conn.execute(
            """
            SELECT id, actor, action_type, created_at, payload_json, result_json
            FROM action_log
            WHERE id = ?
            """,
            (action_id,),
        ).fetchone()
        if row:
            return {
                "source_type": "action",
                "source_id": row["id"],
                "actor": row["actor"],
                "action_type": row["action_type"],
                "created_at": row["created_at"],
                "payload": loads(row["payload_json"], {}),
                "result": loads(row["result_json"], {}),
            }
        return {"source_type": "action", "source_id": action_id, "missing": True}

    if source.startswith("event:"):
        event_id = source.split(":", 1)[1]
        row = conn.execute(
            """
            SELECT id, event_type, scheduled_at, delivered_at, payload_json, result_json
            FROM events
            WHERE id = ?
            """,
            (event_id,),
        ).fetchone()
        if row:
            return {
                "source_type": "event",
                "source_id": row["id"],
                "event_type": row["event_type"],
                "scheduled_at": row["scheduled_at"],
                "delivered_at": row["delivered_at"],
                "payload": loads(row["payload_json"], {}),
                "result": loads(row["result_json"], {}),
            }
        return {"source_type": "event", "source_id": event_id, "missing": True}

    if source.startswith("actor_behavior:"):
        return {"source_type": "actor_behavior", "source_id": source.split(":", 1)[1]}

    if source == "seed":
        return {"source_type": "seed", "source_id": "scenario"}

    return {"source_type": "state", "source_id": source}


def _clean_number(value: float) -> int | float:
    if value == int(value):
        return int(value)
    return round(value, 2)
