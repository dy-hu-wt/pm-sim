from __future__ import annotations

import copy
import contextlib
import io
import json
import os
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

    def test_scripted_agent_forces_deterministic_semantic_matching(self) -> None:
        with unittest.mock.patch.dict(
            os.environ,
            {"PM_SIM_SEMANTIC_MATCHER": "llm", "OPENAI_API_KEY": "test-key"},
        ):
            result = run_scripted_agent(self.db_path, DEFAULT_SCENARIO_PATH, reset_first=True)

            self.assertTrue(result["ok"])
            self.assertEqual(result["evaluation"]["score"], result["evaluation"]["max_score"])
            self.assertEqual(os.environ["PM_SIM_SEMANTIC_MATCHER"], "llm")

    def test_cli_ui_static_writes_html_summary(self) -> None:
        run_scripted_agent(self.db_path, DEFAULT_SCENARIO_PATH, reset_first=True)
        output_path = Path(self.tmpdir.name) / "ui.html"

        output = self._run_cli("ui", "--static", "--output", str(output_path))
        html = output_path.read_text(encoding="utf-8")

        self.assertIn("UI written", output)
        self.assertIn(str(output_path), output)
        self.assertIn("Score:", output)
        self.assertIn("PM Sim Operator UI", html)
        self.assertIn("Evaluation", html)
        self.assertIn("Playback", html)
        self.assertIn("Timeline", html)
        self.assertIn("Debug Logs", html)

    def test_scripted_agent_uses_public_tool_actions(self) -> None:
        run_scripted_agent(self.db_path, DEFAULT_SCENARIO_PATH, reset_first=True)

        log = action_log(self.db_path, limit=100)
        action_types = [entry["action_type"] for entry in log]

        self.assertIn("reset", action_types)
        self.assertIn("send_chat", action_types)
        self.assertIn("send_email", action_types)
        self.assertIn("advance_time", action_types)
        self.assertEqual(action_types.count("finalize_to_deadline"), 1)

    def test_cli_run_agent_prints_summary(self) -> None:
        output = self._run_cli("run-agent", "--policy", "scripted", "--reset")

        self.assertIn("Agent Run", output)
        self.assertIn("Policy: scripted", output)
        self.assertIn("Score:", output)
        self.assertIn("Deadline: advanced to Fri 2026-06-26 15:00", output)
        self.assertIn("project_deadline", output)
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

    def test_semantic_progress_line_is_compact_and_highlighted(self) -> None:
        line = format_semantic_progress(
            {
                "rule_id": "final_readiness_confirmed_email",
                "mode": "llm",
                "model": "gpt-4.1-mini",
                "matches": True,
                "required": [{"matched": True}, {"matched": True}],
            }
        )
        html = format_agent_progress_html(f"[Thu 2026-06-25 12:10] {line}")

        self.assertEqual(
            line,
            "SEMANTIC final_readiness_confirmed_email llm:gpt-4.1-mini matched 2/2 required",
        )
        self.assertIn("agent-tool-semantic", html)

    def test_semantic_progress_line_shows_fail_closed_error(self) -> None:
        line = format_semantic_progress(
            {
                "rule_id": "customer_message_ready",
                "mode": "llm",
                "matches": False,
                "error": "JSONDecodeError: Expecting value",
                "required": [],
            }
        )

        self.assertIn("failed closed", line)
        self.assertIn("JSONDecodeError", line)

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
        client = FakeResponsesClient(
            [
                [
                    function_call("call_1", "read_doc", {"doc_id": "doc_project_brief"}),
                ],
                [function_call("call_2", "finish", {"reason": "done"})],
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
        client = FakeResponsesClient(
            [
                [function_call("call_1", "finish", {"reason": "too early"})],
                [
                    function_call(
                        "call_2",
                        "advance_time",
                        {"target": "to:2026-06-26T15:00:00"},
                    )
                ],
                [function_call("call_3", "finish", {"reason": "calendar complete"})],
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
        client = FakeResponsesClient([[function_call("call_1", "finish", {"reason": "done"})]])

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
        client = FakeResponsesClient(
            [
                [function_call("call_1", "observe", {})],
                [function_call("call_2", "finish", {"reason": "observed"})],
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
        client = FakeResponsesClient(
            [
                [function_call("call_1", "observe", {})],
                [function_call("call_2", "finish", {"reason": "observed"})],
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
        client = FakeResponsesClient(
            [
                [function_call("call_1", "observe", {})],
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
        client = FakeResponsesClient(
            [
                [function_call("call_1", "observe", {})],
                [function_call("call_2", "finish", {"reason": "observed"})],
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
        self.assertIn("customer-ready Nimbus wording early enough for her Thursday account update", instructions)
        self.assertIn("Koopa needs scoped wording before Thursday's security review", instructions)
        self.assertIn("Thursday final-readiness requests need an answer", instructions)
        self.assertIn("Call finish only when", instructions)


if __name__ == "__main__":
    unittest.main()
