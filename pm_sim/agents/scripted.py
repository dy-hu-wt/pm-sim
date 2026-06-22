from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from ..actions import read_doc, send_chat, send_email, update_doc
from ..evaluator import evaluate
from ..paths import DEFAULT_DB_PATH, DEFAULT_SCENARIO_PATH
from ..state import reset
from ..time import advance_time
from .finalize import finalize_to_deadline


StepFn = Callable[[], dict[str, Any]]


def run_scripted_agent(
    db_path: Path | str = DEFAULT_DB_PATH,
    scenario_path: Path | str = DEFAULT_SCENARIO_PATH,
    *,
    reset_first: bool = False,
) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []

    if reset_first:
        steps.append(_step("reset", reset(db_path, scenario_path)))

    scripted_steps: list[tuple[str, StepFn]] = [
        ("read_project_brief", lambda: read_doc(db_path, "doc_project_brief")),
        ("read_rollout_template", lambda: read_doc(db_path, "doc_beta_rollout_template")),
        (
            "ask_luigi_about_repo_sync_risk",
            lambda: send_chat(db_path, "luigi", "Any repo sync blockers or launch risks for Nimbus?"),
        ),
        ("wait_for_luigi_reply", lambda: advance_time(db_path, "2h")),
        (
            "align_daisy_on_draft_mode",
            lambda: send_chat(
                db_path,
                "daisy",
                "Repo sync has stale-code risk. Can we message reliable draft mode for Nimbus?",
            ),
        ),
        ("wait_for_daisy_reply", lambda: advance_time(db_path, "45m")),
        (
            "unblock_peach_on_draft_scope",
            lambda: send_chat(
                db_path,
                "peach",
                "Please finalize draft-mode onboarding with human approval and no auto-commenting.",
            ),
        ),
        ("wait_for_peach_reply", lambda: advance_time(db_path, "90m")),
        (
            "ask_toad_for_draft_mode_approval",
            lambda: send_chat(
                db_path,
                "toad",
                "Repo sync can review stale commits. Approve draft mode for Friday?",
            ),
        ),
        ("wait_for_toad_reply", lambda: advance_time(db_path, "90m")),
        (
            "record_launch_decision",
            lambda: update_doc(
                db_path,
                "doc_launch_decision_record",
                (
                    "Friday launch decision: Toad approved draft mode for Nimbus. "
                    "Draft suggestions require human approval before posting. "
                    "Auto-commenting is out of Friday scope and remains follow-up work. "
                    "Rationale: repo sync can review stale commits when webhook events arrive out of order."
                ),
            ),
        ),
        (
            "send_customer_ready_update",
            lambda: send_email(
                db_path,
                "daisy",
                "Nimbus Friday draft-mode update",
                (
                    "Nimbus can see reliable draft-mode suggestions on Friday. Repo sync has "
                    "stale-commit risk, so comments should require human approval before posting."
                ),
            ),
        ),
        ("wait_for_security_question", lambda: advance_time(db_path, "to:2026-06-24T14:00:00")),
        (
            "ask_luigi_about_security_doc",
            lambda: send_chat(
                db_path,
                "luigi",
                "Nimbus asked if we store source code from private repos. Is there a security doc?",
            ),
        ),
        ("wait_for_security_doc_reply", lambda: advance_time(db_path, "2h")),
        (
            "read_security_baseline",
            lambda: read_doc(db_path, "doc_private_repo_security_baseline"),
        ),
        (
            "send_security_answer",
            lambda: send_email(
                db_path,
                "daisy",
                "Nimbus private repo security answer",
                (
                    "Nimbus can tell their reviewer that private repo source code is processed "
                    "transiently. Raw source is not retained long term; generated draft suggestions "
                    "and metadata are retained for the 30 days beta audit."
                ),
            ),
        ),
    ]

    for name, run_step in scripted_steps:
        steps.append(_step(name, run_step()))

    finalization = finalize_to_deadline(db_path, scenario_path)
    evaluation = evaluate(db_path, scenario_path)
    return {
        "ok": evaluation.get("score") == evaluation.get("max_score"),
        "policy": "scripted",
        "steps": steps,
        "finalization": finalization,
        "evaluation": evaluation,
    }


def _step(name: str, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": name,
        "ok": result.get("ok", True),
        "result": result,
    }
