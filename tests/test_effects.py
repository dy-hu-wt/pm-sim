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
                        "type": "record_milestone",
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
                "SELECT milestone_id FROM milestones WHERE milestone_id = 'blocker_discovered'"
            ).fetchone()

            self.assertEqual(fact["visible_at"], "2026-06-22T11:00:00")
            self.assertEqual(blocker["status"], "surfaced")
            self.assertEqual(blocker["visible_at"], "2026-06-22T11:00:00")
            self.assertEqual(task["status"], "in_progress")
            self.assertEqual(loads(project["metadata_json"])["decision"], "draft_mode_approved")
            self.assertEqual(evidence["milestone_id"], "blocker_discovered")
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

    def test_pressure_effects_mutate_bounded_pressure_state(self) -> None:
        conn = connect(self.db_path)
        try:
            initial = conn.execute(
                """
                SELECT intensity
                FROM pressures
                WHERE id = 'pressure_nimbus_customer_confidence'
                """
            ).fetchone()

            raised = apply_effects(
                conn,
                [
                    {
                        "type": "increase_pressure",
                        "pressure_id": "pressure_nimbus_customer_confidence",
                        "by": 9,
                        "reason": "Customer update window was missed.",
                    }
                ],
                now="2026-06-25T10:00:00",
                source="test",
            )
            lowered = apply_effects(
                conn,
                [
                    {
                        "type": "lower_pressure",
                        "pressure_id": "pressure_nimbus_customer_confidence",
                        "to": 2,
                        "reason": "Daisy received grounded wording.",
                    }
                ],
                now="2026-06-25T10:20:00",
                source="test",
            )
            conn.commit()

            row = conn.execute(
                """
                SELECT intensity, reason, updated_at
                FROM pressures
                WHERE id = 'pressure_nimbus_customer_confidence'
                """
            ).fetchone()

            self.assertEqual(initial["intensity"], 5)
            self.assertEqual(raised[0]["intensity"], 10)
            self.assertEqual(lowered[0]["previous_intensity"], 10)
            self.assertEqual(row["intensity"], 2)
            self.assertEqual(row["reason"], "Daisy received grounded wording.")
            self.assertEqual(row["updated_at"], "2026-06-25T10:20:00")
            self.assertTrue(
                condition_matches(
                    conn,
                    {
                        "pressure_at_most": {
                            "id": "pressure_nimbus_customer_confidence",
                            "intensity": 3,
                        }
                    },
                )
            )
            self.assertFalse(
                condition_matches(
                    conn,
                    {
                        "pressure_at_least": {
                            "id": "pressure_nimbus_customer_confidence",
                            "intensity": 8,
                        }
                    },
                )
            )
        finally:
            conn.close()

    def test_repeated_coworker_state_value_preserves_first_achievement_time(self) -> None:
        conn = connect(self.db_path)
        try:
            apply_effects(
                conn,
                [
                    {
                        "type": "update_coworker_state",
                        "person_id": "daisy",
                        "key": "customer_message_ready",
                        "value": True,
                    }
                ],
                now="2026-06-22T11:00:00",
                source="first",
            )
            apply_effects(
                conn,
                [
                    {
                        "type": "update_coworker_state",
                        "person_id": "daisy",
                        "key": "customer_message_ready",
                        "value": True,
                    }
                ],
                now="2026-06-24T11:00:00",
                source="repeat",
            )
            conn.commit()

            row = conn.execute(
                """
                SELECT value_json, updated_at
                FROM coworker_state
                WHERE person_id = 'daisy'
                  AND key = 'customer_message_ready'
                """
            ).fetchone()

            self.assertTrue(loads(row["value_json"]))
            self.assertEqual(row["updated_at"], "2026-06-22T11:00:00")
        finally:
            conn.close()

    def test_actor_runtime_effects_update_schema_tables(self) -> None:
        conn = connect(self.db_path)
        try:
            applied = apply_effects(
                conn,
                [
                    {
                        "type": "update_actor_workload",
                        "person_id": "daisy",
                        "current_focus": "customer launch wording",
                        "load_level": "high",
                        "capacity_minutes_remaining": 90,
                    },
                    {
                        "type": "add_actor_commitment",
                        "id": "commitment_test_customer_note",
                        "person_id": "daisy",
                        "project_id": "project_pr_review_agent",
                        "commitment_type": "customer_update",
                        "description": "Send customer-ready launch wording.",
                        "due_at": "2026-06-25T12:00:00",
                    },
                    {
                        "type": "update_actor_commitment",
                        "id": "commitment_test_customer_note",
                        "status": "done",
                    },
                ],
                now="2026-06-22T11:00:00",
                source="test",
            )
            conn.commit()

            workload = conn.execute(
                """
                SELECT current_focus, capacity_minutes_remaining, load_level
                FROM actor_workload
                WHERE person_id = 'daisy'
                """
            ).fetchone()
            commitment = conn.execute(
                """
                SELECT status, commitment_type
                FROM actor_commitments
                WHERE id = 'commitment_test_customer_note'
                """
            ).fetchone()

            self.assertEqual([row["type"] for row in applied], [
                "update_actor_workload",
                "add_actor_commitment",
                "update_actor_commitment",
            ])
            self.assertEqual(workload["current_focus"], "customer launch wording")
            self.assertEqual(workload["capacity_minutes_remaining"], 90)
            self.assertEqual(workload["load_level"], "high")
            self.assertEqual(commitment["status"], "done")
            self.assertEqual(commitment["commitment_type"], "customer_update")
        finally:
            conn.close()

    def test_llm_concept_match_failure_fails_closed(self) -> None:
        original = concept_match_module._llm_match
        concept_match_module._llm_match = lambda text, criteria, *, model=None: (_ for _ in ()).throw(
            json.JSONDecodeError("Expecting value", "", 0)
        )
        conn = connect(self.db_path)
        try:
            with unittest.mock.patch.dict(
                "os.environ",
                {"OPENAI_API_KEY": "test-key"},
                clear=False,
            ):
                result = concept_match_module.concept_match(
                    conn,
                    text="Use use draft mode with human approval.",
                    criteria={
                        "required": [
                            {
                                "id": "draft_mode_approval",
                                "description": "Use use draft mode with human approval.",
                                "exemplars": ["use draft mode with human approval"],
                            }
                        ]
                    },
                    rule_id="test_rule",
                )
            conn.commit()
            cached = conn.execute(
                "SELECT value FROM sim_state WHERE key = 'concept_match_cache_json'"
            ).fetchone()

            self.assertFalse(result["matches"])
            self.assertEqual(result["mode"], "concept_match")
            self.assertEqual(result["matcher"], "llm")
            self.assertIn("JSONDecodeError", result["error"])
            self.assertIsNotNone(cached)
        finally:
            concept_match_module._llm_match = original
            conn.close()

    def test_concept_match_requires_openai_api_key(self) -> None:
        original = concept_match_module._llm_match
        concept_match_module._llm_match = lambda text, criteria, *, model=None: (_ for _ in ()).throw(
            RuntimeError("Concept matching requires OPENAI_API_KEY.")
        )
        conn = connect(self.db_path)
        try:
            with unittest.mock.patch.object(concept_match_module, "_load_dotenv", lambda: None):
                result = concept_match_module.concept_match(
                    conn,
                    text="Use use draft mode with human approval.",
                    criteria={
                        "required": [
                            {
                                "id": "draft_mode_approval",
                                "description": "Use use draft mode with human approval.",
                                "exemplars": ["use draft mode with human approval"],
                            }
                        ]
                    },
                    rule_id="test_missing_key",
                )

            self.assertFalse(result["matches"])
            self.assertEqual(result["matcher"], "llm")
            self.assertIn("OPENAI_API_KEY", result["error"])
        finally:
            concept_match_module._llm_match = original
            conn.close()

    def test_concept_cache_uses_model(self) -> None:
        conn = connect(self.db_path)
        original = concept_match_module._llm_match
        try:
            concept_match_module._llm_match = lambda text, criteria, *, model=None: {
                "matches": True,
                "mode": "concept_match",
                "matcher": "llm",
                "model": model,
                "required": [{"id": "draft_mode_approval", "matched": True, "rationale": "ok"}],
                "forbidden": [],
            }
            with unittest.mock.patch.dict("os.environ", {"PM_SIM_CONCEPT_MODEL": "model-a"}, clear=False):
                first = concept_match_module.concept_match(
                    conn,
                    text="Use draft mode with human approval.",
                    criteria={
                        "required": [
                            {
                                "id": "draft_mode_approval",
                                "description": "Use draft mode with human approval.",
                            }
                        ]
                    },
                    rule_id="test_cache_key",
                )
            with unittest.mock.patch.dict("os.environ", {"PM_SIM_CONCEPT_MODEL": "model-b"}, clear=False):
                second = concept_match_module.concept_match(
                    conn,
                    text="Use draft mode with human approval.",
                    criteria={
                        "required": [
                            {
                                "id": "draft_mode_approval",
                                "description": "Use draft mode with human approval.",
                            }
                        ]
                    },
                    rule_id="test_cache_key",
                )

            self.assertEqual(first["matcher"], "llm")
            self.assertEqual(second["matcher"], "llm")
            self.assertNotEqual(first["cache_key"], second["cache_key"])
        finally:
            concept_match_module._llm_match = original
            conn.close()

    def test_local_concept_match_does_not_require_openai_api_key(self) -> None:
        conn = connect(self.db_path)
        try:
            with unittest.mock.patch.dict(
                "os.environ",
                {"PM_SIM_CONCEPT_MODE": "local"},
                clear=True,
            ):
                result = concept_match_module.concept_match(
                    conn,
                    text="Use draft mode with human approval before posting.",
                    criteria={
                        "required": [
                            {
                                "id": "draft_mode",
                                "description": "Friday launch mode is draft mode.",
                                "exemplars": ["use draft mode with human approval before posting"],
                                "must_be_asserted": True,
                            }
                        ]
                    },
                    rule_id="test_local_mode",
                )

            self.assertTrue(result["matches"])
            self.assertEqual(result["matcher"], "local")
        finally:
            conn.close()

    def test_concept_cache_separates_llm_and_local_modes(self) -> None:
        conn = connect(self.db_path)
        original = concept_match_module._llm_match
        try:
            concept_match_module._llm_match = lambda text, criteria, *, model=None: {
                "matches": True,
                "mode": "concept_match",
                "matcher": "llm",
                "model": model,
                "required": [{"id": "draft_mode_approval", "matched": True, "rationale": "ok"}],
                "forbidden": [],
            }
            with unittest.mock.patch.dict(
                "os.environ", {"PM_SIM_CONCEPT_MODE": "llm", "PM_SIM_CONCEPT_MODEL": "model-a"}, clear=False
            ):
                first = concept_match_module.concept_match(
                    conn,
                    text="Use draft mode with human approval.",
                    criteria={
                        "required": [
                            {"id": "draft_mode_approval", "description": "Use draft mode with human approval."}
                        ]
                    },
                    rule_id="test_mode_cache_key",
                )
            with unittest.mock.patch.dict("os.environ", {"PM_SIM_CONCEPT_MODE": "local"}, clear=False):
                second = concept_match_module.concept_match(
                    conn,
                    text="Use draft mode with human approval.",
                    criteria={
                        "required": [
                            {
                                "id": "draft_mode_approval",
                                "description": "Use draft mode with human approval.",
                                "exemplars": ["use draft mode with human approval"],
                            }
                        ]
                    },
                    rule_id="test_mode_cache_key",
                )

            self.assertEqual(first["matcher"], "llm")
            self.assertEqual(second["matcher"], "local")
            self.assertNotEqual(first["cache_key"], second["cache_key"])
        finally:
            concept_match_module._llm_match = original
            conn.close()

    def test_llm_result_requires_authored_ids_and_rationales(self) -> None:
        criteria = {
            "required": [
                {
                    "id": "human_approval_before_posting",
                    "description": "A human must approve before posting.",
                    "exemplars": ["comments require human approval before posting"],
                }
            ],
            "forbidden": [],
        }

        result = concept_match_module._validate_llm_result(
            {
                "matches": True,
                "mode": "concept_match",
                "matcher": "llm",
                "model": "test-model",
                "required": [{"id": "wrong_id", "matched": True, "rationale": "Looks close."}],
                "forbidden": [],
            },
            criteria,
        )

        self.assertFalse(result["matches"])
        self.assertIn("authored concept ids", result["error"])

    def test_duplicate_milestones_is_idempotent(self) -> None:
        conn = connect(self.db_path)
        try:
            first = apply_effects(
                conn,
                [
                    {
                        "type": "record_milestone",
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
                        "type": "record_milestone",
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
                FROM milestones
                WHERE milestone_id = 'blocker_discovered'
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

    def test_email_coworker_reply_event_creates_email_message(self) -> None:
        send_email(
            self.db_path,
            "daisy",
            "Friday confidence",
            "I am checking the launch risk and will follow up.",
        )

        result = advance_time(self.db_path, "until_next_event")
        state = observe(self.db_path)
        reply = state["recent_messages"][0]

        self.assertEqual(result["delivered_events"][0]["event_type"], "coworker_reply")
        self.assertTrue(result["delivered_events"][0]["result"]["handled"])
        self.assertEqual(reply["channel"], "email")
        self.assertEqual(reply["sender_id"], "daisy")
        self.assertEqual(reply["recipient_id"], "agent")
        self.assertEqual(reply["subject"], "Re: received")

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
            evaluation = evaluate(self.db_path, DEFAULT_SCENARIO_PATH)
            milestone_ids = {
                evidence["key"]
                for component in evaluation["components"]
                for evidence in component.get("milestones", [])
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
            self.assertIn("blocker_discovered", milestone_ids)
            self.assertIn("stakeholder_alignment", milestone_ids)
            self.assertIn("draft_mode_approved", milestone_ids)
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
        evaluation = evaluate(self.db_path, DEFAULT_SCENARIO_PATH)
        milestone_ids = {
            evidence["key"]
            for component in evaluation["components"]
            for evidence in component.get("milestones", [])
        }

        fact_ids = {fact["id"] for fact in state["discovered_facts"]}

        self.assertIn("fact_repo_sync_stale", fact_ids)
        self.assertIn("fact_nimbus_values_reliability", fact_ids)
        self.assertIn("fact_draft_mode_scope_confirmed", fact_ids)
        self.assertIn("fact_draft_mode_approved", fact_ids)
        self.assertIn("blocker_discovered", milestone_ids)
        self.assertIn("stakeholder_alignment", milestone_ids)
        self.assertIn("peach_unblocked", milestone_ids)
        self.assertIn("draft_mode_approved", milestone_ids)
