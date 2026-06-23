from __future__ import annotations

import copy
import contextlib
import io
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml

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
from pm_sim.scenario import ScenarioError, _load_scenario_data, load_scenario
from pm_sim.scenario_tools import lint_scenario, scenario_graph
from pm_sim import concept_match as concept_match_module
from pm_sim.state import action_log, event_log, observe, reset
from pm_sim.engine.time import advance_time
from pm_sim.timeline import timeline
from pm_sim.ui import _html, _run_next_ui_step, _scripted_demo_state, _state_payload

class ScenarioValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = load_scenario(DEFAULT_SCENARIO_PATH)
        self.author_base = _load_scenario_data(Path(DEFAULT_SCENARIO_PATH))
        self.tmpdir = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_invalid_task_owner_raises_scenario_error(self) -> None:
        scenario = copy.deepcopy(self.author_base)
        scenario["tasks"][0]["owner_id"] = "unknown_person"

        with self.assertRaises(ScenarioError):
            load_scenario(self._write_scenario(scenario))

    def test_missing_person_response_delay_raises_scenario_error(self) -> None:
        scenario = copy.deepcopy(self.author_base)
        del scenario["people"][0]["response_delay_minutes"]

        with self.assertRaises(ScenarioError):
            load_scenario(self._write_scenario(scenario))

    def test_invalid_dependency_task_raises_scenario_error(self) -> None:
        scenario = copy.deepcopy(self.author_base)
        scenario["dependencies"][0]["upstream_task_id"] = "missing_task"

        with self.assertRaises(ScenarioError):
            load_scenario(self._write_scenario(scenario))

    def test_readable_task_aliases_are_canonicalized(self) -> None:
        task_ids = {task["id"] for task in self.base["tasks"]}
        self.assertIn("task_launch_decision", task_ids)
        self.assertIn("task_repo_sync", task_ids)

        dependency = next(
            row
            for row in self.base["dependencies"]
            if row["id"] == "dep_auto_commenting_needs_repo_sync"
        )
        self.assertEqual(dependency["upstream_task_id"], "task_repo_sync")
        self.assertEqual(dependency["downstream_task_id"], "task_launch_decision")

        gate = next(rule for rule in self.base["task_gate_rules"] if rule["task_id"] == "task_repo_sync")
        self.assertIn("complete", gate["statuses"])

    def test_invalid_event_project_payload_raises_scenario_error(self) -> None:
        scenario = copy.deepcopy(self.author_base)
        scenario["events"][0]["payload"]["project_id"] = "missing_project"

        with self.assertRaises(ScenarioError):
            load_scenario(self._write_scenario(scenario))

    def test_duplicate_ids_raise_scenario_error(self) -> None:
        scenario = copy.deepcopy(self.author_base)
        scenario["events"][1]["id"] = scenario["events"][0]["id"]

        with self.assertRaises(ScenarioError):
            load_scenario(self._write_scenario(scenario))

    def test_invalid_event_rule_effect_raises_scenario_error(self) -> None:
        scenario = copy.deepcopy(self.author_base)
        scenario["event_behaviors"][0]["effects"][1]["project_id"] = "missing_project"

        with self.assertRaises(ScenarioError):
            load_scenario(self._write_scenario(scenario))

    def test_invalid_coworker_state_effect_raises_scenario_error(self) -> None:
        scenario = copy.deepcopy(self.author_base)
        scenario["event_behaviors"][0]["effects"].append(
            {
                "type": "update_coworker_state",
                "person_id": "missing_person",
                "key": "accepted_draft_mode",
                "value": True,
            }
        )

        with self.assertRaises(ScenarioError):
            load_scenario(self._write_scenario(scenario))

    def test_invalid_actor_policy_person_raises_scenario_error(self) -> None:
        scenario = copy.deepcopy(self.author_base)
        scenario["policy_behaviors"][0]["person_id"] = "unknown_person"

        with self.assertRaises(ScenarioError):
            load_scenario(self._write_scenario(scenario))

    def test_legacy_behavior_key_raises_scenario_error(self) -> None:
        scenario = copy.deepcopy(self.author_base)
        scenario["behaviors"] = [{"id": "invalid_actor_behavior", "kind": "wander"}]

        with self.assertRaisesRegex(ScenarioError, "legacy behavior key"):
            load_scenario(self._write_scenario(scenario))

    def test_invalid_actor_workload_effect_raises_scenario_error(self) -> None:
        scenario = copy.deepcopy(self.author_base)
        scenario["policy_behaviors"][0]["effects"].append(
            {
                "type": "update_actor_workload",
                "person_id": "unknown_person",
                "load_level": "high",
            }
        )

        with self.assertRaisesRegex(ScenarioError, "actor workload person_id"):
            load_scenario(self._write_scenario(scenario))

    def test_invalid_action_match_intent_raises_scenario_error(self) -> None:
        scenario = copy.deepcopy(self.author_base)
        scenario["action_checks"][0]["action"]["match"] = {
            "mode": "concept_match",
            "intents": [{"id": "missing_description"}],
            "require_all": ["missing_description"],
        }

        with self.assertRaisesRegex(ScenarioError, "cannot use deterministic intent keys"):
            load_scenario(self._write_scenario(scenario))

    def test_empty_concept_match_raises_scenario_error(self) -> None:
        scenario = copy.deepcopy(self.author_base)
        scenario["action_checks"][0]["action"]["match"] = {"mode": "concept_match"}

        with self.assertRaisesRegex(ScenarioError, "requires required_concepts or forbidden_concepts"):
            load_scenario(self._write_scenario(scenario))

    def test_duplicate_concept_id_raises_scenario_error(self) -> None:
        scenario = copy.deepcopy(self.author_base)
        concepts = scenario["action_checks"][0]["action"]["match"]["required_concepts"]
        concepts[1]["id"] = concepts[0]["id"]

        with self.assertRaisesRegex(ScenarioError, "duplicate concept id"):
            load_scenario(self._write_scenario(scenario))

    def test_overlapping_required_and_forbidden_concept_id_raises_scenario_error(self) -> None:
        scenario = copy.deepcopy(self.author_base)
        match = scenario["action_checks"][0]["action"]["match"]
        match["forbidden_concepts"][0]["id"] = match["required_concepts"][0]["id"]

        with self.assertRaisesRegex(ScenarioError, "cannot appear in both required and forbidden"):
            load_scenario(self._write_scenario(scenario))

    def test_concept_without_description_raises_scenario_error(self) -> None:
        scenario = copy.deepcopy(self.author_base)
        del scenario["action_checks"][0]["action"]["match"]["required_concepts"][0]["description"]

        with self.assertRaisesRegex(ScenarioError, "must include description"):
            load_scenario(self._write_scenario(scenario))

    def test_direct_scored_milestone_effect_raises_scenario_error(self) -> None:
        scenario = copy.deepcopy(self.author_base)
        scenario["event_behaviors"][0]["effects"].append(
            {
                "type": "record_milestone",
                "key": "blocker_discovered",
                "note": "Direct scoring milestone should not be authored.",
            }
        )

        with self.assertRaisesRegex(ScenarioError, "directly writes scored milestone"):
            load_scenario(self._write_scenario(scenario))

    def test_manifest_scenario_includes_files(self) -> None:
        scenario = copy.deepcopy(self.author_base)
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
        (root / "scenario.yaml").write_text(
            yaml.safe_dump({**manifest, "include": ["world.yaml", "rules.yaml"]}, sort_keys=False)
        )
        (root / "world.yaml").write_text(yaml.safe_dump(world, sort_keys=False))
        (root / "rules.yaml").write_text(yaml.safe_dump(rules, sort_keys=False))

        loaded = load_scenario(root / "scenario.yaml")

        self.assertEqual(loaded["id"], "launch_readiness")
        self.assertEqual(len(loaded["people"]), 5)
        self.assertTrue(loaded["actor_behaviors"])
        self.assertTrue(loaded["event_rules"])

        loaded_from_dir = load_scenario(root)
        self.assertEqual(loaded_from_dir["id"], "launch_readiness")

    def test_scenario_lint_summarizes_authoring_surface(self) -> None:
        result = lint_scenario(DEFAULT_SCENARIO_PATH)

        self.assertTrue(result["ok"])
        self.assertEqual(result["scenario_id"], "launch_readiness")
        self.assertGreater(result["counts"]["actor_behaviors"], 0)
        self.assertGreater(result["behavior_primitives"]["policy"], 0)
        self.assertTrue(result["score_links"])

    def test_scenario_graph_links_score_to_state_and_dependencies(self) -> None:
        result = scenario_graph(DEFAULT_SCENARIO_PATH)
        edges = {(edge["from"], edge["type"], edge["to"]) for edge in result["edges"]}

        self.assertTrue(result["ok"])
        self.assertIn(
            ("milestone:customer_message_ready", "scores", "score:stakeholder_communication"),
            edges,
        )
        self.assertIn(
            ("task_launch_decision", "depends_on", "task_draft_mode_docs"),
            edges,
        )

    def test_missing_manifest_include_raises_scenario_error(self) -> None:
        path = Path(self.tmpdir.name) / "scenario.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "id": "broken",
                    "start_time": "2026-06-22T09:00:00",
                    "include": ["missing.yaml"],
                },
                sort_keys=False,
            )
        )

        with self.assertRaises(ScenarioError):
            load_scenario(path)

    def test_event_before_start_time_raises_scenario_error(self) -> None:
        scenario = copy.deepcopy(self.author_base)
        scenario["events"][0]["scheduled_at"] = "2026-06-22T08:59:00"

        with self.assertRaises(ScenarioError):
            load_scenario(self._write_scenario(scenario))

    def _write_scenario(self, scenario: dict[str, Any]) -> Path:
        path = Path(self.tmpdir.name) / "scenario.yaml"
        path.write_text(yaml.safe_dump(scenario, sort_keys=False))
        return path
