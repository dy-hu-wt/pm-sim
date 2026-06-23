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
from pm_sim.formatters import format_agent_progress_html, format_output, format_semantic_progress
from pm_sim.jsonutil import loads
from pm_sim.paths import DEFAULT_SCENARIO_PATH
from pm_sim.report import generate_report
from pm_sim.scenario import ScenarioError, load_scenario
from pm_sim import semantic_match as semantic_match_module
from pm_sim.state import action_log, event_log, observe, reset
from pm_sim.engine.time import advance_time
from pm_sim.timeline import timeline
from pm_sim.ui import _html, _run_next_ui_step, _scripted_demo_state, _state_payload

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

    def test_list_tasks_includes_business_timing_orientation(self) -> None:
        tasks = {task["id"]: task for task in list_tasks(self.db_path)}

        self.assertIn("Thursday morning", tasks["task_customer_talk_track"]["description"])
        self.assertIn("the Thursday security review", tasks["task_koopa_status_update"]["description"])
        self.assertIn("Thursday final-readiness check", tasks["task_beta_rollout_notes"]["description"])

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
            any(
                effect.get("type") == "update_coworker_state"
                and effect.get("person_id") == "toad"
                and "decision_record_written" in effect.get("keys", [])
                for effect in valid["applied_effects"]
            )
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
            any(
                effect.get("type") == "update_coworker_state"
                and effect.get("person_id") == "toad"
                and "decision_record_written" in effect.get("keys", [])
                for effect in result["applied_effects"]
            )
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
            any(
                effect.get("type") == "update_coworker_state"
                and effect.get("person_id") == "toad"
                and "decision_record_written" in effect.get("keys", [])
                for effect in result["applied_effects"]
            )
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
            any(
                effect.get("type") == "update_coworker_state"
                and effect.get("person_id") == "toad"
                and "decision_record_written" in effect.get("keys", [])
                for effect in result["applied_effects"]
            )
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
            state = conn.execute(
                """
                SELECT value_json
                FROM coworker_state
                WHERE person_id = 'luigi'
                  AND key = 'security_doc_shared'
                """
            ).fetchone()
        finally:
            conn.close()

        self.assertFalse(hidden["ok"])
        self.assertTrue(revealed["ok"])
        self.assertIn("Raw source code is not stored long term", revealed["doc"]["body"])
        self.assertTrue(loads(state["value_json"]))

    def test_private_repo_security_reply_is_actor_behavior_driven(self) -> None:
        scenario = load_scenario(DEFAULT_SCENARIO_PATH)
        scenario["actor_behaviors"] = []
        scenario_path = Path(self.tmpdir.name) / "no_actor_behaviors.json"
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

    def test_send_email_records_message_and_schedules_coworker_reply(self) -> None:
        result = send_email(
            self.db_path,
            "daisy",
            "Friday confidence",
            "I am checking the launch risk and will follow up.",
        )
        state = observe(self.db_path)
        events = event_log(self.db_path, limit=20)
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
        self.assertEqual(len(result["scheduled_reply_ids"]), 1)
        reply_events = [event for event in events if event["event_type"] == "coworker_reply"]
        self.assertEqual(len(reply_events), 1)
        self.assertEqual(reply_events[0]["scheduled_at"], "2026-06-22T09:45:00")
        reply_payload = loads(reply_events[0]["payload_json"])
        self.assertEqual(reply_payload["channel"], "email")
        self.assertEqual(reply_payload["subject"], "Re: received")
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

    def test_substantive_daisy_email_after_discovery_records_customer_ready_state(self) -> None:
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
            state = conn.execute(
                """
                SELECT value_json
                FROM coworker_state
                WHERE person_id = 'daisy'
                  AND key = 'customer_message_ready'
                """
            ).fetchone()
        finally:
            conn.close()

        self.assertTrue(result["ok"])
        self.assertTrue(
            any(
                effect.get("type") == "update_coworker_state"
                and effect.get("person_id") == "daisy"
                and "customer_message_ready" in effect.get("keys", [])
                for effect in result["applied_effects"]
            )
        )
        self.assertTrue(loads(state["value_json"]))

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
        self.assertTrue(result["semantic_matches"])
        self.assertIn(
            "grading_customer_message_ready_action",
            {match["rule_id"] for match in result["semantic_matches"]},
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

    def test_schedule_meeting_rejects_short_meetings(self) -> None:
        result = schedule_meeting(
            self.db_path,
            "Too-short risk review",
            "2026-06-22T10:00:00",
            "2026-06-22T10:05:00",
            ["luigi"],
        )
        events = event_log(self.db_path, limit=20)

        self.assertFalse(result["ok"])
        self.assertIn("at least 10 minutes", result["error"])
        self.assertFalse(any(event["event_type"] == "meeting_occurs" for event in events))

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
