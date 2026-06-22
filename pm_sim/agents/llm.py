from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Union

from ..actions import list_tasks, read_doc, schedule_meeting, send_chat, send_email, update_task
from ..evaluator import evaluate
from ..paths import DEFAULT_DB_PATH, DEFAULT_SCENARIO_PATH, REPO_ROOT
from ..state import observe, reset
from ..time import advance_time


ToolResult = Union[dict[str, Any], list[dict[str, Any]]]
ToolFn = Callable[[dict[str, Any]], ToolResult]
ProgressFn = Callable[[str], None]

DEFAULT_MODEL = "gpt-5.5"


class LlmAgentError(RuntimeError):
    pass


def run_llm_agent(
    db_path: Path | str = DEFAULT_DB_PATH,
    scenario_path: Path | str = DEFAULT_SCENARIO_PATH,
    *,
    reset_first: bool = False,
    model: str | None = None,
    max_turns: int = 40,
    client: Any | None = None,
    progress: ProgressFn | None = None,
) -> dict[str, Any]:
    _load_dotenv()
    model = model or os.environ.get("OPENAI_MODEL") or DEFAULT_MODEL
    steps: list[dict[str, Any]] = []

    if reset_first:
        _progress(progress, "resetting scenario")
        steps.append(_step("reset", reset(db_path, scenario_path)))

    if client is None:
        _progress(progress, f"creating OpenAI client for model {model}")
        client = _openai_client()

    tools = _tool_specs()
    tool_handlers = _tool_handlers(db_path, scenario_path)
    input_items: list[Any] = [
        {
            "role": "user",
            "content": _initial_prompt(observe(db_path)),
        }
    ]

    finished = False
    final_message = ""
    for turn in range(1, max_turns + 1):
        _progress(progress, f"turn {turn}/{max_turns}: waiting for model")
        response = client.responses.create(
            model=model,
            instructions=_instructions(),
            input=input_items,
            tools=tools,
            tool_choice="auto",
        )
        output = list(getattr(response, "output", []) or [])
        input_items.extend(output)
        final_message = getattr(response, "output_text", "") or final_message
        tool_calls = [item for item in output if getattr(item, "type", None) == "function_call"]
        _progress(progress, f"turn {turn}/{max_turns}: model returned {len(tool_calls)} tool call(s)")

        if not tool_calls:
            break

        for call in tool_calls:
            name = getattr(call, "name", "")
            args = _parse_arguments(getattr(call, "arguments", "{}"))
            _progress(progress, f"running tool: {name}")
            if name == "finish":
                finished = True
                result: dict[str, Any] = {"ok": True, "reason": args.get("reason", "")}
            else:
                handler = tool_handlers.get(name)
                if handler is None:
                    result = {"ok": False, "error": f"Unknown tool: {name}"}
                else:
                    result = handler(args)

            steps.append(_step(name, result))
            input_items.append(
                {
                    "type": "function_call_output",
                    "call_id": getattr(call, "call_id"),
                    "output": json.dumps(result, default=str),
                }
            )

        if finished:
            break

    evaluation = evaluate(db_path, scenario_path)
    _progress(
        progress,
        f"evaluation complete: {evaluation.get('score')} / {evaluation.get('max_score')}",
    )
    return {
        "ok": evaluation.get("score") == evaluation.get("max_score"),
        "policy": "llm",
        "model": model,
        "finished": finished,
        "turns": turn if "turn" in locals() else 0,
        "steps": steps,
        "final_message": final_message,
        "evaluation": evaluation,
    }


def _openai_client() -> Any:
    if not os.environ.get("OPENAI_API_KEY"):
        raise LlmAgentError("OPENAI_API_KEY is required for --policy llm.")
    try:
        from openai import OpenAI
    except ImportError as error:
        raise LlmAgentError("Install the optional OpenAI SDK to use --policy llm: pip install openai") from error
    return OpenAI()


