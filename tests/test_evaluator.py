from __future__ import annotations

import copy
import contextlib
import io
import json
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pm_sim.actions import (
    list_tasks,
    read_doc,
    schedule_meeting,
    send_chat,
    send_email,
    update_doc,
    update_task,
)
from pm_sim.agents.llm import _instructions, llm_session_state, start_llm_session, step_llm_session, run_llm_agent
from pm_sim.agents.scripted import run_scripted_agent
from pm_sim.cli import main as cli_main
from pm_sim.engine.conditions import condition_matches
from pm_sim.coworkers import effects_for_event, replies_for_chat, replies_for_email
from pm_sim.db import connect
from pm_sim.evaluator import evaluate
from pm_sim.engine.effects import apply_effects
from pm_sim.formatters import format_agent_progress_html, format_output, format_concept_progress
from pm_sim.jsonutil import loads
from pm_sim.paths import DEFAULT_SCENARIO_PATH
from pm_sim.scenario import ScenarioError, load_scenario
from pm_sim import concept_match as concept_match_module
from pm_sim.state import action_log, event_log, observe, reset
from pm_sim.engine.time import advance_time
from pm_sim.timeline import timeline
from pm_sim.ui import _html, _run_next_ui_step, _scripted_demo_state, _state_payload

class EvaluatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test.db"
        reset(self.db_path, DEFAULT_SCENARIO_PATH)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _run_cli(self, *args: str) -> str:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = cli_main(["--db", str(self.db_path), *args])
        self.assertEqual(exit_code, 0)
        return output.getvalue()

    def _run_cli_with_stderr(self, *args: str) -> tuple[str, str]:
        output = io.StringIO()
        error = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            exit_code = cli_main(["--db", str(self.db_path), *args])
        self.assertEqual(exit_code, 0)
        return output.getvalue(), error.getvalue()

    def _drive_happy_path(self) -> None:
        send_chat(self.db_path, "luigi", "Any repo sync blockers for launch?")
        advance_time(self.db_path, "until_next_event")
        send_chat(
            self.db_path,
            "daisy",
            "Repo sync has stale-code risk. Can we message reliable draft mode for Nimbus?",
        )
        advance_time(self.db_path, "45m")
        send_chat(
            self.db_path,
            "peach",
            "Please finalize draft-mode onboarding with human approval and no auto-commenting.",
        )
        advance_time(self.db_path, "90m")
        send_chat(
            self.db_path,
            "toad",
            "Repo sync can review stale commits. Approve draft mode for Friday?",
        )
        advance_time(self.db_path, "90m")
        update_doc(
            self.db_path,
            "doc_launch_decision_record",
            (
                "Friday launch decision: Toad approved draft mode for Nimbus. "
                "Draft suggestions require human approval before posting. "
                "Auto-commenting is out of Friday scope and remains follow-up work. "
                "Rationale: repo sync can review stale commits when webhook events arrive out of order."
            ),
        )
        send_email(
            self.db_path,
            "daisy",
            "Nimbus Friday draft-mode update",
            (
                "Nimbus can see reliable draft-mode suggestions on Friday. Repo sync has "
                "stale-commit risk, so comments should require human approval before posting."
            ),
        )
        advance_time(self.db_path, "to:2026-06-24T14:00:00")
        send_chat(
            self.db_path,
            "luigi",
            "Nimbus asked if we store source code from private repos. Is there a security doc?",
        )
        advance_time(self.db_path, "2h")
        read_doc(self.db_path, "doc_private_repo_security_baseline")
        send_email(
            self.db_path,
            "daisy",
            "Nimbus private repo security answer",
            (
                "Nimbus can tell their reviewer that private repo source code is processed "
                "transiently. Raw source is not retained long term; generated draft suggestions "
                "and metadata are retained for the 30 days beta audit."
            ),
        )
        send_chat(
            self.db_path,
            "luigi",
            "Koopa Bank needs admin audit log CSV export clarity for Thursday's security review. Is a one-time CSV feasible without derailing Nimbus?",
        )
        advance_time(self.db_path, "to:2026-06-25T10:30:00")
        send_chat(
            self.db_path,
            "toad",
            "Luigi says a one-time admin audit log CSV is feasible for Koopa, while full self-serve export is follow-up. Can we scope Koopa to the one-time CSV for Thursday so Nimbus launch stays protected?",
        )
        advance_time(self.db_path, "until_next_event")
        send_email(
            self.db_path,
            "daisy",
            "Koopa audit log export scope for Thursday",
            (
                "Koopa can get a one-time CSV export of admin audit logs for Thursday's "
                "security review. Full self-serve export should stay follow-up after Nimbus launch work."
            ),
        )

        advance_time(self.db_path, "to:2026-06-25T12:10:00")
        send_email(
            self.db_path,
            "daisy",
            "Thursday final readiness for Nimbus Friday beta",
            (
                "Final readiness is go for the Nimbus Friday beta. Launch mode is draft mode "
                "with human approval before posting, private repo security wording is covered, "
                "and Koopa stays scoped to a one-time audit CSV so it does not derail the Friday beta."
            ),
        )

    def _project_outcome(self) -> dict[str, Any]:
        conn = connect(self.db_path)
        try:
            project = conn.execute(
                """
                SELECT status, risk_level, metadata_json
                FROM projects
                WHERE id = 'project_pr_review_agent'
                """
            ).fetchone()
            return {
                "status": project["status"],
                "risk_level": project["risk_level"],
                "metadata": loads(project["metadata_json"]),
            }
        finally:
            conn.close()

    def test_reset_state_scores_below_agent_improved_path(self) -> None:
        baseline = evaluate(self.db_path, DEFAULT_SCENARIO_PATH)

        self._drive_happy_path()
        improved = evaluate(self.db_path, DEFAULT_SCENARIO_PATH)

        self.assertLess(baseline["score"], improved["score"])
        self.assertIsNone(baseline["final_outcome"])
        self.assertEqual(improved["score"], improved["max_score"])
        component_scores = {
            component["key"]: component["earned"] for component in improved["components"]
        }
        self.assertEqual(component_scores["blocker_discovery"], 30)
        self.assertEqual(component_scores["risk_handling"], 15)
        delta_ids = {delta["id"] for delta in improved["state_delta"]}
        self.assertIn("blocker_repo_sync_stale", delta_ids)
        self.assertIn("project_pr_review_agent", delta_ids)
        self.assertIn("toad.approval_recorded", delta_ids)

    def test_final_readiness_check_is_required_for_full_score(self) -> None:
        self._drive_happy_path()

        conn = connect(self.db_path)
        try:
            conn.execute(
                """
                UPDATE coworker_state
                SET value_json = ?, updated_at = '2026-06-22T09:00:00'
                WHERE person_id = 'daisy'
                  AND key = 'final_readiness_confirmed'
                """,
                (json.dumps(False),),
            )
            conn.commit()
        finally:
            conn.close()

        result = evaluate(self.db_path, DEFAULT_SCENARIO_PATH)
        risk_component = next(
            component for component in result["components"] if component["key"] == "risk_handling"
        )

        self.assertEqual(result["score"], 115)
        self.assertIn("final_readiness_confirmed", risk_component["missing_milestones"])

    def test_final_readiness_chat_can_confirm_go_no_go(self) -> None:
        self._drive_happy_path()

        conn = connect(self.db_path)
        try:
            conn.execute(
                """
                UPDATE coworker_state
                SET value_json = ?, updated_at = '2026-06-22T09:00:00'
                WHERE person_id = 'daisy'
                  AND key = 'final_readiness_confirmed'
                """,
                (json.dumps(False),),
            )
            conn.commit()
        finally:
            conn.close()

        send_chat(
            self.db_path,
            "daisy",
            (
                "Go for Friday in draft mode. Nimbus beta launch mode is draft mode with "
                "human approval, private repo security wording is covered, and Koopa audit "
                "CSV scope stays one-time so it does not derail the Friday beta."
            ),
        )

        result = evaluate(self.db_path, DEFAULT_SCENARIO_PATH)
        risk_component = next(
            component for component in result["components"] if component["key"] == "risk_handling"
        )

        self.assertEqual(result["score"], 120)
        self.assertNotIn("final_readiness_confirmed", risk_component["missing_milestones"])

    def test_busywork_does_not_score_like_good_pm_work(self) -> None:
        send_chat(self.db_path, "mario", "I am checking on the Friday launch.")
        send_chat(self.db_path, "luigi", "How are things going this week?")
        send_chat(self.db_path, "peach", "Please keep making progress on onboarding.")
        update_task(self.db_path, "task_launch_decision", status="in_progress", priority=None)
        update_task(self.db_path, "task_customer_talk_track", status="in_progress", priority=None)
        send_email(
            self.db_path,
            "daisy",
            "Friday status",
            "We are actively checking on the beta and will follow up with details.",
        )

        advance_time(self.db_path, "to:2026-06-26T15:00:00")

        result = evaluate(self.db_path, DEFAULT_SCENARIO_PATH)
        stakeholder_component = next(
            component
            for component in result["components"]
            if component["key"] == "stakeholder_communication"
        )
        outcome = self._project_outcome()

        self.assertLess(result["score"], result["max_score"])
        self.assertIn("customer_message_ready", stakeholder_component["missing_milestones"])
        self.assertNotEqual(outcome["metadata"]["final_outcome"], "draft_mode_beta_shipped")

    def test_late_milestone_gets_partial_timing_credit(self) -> None:
        advance_time(self.db_path, "to:2026-06-25T10:00:00")

        result = evaluate(self.db_path, DEFAULT_SCENARIO_PATH)
        blocker_component = next(
            component
            for component in result["components"]
            if component["key"] == "blocker_discovery"
        )

        self.assertEqual(blocker_component["earned"], 15)
        self.assertEqual(blocker_component["status"], "partial")

    def test_invalid_repo_sync_completion_is_rejected_before_evaluation(self) -> None:
        result = update_task(self.db_path, "task_repo_sync", status="complete", priority=None)

        evaluation = evaluate(self.db_path, DEFAULT_SCENARIO_PATH)
        harmful_component = next(
            component
            for component in evaluation["components"]
            if component["key"] == "avoid_harmful_actions"
        )

        self.assertFalse(result["ok"])
        self.assertEqual(harmful_component["earned"], 15)
        self.assertEqual(harmful_component["detected_harms"], [])

    def test_invalid_repo_sync_state_is_still_penalized_by_evaluator(self) -> None:
        conn = connect(self.db_path)
        try:
            apply_effects(
                conn,
                [
                    {
                        "type": "update_task",
                        "task_id": "task_repo_sync",
                        "status": "complete",
                    }
                ],
                now="2026-06-22T10:00:00",
                source="test:invalid_state",
            )
            conn.commit()
        finally:
            conn.close()

        result = evaluate(self.db_path, DEFAULT_SCENARIO_PATH)
        harmful_component = next(
            component
            for component in result["components"]
            if component["key"] == "avoid_harmful_actions"
        )

        self.assertEqual(harmful_component["earned"], 0)
        self.assertTrue(harmful_component["detected_harms"])

    def test_excessive_direct_outreach_gets_light_penalty(self) -> None:
        self._drive_happy_path()
        for index in range(20):
            send_chat(self.db_path, "mario", f"Extra low-signal check-in {index}.")

        result = evaluate(self.db_path, DEFAULT_SCENARIO_PATH)
        harmful_component = next(
            component
            for component in result["components"]
            if component["key"] == "avoid_harmful_actions"
        )

        self.assertEqual(result["score"], 115)
        self.assertEqual(harmful_component["earned"], 10)
        self.assertEqual(harmful_component["coordination_penalty"], 5)
        self.assertIn("excessive direct outreach", harmful_component["note"])

    def test_substantive_daisy_email_can_satisfy_stakeholder_communication(self) -> None:
        send_chat(self.db_path, "luigi", "Any repo sync blockers for launch?")
        advance_time(self.db_path, "until_next_event")
        send_chat(
            self.db_path,
            "daisy",
            "Repo sync has stale-code risk. Can we message reliable draft mode for Nimbus?",
        )
        advance_time(self.db_path, "45m")
        send_chat(
            self.db_path,
            "toad",
            "Repo sync can review stale commits. Approve draft mode for Friday?",
        )
        advance_time(self.db_path, "90m")
        send_email(
            self.db_path,
            "daisy",
            "Nimbus Friday draft-mode status",
            (
                "Repo sync has stale-commit risk. I recommend reliable draft mode "
                "for Friday with human approval."
            ),
        )

        result = evaluate(self.db_path, DEFAULT_SCENARIO_PATH)
        stakeholder_component = next(
            component
            for component in result["components"]
            if component["key"] == "stakeholder_communication"
        )

        self.assertEqual(stakeholder_component["earned"], 20)
        self.assertEqual(
            {evidence["key"] for evidence in stakeholder_component["milestones"]},
            {"stakeholder_alignment", "customer_message_ready"},
        )

    def test_daisy_email_without_human_approval_is_not_customer_ready(self) -> None:
        send_chat(self.db_path, "luigi", "Any repo sync blockers for launch?")
        advance_time(self.db_path, "until_next_event")
        send_chat(
            self.db_path,
            "daisy",
            "Repo sync has stale-code risk. Can we message reliable draft mode for Nimbus?",
        )
        advance_time(self.db_path, "45m")
        send_email(
            self.db_path,
            "daisy",
            "Nimbus Friday draft-mode status",
            "Repo sync has stale-commit risk. I recommend reliable draft mode for Friday.",
        )

        result = evaluate(self.db_path, DEFAULT_SCENARIO_PATH)
        stakeholder_component = next(
            component
            for component in result["components"]
            if component["key"] == "stakeholder_communication"
        )

        self.assertEqual(stakeholder_component["earned"], 10)
        self.assertIn("customer_message_ready", stakeholder_component["missing_milestones"])

    def test_fake_draft_mode_progress_does_not_improve_task_score(self) -> None:
        baseline = evaluate(self.db_path, DEFAULT_SCENARIO_PATH)

        update_result = update_task(
            self.db_path,
            "task_draft_mode_docs",
            status="complete",
            priority=None,
        )
        result = evaluate(self.db_path, DEFAULT_SCENARIO_PATH)
        task_component = next(
            component
            for component in result["components"]
            if component["key"] == "task_state_improvement"
        )

        self.assertFalse(update_result["ok"])
        self.assertEqual(result["score"], baseline["score"])
        self.assertEqual(task_component["earned"], 0)
        self.assertIn("peach_unblocked", task_component["missing_milestones"])

    def test_draft_mode_progress_counts_only_after_peach_state_records_unblock(self) -> None:
        update_task(self.db_path, "task_draft_mode_docs", status="in_progress", priority=None)

        conn = connect(self.db_path)
        try:
            apply_effects(
                conn,
                [
                    {
                        "type": "discover_fact",
                        "fact_id": "fact_draft_mode_scope_confirmed",
                    }
                ],
                now="2026-06-22T10:00:00",
                source="test:scope_known",
            )
            conn.commit()
        finally:
            conn.close()

        fact_only = evaluate(self.db_path, DEFAULT_SCENARIO_PATH)
        fact_only_component = next(
            component
            for component in fact_only["components"]
            if component["key"] == "task_state_improvement"
        )
        self.assertEqual(fact_only_component["earned"], 0)

        conn = connect(self.db_path)
        try:
            apply_effects(
                conn,
                [
                    {
                        "type": "update_blocker",
                        "blocker_id": "blocker_scope_unclear",
                        "status": "resolved",
                    }
                ],
                now="2026-06-22T10:15:00",
                source="test:scope_resolved",
            )
            conn.commit()
        finally:
            conn.close()

        fact_and_blocker = evaluate(self.db_path, DEFAULT_SCENARIO_PATH)
        fact_and_blocker_component = next(
            component
            for component in fact_and_blocker["components"]
            if component["key"] == "task_state_improvement"
        )
        self.assertEqual(fact_and_blocker_component["earned"], 0)

        conn = connect(self.db_path)
        try:
            apply_effects(
                conn,
                [
                    {
                        "type": "update_coworker_state",
                        "person_id": "peach",
                        "key": "scope_unblocked",
                        "value": True,
                    }
                ],
                now="2026-06-22T10:20:00",
                source="test:peach_unblocked",
            )
            conn.commit()
        finally:
            conn.close()

        valid = evaluate(self.db_path, DEFAULT_SCENARIO_PATH)
        valid_component = next(
            component
            for component in valid["components"]
            if component["key"] == "task_state_improvement"
        )

        self.assertEqual(valid_component["earned"], 10)
        self.assertEqual(valid_component["status"], "partial")
        self.assertEqual(valid_component["milestones"][0]["key"], "peach_unblocked")

    def test_evaluate_explain_prints_component_milestones(self) -> None:
        self._drive_happy_path()

        output = self._run_cli("evaluate", "--explain")

        self.assertIn("Evaluation Explanation", output)
        self.assertIn("Score:", output)
        self.assertIn("Outcome Comparison:", output)
        self.assertIn("Critical Path:", output)
        self.assertIn("blocker_discovery", output)
        self.assertIn("stakeholder_communication", output)
        self.assertIn("task_state_improvement", output)
        self.assertIn("risk_handling", output)
        self.assertIn("security_interruption", output)
        self.assertIn("portfolio_tradeoff", output)
        self.assertIn("Milestones:", output)
        self.assertIn("State Improvements:", output)
        self.assertIn("blocker blocker_repo_sync_stale", output)
        self.assertIn("Luigi has surfaced the stale repo-sync risk.", output)
        self.assertIn("Daisy has received grounded customer-ready Nimbus beta wording.", output)
        self.assertIn("Toad has recorded approval for draft mode.", output)
        self.assertIn("Luigi has shared the private repo security baseline.", output)

    def test_evaluator_includes_outcome_comparison_and_critical_path(self) -> None:
        result = evaluate(self.db_path, DEFAULT_SCENARIO_PATH)

        comparison = result["outcome_comparison"]
        critical_path = result["critical_path"]

        self.assertEqual(comparison["baseline_expected_score"], 15)
        self.assertEqual(comparison["actual_outcome"], None)
        self.assertFalse(comparison["improved_over_baseline"])
        self.assertGreaterEqual(critical_path["blocked_count"], 1)
        self.assertGreaterEqual(critical_path["dependency_count"], 1)

    def test_scored_milestone_trace_points_to_action_log(self) -> None:
        self._drive_happy_path()
        result = evaluate(self.db_path, DEFAULT_SCENARIO_PATH)
        risk = next(component for component in result["components"] if component["key"] == "risk_handling")
        milestone = next(item for item in risk["milestones"] if item["key"] == "decision_record_written")

        self.assertEqual(milestone["trace"]["source_type"], "action")
        self.assertEqual(milestone["trace"]["action_type"], "update_doc")
        self.assertEqual(milestone["trace"]["actor"], "agent")

    def test_evaluate_explain_prints_missing_milestones(self) -> None:
        output = self._run_cli("evaluate", "--explain")

        self.assertIn("Score:", output)
        self.assertIn("stakeholder_communication", output)
        self.assertIn("Missing: stakeholder_alignment, customer_message_ready", output)
        self.assertIn("Missing: peach_unblocked, draft_mode_approved", output)
        self.assertIn("Missing: security_doc_found, security_question_answered", output)
        self.assertIn("Missing: koopa_scoped, koopa_update_sent", output)
        self.assertIn("Failed gates:", output)
        self.assertIn("daisy.customer_message_ready", output)
        self.assertIn("fact fact_repo_sync_stale discovered", output)

    def test_documented_noop_baseline_path_is_runnable(self) -> None:
        self._run_cli("reset")
        self._run_cli("advance-time", "to:2026-06-26T15:00:00")

        evaluation = self._run_cli("evaluate", "--explain")
        outcome = self._run_cli("read-doc", "doc_friday_outcome")

        self.assertIn("Score:", evaluation)
        self.assertIn("Late milestones: blocker_discovered.", evaluation)
        self.assertIn("Luigi has surfaced the stale repo-sync risk.", evaluation)
        self.assertIn("Missing: stakeholder_alignment, customer_message_ready", evaluation)
        self.assertIn("Friday Outcome", outcome)
        self.assertIn("Friday arrived without an approved reliable launch plan.", outcome)

    def test_documented_meeting_path_is_runnable(self) -> None:
        self._run_cli("reset")
        self._run_cli("read-doc", "doc_project_brief")
        self._run_cli("read-doc", "doc_beta_rollout_template")
        self._run_cli(
            "schedule-meeting",
            "Draft-mode risk review for Nimbus launch",
            "2026-06-22T10:00:00",
            "2026-06-22T10:30:00",
            "luigi",
            "daisy",
            "mario",
            "peach",
            "toad",
        )
        self._run_cli("advance-time", "to:2026-06-22T10:30:00")
        transcript = self._run_cli("read-doc", "doc_transcript_cal_1")
        self._run_cli(
            "update-doc",
            "doc_launch_decision_record",
            (
                "Friday launch decision: Toad approved draft mode for Nimbus. "
                "Draft suggestions require human approval before posting. "
                "Auto-commenting is out of Friday scope and remains follow-up work. "
                "Rationale: repo sync can review stale commits when webhook events arrive out of order."
            ),
        )
        self._run_cli(
            "send-email",
            "daisy",
            "Nimbus Friday draft-mode update",
            (
                "Nimbus can see reliable draft-mode suggestions on Friday. Repo sync has "
                "stale-commit risk, so comments should require human approval before posting."
            ),
        )
        self._run_cli("advance-time", "to:2026-06-24T14:00:00")
        self._run_cli(
            "send-chat",
            "luigi",
            "Nimbus asked if we store source code from private repos. Is there a security doc?",
        )
        self._run_cli("advance-time", "2h")
        self._run_cli("read-doc", "doc_private_repo_security_baseline")
        self._run_cli(
            "send-email",
            "daisy",
            "Nimbus private repo security answer",
            (
                "Nimbus can tell their reviewer that private repo source code is processed "
                "transiently. Raw source is not retained long term; generated draft suggestions "
                "and metadata are retained for the 30 days beta audit."
            ),
        )
        self._run_cli(
            "send-chat",
            "luigi",
            "Koopa Bank needs admin audit log CSV export clarity for Thursday's security review. Is a one-time CSV feasible without derailing Nimbus?",
        )
        self._run_cli("advance-time", "to:2026-06-25T10:30:00")
        self._run_cli(
            "send-chat",
            "toad",
            "Luigi says a one-time admin audit log CSV is feasible for Koopa, while full self-serve export is follow-up. Can we scope Koopa to the one-time CSV for Thursday so Nimbus launch stays protected?",
        )
        self._run_cli("advance-time", "until_next_event")
        self._run_cli(
            "send-email",
            "daisy",
            "Koopa audit log export scope for Thursday",
            (
                "Koopa can get a one-time CSV export of admin audit logs for Thursday's "
                "security review. Full self-serve export should stay follow-up after Nimbus launch work."
            ),
        )
        self._run_cli("advance-time", "to:2026-06-25T12:10:00")
        self._run_cli(
            "send-email",
            "daisy",
            "Thursday final readiness for Nimbus Friday beta",
            (
                "Final readiness is go for the Nimbus Friday beta. Launch mode is draft mode "
                "with human approval before posting, private repo security wording is covered, "
                "and Koopa stays scoped to a one-time audit CSV so it does not derail the Friday beta."
            ),
        )

        before_deadline = self._run_cli("evaluate", "--explain")
        self._run_cli("advance-time", "to:2026-06-26T15:00:00")
        after_deadline = self._run_cli("evaluate")

        self.assertIn("Transcript: Draft-mode risk review for Nimbus launch", transcript)
        self.assertIn("Toad approved draft mode", transcript)
        self.assertIn("Score:", before_deadline)
        self.assertIn("Outcome:  draft_mode_beta_shipped", after_deadline)
