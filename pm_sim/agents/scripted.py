from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ..actions import read_doc, schedule_meeting, send_chat, send_email, update_doc, update_task
from ..evaluator import evaluate
from ..paths import DEFAULT_DB_PATH, DEFAULT_SCENARIO_PATH
from ..scenario import load_scenario
from ..state import reset
from ..engine.time import advance_time
from .finalize import finalize_to_deadline


def run_scripted_agent(
    db_path: Path | str = DEFAULT_DB_PATH,
    scenario_path: Path | str = DEFAULT_SCENARIO_PATH,
    *,
    reset_first: bool = False,
) -> dict[str, Any]:
    with _scripted_semantic_matcher():
        return _run_scripted_agent(db_path, scenario_path, reset_first=reset_first)


def _run_scripted_agent(
    db_path: Path | str,
    scenario_path: Path | str,
    *,
    reset_first: bool,
) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []

    if reset_first:
        steps.append(_step("reset", reset(db_path, scenario_path)))

    scenario = load_scenario(scenario_path)
    for step in scripted_policy_steps(scenario_path):
        steps.append(_step(step["name"], run_scripted_step(db_path, step)))

    finalization = finalize_to_deadline(db_path, scenario_path)
    evaluation = evaluate(db_path, scenario_path)
    return {
        "ok": evaluation.get("score") == evaluation.get("max_score"),
        "policy": "scripted",
        "steps": steps,
        "finalization": finalization,
        "evaluation": evaluation,
    }


def scripted_policy_steps(scenario_path: Path | str = DEFAULT_SCENARIO_PATH) -> list[dict[str, Any]]:
    return list(load_scenario(scenario_path).get("scripted_policy", []))


def run_scripted_step(db_path: Path | str, step: dict[str, Any]) -> dict[str, Any]:
    tool = step["tool"]
    args = step.get("args", {})
    if tool == "read_doc":
        return read_doc(db_path, args["doc_id"])
    if tool == "update_doc":
        return update_doc(db_path, args["doc_id"], args["body"])
    if tool == "send_chat":
        return send_chat(db_path, args["person_id"], args["body"])
    if tool == "send_email":
        return send_email(db_path, args["person_id"], args["subject"], args["body"])
    if tool == "advance_time":
        return advance_time(db_path, args["target"])
    if tool == "update_task":
        return update_task(db_path, args["task_id"], status=args.get("status"), priority=args.get("priority"))
    if tool == "schedule_meeting":
        return schedule_meeting(
            db_path,
            args["title"],
            args["start_at"],
            args["end_at"],
            args["attendees"],
        )
    raise ValueError(f"Unsupported scripted policy tool: {tool}")


def _step(name: str, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": name,
        "ok": result.get("ok", True),
        "result": result,
    }


@contextmanager
def _scripted_semantic_matcher():
    previous = os.environ.get("PM_SIM_SEMANTIC_MATCHER")
    os.environ["PM_SIM_SEMANTIC_MATCHER"] = "deterministic"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("PM_SIM_SEMANTIC_MATCHER", None)
        else:
            os.environ["PM_SIM_SEMANTIC_MATCHER"] = previous
