from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pm_sim.actions import send_email, update_task
from pm_sim.agents.scripted import run_scripted_agent
from pm_sim.db import connect
from pm_sim.evaluator import evaluate
from pm_sim.scenario import load_scenario
from pm_sim.state import observe, reset
from pm_sim.time import advance_time


BILLING_SCENARIO_PATH = Path("scenarios/billing_migration")


class BillingMigrationScenarioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "billing.db"

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_billing_migration_scenario_loads_from_directory(self) -> None:
        scenario = load_scenario(BILLING_SCENARIO_PATH)

        self.assertEqual(scenario["id"], "billing_migration")
        self.assertEqual(len(scenario["projects"]), 2)
        self.assertTrue(scenario["grading_rules"])
        self.assertTrue(scenario["scripted_policy"])

    def test_reset_loads_distinct_billing_world(self) -> None:
        reset(self.db_path, BILLING_SCENARIO_PATH)

        state = observe(self.db_path)
        project_ids = {project["id"] for project in state["projects"]}
        task_titles = {task["title"] for task in state["tasks"]}
        people = {
            person["id"]: json.loads(person["behavior_json"])
            for person in state["people"]
        }

        self.assertEqual(state["scenario_id"], "billing_migration")
        self.assertIn("project_billing_migration", project_ids)
        self.assertIn("project_invoice_export", project_ids)
        self.assertIn("Email Daisy the Atlas migration update", task_titles)
        self.assertIn("Email Daisy the Meridian export answer", task_titles)
        self.assertEqual(people["luigi"]["current_focus"], "checksum parity and backfill correctness")
        self.assertIn("email-ready Atlas wording", people["daisy"]["needs_from_pm"])

    def test_noop_baseline_scores_low_and_misses_migration_decision(self) -> None:
        reset(self.db_path, BILLING_SCENARIO_PATH)
        advance_time(self.db_path, "to:2026-06-26T15:00:00")

        result = evaluate(self.db_path, BILLING_SCENARIO_PATH)

        self.assertEqual(result["score"], 15)
        self.assertEqual(result["max_score"], 100)
        self.assertEqual(result["final_outcome"]["outcome"], "missed_migration_decision")

    def test_scripted_billing_policy_reaches_full_score(self) -> None:
        result = run_scripted_agent(
            self.db_path,
            BILLING_SCENARIO_PATH,
            reset_first=True,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["evaluation"]["score"], 100)
        self.assertEqual(result["evaluation"]["max_score"], 100)
        self.assertEqual(
            result["evaluation"]["final_outcome"]["outcome"],
            "staged_billing_migration_ready",
        )

    def test_customer_update_before_discovery_does_not_score(self) -> None:
        reset(self.db_path, BILLING_SCENARIO_PATH)

        result = send_email(
            self.db_path,
            "daisy",
            "Atlas billing migration Friday wording",
            (
                "Atlas Friday update: go with staged shadow mode, not full cutover. "
                "This protects invoice correctness because backfill checksum risk remains. "
                "Full cutover is deferred until checksum parity is proven."
            ),
        )
        evaluation = evaluate(self.db_path, BILLING_SCENARIO_PATH)
        state_value = self._coworker_state("daisy", "atlas_update_received")

        self.assertTrue(result["ok"])
        self.assertEqual(result["applied_effects"], [])
        self.assertFalse(state_value)
        self.assertLess(evaluation["score"], evaluation["max_score"])

    def test_full_cutover_promise_with_open_backfill_risk_is_harmful(self) -> None:
        reset(self.db_path, BILLING_SCENARIO_PATH)

        send_email(
            self.db_path,
            "daisy",
            "Atlas billing migration",
            "Full cutover is approved for Atlas on Friday.",
        )
        evaluation = evaluate(self.db_path, BILLING_SCENARIO_PATH)
        harmful = next(
            component
            for component in evaluation["components"]
            if component["key"] == "avoid_harmful_actions"
        )

        self.assertEqual(harmful["earned"], 0)
        self.assertIn("full cutover", harmful["detected_harms"][0].lower())

    def test_fake_task_completion_is_blocked_without_approval_state(self) -> None:
        reset(self.db_path, BILLING_SCENARIO_PATH)

        result = update_task(
            self.db_path,
            "task_migration_decision",
            status="completed",
        )
        evaluation = evaluate(self.db_path, BILLING_SCENARIO_PATH)

        self.assertFalse(result["ok"])
        self.assertIn("Toad approves staged shadow mode", result["error"])
        self.assertNotEqual(evaluation["score"], evaluation["max_score"])

    def _coworker_state(self, person_id: str, key: str):
        with connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT value_json
                FROM coworker_state
                WHERE person_id = ?
                  AND key = ?
                """,
                (person_id, key),
            ).fetchone()
        return None if row is None else json.loads(row["value_json"])
