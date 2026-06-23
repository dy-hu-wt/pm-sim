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
from pm_sim.conditions import condition_matches
from pm_sim.coworkers import effects_for_event, replies_for_chat, replies_for_email
from pm_sim.db import connect
from pm_sim.evaluator import evaluate
from pm_sim.effects import apply_effects
from pm_sim.formatters import format_agent_progress_html, format_output, format_semantic_progress
from pm_sim.jsonutil import loads
from pm_sim.paths import DEFAULT_SCENARIO_PATH
from pm_sim.report import generate_report
from pm_sim.scenario import ScenarioError, load_scenario
from pm_sim import semantic_match as semantic_match_module
from pm_sim.state import action_log, event_log, observe, reset
from pm_sim.time import advance_time
from pm_sim.timeline import timeline
from pm_sim.ui import _html, _run_next_ui_step, _scripted_demo_state, _state_payload
from tests.helpers import FakeResponsesClient, function_call

class CoreSimulationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test.db"
        reset(self.db_path, DEFAULT_SCENARIO_PATH)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_reset_loads_scenario_state(self) -> None:
        state = observe(self.db_path)

        self.assertEqual(state["scenario_id"], "launch_readiness")
        self.assertEqual(state["current_time"], "2026-06-22T09:00:00")
        self.assertEqual(len(state["people"]), 5)
        self.assertEqual(len(state["projects"]), 2)
        self.assertGreaterEqual(len(state["tasks"]), 5)
        people = {
            person["id"]: loads(person["behavior_json"])
            for person in state["people"]
        }
        self.assertEqual(people["luigi"]["current_focus"], "repo sync hardening and private-repo security details")
        self.assertIn("recommended safer Friday scope", people["toad"]["needs_from_pm"])
        project_ids = {project["id"] for project in state["projects"]}
        self.assertIn("project_pr_review_agent", project_ids)
        self.assertIn("project_audit_log_export", project_ids)
        coworker_state = {
            (row["person_id"], row["key"]): loads(row["value_json"])
            for row in state["coworker_state"]
        }
        self.assertFalse(coworker_state[("luigi", "risk_surfaced")])
        self.assertFalse(coworker_state[("peach", "scope_unblocked")])
        self.assertFalse(coworker_state[("toad", "approval_recorded")])
        self.assertFalse(coworker_state[("daisy", "koopa_update_received")])
        obligations = {row["title"] for row in state["calendar_obligations"]}
        self.assertIn("Daisy final Nimbus go/no-go", obligations)
        self.assertIn("Admin Audit Log Export deadline", obligations)
        self.assertIn("PR Review Agent Beta deadline", obligations)

    def test_reset_seeds_open_launch_conflict(self) -> None:
        conflict = self._project_metadata()["launch_conflict"]

        self.assertEqual(conflict["status"], "open")
        self.assertIsNone(conflict["resolution"])
        self.assertTrue(conflict["inputs"]["product_pressure_acknowledged"])
        self.assertFalse(conflict["inputs"]["technical_risk_substantiated"])
        self.assertFalse(conflict["inputs"]["customer_constraint_known"])
        self.assertFalse(conflict["inputs"]["implementation_scope_clear"])

    def test_hidden_blocker_and_hidden_fact_are_not_observed_initially(self) -> None:
        state = observe(self.db_path)

        blocker_ids = {blocker["id"] for blocker in state["known_blockers"]}
        fact_ids = {fact["id"] for fact in state["discovered_facts"]}

        self.assertNotIn("blocker_repo_sync_stale", blocker_ids)
        self.assertNotIn("fact_repo_sync_stale", fact_ids)

    def test_advance_time_by_duration_does_not_deliver_future_events(self) -> None:
        result = advance_time(self.db_path, "2h")
        state = observe(self.db_path)

        self.assertEqual(result["from"], "2026-06-22T09:00:00")
        self.assertEqual(result["to"], "2026-06-22T11:00:00")
        self.assertEqual(result["delivered_events"], [])
        self.assertEqual(state["current_time"], "2026-06-22T11:00:00")

    def test_advance_until_next_event_delivers_one_due_event(self) -> None:
        result = advance_time(self.db_path, "until_next_event")
        state = observe(self.db_path)

        self.assertEqual(result["to"], "2026-06-23T10:00:00")
        self.assertEqual(len(result["delivered_events"]), 1)
        self.assertEqual(result["delivered_events"][0]["id"], "event_mario_auto_comment_push")
        self.assertTrue(result["delivered_events"][0]["result"]["handled"])
        self.assertEqual(state["current_time"], "2026-06-23T10:00:00")

    def test_background_event_applies_coworker_effects(self) -> None:
        advance_time(self.db_path, "to:2026-06-25T10:00:00")
        state = observe(self.db_path)
        conflict = self._project_metadata()["launch_conflict"]

        blocker_ids = {blocker["id"] for blocker in state["known_blockers"]}
        fact_ids = {fact["id"] for fact in state["discovered_facts"]}
        recent_bodies = [message["body"] for message in state["recent_messages"]]

        self.assertIn("blocker_repo_sync_stale", blocker_ids)
        self.assertIn("fact_repo_sync_stale", fact_ids)
        self.assertTrue(any("repo sync" in body for body in recent_bodies))
        self.assertEqual(conflict["status"], "investigated")
        self.assertTrue(conflict["inputs"]["technical_risk_substantiated"])
        coworker_state = {
            (row["person_id"], row["key"]): loads(row["value_json"])
            for row in state["coworker_state"]
        }
        self.assertTrue(coworker_state[("luigi", "risk_surfaced")])

    def test_agent_path_moves_launch_conflict_to_resolved_draft_mode(self) -> None:
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

        conflict = self._project_metadata()["launch_conflict"]

        self.assertEqual(conflict["status"], "resolved")
        self.assertEqual(conflict["resolution"], "draft_mode")
        self.assertEqual(conflict["final_launch_mode"], "draft_mode")
        self.assertTrue(conflict["inputs"]["product_pressure_acknowledged"])
        self.assertTrue(conflict["inputs"]["technical_risk_substantiated"])
        self.assertTrue(conflict["inputs"]["customer_constraint_known"])
        self.assertTrue(conflict["inputs"]["implementation_scope_clear"])

    def test_customer_launch_mode_question_adds_pressure_event(self) -> None:
        result = advance_time(self.db_path, "to:2026-06-24T15:30:00")
        state = observe(self.db_path)

        event_types = {event["event_type"] for event in result["delivered_events"]}
        recent = state["recent_messages"][0]

        self.assertIn("nimbus_launch_mode_question", event_types)
        self.assertEqual(recent["sender_id"], "daisy")
        self.assertEqual(recent["channel"], "email")
        self.assertIn("post comments automatically", recent["body"])

    def test_private_repo_security_question_arrives_as_background_event(self) -> None:
        result = advance_time(self.db_path, "to:2026-06-24T14:00:00")
        state = observe(self.db_path)

        event_types = {event["event_type"] for event in result["delivered_events"]}
        recent = state["recent_messages"][0]

        self.assertIn("daisy_private_repo_security_question", event_types)
        self.assertEqual(recent["sender_id"], "daisy")
        self.assertEqual(recent["channel"], "email")
        self.assertIn("stores source code", recent["body"])

    def test_koopa_audit_export_note_is_revealed_by_async_request(self) -> None:
        hidden = read_doc(self.db_path, "doc_koopa_audit_export_note")

        result = advance_time(self.db_path, "to:2026-06-24T10:00:00")
        revealed = read_doc(self.db_path, "doc_koopa_audit_export_note")

        event_types = {event["event_type"] for event in result["delivered_events"]}

        self.assertFalse(hidden["ok"])
        self.assertIn("koopa_audit_export_request", event_types)
        self.assertTrue(revealed["ok"])
        self.assertIn("one-time CSV", revealed["doc"]["body"])

    def test_can_deliver_all_seeded_events_through_friday_deadline(self) -> None:
        result = advance_time(self.db_path, "to:2026-06-26T15:00:00")

        event_types = {event["event_type"] for event in result["delivered_events"]}
        conn = connect(self.db_path)
        try:
            project = conn.execute(
                """
                SELECT status, risk_level, metadata_json
                FROM projects
                WHERE id = 'project_pr_review_agent'
                """
            ).fetchone()
            outcome_doc = conn.execute(
                """
                SELECT kind, body, visibility_scope
                FROM docs
                WHERE id = 'doc_friday_outcome'
                """
            ).fetchone()
            koopa_project = conn.execute(
                """
                SELECT status, risk_level, metadata_json
                FROM projects
                WHERE id = 'project_audit_log_export'
                """
            ).fetchone()
            koopa_outcome_doc = conn.execute(
                """
                SELECT kind, body, visibility_scope
                FROM docs
                WHERE id = 'doc_koopa_audit_export_outcome'
                """
            ).fetchone()
        finally:
            conn.close()

        self.assertIn("project_deadline", event_types)
        self.assertTrue(all(event["result"]["handled"] for event in result["delivered_events"]))
        self.assertEqual(project["status"], "missed")
        self.assertEqual(project["risk_level"], "high")
        self.assertEqual(loads(project["metadata_json"])["final_outcome"], "no_approved_friday_plan")
        self.assertEqual(outcome_doc["kind"], "outcome_report")
        self.assertEqual(outcome_doc["visibility_scope"], "generated")
        self.assertIn("without an approved reliable launch plan", outcome_doc["body"])
        self.assertEqual(koopa_project["status"], "at_risk")
        self.assertEqual(koopa_project["risk_level"], "medium")
        self.assertEqual(
            loads(koopa_project["metadata_json"])["final_outcome"],
            "koopa_audit_scope_unresolved",
        )
        self.assertEqual(koopa_outcome_doc["kind"], "outcome_report")
        self.assertEqual(koopa_outcome_doc["visibility_scope"], "generated")
        self.assertIn("without a clear scoped answer", koopa_outcome_doc["body"])

        evaluation = evaluate(self.db_path, DEFAULT_SCENARIO_PATH)
        self.assertEqual(evaluation["final_outcome"]["project_id"], "project_pr_review_agent")
        self.assertEqual(evaluation["final_outcome"]["outcome"], "no_approved_friday_plan")
        self.assertEqual(evaluation["final_outcome"]["project_status"], "missed")

    def test_friday_deadline_records_successful_draft_mode_outcome(self) -> None:
        schedule_meeting(
            self.db_path,
            "Draft-mode risk review for Nimbus launch",
            "2026-06-22T10:00:00",
            "2026-06-22T10:30:00",
            ["luigi", "daisy", "mario", "toad", "peach"],
        )
        advance_time(self.db_path, "to:2026-06-22T10:30:00")
        self._send_customer_ready_email()

        advance_time(self.db_path, "to:2026-06-26T15:00:00")

        conn = connect(self.db_path)
        try:
            project = conn.execute(
                """
                SELECT status, risk_level, metadata_json
                FROM projects
                WHERE id = 'project_pr_review_agent'
                """
            ).fetchone()
            outcome_doc = conn.execute(
                """
                SELECT body
                FROM docs
                WHERE id = 'doc_friday_outcome'
                """
            ).fetchone()
        finally:
            conn.close()

        metadata = loads(project["metadata_json"])
        self.assertEqual(project["status"], "shipped")
        self.assertEqual(project["risk_level"], "low")
        self.assertEqual(metadata["final_outcome"], "draft_mode_beta_shipped")
        self.assertEqual(outcome_doc["body"], "")

    def test_koopa_audit_export_can_be_scoped_and_closed_before_deadline(self) -> None:
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
        advance_time(self.db_path, "to:2026-06-25T16:00:00")

        conn = connect(self.db_path)
        try:
            project = conn.execute(
                """
                SELECT status, risk_level, metadata_json
                FROM projects
                WHERE id = 'project_audit_log_export'
                """
            ).fetchone()
            blocker = conn.execute(
                "SELECT status FROM blockers WHERE id = 'blocker_audit_export_scope_unclear'"
            ).fetchone()
            outcome_doc = conn.execute(
                """
                SELECT body
                FROM docs
                WHERE id = 'doc_koopa_audit_export_outcome'
                """
            ).fetchone()
        finally:
            conn.close()

        metadata = loads(project["metadata_json"])
        self.assertEqual(project["status"], "active")
        self.assertEqual(project["risk_level"], "low")
        self.assertEqual(metadata["final_outcome"], "koopa_audit_update_ready")
        self.assertEqual(blocker["status"], "resolved")
        self.assertIn("one-time CSV", outcome_doc["body"])

    def test_friday_deadline_records_late_draft_mode_outcome(self) -> None:
        schedule_meeting(
            self.db_path,
            "Late draft-mode risk review for Nimbus launch",
            "2026-06-25T16:00:00",
            "2026-06-25T16:30:00",
            ["luigi", "daisy", "mario", "toad", "peach"],
        )
        advance_time(self.db_path, "to:2026-06-25T16:30:00")
        self._send_customer_ready_email()

        advance_time(self.db_path, "to:2026-06-26T15:00:00")

        outcome = self._project_outcome()

        self.assertEqual(outcome["status"], "partial")
        self.assertEqual(outcome["risk_level"], "medium")
        self.assertEqual(outcome["metadata"]["final_outcome"], "late_draft_mode")
        self.assertIn("landed late", outcome["metadata"]["final_outcome_summary"])

    def test_friday_deadline_does_not_ship_from_fake_task_completion(self) -> None:
        conn = connect(self.db_path)
        try:
            apply_effects(
                conn,
                [
                    {
                        "type": "update_task",
                        "task_id": "task_draft_mode_docs",
                        "status": "complete",
                    },
                    {
                        "type": "discover_fact",
                        "fact_id": "fact_draft_mode_approved",
                    },
                    {
                        "type": "add_evaluation_evidence",
                        "key": "stakeholder_alignment",
                        "note": "Test-only customer alignment.",
                    },
                ],
                now="2026-06-24T10:00:00",
                source="test:fake_task_completion",
            )
            conn.commit()
        finally:
            conn.close()

        advance_time(self.db_path, "to:2026-06-26T15:00:00")

        outcome = self._project_outcome()

        self.assertEqual(outcome["status"], "missed")
        self.assertEqual(outcome["risk_level"], "high")
        self.assertEqual(outcome["metadata"]["final_outcome"], "missed_due_to_blockers")

    def test_friday_deadline_records_risky_auto_commenting_outcome(self) -> None:
        conn = connect(self.db_path)
        try:
            apply_effects(
                conn,
                [
                    {
                        "type": "update_project",
                        "project_id": "project_pr_review_agent",
                        "decision": "auto_commenting_approved",
                    }
                ],
                now="2026-06-24T10:00:00",
                source="test:auto_commenting_commitment",
            )
            conn.commit()
        finally:
            conn.close()

        advance_time(self.db_path, "to:2026-06-26T15:00:00")

        outcome = self._project_outcome()

        self.assertEqual(outcome["status"], "shipped")
        self.assertEqual(outcome["risk_level"], "high")
        self.assertEqual(outcome["metadata"]["final_outcome"], "risky_auto_commenting")

    def test_friday_deadline_requires_customer_ready_email_for_clean_ship(self) -> None:
        schedule_meeting(
            self.db_path,
            "Draft-mode risk review for Nimbus launch",
            "2026-06-22T10:00:00",
            "2026-06-22T10:30:00",
            ["luigi", "daisy", "mario", "toad", "peach"],
        )

        advance_time(self.db_path, "to:2026-06-26T15:00:00")

        outcome = self._project_outcome()

        self.assertEqual(outcome["status"], "missed")
        self.assertEqual(outcome["risk_level"], "high")
        self.assertEqual(outcome["metadata"]["final_outcome"], "missed_due_to_blockers")

    def test_events_delivered_during_large_time_jump_keep_scheduled_times(self) -> None:
        schedule_meeting(
            self.db_path,
            "Draft-mode risk review for Nimbus launch",
            "2026-06-22T10:00:00",
            "2026-06-22T10:30:00",
            ["luigi", "daisy", "mario", "toad", "peach"],
        )

        result = advance_time(self.db_path, "to:2026-06-22T10:30:00")
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
        self._send_customer_ready_email()
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
        advance_time(self.db_path, "to:2026-06-26T15:00:00")
        meeting_event = next(
            event for event in result["delivered_events"] if event["event_type"] == "meeting_occurs"
        )
        evaluation = evaluate(self.db_path, DEFAULT_SCENARIO_PATH)

        self.assertEqual(meeting_event["delivered_at"], "2026-06-22T10:30:00")
        self.assertEqual(evaluation["score"], evaluation["max_score"])

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

    def _project_metadata(self) -> dict[str, Any]:
        conn = connect(self.db_path)
        try:
            project = conn.execute(
                """
                SELECT metadata_json
                FROM projects
                WHERE id = 'project_pr_review_agent'
                """
            ).fetchone()
            return loads(project["metadata_json"])
        finally:
            conn.close()

    def _send_customer_ready_email(self) -> None:
        send_email(
            self.db_path,
            "daisy",
            "Nimbus Friday draft-mode update",
            (
                "Nimbus can see reliable draft-mode suggestions on Friday. Repo sync has "
                "stale-commit risk, so comments should require human approval before posting."
            ),
        )

    def _answer_security_question(self) -> None:
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

    def test_timeline_shows_actions_events_messages_and_evidence_in_order(self) -> None:
        send_chat(self.db_path, "luigi", "Any repo sync blockers for launch?")
        advance_time(self.db_path, "until_next_event")

        entries = timeline(self.db_path, limit=0)
        kinds = {entry["kind"] for entry in entries}
        times = [entry["time"] for entry in entries]

        self.assertEqual(times, sorted(times))
        self.assertIn("action", kinds)
        self.assertIn("event_scheduled", kinds)
        self.assertIn("event_delivered", kinds)
        self.assertIn("message", kinds)
        self.assertIn("evidence", kinds)

        delivered = [
            entry
            for entry in entries
            if entry["kind"] == "event_delivered" and entry["event_type"] == "coworker_reply"
        ]
        evidence = [
            entry
            for entry in entries
            if entry["kind"] == "evidence" and entry["evidence_key"] == "blocker_discovered"
        ]

        self.assertEqual(len(delivered), 1)
        self.assertTrue(delivered[0]["result"]["applied_effects"])
        self.assertTrue(evidence)

    def test_timeline_filters_by_kind(self) -> None:
        send_chat(self.db_path, "luigi", "Any repo sync blockers for launch?")
        advance_time(self.db_path, "until_next_event")

        actions = timeline(self.db_path, kind="action")
        events = timeline(self.db_path, kind="event")
        messages = timeline(self.db_path, kind="message")
        evidence = timeline(self.db_path, kind="evidence")

        self.assertTrue(actions)
        self.assertTrue(events)
        self.assertTrue(messages)
        self.assertTrue(evidence)
        self.assertEqual({entry["kind"] for entry in actions}, {"action"})
        self.assertTrue(all(entry["kind"].startswith("event_") for entry in events))
        self.assertEqual({entry["kind"] for entry in messages}, {"message"})
        self.assertEqual({entry["kind"] for entry in evidence}, {"evidence"})

    def test_static_ui_writes_operator_html(self) -> None:
        send_chat(self.db_path, "luigi", "Any repo sync blockers for launch?")
        advance_time(self.db_path, "until_next_event")
        output_path = Path(self.tmpdir.name) / "operator_ui.html"

        result = generate_report(self.db_path, DEFAULT_SCENARIO_PATH, output_path, timeline_limit=20)
        html = output_path.read_text(encoding="utf-8")

        self.assertTrue(result["ok"])
        self.assertEqual(result["path"], str(output_path))
        self.assertIn("PM Sim Operator UI", html)
        self.assertIn("Playback", html)
        self.assertIn("Evaluation", html)
        self.assertIn("Timeline", html)
        self.assertIn("Action Log", html)
        self.assertIn("blocker_discovered", html)

    def test_live_ui_play_steps_run_scripted_pm_path(self) -> None:
        initial = _scripted_demo_state(self.db_path, DEFAULT_SCENARIO_PATH)
        self.assertEqual(initial["index"], 0)
        self.assertFalse(initial["done"])

        result = {"done": False}
        guard = 0
        while not result["done"]:
            result = _run_next_ui_step(self.db_path, DEFAULT_SCENARIO_PATH)
            guard += 1
            self.assertLess(guard, 100)

        final = _scripted_demo_state(self.db_path, DEFAULT_SCENARIO_PATH)
        evaluation = evaluate(self.db_path, DEFAULT_SCENARIO_PATH)

        self.assertTrue(final["done"])
        self.assertEqual(final["index"], final["total"])
        self.assertIn("score", evaluation)
        self.assertIn("max_score", evaluation)
        self.assertGreater(evaluation["max_score"], 0)

    def test_live_ui_log_uses_pretty_agent_progress(self) -> None:
        _run_next_ui_step(self.db_path, DEFAULT_SCENARIO_PATH)

        payload = _state_payload(self.db_path, DEFAULT_SCENARIO_PATH, timeline_limit=20)
        log_lines = payload["log_lines"]
        log_entries = payload["log_entries"]

        self.assertTrue(any("[agent]" not in line and "READ doc_project_brief" in line for line in log_lines))
        self.assertTrue(any("(+15m)" in line for line in log_lines))
        self.assertTrue(any("agent-prefix" in entry["html"] for entry in log_entries))
        self.assertTrue(any("agent-tool-read" in entry["html"] for entry in log_entries))

    def test_live_ui_inspector_renders_causal_evidence_hooks(self) -> None:
        send_chat(self.db_path, "luigi", "Any repo sync blockers for launch?")
        advance_time(self.db_path, "until_next_event")

        payload = _state_payload(self.db_path, DEFAULT_SCENARIO_PATH, timeline_limit=20)
        blocker_component = next(
            component
            for component in payload["evaluation"]["components"]
            if component["key"] == "blocker_discovery"
        )
        evidence = blocker_component["evidence"][0]
        html = _html()

        self.assertEqual(evidence["key"], "blocker_discovered")
        self.assertIn("created_at", evidence)
        self.assertIn("source", evidence)
        self.assertIn("note", evidence)
        self.assertIn("evidence-list", html)
        self.assertIn("evidenceItem", html)
        self.assertIn("Source:", html)

    def test_live_ui_keeps_tasks_in_operator_inspector(self) -> None:
        html = _html()

        self.assertIn("Task State", html)
        self.assertIn("Project status:", html)
        self.assertIn("Risk:", html)
        self.assertNotIn("<section><div class=\"section-head\"><h2>Tasks</h2>", html)

    def test_live_ui_disables_page_scroll_anchoring_during_playback(self) -> None:
        html = _html()

        self.assertIn("overflow-anchor:none", html)
        self.assertIn("window.scrollTo(pageX, pageY)", html)
        self.assertIn("overscroll-behavior:contain", html)

    def test_live_ui_can_step_llm_policy(self) -> None:
        client = FakeResponsesClient(
            [
                [function_call("call_1", "observe", {})],
            ]
        )
        start_llm_session(self.db_path, DEFAULT_SCENARIO_PATH, model="test-model")

        result = _run_next_ui_step(
            self.db_path,
            DEFAULT_SCENARIO_PATH,
            policy="llm",
            model="test-model",
            max_turns=3,
            client=client,
        )

        session = llm_session_state(self.db_path)
        self.assertEqual(result["policy"], "llm")
        self.assertEqual(result["turns"], 1)
        self.assertEqual([step["name"] for step in result["steps"]], ["observe"])
        self.assertEqual(session["turns"], 1)
        self.assertEqual(session["steps"], 1)
