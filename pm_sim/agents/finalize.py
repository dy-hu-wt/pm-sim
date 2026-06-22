from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ..db import connect
from ..jsonutil import loads
from ..scenario import load_scenario
from ..state import observe
from ..time import advance_time


ProgressFn = Callable[[str], None]


def finalize_to_deadline(
    db_path: Path | str,
    scenario_path: Path | str,
    *,
    progress: ProgressFn | None = None,
) -> dict[str, Any]:
    scenario = load_scenario(scenario_path)
    deadline = _scenario_deadline(scenario)
    current_time = observe(db_path).get("current_time")
    result: dict[str, Any] = {
        "ok": True,
        "deadline": deadline,
        "from": current_time,
        "to": current_time,
        "advanced": False,
        "delivered_events": [],
        "final_outcome": _final_outcome(db_path),
    }
    if not deadline or not current_time:
        return result

    if _parse_time(current_time) >= _parse_time(deadline):
        result["to"] = current_time
        return result

    if progress is not None:
        progress(f"finalizing simulation to deadline {deadline}")

    advanced = advance_time(
        db_path,
        f"to:{deadline}",
        actor="operator",
        action_type="finalize_to_deadline",
    )
    result.update(
        {
            "ok": advanced.get("ok", True),
            "to": advanced.get("to"),
            "advanced": True,
            "delivered_events": advanced.get("delivered_events", []),
            "advance_result": advanced,
            "final_outcome": _final_outcome(db_path),
        }
    )
    return result


def _scenario_deadline(scenario: dict[str, Any]) -> str | None:
    deadlines = [
        project.get("deadline")
        for project in scenario.get("projects", [])
        if isinstance(project.get("deadline"), str)
    ]
    if deadlines:
        return max(deadlines, key=_parse_time)

    scheduled_events = [
        event.get("scheduled_at")
        for event in scenario.get("events", [])
        if isinstance(event.get("scheduled_at"), str)
    ]
    return max(scheduled_events, key=_parse_time) if scheduled_events else None


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _final_outcome(db_path: Path | str) -> str | None:
    conn = connect(db_path)
    try:
        row = conn.execute(
            """
            SELECT metadata_json
            FROM projects
            ORDER BY deadline DESC, id
            LIMIT 1
            """,
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    metadata = loads(row["metadata_json"], {})
    outcome = metadata.get("final_outcome")
    return outcome if isinstance(outcome, str) else None
