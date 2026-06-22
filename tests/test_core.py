from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from pm_sim.actions import (
    list_tasks,
    read_doc,
    schedule_meeting,
    send_chat,
    send_email,
    update_task,
)
from pm_sim.cli import main as cli_main
from pm_sim.coworkers import effects_for_event, replies_for_chat
from pm_sim.db import connect
from pm_sim.evaluator import evaluate
from pm_sim.effects import apply_effects
from pm_sim.jsonutil import loads
from pm_sim.paths import DEFAULT_SCENARIO_PATH
from pm_sim.state import event_log, observe, reset
from pm_sim.time import advance_time
from pm_sim.timeline import timeline


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
        self.assertEqual(len(state["projects"]), 1)
        self.assertGreaterEqual(len(state["tasks"]), 5)

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

        blocker_ids = {blocker["id"] for blocker in state["known_blockers"]}
        fact_ids = {fact["id"] for fact in state["discovered_facts"]}
        recent_bodies = [message["body"] for message in state["recent_messages"]]

        self.assertIn("blocker_repo_sync_stale", blocker_ids)
        self.assertIn("fact_repo_sync_stale", fact_ids)
        self.assertTrue(any("repo sync" in body for body in recent_bodies))

    def test_customer_launch_mode_question_adds_pressure_event(self) -> None:
        result = advance_time(self.db_path, "to:2026-06-24T15:30:00")
        state = observe(self.db_path)

        event_types = {event["event_type"] for event in result["delivered_events"]}
        recent = state["recent_messages"][0]

        self.assertIn("nimbus_launch_mode_question", event_types)
        self.assertEqual(recent["sender_id"], "daisy")
        self.assertEqual(recent["channel"], "email")
        self.assertIn("post comments automatically", recent["body"])

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
                SELECT kind, body
                FROM docs
                WHERE id = 'doc_friday_outcome'
                """
            ).fetchone()
        finally:
            conn.close()

        self.assertIn("friday_nimbus_deadline", event_types)
        self.assertTrue(all(event["result"]["handled"] for event in result["delivered_events"]))
        self.assertEqual(project["status"], "missed")
        self.assertEqual(project["risk_level"], "high")
        self.assertEqual(loads(project["metadata_json"])["final_outcome"], "no_approved_friday_plan")
        self.assertEqual(outcome_doc["kind"], "outcome_report")
        self.assertIn("without an approved reliable launch plan", outcome_doc["body"])

    def test_friday_deadline_records_successful_draft_mode_outcome(self) -> None:
        schedule_meeting(
            self.db_path,
            "Draft-mode risk review for Nimbus launch",
            "2026-06-22T10:00:00",
            "2026-06-22T10:30:00",
            ["luigi", "daisy", "mario", "toad", "peach"],
        )

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
        self.assertIn("shipped the reliable draft-mode beta", outcome_doc["body"])

    def test_events_delivered_during_large_time_jump_keep_scheduled_times(self) -> None:
        schedule_meeting(
            self.db_path,
            "Draft-mode risk review for Nimbus launch",
            "2026-06-22T10:00:00",
            "2026-06-22T10:30:00",
            ["luigi", "daisy", "mario", "toad", "peach"],
        )

        result = advance_time(self.db_path, "to:2026-06-26T15:00:00")
        meeting_event = next(
            event for event in result["delivered_events"] if event["event_type"] == "meeting_occurs"
        )
        evaluation = evaluate(self.db_path, DEFAULT_SCENARIO_PATH)

        self.assertEqual(meeting_event["delivered_at"], "2026-06-22T10:30:00")
        self.assertEqual(evaluation["score"], evaluation["max_score"])

    def test_timeline_shows_actions_events_messages_and_evidence_in_order(self) -> None:
        send_chat(self.db_path, "luigi", "Any repo sync blockers for launch?")
        advance_time(self.db_path, "2h")

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


class CoworkerRuleTests(unittest.TestCase):
    def test_luigi_reveals_repo_sync_risk_when_asked_about_blockers(self) -> None:
        replies = replies_for_chat("luigi", "Any blockers or repo sync stale-code risk for launch?")

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
            {"discovered_facts": ["fact_repo_sync_stale"]},
        )

        self.assertEqual(len(replies), 1)
        self.assertIn("Same repo sync risk", replies[0].body)
        self.assertEqual(replies[0].effects, ())

    def test_background_event_has_deterministic_effects(self) -> None:
        effects = effects_for_event(
            "luigi_proactive_repo_risk",
            {
                "project_id": "project_pr_review_agent",
                "blocker_id": "blocker_repo_sync_stale",
            },
        )

        self.assertGreaterEqual(len(effects), 3)
        self.assertEqual(effects[0]["type"], "create_message")
        self.assertIn("repo sync", effects[0]["body"])


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
                "SELECT discovered_at FROM facts WHERE id = 'fact_repo_sync_stale'"
            ).fetchone()
            blocker = conn.execute(
                "SELECT status, discovered_at FROM blockers WHERE id = 'blocker_repo_sync_stale'"
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

            self.assertEqual(fact["discovered_at"], "2026-06-22T11:00:00")
            self.assertEqual(blocker["status"], "surfaced")
            self.assertEqual(blocker["discovered_at"], "2026-06-22T11:00:00")
            self.assertEqual(task["status"], "in_progress")
            self.assertEqual(loads(project["metadata_json"])["decision"], "draft_mode_approved")
            self.assertEqual(evidence["evidence_key"], "blocker_discovered")
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

        result = advance_time(self.db_path, "2h")
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
            ["luigi", "daisy", "mario", "toad"],
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
                SELECT title, kind, body, visible
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
            self.assertEqual(transcript["visible"], 1)
            self.assertIn("repo sync", transcript["body"])
            self.assertIn("blocker_repo_sync_stale", blocker_ids)
            self.assertIn("fact_repo_sync_stale", fact_ids)
            self.assertIn("fact_draft_mode_approved", fact_ids)
            self.assertIn("blocker_discovered", evidence_keys)
            self.assertIn("stakeholder_alignment", evidence_keys)
            self.assertIn("draft_mode_approved", evidence_keys)
        finally:
            conn.close()


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

    def test_read_doc_returns_rollout_template(self) -> None:
        result = read_doc(self.db_path, "doc_beta_rollout_template")

        self.assertTrue(result["ok"])
        self.assertIn("human approval", result["doc"]["body"])

    def test_read_doc_blocks_invisible_doc(self) -> None:
        result = read_doc(self.db_path, "doc_repo_sync_notes")

        self.assertFalse(result["ok"])
        self.assertIn("not visible", result["error"])

    def test_send_chat_schedules_coworker_reply(self) -> None:
        result = send_chat(self.db_path, "luigi", "Any repo sync blockers for launch?")
        events = event_log(self.db_path, limit=20)

        self.assertTrue(result["ok"])
        self.assertEqual(len(result["scheduled_reply_ids"]), 1)
        reply_events = [event for event in events if event["event_type"] == "coworker_reply"]
        self.assertEqual(len(reply_events), 1)
        self.assertIn("repo sync", reply_events[0]["payload_json"])

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
        self.assertEqual(result["applied_effects"], [])
        self.assertEqual(evidence_count, 0)
        message = next(
            message for message in state["recent_messages"] if message["id"] == result["message_id"]
        )
        self.assertEqual(message["channel"], "email")
        self.assertEqual(message["recipient_id"], "daisy")

    def test_substantive_daisy_email_records_stakeholder_evidence(self) -> None:
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
        self.assertEqual(result["applied_effects"][0]["type"], "add_evaluation_evidence")
        self.assertEqual(evidence["evidence_key"], "stakeholder_alignment")
        self.assertIn("Nimbus repo-sync risk and draft-mode", evidence["note"])

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
        meeting_events = [event for event in events if event["event_type"] == "meeting_occurs"]
        self.assertEqual(len(meeting_events), 1)
        self.assertEqual(meeting_events[0]["scheduled_at"], "2026-06-22T10:30:00")
        self.assertIn(result["meeting_id"], meeting_events[0]["payload_json"])

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

    def _drive_happy_path(self) -> None:
        send_chat(self.db_path, "luigi", "Any repo sync blockers for launch?")
        advance_time(self.db_path, "2h")
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

    def test_reset_state_scores_below_agent_improved_path(self) -> None:
        baseline = evaluate(self.db_path, DEFAULT_SCENARIO_PATH)

        self._drive_happy_path()
        improved = evaluate(self.db_path, DEFAULT_SCENARIO_PATH)

        self.assertLess(baseline["score"], improved["score"])
        self.assertEqual(improved["score"], improved["max_score"])
        component_scores = {
            component["key"]: component["earned"] for component in improved["components"]
        }
        self.assertEqual(component_scores["blocker_discovery"], 30)
        self.assertEqual(component_scores["risk_handling"], 15)

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

    def test_harmful_task_completion_is_penalized(self) -> None:
        update_task(self.db_path, "task_repo_sync", status="complete", priority=None)

        result = evaluate(self.db_path, DEFAULT_SCENARIO_PATH)
        harmful_component = next(
            component
            for component in result["components"]
            if component["key"] == "avoid_harmful_actions"
        )

        self.assertEqual(harmful_component["earned"], 0)
        self.assertTrue(harmful_component["detected_harms"])

    def test_substantive_daisy_email_can_satisfy_stakeholder_communication(self) -> None:
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
        self.assertEqual(stakeholder_component["evidence"][0]["source"], "action:msg_agent_email_3")

    def test_fake_draft_mode_progress_does_not_improve_task_score(self) -> None:
        baseline = evaluate(self.db_path, DEFAULT_SCENARIO_PATH)

        update_task(self.db_path, "task_draft_mode_docs", status="complete", priority=None)
        result = evaluate(self.db_path, DEFAULT_SCENARIO_PATH)
        task_component = next(
            component
            for component in result["components"]
            if component["key"] == "task_state_improvement"
        )

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
        self.assertIn("Score: 100 / 100", output)
        self.assertIn("+30 blocker_discovery (passed, max 30)", output)
        self.assertIn("+20 stakeholder_communication (passed, max 20)", output)
        self.assertIn("+20 task_state_improvement (passed, max 20)", output)
        self.assertIn("+15 risk_handling (passed, max 15)", output)
        self.assertIn("Evidence:", output)
        self.assertIn("Stale repo sync risk is discovered in world state.", output)
        self.assertIn("Draft-mode approval is recorded in world state.", output)

    def test_evaluate_explain_prints_missing_evidence(self) -> None:
        output = self._run_cli("evaluate", "--explain")

        self.assertIn("Score: 15 / 100", output)
        self.assertIn("+0 stakeholder_communication (missing, max 20)", output)
        self.assertIn("Missing: stakeholder_alignment", output)
        self.assertIn("Missing: peach_unblocked, draft_mode_approved", output)


if __name__ == "__main__":
    unittest.main()
