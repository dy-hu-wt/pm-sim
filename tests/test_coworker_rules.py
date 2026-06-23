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

class ActorBehaviorTests(unittest.TestCase):
    def setUp(self) -> None:
        scenario = load_scenario(DEFAULT_SCENARIO_PATH)
        self.actor_behaviors = scenario.get("actor_behaviors", [])
        self.response_delays = {
            person["id"]: person["response_delay_minutes"]
            for person in scenario.get("people", [])
        }

    def _state(self, facts: list[str] | None = None) -> dict[str, Any]:
        return {
            "discovered_facts": facts or [],
            "actor_behaviors": self.actor_behaviors,
            "response_delays": self.response_delays,
        }

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

    def test_email_rules_return_email_channel_reply(self) -> None:
        replies = replies_for_email(
            "daisy",
            "Nimbus customer wording",
            "Please confirm you received the customer-ready wording.",
            self._state(),
        )

        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0].channel, "email")
        self.assertEqual(replies[0].subject, "Re: received")
        self.assertIn("email as the source of truth", replies[0].body)
        self.assertEqual(replies[0].effects, ())

    def test_coworker_combines_multiple_matched_concerns(self) -> None:
        behaviors = [
            {
                "id": "luigi_risk_answer",
                "kind": "reply",
                "person_id": "luigi",
                "channel": "chat",
                "priority": 100,
                "match": {
                    "mode": "deterministic",
                    "intents": [
                        {
                            "id": "risk",
                            "description": "Agent asks about risk.",
                            "signals": ["risk"],
                        }
                    ],
                    "require_all": ["risk"],
                },
                "reply": {"delay_minutes": 20, "body": "Repo sync risk exists."},
                "effects": [{"type": "discover_fact", "fact_id": "fact_repo_sync_stale"}],
            },
            {
                "id": "luigi_security_answer",
                "kind": "reply",
                "person_id": "luigi",
                "channel": "chat",
                "priority": 90,
                "match": {
                    "mode": "deterministic",
                    "intents": [
                        {
                            "id": "security",
                            "description": "Agent asks about security.",
                            "signals": ["security"],
                        }
                    ],
                    "require_all": ["security"],
                },
                "reply": {"delay_minutes": 10, "body": "Use the security baseline."},
                "effects": [{"type": "reveal_doc", "doc_id": "doc_private_repo_security_baseline"}],
            },
        ]

        replies = replies_for_chat(
            "luigi",
            "Can you cover repo risk and security?",
            {"actor_behaviors": behaviors, "response_delays": {"luigi": 120}},
        )

        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0].delay_minutes, 20)
        self.assertIn("Repo sync risk exists.", replies[0].body)
        self.assertIn("Use the security baseline.", replies[0].body)
        self.assertEqual(
            replies[0].matched_rule_ids,
            ("luigi_risk_answer", "luigi_security_answer"),
        )
        self.assertEqual({effect["type"] for effect in replies[0].effects}, {"discover_fact", "reveal_doc"})

    def test_coworker_does_not_combine_generic_fallback_with_specific_reply(self) -> None:
        behaviors = [
            {
                "id": "specific",
                "kind": "reply",
                "person_id": "luigi",
                "channel": "chat",
                "priority": 100,
                "match": {
                    "mode": "deterministic",
                    "intents": [
                        {"id": "risk", "description": "Risk question.", "signals": ["risk"]}
                    ],
                },
                "reply": {"delay_minutes": 10, "body": "Specific risk answer."},
            },
            {
                "id": "fallback",
                "kind": "reply",
                "person_id": "luigi",
                "channel": "chat",
                "priority": 0,
                "match": {"mode": "deterministic"},
                "reply": {"delay_minutes": 10, "body": "Generic fallback."},
            },
        ]

        replies = replies_for_chat(
            "luigi",
            "risk?",
            {"actor_behaviors": behaviors, "response_delays": {"luigi": 120}},
        )

        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0].body, "Specific risk answer.")

    def test_actor_planner_combines_matched_reply_with_capacity_constraint(self) -> None:
        behaviors = [
            {
                "id": "luigi_risk_answer",
                "kind": "reply",
                "person_id": "luigi",
                "channel": "chat",
                "priority": 100,
                "match": {
                    "mode": "deterministic",
                    "intents": [
                        {"id": "risk", "description": "Risk question.", "signals": ["risk"]}
                    ],
                },
                "reply": {"delay_minutes": 20, "body": "Repo sync risk exists."},
            },
        ]

        replies = replies_for_chat(
            "luigi",
            "Can you explain the risk and can you implement the export today?",
            {
                "actor_behaviors": behaviors,
                "response_delays": {"luigi": 120},
                "actor_workload": {
                    "luigi": {
                        "current_focus": "repo sync hardening",
                        "load_level": "overloaded",
                        "capacity_minutes_remaining": 0,
                    }
                },
            },
        )

        self.assertEqual(len(replies), 1)
        self.assertIn("Repo sync risk exists.", replies[0].body)
        self.assertIn("I am at capacity on repo sync hardening", replies[0].body)
        self.assertIn("agenda_capacity_constraint", replies[0].matched_rule_ids)

    def test_actor_planner_references_open_commitment_memory(self) -> None:
        replies = replies_for_chat(
            "daisy",
            "What is happening with the Koopa scoped answer?",
            {
                "actor_behaviors": [],
                "response_delays": {"daisy": 45},
                "actor_commitments": [
                    {
                        "id": "commitment_daisy_koopa_scoped_answer",
                        "person_id": "daisy",
                        "description": "Send Koopa a scoped audit export answer.",
                        "status": "open",
                    }
                ],
            },
        )

        self.assertEqual(len(replies), 1)
        self.assertIn("open commitment", replies[0].body)
        self.assertIn("Koopa", replies[0].body)
        self.assertIn(
            "agenda_commitment_commitment_daisy_koopa_scoped_answer",
            replies[0].matched_rule_ids,
        )

    def test_actor_planner_challenges_message_that_contradicts_recorded_decision(self) -> None:
        replies = replies_for_chat(
            "toad",
            "Let's switch back to auto-commenting for Friday.",
            {
                "actor_behaviors": [],
                "response_delays": {"toad": 90},
                "project_decisions": {"project_pr_review_agent": "draft_mode_approved"},
            },
        )

        self.assertEqual(len(replies), 1)
        self.assertIn("conflicts with the recorded draft-mode decision", replies[0].body)
        self.assertIn("agenda_contradicts_draft_decision", replies[0].matched_rule_ids)

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
        behaviors = copy.deepcopy(self.actor_behaviors)
        rule = next(rule for rule in behaviors if rule["id"] == "luigi_private_repo_security_doc")
        del rule["reply"]["delay_minutes"]

        replies = replies_for_chat(
            "luigi",
            "Any private repo security docs?",
            {
                "discovered_facts": [],
                "actor_behaviors": behaviors,
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
        self.assertIn(
            {
                "type": "update_coworker_state",
                "person_id": "daisy",
                "key": "reliability_preference_shared",
                "value": True,
            },
            concrete[0].effects,
        )

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
        self.assertIn(
            {
                "type": "update_coworker_state",
                "person_id": "peach",
                "key": "scope_unblocked",
                "value": True,
            },
            ready[0].effects,
        )

    def test_peach_accepts_out_of_scope_auto_commenting_wording(self) -> None:
        replies = replies_for_chat(
            "peach",
            (
                "Implement only the draft-mode onboarding path for Friday with human approval. "
                "Auto-commenting is out of Friday scope and should not be included."
            ),
            self._state(["fact_repo_sync_stale", "fact_nimbus_values_reliability"]),
        )

        self.assertIn(
            {
                "type": "update_coworker_state",
                "person_id": "peach",
                "key": "scope_unblocked",
                "value": True,
            },
            replies[0].effects,
        )

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
        self.assertIn(
            {
                "type": "update_coworker_state",
                "person_id": "toad",
                "key": "approval_recorded",
                "value": True,
            },
            ready[0].effects,
        )

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