def _load_dotenv(path: Path | None = None) -> None:
    env_path = path or (REPO_ROOT / ".env")
    if not env_path.exists():
        return
    for raw_line in env_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _instructions() -> str:
    return (
        "You are the project-manager agent inside pm-sim. Use only the provided tools. "
        "Do not assume hidden facts; discover them through docs, coworkers, meetings, and time. "
        "Your objective is to improve the Friday launch outcome and get a high evaluator score. "
        "Prefer substantive project progress over tool volume. Use evaluate to check missing evidence before "
        "you finish. If the score is not complete, continue using tools until the missing evidence is addressed "
        "or the turn limit is reached. Pay attention to scheduled future events; some required work only appears "
        "after simulated time advances. Call finish only when evaluation reaches full score or no useful action remains."
    )


def _initial_prompt(observation: dict[str, Any]) -> str:
    return (
        "Run the launch-readiness week for the PR Review Agent beta. "
        "Start from this observation and choose tool calls step by step:\n"
        f"{json.dumps(observation, default=str)}"
    )


def _tool_handlers(db_path: Path | str, scenario_path: Path | str) -> dict[str, ToolFn]:
    return {
        "observe": lambda _args: observe(db_path),
        "list_tasks": lambda _args: list_tasks(db_path),
        "read_doc": lambda args: read_doc(db_path, args["doc_id"]),
        "send_chat": lambda args: send_chat(db_path, args["person_id"], args["body"]),
        "send_email": lambda args: send_email(
            db_path,
            args["person_id"],
            args["subject"],
            args["body"],
        ),
        "update_task": lambda args: update_task(
            db_path,
            args["task_id"],
            status=args.get("status"),
            priority=args.get("priority"),
        ),
        "schedule_meeting": lambda args: schedule_meeting(
            db_path,
            args["title"],
            args["start_at"],
            args["end_at"],
            args["attendees"],
        ),
        "advance_time": lambda args: advance_time(db_path, args["target"]),
        "evaluate": lambda _args: evaluate(db_path, scenario_path),
    }


def _tool_specs() -> list[dict[str, Any]]:
    return [
        _tool("observe", "Inspect visible current simulation state.", {}),
        _tool("list_tasks", "List project tasks.", {}),
        _tool(
            "read_doc",
            "Read a visible document by id.",
            {"doc_id": {"type": "string"}},
            ["doc_id"],
        ),
        _tool(
            "send_chat",
            "Send a chat message to a coworker.",
            {"person_id": {"type": "string"}, "body": {"type": "string"}},
            ["person_id", "body"],
        ),
        _tool(
            "send_email",
            "Send an email message to a coworker.",
            {
                "person_id": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
            },
            ["person_id", "subject", "body"],
        ),
        _tool(
            "update_task",
            "Update a task status or priority.",
            {
                "task_id": {"type": "string"},
                "status": {"type": "string"},
                "priority": {"type": "string"},
            },
            ["task_id"],
        ),
        _tool(
            "schedule_meeting",
            "Schedule a meeting; the meeting resolves when time reaches end_at.",
            {
                "title": {"type": "string"},
                "start_at": {"type": "string"},
                "end_at": {"type": "string"},
                "attendees": {"type": "array", "items": {"type": "string"}},
            },
            ["title", "start_at", "end_at", "attendees"],
        ),
        _tool(
            "advance_time",
            "Advance simulated time by duration, until_next_event, or to:<iso timestamp>.",
            {"target": {"type": "string"}},
            ["target"],
        ),
        _tool("evaluate", "Run the deterministic evaluator on the current state.", {}),
        _tool(
            "finish",
            "Stop the LLM agent run when no more tool calls are needed.",
            {"reason": {"type": "string"}},
            ["reason"],
        ),
    ]


def _tool(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "type": "function",
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required or [],
            "additionalProperties": False,
        },
    }


def _parse_arguments(arguments: str) -> dict[str, Any]:
    try:
        parsed = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _step(name: str, result: ToolResult) -> dict[str, Any]:
    ok = result.get("ok", True) if isinstance(result, dict) else True
    return {
        "name": name,
        "ok": ok,
        "result": result,
    }


def _progress(progress: ProgressFn | None, message: str) -> None:
    if progress is not None:
        progress(message)
