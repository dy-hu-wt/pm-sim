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

    def test_invalid_action_semantic_match_raises_scenario_error(self) -> None:
        scenario = copy.deepcopy(self.base)
        scenario["action_rules"][0]["semantic_match"] = {"required": [{"id": "missing_description"}]}

        with self.assertRaises(ScenarioError):
            load_scenario(self._write_scenario(scenario))

    def test_direct_scored_evidence_effect_raises_scenario_error(self) -> None:
        scenario = copy.deepcopy(self.base)
        scenario["event_rules"][0]["effects"].append(
            {
                "type": "add_evaluation_evidence",
                "key": "blocker_discovered",
                "note": "Direct scoring evidence should not be authored.",
            }
        )

        with self.assertRaisesRegex(ScenarioError, "directly writes scored evidence"):
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
