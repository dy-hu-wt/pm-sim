from __future__ import annotations

import copy
import contextlib
import io
import json
import tempfile
import unittest
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
from pm_sim.coworkers import effects_for_event, replies_for_chat
from pm_sim.db import connect
from pm_sim.evaluator import evaluate
from pm_sim.effects import apply_effects
from pm_sim.formatters import format_output
from pm_sim.jsonutil import loads
from pm_sim.paths import DEFAULT_SCENARIO_PATH
from pm_sim.report import generate_report
from pm_sim.scenario import ScenarioError, load_scenario
from pm_sim.state import action_log, event_log, observe, reset
from pm_sim.time import advance_time
from pm_sim.timeline import timeline
from pm_sim.ui import _run_next_ui_step, _scripted_demo_state, _state_payload


class ScenarioValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = load_scenario(DEFAULT_SCENARIO_PATH)
        self.tmpdir = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_invalid_task_owner_raises_scenario_error(self) -> None:
        scenario = copy.deepcopy(self.base)
        scenario["tasks"][0]["owner_id"] = "unknown_person"

        with self.assertRaises(ScenarioError):
            load_scenario(self._write_scenario(scenario))

    def test_missing_person_response_delay_raises_scenario_error(self) -> None:
        scenario = copy.deepcopy(self.base)
        del scenario["people"][0]["response_delay_minutes"]

        with self.assertRaises(ScenarioError):
            load_scenario(self._write_scenario(scenario))

    def test_invalid_dependency_task_raises_scenario_error(self) -> None:
        scenario = copy.deepcopy(self.base)
        scenario["dependencies"][0]["upstream_task_id"] = "missing_task"

        with self.assertRaises(ScenarioError):
            load_scenario(self._write_scenario(scenario))

    def test_invalid_event_project_payload_raises_scenario_error(self) -> None:
        scenario = copy.deepcopy(self.base)
        scenario["events"][0]["payload"]["project_id"] = "missing_project"

        with self.assertRaises(ScenarioError):
            load_scenario(self._write_scenario(scenario))

    def test_duplicate_ids_raise_scenario_error(self) -> None:
        scenario = copy.deepcopy(self.base)
        scenario["events"][1]["id"] = scenario["events"][0]["id"]

        with self.assertRaises(ScenarioError):
            load_scenario(self._write_scenario(scenario))

    def test_invalid_event_rule_effect_raises_scenario_error(self) -> None:
        scenario = copy.deepcopy(self.base)
        scenario["event_rules"][0]["effects"][1]["project_id"] = "missing_project"

        with self.assertRaises(ScenarioError):
            load_scenario(self._write_scenario(scenario))

    def test_invalid_coworker_state_effect_raises_scenario_error(self) -> None:
        scenario = copy.deepcopy(self.base)
        scenario["event_rules"][0]["effects"].append(
            {
                "type": "update_coworker_state",
                "person_id": "missing_person",
                "key": "accepted_draft_mode",
                "value": True,
            }
        )

        with self.assertRaises(ScenarioError):
            load_scenario(self._write_scenario(scenario))

    def test_unknown_action_claim_reference_raises_scenario_error(self) -> None:
        scenario = copy.deepcopy(self.base)
        scenario["action_rules"][0]["claims_all"] = ["missing_claim"]

        with self.assertRaises(ScenarioError):
            load_scenario(self._write_scenario(scenario))

    def test_manifest_scenario_includes_files(self) -> None:
        scenario = copy.deepcopy(self.base)
        manifest = {
            key: scenario.pop(key)
            for key in ("id", "name", "company", "start_time", "timezone", "summary")
            if key in scenario
        }
        world_keys = {
            "projects",
            "people",
            "facts",
            "tasks",
            "dependencies",
            "blockers",
            "docs",
            "messages",
            "events",
        }
        world = {key: scenario.pop(key) for key in list(scenario) if key in world_keys}
        rules = scenario

        root = Path(self.tmpdir.name) / "scenario_dir"
        root.mkdir()
        (root / "scenario.json").write_text(
            json.dumps({**manifest, "include": ["world.json", "rules.json"]})
        )
        (root / "world.json").write_text(json.dumps(world))
        (root / "rules.json").write_text(json.dumps(rules))

        loaded = load_scenario(root / "scenario.json")

        self.assertEqual(loaded["id"], "launch_readiness")
        self.assertEqual(len(loaded["people"]), 5)
        self.assertTrue(loaded["coworker_rules"])
        self.assertTrue(loaded["event_rules"])

        loaded_from_dir = load_scenario(root)
        self.assertEqual(loaded_from_dir["id"], "launch_readiness")

    def test_missing_manifest_include_raises_scenario_error(self) -> None:
        path = Path(self.tmpdir.name) / "scenario.json"
        path.write_text(
            json.dumps(
                {
                    "id": "broken",
                    "start_time": "2026-06-22T09:00:00",
                    "include": ["missing.json"],
                }
            )
        )

        with self.assertRaises(ScenarioError):
            load_scenario(path)

    def test_event_before_start_time_raises_scenario_error(self) -> None:
        scenario = copy.deepcopy(self.base)
        scenario["events"][0]["scheduled_at"] = "2026-06-22T08:59:00"

        with self.assertRaises(ScenarioError):
            load_scenario(self._write_scenario(scenario))

    def _write_scenario(self, scenario: dict[str, Any]) -> Path:
        path = Path(self.tmpdir.name) / "scenario.json"
        path.write_text(json.dumps(scenario))
        return path


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
        self.assertEqual(evaluation["score"], 120)
        self.assertEqual(evaluation["score"], evaluation["max_score"])

    def test_live_ui_log_uses_pretty_agent_progress(self) -> None:
        _run_next_ui_step(self.db_path, DEFAULT_SCENARIO_PATH)

        payload = _state_payload(self.db_path, DEFAULT_SCENARIO_PATH, timeline_limit=20)
        log_lines = payload["log_lines"]
        log_entries = payload["log_entries"]

        self.assertTrue(any("[agent]" not in line and "READ doc_project_brief" in line for line in log_lines))
        self.assertTrue(any("(+15m)" in line for line in log_lines))
        self.assertTrue(any("agent-prefix" in entry["html"] for entry in log_entries))
        self.assertTrue(any("agent-tool-read" in entry["html"] for entry in log_entries))

    def test_live_ui_can_step_llm_policy(self) -> None:
        client = _FakeResponsesClient(
            [
                [_function_call("call_1", "observe", {})],
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


class CoworkerRuleTests(unittest.TestCase):
    def setUp(self) -> None:
        scenario = load_scenario(DEFAULT_SCENARIO_PATH)
        self.rules = scenario.get("coworker_rules", [])

    def _state(self, facts: list[str] | None = None) -> dict[str, Any]:
        return {"discovered_facts": facts or [], "coworker_rules": self.rules}

    def test_luigi_reveals_repo_sync_risk_when_asked_about_blockers(self) -> None:
        replies = replies_for_chat(
            "luigi",
            "Any blockers or repo sync stale-code risk for launch?",
            self._state(),
        )

        self.assertEqual(len(replies), 1)
        self.assertIn("repo sync", replies[0].body)
        effect_types = {effect["type"] for effect in replies[0].effects}
        self.assertIn("discover_fact", effect_types)
        self.assertIn("update_blocker", effect_types)
        discovered_facts = {
            effect["fact_id"] for effect in replies[0].effects if effect["type"] == "discover_fact"
        }
        self.assertIn("fact_repo_sync_stale", discovered_facts)
        self.assertIn("fact_draft_mode_limits_customer_visible_risk", discovered_facts)

    def test_luigi_repeat_risk_reply_does_not_duplicate_discovery_effects(self) -> None:
        replies = replies_for_chat(
            "luigi",
            "Any repo sync blockers for launch?",
            {
                **self._state(["fact_repo_sync_stale"]),
                "coworker_state": {("luigi", "risk_surfaced"): True},
            },
        )

        self.assertEqual(len(replies), 1)
        self.assertIn("Same repo sync risk", replies[0].body)
        self.assertEqual(replies[0].effects, ())

    def test_luigi_does_not_reveal_hidden_risk_to_vague_status_ping(self) -> None:
        replies = replies_for_chat("luigi", "How is your week going?", self._state())

        self.assertEqual(len(replies), 1)
        effect_types = {effect["type"] for effect in replies[0].effects}
        self.assertNotIn("discover_fact", effect_types)
        self.assertIn("ask me specifically", replies[0].body)

    def test_rule_without_delay_uses_scenario_person_delay(self) -> None:
        rules = copy.deepcopy(self.rules)
        del rules[0]["reply"]["delay_minutes"]

        replies = replies_for_chat(
            "luigi",
            "Any private repo security docs?",
            {
                "discovered_facts": [],
                "coworker_rules": rules,
                "response_delays": {"luigi": 120},
            },
        )

        self.assertEqual(replies[0].delay_minutes, 120)

    def test_daisy_requires_customer_facing_risk_and_draft_context(self) -> None:
        vague = replies_for_chat(
            "daisy",
            "We found some risk and are working on it.",
            self._state(["fact_repo_sync_stale"]),
        )
        concrete = replies_for_chat(
            "daisy",
            "Repo sync has stale-code risk. Can we message reliable draft mode for Nimbus?",
            self._state(["fact_repo_sync_stale"]),
        )

        self.assertEqual(vague[0].effects, ())
        concrete_effect_keys = {
            effect["key"]
            for effect in concrete[0].effects
            if effect["type"] == "add_evaluation_evidence"
        }
        self.assertIn("stakeholder_alignment", concrete_effect_keys)

    def test_peach_requires_draft_scope_and_prior_customer_context(self) -> None:
        early = replies_for_chat(
            "peach",
            "Please finalize draft-mode onboarding with human approval and no auto-commenting.",
            self._state(["fact_repo_sync_stale"]),
        )
        ready = replies_for_chat(
            "peach",
            "Please finalize draft-mode onboarding with human approval and no auto-commenting.",
            self._state(["fact_repo_sync_stale", "fact_nimbus_values_reliability"]),
        )

        self.assertEqual({effect["type"] for effect in early[0].effects}, {"update_blocker"})
        ready_effect_keys = {
            effect["key"]
            for effect in ready[0].effects
            if effect["type"] == "add_evaluation_evidence"
        }
        self.assertIn("peach_unblocked", ready_effect_keys)

    def test_peach_accepts_out_of_scope_auto_commenting_wording(self) -> None:
        replies = replies_for_chat(
            "peach",
            (
                "Implement only the draft-mode onboarding path for Friday with human approval. "
                "Auto-commenting is out of Friday scope and should not be included."
            ),
            self._state(["fact_repo_sync_stale", "fact_nimbus_values_reliability"]),
        )

        effect_keys = {
            effect["key"]
            for effect in replies[0].effects
            if effect["type"] == "add_evaluation_evidence"
        }

        self.assertIn("peach_unblocked", effect_keys)

    def test_toad_refuses_approval_until_risk_and_customer_context_exist(self) -> None:
        early = replies_for_chat(
            "toad",
            "Approve draft mode for Friday?",
            self._state([]),
        )
        missing_customer_context = replies_for_chat(
            "toad",
            "Repo sync can review stale commits. Approve draft mode for Friday?",
            self._state(["fact_repo_sync_stale"]),
        )
        ready = replies_for_chat(
            "toad",
            "Repo sync can review stale commits. Approve draft mode for Friday?",
            self._state(["fact_repo_sync_stale", "fact_nimbus_values_reliability"]),
        )

        self.assertEqual(early[0].effects, ())
        self.assertEqual(missing_customer_context[0].effects, ())
        ready_effect_keys = {
            effect["key"]
            for effect in ready[0].effects
            if effect["type"] == "add_evaluation_evidence"
        }
        self.assertIn("draft_mode_approved", ready_effect_keys)

    def test_background_event_has_deterministic_effects(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            reset(db_path, DEFAULT_SCENARIO_PATH)
            conn = connect(db_path)
            try:
                effects = effects_for_event(
                    conn,
                    "luigi_proactive_repo_risk",
                    {
                        "project_id": "project_pr_review_agent",
                        "blocker_id": "blocker_repo_sync_stale",
                    },
                )
            finally:
                conn.close()

        self.assertGreaterEqual(len(effects), 3)
        self.assertEqual(effects[0]["type"], "create_message")
        self.assertIn("repo sync", effects[0]["body"])

    def test_background_event_rule_can_be_suppressed_by_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            reset(db_path, DEFAULT_SCENARIO_PATH)
            send_chat(db_path, "luigi", "Any repo sync blockers for launch?")
            advance_time(db_path, "until_next_event")

            conn = connect(db_path)
            try:
                effects = effects_for_event(
                    conn,
                    "luigi_proactive_repo_risk",
                    {
                        "project_id": "project_pr_review_agent",
                        "blocker_id": "blocker_repo_sync_stale",
                    },
                )
            finally:
                conn.close()

        self.assertEqual(effects, [])

    def test_coworker_when_conditions_use_actor_memory_for_repeat_reply(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            reset(db_path, DEFAULT_SCENARIO_PATH)
            send_chat(db_path, "luigi", "Any repo sync blockers for launch?")
            advance_time(db_path, "until_next_event")
            send_chat(db_path, "luigi", "Any repo sync blockers for launch?")
            advance_time(db_path, "until_next_event")

            messages = [
                message for message in observe(db_path)["recent_messages"]
                if message["sender_id"] == "luigi"
            ]

        self.assertTrue(any("The risky part is repo sync" in message["body"] for message in messages))
        self.assertTrue(any("Same repo sync risk as before" in message["body"] for message in messages))

    def test_toad_memory_prevents_duplicate_approval_effects(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            reset(db_path, DEFAULT_SCENARIO_PATH)
            send_chat(db_path, "luigi", "Any repo sync blockers for launch?")
            advance_time(db_path, "until_next_event")
            send_chat(
                db_path,
                "daisy",
                "Repo sync has stale-code risk. Can we message reliable draft mode for Nimbus?",
            )
            advance_time(db_path, "45m")
            first = send_chat(
                db_path,
                "toad",
                "Repo sync can review stale commits. Approve draft mode for Friday Nimbus beta?",
            )
            advance_time(db_path, "until_next_event")
            second = send_chat(
                db_path,
                "toad",
                "Can you approve the Friday launch decision again?",
            )
            advance_time(db_path, "until_next_event")
            messages = [
                message for message in observe(db_path)["recent_messages"]
                if message["sender_id"] == "toad"
            ]

        self.assertTrue(first["scheduled_reply_ids"])
        self.assertTrue(second["scheduled_reply_ids"])
        self.assertTrue(any("Approved to de-scope auto-commenting" in message["body"] for message in messages))
        self.assertTrue(any("approval is already recorded" in message["body"] for message in messages))


class EffectApplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test.db"
        reset(self.db_path, DEFAULT_SCENARIO_PATH)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_apply_effects_mutates_supported_state(self) -> None:
        conn = connect(self.db_path)
        try:
            applied = apply_effects(
                conn,
                [
                    {
                        "type": "create_message",
                        "sender_id": "luigi",
                        "recipient_id": "agent",
                        "body": "Repo sync is risky.",
                    },
                    {
                        "type": "discover_fact",
                        "fact_id": "fact_repo_sync_stale",
                    },
                    {
                        "type": "update_blocker",
                        "blocker_id": "blocker_repo_sync_stale",
                        "status": "surfaced",
                    },
                    {
                        "type": "update_task",
                        "task_id": "task_draft_mode_docs",
                        "status": "in_progress",
                    },
                    {
                        "type": "update_project",
                        "project_id": "project_pr_review_agent",
                        "decision": "draft_mode_approved",
                    },
                    {
                        "type": "add_evaluation_evidence",
                        "key": "blocker_discovered",
                        "note": "Luigi disclosed stale repo sync risk.",
                    },
                ],
                now="2026-06-22T11:00:00",
                source="test",
            )
            conn.commit()

            self.assertEqual(len(applied), 6)
            fact = conn.execute(
                "SELECT visible_at FROM facts WHERE id = 'fact_repo_sync_stale'"
            ).fetchone()
            blocker = conn.execute(
                "SELECT status, visible_at FROM blockers WHERE id = 'blocker_repo_sync_stale'"
            ).fetchone()
            task = conn.execute(
                "SELECT status FROM tasks WHERE id = 'task_draft_mode_docs'"
            ).fetchone()
            project = conn.execute(
                "SELECT metadata_json FROM projects WHERE id = 'project_pr_review_agent'"
            ).fetchone()
            evidence = conn.execute(
                "SELECT evidence_key FROM evaluation_evidence WHERE evidence_key = 'blocker_discovered'"
            ).fetchone()

            self.assertEqual(fact["visible_at"], "2026-06-22T11:00:00")
            self.assertEqual(blocker["status"], "surfaced")
            self.assertEqual(blocker["visible_at"], "2026-06-22T11:00:00")
            self.assertEqual(task["status"], "in_progress")
            self.assertEqual(loads(project["metadata_json"])["decision"], "draft_mode_approved")
            self.assertEqual(evidence["evidence_key"], "blocker_discovered")
        finally:
            conn.close()

    def test_update_coworker_state_effect_is_queryable_by_conditions(self) -> None:
        conn = connect(self.db_path)
        try:
            applied = apply_effects(
                conn,
                [
                    {
                        "type": "update_coworker_state",
                        "person_id": "mario",
                        "values": {
                            "accepted_draft_mode": True,
                            "product_pressure_active": False,
                        },
                    }
                ],
                now="2026-06-22T11:00:00",
                source="test",
            )
            conn.commit()

            rows = conn.execute(
                """
                SELECT key, value_json, updated_at
                FROM coworker_state
                WHERE person_id = 'mario'
                ORDER BY key
                """
            ).fetchall()

            self.assertEqual(applied[0]["type"], "update_coworker_state")
            self.assertEqual(applied[0]["person_id"], "mario")
            values = {row["key"]: loads(row["value_json"]) for row in rows}
            self.assertTrue(values["accepted_draft_mode"])
            self.assertFalse(values["product_pressure_active"])
            self.assertTrue(
                condition_matches(
                    conn,
                    {
                        "coworker_state": {
                            "person_id": "mario",
                            "key": "accepted_draft_mode",
                            "equals": True,
                        }
                    },
                )
            )
        finally:
            conn.close()

    def test_duplicate_evaluation_evidence_is_idempotent(self) -> None:
        conn = connect(self.db_path)
        try:
            first = apply_effects(
                conn,
                [
                    {
                        "type": "add_evaluation_evidence",
                        "key": "blocker_discovered",
                        "note": "Luigi disclosed stale repo sync risk.",
                    }
                ],
                now="2026-06-22T11:00:00",
                source="test:first",
            )
            second = apply_effects(
                conn,
                [
                    {
                        "type": "add_evaluation_evidence",
                        "key": "blocker_discovered",
                        "note": "Luigi disclosed stale repo sync risk.",
                    }
                ],
                now="2026-06-22T13:00:00",
                source="test:second",
            )
            conn.commit()

            count = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM evaluation_evidence
                WHERE evidence_key = 'blocker_discovered'
                  AND note = 'Luigi disclosed stale repo sync risk.'
                """
            ).fetchone()["count"]

            self.assertEqual(count, 1)
            self.assertFalse(first[0]["deduped"])
            self.assertTrue(second[0]["deduped"])
            self.assertEqual(first[0]["id"], second[0]["id"])
        finally:
            conn.close()

    def test_resolved_blocker_is_not_downgraded_by_later_stale_event(self) -> None:
        conn = connect(self.db_path)
        try:
            first = apply_effects(
                conn,
                [
                    {
                        "type": "update_blocker",
                        "blocker_id": "blocker_scope_unclear",
                        "status": "resolved",
                    }
                ],
                now="2026-06-22T10:00:00",
                source="test:resolve",
            )
            second = apply_effects(
                conn,
                [
                    {
                        "type": "update_blocker",
                        "blocker_id": "blocker_scope_unclear",
                        "status": "surfaced",
                    }
                ],
                now="2026-06-24T11:00:00",
                source="test:stale_escalation",
            )
            conn.commit()

            blocker = conn.execute(
                """
                SELECT status, resolved_at
                FROM blockers
                WHERE id = 'blocker_scope_unclear'
                """
            ).fetchone()

            self.assertEqual(first[0]["status"], "resolved")
            self.assertTrue(second[0]["skipped"])
            self.assertEqual(blocker["status"], "resolved")
            self.assertEqual(blocker["resolved_at"], "2026-06-22T10:00:00")
        finally:
            conn.close()

    def test_coworker_reply_event_creates_message_and_applies_attached_effects(self) -> None:
        send_chat(self.db_path, "luigi", "Any repo sync blockers for launch?")

        result = advance_time(self.db_path, "until_next_event")
        state = observe(self.db_path)

        self.assertEqual(result["delivered_events"][0]["event_type"], "coworker_reply")
        self.assertTrue(result["delivered_events"][0]["result"]["handled"])
        self.assertEqual(state["recent_messages"][0]["sender_id"], "luigi")
        self.assertIn("fact_repo_sync_stale", {fact["id"] for fact in state["discovered_facts"]})

    def test_meeting_event_creates_transcript_and_applies_coordination_effects(self) -> None:
        meeting = schedule_meeting(
            self.db_path,
            "Draft-mode risk review for Nimbus launch",
            "2026-06-22T10:00:00",
            "2026-06-22T10:30:00",
            ["luigi", "daisy", "mario", "toad", "peach"],
        )

        result = advance_time(self.db_path, "to:2026-06-22T10:30:00")
        state = observe(self.db_path)
        conn = connect(self.db_path)
        try:
            calendar_event = conn.execute(
                """
                SELECT status, transcript_doc_id
                FROM calendar_events
                WHERE id = ?
                """,
                (meeting["meeting_id"],),
            ).fetchone()
            transcript = conn.execute(
                """
                SELECT title, kind, body, visibility_scope, visible_at
                FROM docs
                WHERE id = ?
                """,
                (calendar_event["transcript_doc_id"],),
            ).fetchone()
            evidence_keys = {
                row["evidence_key"]
                for row in conn.execute(
                    "SELECT evidence_key FROM evaluation_evidence"
                ).fetchall()
            }

            delivered = result["delivered_events"][0]
            blocker_ids = {blocker["id"] for blocker in state["known_blockers"]}
            fact_ids = {fact["id"] for fact in state["discovered_facts"]}

            self.assertEqual(delivered["event_type"], "meeting_occurs")
            self.assertTrue(delivered["result"]["handled"])
            self.assertEqual(calendar_event["status"], "completed")
            self.assertEqual(calendar_event["transcript_doc_id"], "doc_transcript_cal_1")
            self.assertEqual(transcript["kind"], "meeting_transcript")
            self.assertEqual(transcript["visibility_scope"], "generated")
            self.assertEqual(transcript["visible_at"], "2026-06-22T10:30:00")
            self.assertIn("repo sync", transcript["body"])
            self.assertIn("blocker_repo_sync_stale", blocker_ids)
            self.assertIn("fact_repo_sync_stale", fact_ids)
            self.assertIn("fact_draft_mode_approved", fact_ids)
            self.assertIn("blocker_discovered", evidence_keys)
            self.assertIn("stakeholder_alignment", evidence_keys)
            self.assertIn("draft_mode_approved", evidence_keys)
        finally:
            conn.close()

    def test_meeting_without_luigi_does_not_discover_repo_sync_risk(self) -> None:
        schedule_meeting(
            self.db_path,
            "Draft-mode planning for Nimbus launch",
            "2026-06-22T10:00:00",
            "2026-06-22T10:30:00",
            ["daisy", "peach"],
        )

        advance_time(self.db_path, "to:2026-06-22T10:30:00")
        state = observe(self.db_path)

        fact_ids = {fact["id"] for fact in state["discovered_facts"]}

        self.assertNotIn("fact_repo_sync_stale", fact_ids)
        self.assertIn("fact_nimbus_values_reliability", fact_ids)
        self.assertIn("fact_draft_mode_scope_confirmed", fact_ids)

    def test_meeting_with_luigi_discovers_repo_sync_risk(self) -> None:
        schedule_meeting(
            self.db_path,
            "Repo sync risk review for Nimbus launch",
            "2026-06-22T10:00:00",
            "2026-06-22T10:30:00",
            ["luigi"],
        )

        advance_time(self.db_path, "to:2026-06-22T10:30:00")
        state = observe(self.db_path)

        fact_ids = {fact["id"] for fact in state["discovered_facts"]}
        blocker_ids = {blocker["id"] for blocker in state["known_blockers"]}

        self.assertIn("fact_repo_sync_stale", fact_ids)
        self.assertIn("blocker_repo_sync_stale", blocker_ids)

    def test_meeting_with_toad_before_risk_does_not_approve(self) -> None:
        schedule_meeting(
            self.db_path,
            "Draft-mode approval for Friday launch",
            "2026-06-22T10:00:00",
            "2026-06-22T10:30:00",
            ["toad", "daisy"],
        )

        advance_time(self.db_path, "to:2026-06-22T10:30:00")
        state = observe(self.db_path)

        fact_ids = {fact["id"] for fact in state["discovered_facts"]}

        self.assertNotIn("fact_repo_sync_stale", fact_ids)
        self.assertNotIn("fact_draft_mode_approved", fact_ids)

    def test_meeting_with_toad_and_luigi_still_needs_scope_before_approval(self) -> None:
        schedule_meeting(
            self.db_path,
            "Draft-mode risk review for Nimbus launch",
            "2026-06-22T10:00:00",
            "2026-06-22T10:30:00",
            ["luigi", "toad"],
        )

        advance_time(self.db_path, "to:2026-06-22T10:30:00")
        state = observe(self.db_path)

        fact_ids = {fact["id"] for fact in state["discovered_facts"]}

        self.assertIn("fact_repo_sync_stale", fact_ids)
        self.assertNotIn("fact_draft_mode_approved", fact_ids)

    def test_meeting_with_luigi_daisy_peach_and_toad_can_fully_align(self) -> None:
        schedule_meeting(
            self.db_path,
            "Draft-mode risk review for Nimbus launch",
            "2026-06-22T10:00:00",
            "2026-06-22T10:30:00",
            ["luigi", "daisy", "peach", "toad"],
        )

        advance_time(self.db_path, "to:2026-06-22T10:30:00")
        state = observe(self.db_path)
        conn = connect(self.db_path)
        try:
            evidence_keys = {
                row["evidence_key"]
                for row in conn.execute("SELECT evidence_key FROM evaluation_evidence").fetchall()
            }
        finally:
            conn.close()

        fact_ids = {fact["id"] for fact in state["discovered_facts"]}

        self.assertIn("fact_repo_sync_stale", fact_ids)
        self.assertIn("fact_nimbus_values_reliability", fact_ids)
        self.assertIn("fact_draft_mode_scope_confirmed", fact_ids)
        self.assertIn("fact_draft_mode_approved", fact_ids)
        self.assertIn("blocker_discovered", evidence_keys)
        self.assertIn("stakeholder_alignment", evidence_keys)
        self.assertIn("peach_unblocked", evidence_keys)
        self.assertIn("draft_mode_approved", evidence_keys)


class ToolActionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test.db"
        reset(self.db_path, DEFAULT_SCENARIO_PATH)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_list_tasks_returns_seeded_tasks(self) -> None:
        tasks = list_tasks(self.db_path)

        task_ids = {task["id"] for task in tasks}
        self.assertIn("task_repo_sync", task_ids)
        self.assertIn("task_launch_decision", task_ids)

    def test_read_doc_returns_visible_doc_body(self) -> None:
        result = read_doc(self.db_path, "doc_project_brief")

        self.assertTrue(result["ok"])
        self.assertIn("review pull requests faster", result["doc"]["body"])
        self.assertEqual(result["time_cost"]["minutes"], 15)
        self.assertEqual(result["time_cost"]["from"], "2026-06-22T09:00:00")
        self.assertEqual(result["time_cost"]["to"], "2026-06-22T09:15:00")
        self.assertEqual(observe(self.db_path)["current_time"], "2026-06-22T09:15:00")

    def test_read_doc_returns_rollout_template(self) -> None:
        result = read_doc(self.db_path, "doc_beta_rollout_template")

        self.assertTrue(result["ok"])
        self.assertIn("human approval", result["doc"]["body"])

    def test_read_doc_blocks_invisible_doc(self) -> None:
        result = read_doc(self.db_path, "doc_repo_sync_notes")

        self.assertFalse(result["ok"])
        self.assertIn("not visible", result["error"])

    def test_update_doc_records_revision_and_time_cost(self) -> None:
        result = update_doc(
            self.db_path,
            "doc_launch_decision_record",
            "Decision draft: waiting for Toad approval.",
        )
        doc = read_doc(self.db_path, "doc_launch_decision_record")
        conn = connect(self.db_path)
        try:
            revision = conn.execute(
                """
                SELECT doc_id, actor, previous_body, new_body
                FROM doc_revisions
                WHERE id = ?
                """,
                (result["revision_id"],),
            ).fetchone()
        finally:
            conn.close()

        self.assertTrue(result["ok"])
        self.assertEqual(result["time_cost"]["minutes"], 20)
        self.assertIn("waiting for Toad approval", doc["doc"]["body"])
        self.assertEqual(revision["doc_id"], "doc_launch_decision_record")
        self.assertEqual(revision["actor"], "agent")
        self.assertIn("Decision pending", revision["previous_body"])
        self.assertIn("waiting for Toad approval", revision["new_body"])

    def test_update_doc_blocks_invisible_doc(self) -> None:
        result = update_doc(self.db_path, "doc_repo_sync_notes", "Make this visible?")

        self.assertFalse(result["ok"])
        self.assertIn("not visible", result["error"])

    def test_decision_record_evidence_requires_approval_and_complete_content(self) -> None:
        early = update_doc(
            self.db_path,
            "doc_launch_decision_record",
            (
                "Friday launch decision: Toad approved draft mode for Nimbus. "
                "Draft suggestions require human approval before posting. "
                "Auto-commenting is out of Friday scope and remains follow-up work. "
                "Rationale: repo sync can review stale commits."
            ),
        )
        self.assertEqual(early["applied_effects"], [])

        self._drive_to_draft_approval()
        valid = update_doc(
            self.db_path,
            "doc_launch_decision_record",
            (
                "Friday launch decision: Toad approved draft mode for Nimbus. "
                "Draft suggestions require human approval before posting. "
                "Auto-commenting is out of Friday scope and remains follow-up work. "
                "Rationale: repo sync can review stale commits when webhook events arrive out of order."
            ),
        )

        self.assertTrue(
            any(effect.get("key") == "decision_record_written" for effect in valid["applied_effects"])
        )

    def test_decision_record_accepts_natural_out_of_scope_wording(self) -> None:
        self._drive_to_draft_approval()

        result = update_doc(
            self.db_path,
            "doc_launch_decision_record",
            (
                "Friday launch decision: Toad approved draft mode for Nimbus. "
                "Draft suggestions require human approval before posting. "
                "Auto-commenting is out of Friday scope and remains follow-up work. "
                "Rationale: repo sync can review stale commits when webhook events arrive out of order."
            ),
        )

        self.assertTrue(
            any(effect.get("key") == "decision_record_written" for effect in result["applied_effects"])
        )

    def test_decision_record_accepts_llm_markdown_record(self) -> None:
        self._drive_to_draft_approval()

        result = update_doc(
            self.db_path,
            "doc_launch_decision_record",
            """
# Friday Launch Decision Record - PR Review Agent Beta

Decision owner/approval: Toad approved the Friday launch mode.

## Approved Friday launch mode
Ship the Nimbus Labs beta in **draft mode**.

## Customer-visible behavior
- The PR Review Agent may generate review suggestions for a real pull request.
- The agent must **not post comments automatically** in the Friday beta.
- A human must review and approve suggestions before anything is posted to a PR.

## Scope decision
Auto-commenting is **out of Friday scope** and remains a follow-up after repo-sync hardening.

## Rationale
Luigi surfaced a repo-sync stale-commit risk: webhook events can arrive out of order, so auto-commenting could review or post against a stale commit on Friday. Draft mode keeps the beta useful while preventing incorrect comments from being posted directly.
""",
        )

        self.assertTrue(
            any(effect.get("key") == "decision_record_written" for effect in result["applied_effects"])
        )

    def test_decision_record_accepts_automatic_pr_commenting_wording(self) -> None:
        self._drive_to_draft_approval()

        result = update_doc(
            self.db_path,
            "doc_launch_decision_record",
            """
# Friday Launch Decision Record

Decision: PR Review Agent beta will launch for Nimbus Labs on Friday in **draft mode only**.

Approval: Toad approved de-scoping auto-commenting for Friday and approved draft mode after reviewing Daisy's customer input and Luigi's engineering risk assessment.

Implementation / onboarding scope: Peach should finish the draft-mode flow only. The onboarding must state that the PR Review Agent prepares review suggestions as drafts, and a human must review and approve those suggestions before anything is posted to a pull request.

Customer-visible behavior: Nimbus will see draft review suggestions for a real PR. The system will not automatically post PR comments during Friday's beta.

Friday scope: draft-mode beta, onboarding and talk track that explain human approval before posting, and continued repo-sync hardening.

Out of Friday scope / follow-up: automatic PR commenting is not part of the Friday beta. Auto-commenting remains a follow-up after repo sync is proven reliable enough.

Repo-sync stale-commit rationale: Luigi confirmed the review context pipeline is solid, but repo-sync webhooks can arrive out of order, so the agent may review a stale commit. Auto-commenting could make that customer-visible by posting against an older diff.
""",
        )

        self.assertTrue(
            any(effect.get("key") == "decision_record_written" for effect in result["applied_effects"])
        )

    def test_private_repo_security_doc_is_hidden_until_luigi_reveals_it(self) -> None:
        hidden = read_doc(self.db_path, "doc_private_repo_security_baseline")

        send_chat(
            self.db_path,
            "luigi",
            "Nimbus asked if we store source code from private repos. Is there a security doc?",
        )
        advance_time(self.db_path, "until_next_event")
        revealed = read_doc(self.db_path, "doc_private_repo_security_baseline")
        conn = connect(self.db_path)
        try:
            evidence = conn.execute(
                """
                SELECT evidence_key
                FROM evaluation_evidence
                WHERE evidence_key = 'security_doc_found'
                """
            ).fetchone()
        finally:
            conn.close()

        self.assertFalse(hidden["ok"])
        self.assertTrue(revealed["ok"])
        self.assertIn("Raw source code is not stored long term", revealed["doc"]["body"])
        self.assertIsNotNone(evidence)

    def test_private_repo_security_reply_is_scenario_rule_driven(self) -> None:
        scenario = load_scenario(DEFAULT_SCENARIO_PATH)
        scenario["coworker_rules"] = []
        scenario_path = Path(self.tmpdir.name) / "no_coworker_rules.json"
        scenario_path.write_text(json.dumps(scenario))
        reset(self.db_path, scenario_path)

        send_chat(
            self.db_path,
            "luigi",
            "Nimbus asked if we store source code from private repos. Is there a security doc?",
        )
        advance_time(self.db_path, "until_next_event")
        revealed = read_doc(self.db_path, "doc_private_repo_security_baseline")
        conn = connect(self.db_path)
        try:
            evidence = conn.execute(
                """
                SELECT evidence_key
                FROM evaluation_evidence
                WHERE evidence_key = 'security_doc_found'
                """
            ).fetchone()
        finally:
            conn.close()

        self.assertFalse(revealed["ok"])
        self.assertIsNone(evidence)

    def test_send_chat_schedules_coworker_reply(self) -> None:
        result = send_chat(self.db_path, "luigi", "Any repo sync blockers for launch?")
        events = event_log(self.db_path, limit=20)
        conn = connect(self.db_path)
        try:
            message = conn.execute(
                "SELECT sent_at FROM messages WHERE id = ?",
                (result["message_id"],),
            ).fetchone()
        finally:
            conn.close()

        self.assertTrue(result["ok"])
        self.assertEqual(result["time_cost"]["minutes"], 5)
        self.assertEqual(result["time_cost"]["to"], "2026-06-22T09:05:00")
        self.assertEqual(observe(self.db_path)["current_time"], "2026-06-22T09:05:00")
        self.assertEqual(message["sent_at"], "2026-06-22T09:00:00")
        self.assertEqual(len(result["scheduled_reply_ids"]), 1)
        reply_events = [event for event in events if event["event_type"] == "coworker_reply"]
        self.assertEqual(len(reply_events), 1)
        self.assertEqual(reply_events[0]["scheduled_at"], "2026-06-22T12:00:00")
        self.assertIn("repo sync", reply_events[0]["payload_json"])

    def test_chat_reply_delay_respects_coworker_availability(self) -> None:
        conn = connect(self.db_path)
        try:
            conn.execute(
                "UPDATE sim_state SET value = ? WHERE key = 'current_time'",
                ("2026-06-22T17:30:00",),
            )
            conn.commit()
        finally:
            conn.close()

        send_chat(self.db_path, "luigi", "Any repo sync blockers for launch?")
        reply_events = [
            event for event in event_log(self.db_path, limit=20)
            if event["event_type"] == "coworker_reply"
        ]

        self.assertEqual(reply_events[0]["scheduled_at"], "2026-06-23T11:30:00")

    def test_chat_reply_before_working_hours_starts_at_next_available_window(self) -> None:
        conn = connect(self.db_path)
        try:
            conn.execute(
                "UPDATE sim_state SET value = ? WHERE key = 'current_time'",
                ("2026-06-24T07:30:00",),
            )
            conn.commit()
        finally:
            conn.close()

        send_chat(self.db_path, "daisy", "Can you send a confidence check for Nimbus?")
        reply_events = [
            event for event in event_log(self.db_path, limit=20)
            if event["event_type"] == "coworker_reply"
        ]

        self.assertEqual(reply_events[0]["scheduled_at"], "2026-06-24T09:15:00")

    def test_send_email_records_message_without_scheduling_reply(self) -> None:
        result = send_email(
            self.db_path,
            "daisy",
            "Friday confidence",
            "I am checking the launch risk and will follow up.",
        )
        state = observe(self.db_path)
        conn = connect(self.db_path)
        try:
            evidence_count = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM evaluation_evidence
                WHERE evidence_key = 'stakeholder_alignment'
                """
            ).fetchone()["count"]
        finally:
            conn.close()

        self.assertTrue(result["ok"])
        self.assertEqual(result["time_cost"]["minutes"], 10)
        self.assertEqual(result["time_cost"]["to"], "2026-06-22T09:10:00")
        self.assertEqual(observe(self.db_path)["current_time"], "2026-06-22T09:10:00")
        self.assertEqual(result["applied_effects"], [])
        self.assertEqual(evidence_count, 0)
        message = next(
            message for message in state["recent_messages"] if message["id"] == result["message_id"]
        )
        self.assertEqual(message["channel"], "email")
        self.assertEqual(message["recipient_id"], "daisy")

    def test_substantive_daisy_email_before_discovery_does_not_score(self) -> None:
        result = send_email(
            self.db_path,
            "daisy",
            "Nimbus Friday draft-mode status",
            (
                "Repo sync has stale-commit risk. I recommend reliable draft mode "
                "for Friday with human approval."
            ),
        )
        conn = connect(self.db_path)
        try:
            evidence = conn.execute(
                """
                SELECT evidence_key, note
                FROM evaluation_evidence
                WHERE evidence_key = 'stakeholder_alignment'
                """
            ).fetchone()
        finally:
            conn.close()

        self.assertTrue(result["ok"])
        self.assertEqual(result["applied_effects"], [])
        self.assertIsNone(evidence)

    def test_substantive_daisy_email_after_discovery_records_stakeholder_evidence(self) -> None:
        self._drive_to_draft_approval()

        result = send_email(
            self.db_path,
            "daisy",
            "Nimbus Friday draft-mode status",
            (
                "Repo sync has stale-commit risk. I recommend reliable draft mode "
                "for Friday with human approval."
            ),
        )
        conn = connect(self.db_path)
        try:
            evidence = conn.execute(
                """
                SELECT evidence_key, note
                FROM evaluation_evidence
                WHERE evidence_key = 'stakeholder_alignment'
                """
            ).fetchone()
        finally:
            conn.close()

        self.assertTrue(result["ok"])
        self.assertTrue(any(effect.get("key") == "stakeholder_alignment" for effect in result["applied_effects"]))
        self.assertEqual(evidence["evidence_key"], "stakeholder_alignment")

    def test_customer_message_ready_uses_scenario_authored_claims(self) -> None:
        self._drive_to_draft_approval()

        result = send_email(
            self.db_path,
            "daisy",
            "Nimbus pilot plan",
            (
                "For the Friday Nimbus beta, use draft mode: the agent queues draft "
                "suggestions and a reviewer approves before posting. The reason is "
                "repo sync webhook ordering can leave the agent reviewing an older commit."
            ),
        )
        conn = connect(self.db_path)
        try:
            row = conn.execute(
                """
                SELECT value_json
                FROM coworker_state
                WHERE person_id = 'daisy'
                  AND key = 'customer_message_ready'
                """
            ).fetchone()
        finally:
            conn.close()
        evaluation = evaluate(self.db_path, DEFAULT_SCENARIO_PATH)
        stakeholder_component = next(
            component
            for component in evaluation["components"]
            if component["key"] == "stakeholder_communication"
        )

        self.assertTrue(result["ok"])
        self.assertTrue(
            any(
                effect.get("type") == "update_coworker_state"
                and effect.get("person_id") == "daisy"
                and "customer_message_ready" in effect.get("keys", [])
                for effect in result["applied_effects"]
            )
        )
        self.assertTrue(loads(row["value_json"]))
        self.assertIn(
            "customer_message_ready",
            {evidence["key"] for evidence in stakeholder_component["evidence"]},
        )

    def test_guessed_customer_message_before_discovery_is_not_customer_ready(self) -> None:
        result = send_email(
            self.db_path,
            "daisy",
            "Nimbus Friday draft-mode update",
            (
                "Nimbus can see reliable draft-mode suggestions on Friday. Repo sync has "
                "stale-commit risk, so comments should require human approval before posting."
            ),
        )
        conn = connect(self.db_path)
        try:
            evidence = conn.execute(
                """
                SELECT evidence_key
                FROM evaluation_evidence
                WHERE evidence_key = 'customer_message_ready'
                """
            ).fetchone()
            coworker_state = conn.execute(
                """
                SELECT value_json
                FROM coworker_state
                WHERE person_id = 'daisy'
                  AND key = 'customer_message_ready'
                """
            ).fetchone()
        finally:
            conn.close()
        evaluation = evaluate(self.db_path, DEFAULT_SCENARIO_PATH)
        stakeholder_component = next(
            component
            for component in evaluation["components"]
            if component["key"] == "stakeholder_communication"
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["applied_effects"], [])
        self.assertIsNone(evidence)
        self.assertFalse(loads(coworker_state["value_json"]))
        self.assertIn("customer_message_ready", stakeholder_component["missing_evidence"])
        self.assertEqual(stakeholder_component["earned"], 0)

    def test_security_answer_email_before_daisy_question_does_not_score(self) -> None:
        result = send_email(
            self.db_path,
            "daisy",
            "Nimbus private repo security answer",
            (
                "Nimbus can tell their reviewer that private repo source code is "
                "processed transiently. Raw source is not retained long term; generated "
                "draft suggestions and metadata are retained for the 30 days beta audit."
            ),
        )
        conn = connect(self.db_path)
        try:
            evidence = conn.execute(
                """
                SELECT evidence_key, note
                FROM evaluation_evidence
                WHERE evidence_key = 'security_question_answered'
                """
            ).fetchone()
        finally:
            conn.close()

        self.assertTrue(result["ok"])
        self.assertFalse(
            any(effect.get("key") == "security_question_answered" for effect in result["applied_effects"])
        )
        self.assertIsNone(evidence)

    def test_security_answer_email_records_security_question_evidence_after_daisy_asks(self) -> None:
        advance_time(self.db_path, "to:2026-06-24T14:00:00")
        send_chat(
            self.db_path,
            "luigi",
            "Nimbus asked if we store source code from private repos. Is there a security doc?",
        )
        advance_time(self.db_path, "2h")
        read_doc(self.db_path, "doc_private_repo_security_baseline")

        result = send_email(
            self.db_path,
            "daisy",
            "Nimbus private repo security answer",
            (
                "Nimbus can tell their reviewer that private repo source code is "
                "processed transiently. Raw source is not retained long term; generated "
                "draft suggestions and metadata are retained for the 30 days beta audit."
            ),
        )
        conn = connect(self.db_path)
        try:
            row = conn.execute(
                """
                SELECT value_json
                FROM coworker_state
                WHERE person_id = 'daisy'
                  AND key = 'security_answer_received'
                """
            ).fetchone()
        finally:
            conn.close()
        evaluation = evaluate(self.db_path, DEFAULT_SCENARIO_PATH)
        security_component = next(
            component
            for component in evaluation["components"]
            if component["key"] == "security_interruption"
        )
        evidence = next(
            item
            for item in security_component["evidence"]
            if item["key"] == "security_question_answered"
        )

        self.assertTrue(result["ok"])
        self.assertTrue(
            any(
                effect.get("type") == "update_coworker_state"
                and effect.get("person_id") == "daisy"
                and "security_answer_received" in effect.get("keys", [])
                for effect in result["applied_effects"]
            )
        )
        self.assertTrue(loads(row["value_json"]))
        self.assertEqual(evidence["key"], "security_question_answered")
        self.assertIn("private repo source handling", evidence["note"])

    def test_schedule_meeting_creates_future_meeting_event(self) -> None:
        result = schedule_meeting(
            self.db_path,
            "Draft-mode risk review",
            "2026-06-22T10:00:00",
            "2026-06-22T10:30:00",
            ["luigi", "daisy", "mario", "toad"],
        )
        events = event_log(self.db_path, limit=20)

        self.assertTrue(result["ok"])
        self.assertEqual(result["time_cost"]["minutes"], 5)
        self.assertEqual(result["time_cost"]["to"], "2026-06-22T09:05:00")
        self.assertEqual(observe(self.db_path)["current_time"], "2026-06-22T09:05:00")
        meeting_events = [event for event in events if event["event_type"] == "meeting_occurs"]
        self.assertEqual(len(meeting_events), 1)
        self.assertEqual(meeting_events[0]["scheduled_at"], "2026-06-22T10:30:00")
        self.assertIn(result["meeting_id"], meeting_events[0]["payload_json"])

    def test_action_time_cost_delivers_events_crossed_during_work(self) -> None:
        advance_time(self.db_path, "to:2026-06-24T13:55:00")

        result = read_doc(self.db_path, "doc_project_brief")
        state = observe(self.db_path)

        delivered_event_types = [
            event["event_type"] for event in result["time_cost"]["delivered_events"]
        ]
        self.assertEqual(result["time_cost"]["to"], "2026-06-24T14:10:00")
        self.assertIn("daisy_private_repo_security_question", delivered_event_types)
        self.assertEqual(state["current_time"], "2026-06-24T14:10:00")

    def test_update_task_changes_status_and_priority(self) -> None:
        result = update_task(
            self.db_path,
            "task_launch_decision",
            status="in_progress",
            priority="critical",
        )
        tasks = list_tasks(self.db_path)
        task = next(task for task in tasks if task["id"] == "task_launch_decision")

        self.assertTrue(result["ok"])
        self.assertEqual(task["status"], "in_progress")
        self.assertEqual(task["priority"], "critical")
        self.assertEqual(result["time_cost"]["minutes"], 1)
        self.assertEqual(result["time_cost"]["to"], "2026-06-22T09:01:00")

    def test_cannot_complete_repo_sync_with_stale_blocker_unresolved(self) -> None:
        before = self._task_state("task_repo_sync")
        action_count_before = self._action_count()

        result = update_task(self.db_path, "task_repo_sync", status="complete", priority=None)

        self.assertFalse(result["ok"])
        self.assertIn("blocker_repo_sync_stale is unresolved", result["error"])
        self.assertEqual(self._task_state("task_repo_sync"), before)
        self.assertEqual(self._action_count(), action_count_before)

    def test_can_move_repo_sync_to_in_progress(self) -> None:
        result = update_task(
            self.db_path,
            "task_repo_sync",
            status="in_progress",
            priority="critical",
        )
        task = self._task_state("task_repo_sync")

        self.assertTrue(result["ok"])
        self.assertEqual(task["status"], "in_progress")
        self.assertEqual(task["priority"], "critical")

    def test_cannot_complete_draft_docs_before_scope_confirmation(self) -> None:
        before = self._task_state("task_draft_mode_docs")

        result = update_task(self.db_path, "task_draft_mode_docs", status="complete", priority=None)

        self.assertFalse(result["ok"])
        self.assertIn("draft-mode scope confirmation", result["error"])
        self.assertEqual(self._task_state("task_draft_mode_docs"), before)

    def test_can_complete_draft_docs_after_peach_and_toad_path(self) -> None:
        schedule_meeting(
            self.db_path,
            "Draft-mode risk review for Nimbus launch",
            "2026-06-22T10:00:00",
            "2026-06-22T10:30:00",
            ["luigi", "daisy", "mario", "toad", "peach"],
        )
        advance_time(self.db_path, "to:2026-06-22T10:30:00")

        result = update_task(self.db_path, "task_draft_mode_docs", status="complete", priority=None)
        task = self._task_state("task_draft_mode_docs")

        self.assertTrue(result["ok"])
        self.assertEqual(task["status"], "complete")

    def test_cannot_complete_customer_talk_track_without_alignment_and_decision(self) -> None:
        before = self._task_state("task_customer_talk_track")

        result = update_task(
            self.db_path,
            "task_customer_talk_track",
            status="complete",
            priority=None,
        )

        self.assertFalse(result["ok"])
        self.assertIn("Daisy alignment", result["error"])
        self.assertIn("launch mode decision", result["error"])
        self.assertEqual(self._task_state("task_customer_talk_track"), before)

    def test_can_complete_customer_talk_track_after_customer_ready_email_and_decision(self) -> None:
        schedule_meeting(
            self.db_path,
            "Draft-mode risk review for Nimbus launch",
            "2026-06-22T10:00:00",
            "2026-06-22T10:30:00",
            ["luigi", "daisy", "mario", "toad", "peach"],
        )
        advance_time(self.db_path, "to:2026-06-22T10:30:00")
        send_email(
            self.db_path,
            "daisy",
            "Nimbus Friday draft-mode update",
            (
                "Nimbus can see reliable draft-mode suggestions on Friday. Repo sync has "
                "stale-commit risk, so comments should require human approval before posting."
            ),
        )

        result = update_task(
            self.db_path,
            "task_customer_talk_track",
            status="complete",
            priority=None,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(self._task_state("task_customer_talk_track")["status"], "complete")

    def test_cannot_complete_launch_decision_without_toad_approval(self) -> None:
        before = self._task_state("task_launch_decision")

        result = update_task(self.db_path, "task_launch_decision", status="complete", priority=None)

        self.assertFalse(result["ok"])
        self.assertIn("Toad approval", result["error"])
        self.assertEqual(self._task_state("task_launch_decision"), before)

    def _drive_to_draft_approval(self) -> None:
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

    def _task_state(self, task_id: str) -> dict[str, str]:
        conn = connect(self.db_path)
        try:
            row = conn.execute(
                """
                SELECT status, priority
                FROM tasks
                WHERE id = ?
                """,
                (task_id,),
            ).fetchone()
            return {"status": row["status"], "priority": row["priority"]}
        finally:
            conn.close()

    def _action_count(self) -> int:
        conn = connect(self.db_path)
        try:
            row = conn.execute("SELECT COUNT(*) AS count FROM action_log").fetchone()
            return int(row["count"])
        finally:
            conn.close()


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
        self.assertIn("final_readiness_confirmed", risk_component["missing_evidence"])

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
        self.assertNotIn("final_readiness_confirmed", risk_component["missing_evidence"])

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
        self.assertIn("customer_message_ready", stakeholder_component["missing_evidence"])
        self.assertNotEqual(outcome["metadata"]["final_outcome"], "draft_mode_beta_shipped")

    def test_late_evidence_gets_partial_timing_credit(self) -> None:
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
            {evidence["key"] for evidence in stakeholder_component["evidence"]},
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
        self.assertIn("customer_message_ready", stakeholder_component["missing_evidence"])

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
        self.assertIn("peach_unblocked", task_component["missing_evidence"])

    def test_draft_mode_progress_counts_only_after_scope_fact_and_blocker_resolution(self) -> None:
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

        valid = evaluate(self.db_path, DEFAULT_SCENARIO_PATH)
        valid_component = next(
            component
            for component in valid["components"]
            if component["key"] == "task_state_improvement"
        )

        self.assertEqual(valid_component["earned"], 10)
        self.assertEqual(valid_component["status"], "partial")
        self.assertEqual(valid_component["evidence"][0]["key"], "peach_unblocked")

    def test_evaluate_explain_prints_component_evidence(self) -> None:
        self._drive_happy_path()

        output = self._run_cli("evaluate", "--explain")

        self.assertIn("Evaluation Explanation", output)
        self.assertIn("Score: 120 / 120", output)
        self.assertIn("+30 blocker_discovery (passed, max 30)", output)
        self.assertIn("+20 stakeholder_communication (passed, max 20)", output)
        self.assertIn("+20 task_state_improvement (passed, max 20)", output)
        self.assertIn("+15 risk_handling (passed, max 15)", output)
        self.assertIn("+10 security_interruption (passed, max 10)", output)
        self.assertIn("+10 portfolio_tradeoff (passed, max 10)", output)
        self.assertIn("Evidence:", output)
        self.assertIn("Stale repo sync risk is discovered in world state.", output)
        self.assertIn("Daisy has received grounded customer-ready Nimbus beta wording.", output)
        self.assertIn("Draft-mode approval is recorded in world state.", output)
        self.assertIn("Luigi pointed the agent to the private repo security baseline.", output)

    def test_evaluate_explain_prints_missing_evidence(self) -> None:
        output = self._run_cli("evaluate", "--explain")

        self.assertIn("Score: 15 / 120", output)
        self.assertIn("+0 stakeholder_communication (missing, max 20)", output)
        self.assertIn("Missing: stakeholder_alignment, customer_message_ready", output)
        self.assertIn("Missing: peach_unblocked, draft_mode_approved", output)
        self.assertIn("Missing: security_doc_found, security_question_answered", output)
        self.assertIn("Missing: koopa_scoped, koopa_update_sent", output)

    def test_documented_noop_baseline_path_is_runnable(self) -> None:
        self._run_cli("reset")
        self._run_cli("advance-time", "to:2026-06-26T15:00:00")

        evaluation = self._run_cli("evaluate", "--explain")
        outcome = self._run_cli("read-doc", "doc_friday_outcome")

        self.assertIn("Score: 15 / 120", evaluation)
        self.assertIn("Late evidence: blocker_discovered.", evaluation)
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
        self.assertIn("Score: 120 / 120", before_deadline)
        self.assertIn("Outcome:  draft_mode_beta_shipped", after_deadline)


class ScriptedAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test.db"

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _run_cli(self, *args: str) -> str:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = cli_main(["--db", str(self.db_path), *args])
        self.assertEqual(exit_code, 0)
        return output.getvalue()

    def test_scripted_agent_reaches_full_score_from_reset(self) -> None:
        result = run_scripted_agent(self.db_path, DEFAULT_SCENARIO_PATH, reset_first=True)

        self.assertTrue(result["ok"])
        self.assertEqual(result["policy"], "scripted")
        self.assertEqual(result["evaluation"]["score"], 120)
        self.assertEqual(result["evaluation"]["score"], result["evaluation"]["max_score"])
        self.assertTrue(result["finalization"]["advanced"])
        self.assertEqual(result["finalization"]["to"], "2026-06-26T15:00:00")

        with connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT metadata_json FROM docs WHERE id = ?",
                ("doc_friday_outcome",),
            ).fetchone()
        self.assertEqual(json.loads(row["metadata_json"])["final_outcome"], "draft_mode_beta_shipped")

    def test_cli_ui_static_writes_html_summary(self) -> None:
        run_scripted_agent(self.db_path, DEFAULT_SCENARIO_PATH, reset_first=True)
        output_path = Path(self.tmpdir.name) / "ui.html"

        output = self._run_cli("ui", "--static", "--output", str(output_path))
        html = output_path.read_text(encoding="utf-8")

        self.assertIn("UI written", output)
        self.assertIn(str(output_path), output)
        self.assertIn("Score:    120 / 120", output)
        self.assertIn("PM Sim Operator UI", html)
        self.assertIn("Draft mode beta shipped", html)
        self.assertIn("Playback", html)
        self.assertIn("Timeline", html)
        self.assertIn("Debug Logs", html)

    def test_scripted_agent_uses_public_tool_actions(self) -> None:
        run_scripted_agent(self.db_path, DEFAULT_SCENARIO_PATH, reset_first=True)

        log = action_log(self.db_path, limit=100)
        action_types = [entry["action_type"] for entry in log]

        self.assertIn("reset", action_types)
        self.assertEqual(action_types.count("send_chat"), 7)
        self.assertEqual(action_types.count("send_email"), 4)
        self.assertEqual(action_types.count("advance_time"), 9)
        self.assertEqual(action_types.count("finalize_to_deadline"), 1)

    def test_cli_run_agent_prints_summary(self) -> None:
        output = self._run_cli("run-agent", "--policy", "scripted", "--reset")

        self.assertIn("Agent Run", output)
        self.assertIn("Policy: scripted", output)
        self.assertIn("Score:  120 / 120", output)
        self.assertIn("Deadline: advanced to Fri 2026-06-26 15:00", output)
        self.assertIn("events: project_deadline", output)
        self.assertIn("outcome: draft_mode_beta_shipped", output)
        self.assertIn("send_security_answer", output)
        self.assertIn("send_final_readiness_note", output)

    def test_run_agent_summary_prints_missing_evidence(self) -> None:
        reset(self.db_path, DEFAULT_SCENARIO_PATH)
        result = evaluate(self.db_path, DEFAULT_SCENARIO_PATH)

        output = format_output(
            "run-agent",
            {
                "ok": False,
                "policy": "llm",
                "model": "test-model",
                "turns": 20,
                "finished": False,
                "stop_reason": "max_turns",
                "steps": [],
                "evaluation": result,
            },
        )

        self.assertIn("Stop:   max turns reached", output)
        self.assertIn("Finish: not called", output)
        self.assertIn("Missing Evaluation", output)
        self.assertIn("security_interruption: security_doc_found, security_question_answered", output)

    def test_long_run_agent_summary_compacts_steps(self) -> None:
        reset(self.db_path, DEFAULT_SCENARIO_PATH)
        evaluation = evaluate(self.db_path, DEFAULT_SCENARIO_PATH)
        steps = [{"name": "send_chat", "ok": True} for _ in range(30)]

        output = format_output(
            "run-agent",
            {
                "ok": False,
                "policy": "llm",
                "model": "test-model",
                "turns": 30,
                "finished": False,
                "stop_reason": "max_turns",
                "steps": steps,
                "evaluation": evaluation,
            },
        )

        self.assertIn("Step Counts", output)
        self.assertIn("send_chat: 30", output)
        self.assertIn("Recent Steps", output)
        self.assertNotIn("  1. send_chat", output)


class LlmAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test.db"

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_llm_agent_rejects_finish_with_visible_obligations_remaining(self) -> None:
        client = _FakeResponsesClient(
            [
                [
                    _function_call("call_1", "read_doc", {"doc_id": "doc_project_brief"}),
                ],
                [_function_call("call_2", "finish", {"reason": "done"})],
            ]
        )

        result = run_llm_agent(
            self.db_path,
            DEFAULT_SCENARIO_PATH,
            reset_first=True,
            model="test-model",
            client=client,
            max_turns=3,
        )
        log = action_log(self.db_path, limit=100)
        action_types = [entry["action_type"] for entry in log]

        self.assertEqual(result["policy"], "llm")
        self.assertEqual(result["model"], "test-model")
        self.assertFalse(result["finished"])
        self.assertEqual(result["stop_reason"], "no_tool_calls")
        self.assertTrue(result["finalization"]["advanced"])
        self.assertEqual(result["finalization"]["to"], "2026-06-26T15:00:00")
        self.assertEqual([step["name"] for step in result["steps"]], ["reset", "read_doc", "finish"])
        self.assertFalse(result["steps"][-1]["result"]["ok"])
        self.assertIn("remaining_obligations", result["steps"][-1]["result"])
        self.assertIn("reset", action_types)
        self.assertNotIn("send_chat", action_types)

    def test_llm_agent_accepts_finish_after_visible_obligations_pass(self) -> None:
        client = _FakeResponsesClient(
            [
                [_function_call("call_1", "finish", {"reason": "too early"})],
                [
                    _function_call(
                        "call_2",
                        "advance_time",
                        {"target": "to:2026-06-26T15:00:00"},
                    )
                ],
                [_function_call("call_3", "finish", {"reason": "calendar complete"})],
            ]
        )

        result = run_llm_agent(
            self.db_path,
            DEFAULT_SCENARIO_PATH,
            reset_first=True,
            model="test-model",
            client=client,
            max_turns=4,
        )

        self.assertTrue(result["finished"])
        self.assertEqual(result["stop_reason"], "agent_finish")
        self.assertEqual(
            [step["name"] for step in result["steps"]],
            ["reset", "finish", "advance_time", "finish"],
        )
        self.assertFalse(result["steps"][1]["result"]["ok"])
        self.assertTrue(result["steps"][-1]["result"]["ok"])

    def test_llm_agent_does_not_expose_evaluator_as_agent_tool(self) -> None:
        client = _FakeResponsesClient([[_function_call("call_1", "finish", {"reason": "done"})]])

        run_llm_agent(
            self.db_path,
            DEFAULT_SCENARIO_PATH,
            reset_first=True,
            model="test-model",
            client=client,
            max_turns=1,
        )

        tool_names = {tool["name"] for tool in client.calls[0]["tools"]}

        self.assertNotIn("evaluate", tool_names)
        self.assertIn("advance_time", tool_names)

    def test_llm_agent_appends_function_outputs_to_model_input(self) -> None:
        client = _FakeResponsesClient(
            [
                [_function_call("call_1", "observe", {})],
                [_function_call("call_2", "finish", {"reason": "observed"})],
            ]
        )

        run_llm_agent(
            self.db_path,
            DEFAULT_SCENARIO_PATH,
            reset_first=True,
            model="test-model",
            client=client,
            max_turns=3,
        )

        second_input = client.calls[1]["input"]
        function_outputs = [
            item for item in second_input if isinstance(item, dict) and item.get("type") == "function_call_output"
        ]

        self.assertEqual(function_outputs[0]["call_id"], "call_1")
        self.assertIn("current_time", function_outputs[0]["output"])
        self.assertIn("calendar_obligations", function_outputs[0]["output"])

    def test_llm_agent_reports_progress(self) -> None:
        messages = []
        client = _FakeResponsesClient(
            [
                [_function_call("call_1", "observe", {})],
                [_function_call("call_2", "finish", {"reason": "observed"})],
            ]
        )

        run_llm_agent(
            self.db_path,
            DEFAULT_SCENARIO_PATH,
            reset_first=True,
            model="test-model",
            client=client,
            max_turns=3,
            progress=messages.append,
        )

        self.assertIn("resetting scenario", messages)
        self.assertTrue(any("waiting for model" in message for message in messages))
        self.assertTrue(any("Mon 2026-06-22 09:00" in message for message in messages))
        self.assertTrue(any("model requested 1 tool call(s): observe" in message for message in messages))
        self.assertTrue(any("OBSERVE — current time" in message for message in messages))

    def test_llm_agent_reports_max_turn_stop_reason(self) -> None:
        client = _FakeResponsesClient(
            [
                [_function_call("call_1", "observe", {})],
            ]
        )

        result = run_llm_agent(
            self.db_path,
            DEFAULT_SCENARIO_PATH,
            reset_first=True,
            model="test-model",
            client=client,
            max_turns=1,
        )

        self.assertFalse(result["finished"])
        self.assertEqual(result["stop_reason"], "max_turns")

    def test_llm_session_persists_model_context_between_steps(self) -> None:
        client = _FakeResponsesClient(
            [
                [_function_call("call_1", "observe", {})],
                [_function_call("call_2", "finish", {"reason": "observed"})],
            ]
        )
        start_llm_session(self.db_path, DEFAULT_SCENARIO_PATH, reset_first=True, model="test-model")

        first = step_llm_session(
            self.db_path,
            DEFAULT_SCENARIO_PATH,
            model="test-model",
            client=client,
            max_turns=3,
        )
        second = step_llm_session(
            self.db_path,
            DEFAULT_SCENARIO_PATH,
            model="test-model",
            client=client,
            max_turns=3,
        )

        second_input = client.calls[1]["input"]
        function_outputs = [
            item for item in second_input if isinstance(item, dict) and item.get("type") == "function_call_output"
        ]

        self.assertEqual(first["turns"], 1)
        self.assertEqual(second["turns"], 2)
        self.assertEqual(function_outputs[0]["call_id"], "call_1")
        self.assertEqual(llm_session_state(self.db_path)["steps"], 3)

    def test_llm_instructions_discourage_chatty_busywork(self) -> None:
        instructions = _instructions()

        self.assertIn("Coworker attention is limited", instructions)
        self.assertIn("smallest useful set of people", instructions)
        self.assertIn("You do not need to simulate every empty hour", instructions)
        self.assertIn("visible calendar obligations", instructions)
        self.assertIn("Call finish only when", instructions)


class _FakeResponsesClient:
    def __init__(self, outputs: list[list[SimpleNamespace]]) -> None:
        self._outputs = outputs
        self.calls: list[dict[str, Any]] = []
        self.responses = self

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        output = self._outputs.pop(0) if self._outputs else []
        return SimpleNamespace(output=output, output_text="")


def _function_call(call_id: str, name: str, arguments: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        type="function_call",
        call_id=call_id,
        name=name,
        arguments=json.dumps(arguments),
    )


if __name__ == "__main__":
    unittest.main()
