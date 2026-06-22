from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pm_sim.coworkers import effects_for_event, replies_for_chat
from pm_sim.paths import DEFAULT_SCENARIO_PATH
from pm_sim.state import observe, reset
from pm_sim.time import advance_time


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

        self.assertNotIn("blocker_crm_sync_flaky", blocker_ids)
        self.assertNotIn("fact_crm_sync_flaky", fact_ids)

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
        self.assertEqual(result["delivered_events"][0]["id"], "event_mario_full_report_push")
        self.assertEqual(state["current_time"], "2026-06-23T10:00:00")


class CoworkerRuleTests(unittest.TestCase):
    def test_luigi_reveals_crm_risk_when_asked_about_blockers(self) -> None:
        replies = replies_for_chat("luigi", "Any blockers or CRM sync risk for launch?")

        self.assertEqual(len(replies), 1)
        self.assertIn("CRM enrichment sync", replies[0].body)
        effect_types = {effect["type"] for effect in replies[0].effects}
        self.assertIn("discover_fact", effect_types)
        self.assertIn("update_blocker", effect_types)

    def test_background_event_has_deterministic_effects(self) -> None:
        effects = effects_for_event(
            "luigi_proactive_crm_risk",
            {
                "project_id": "project_exec_health_report",
                "blocker_id": "blocker_crm_sync_flaky",
            },
        )

        self.assertGreaterEqual(len(effects), 3)
        self.assertEqual(effects[0]["type"], "create_message")
        self.assertIn("CRM enrichment sync", effects[0]["body"])


if __name__ == "__main__":
    unittest.main()
