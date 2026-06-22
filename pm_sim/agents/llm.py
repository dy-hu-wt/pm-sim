from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Union

from ..actions import (
    list_tasks,
    read_doc,
    schedule_meeting,
    send_chat,
    send_email,
    update_doc,
    update_task,
)
from ..evaluator import evaluate
from ..paths import DEFAULT_DB_PATH, DEFAULT_SCENARIO_PATH, REPO_ROOT
from ..state import observe, reset
from ..time import advance_time
from .finalize import finalize_to_deadline


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
    stop_reason = "max_turns"
    for turn in range(1, max_turns + 1):
        _progress(progress, f"{_sim_time_label(db_path)} turn {turn}/{max_turns}: waiting for model")
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
        if tool_calls:
            _progress(
                progress,
                f"{_sim_time_label(db_path)} turn {turn}/{max_turns}: "
                f"{_tool_call_summary(tool_calls)}",
            )

        if not tool_calls:
            stop_reason = "no_tool_calls"
            break

        for call in tool_calls:
            name = getattr(call, "name", "")
            args = _parse_arguments(getattr(call, "arguments", "{}"))
            if name == "finish":
                finished = True
                result: dict[str, Any] = {"ok": True, "reason": args.get("reason", "")}
                stop_reason = "agent_finish"
            else:
                handler = tool_handlers.get(name)
                if handler is None:
                    result = {"ok": False, "error": f"Unknown tool: {name}"}
                else:
                    result = handler(args)

            steps.append(_step(name, result))
            _progress(progress, f"{_sim_time_label(db_path)} {_tool_progress_line(name, args, result)}")
            input_items.append(
                {
                    "type": "function_call_output",
                    "call_id": getattr(call, "call_id"),
                    "output": json.dumps(result, default=str),
                }
            )

        if finished:
            break

    finalization = finalize_to_deadline(db_path, scenario_path, progress=progress)
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
        "stop_reason": stop_reason,
        "turns": turn if "turn" in locals() else 0,
        "steps": steps,
        "finalization": finalization,
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
        "You are the project-manager agent operating inside pm-sim. Use only the workplace tools "
        "provided to you: observation, docs, tasks, chat, email, calendar meetings, and explicit time "
        "advancement. Do not assume hidden facts, inspect scenario files, infer evaluator evidence keys, "
        "or claim task progress that the visible world does not support. Discover information through "
        "coworkers, docs, meeting transcripts, messages, and scheduled events. Advancing time is allowed "
        "when you are waiting for replies, meetings, stakeholder follow-ups, or known future events. "
        "Coworker attention is limited. Do not broadcast routine updates, send courtesy confirmations, "
        "or ask the same person to confirm a decision that is already visible in a message, transcript, "
        "or task. Message the smallest useful set of people. Prefer one concise email or one focused "
        "meeting over repeated individual pings when several people need the same context. Only message "
        "someone when you need private information, a concrete decision, or a specific unblock. Do not "
        "update a task just to show activity. When alignment or a decision becomes clear, preserve it "
        "in a durable artifact such as a visible decision record or launch note. Do not update docs "
        "with guesses or vague summaries; written artifacts should reflect decisions, risks, blockers, "
        "or customer commitments that are already supported by visible state. When writing the launch "
        "decision record, include the approved launch mode, Toad's approval, human approval before "
        "posting, auto-commenting out of Friday scope or left as follow-up, and the repo-sync stale-commit "
        "rationale. If a critical decision-record update applies no effect, revise the doc with the "
        "missing grounded details before finishing. When a customer-facing "
        "security or compliance question appears, handle it as a same-day interruption: ask the owner "
        "for the source of truth, wait for the reply, read the referenced doc, then answer the customer "
        "owner directly. Do not schedule a meeting for a security wording question unless the needed "
        "document or owner answer is unavailable. "
        "Your objective is to improve the Friday launch outcome through realistic PM behavior: discover "
        "blockers, resolve conflicts, prioritize tradeoffs, communicate clearly, and keep work moving. "
        "You do not need to simulate every hour through Friday. Call finish when the launch mode is "
        "approved, customer messaging is ready, blocked work is unblocked, any visible async customer "
        "questions have been answered from evidence, and no useful action remains."
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
        "update_doc": lambda args: update_doc(db_path, args["doc_id"], args["body"]),
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
            "update_doc",
            (
                "Replace the body of a visible existing document. For doc_launch_decision_record, "
                "write the complete decision: Toad approval, draft mode, human approval before posting, "
                "auto-commenting out of Friday scope or follow-up, and repo-sync stale-commit rationale."
            ),
            {"doc_id": {"type": "string"}, "body": {"type": "string"}},
            ["doc_id", "body"],
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


def _sim_time_label(db_path: Path | str) -> str:
    try:
        current_time = observe(db_path).get("current_time")
    except Exception:
        current_time = None
    return f"[{_pretty_time(current_time)}]"


def _pretty_time(value: str | None) -> str:
    if not value:
        return "sim time unknown"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    return parsed.strftime("%a %Y-%m-%d %H:%M")


def _args_summary(name: str, args: dict[str, Any]) -> str:
    if name == "send_chat":
        return f"CHAT to {args.get('person_id')}: {_short(args.get('body', ''), 80)}"
    if name == "send_email":
        return f"EMAIL to {args.get('person_id')} [{_short(args.get('subject', ''), 60)}]"
    if name == "read_doc":
        return f"READ {args.get('doc_id')}"
    if name == "update_doc":
        return f"UPDATE_DOC {args.get('doc_id')}"
    if name == "advance_time":
        return f"WAIT {args.get('target')}"
    if name == "update_task":
        updates = ", ".join(
            f"{key}={value}" for key, value in args.items() if key in {"status", "priority"}
        )
        suffix = f" {updates}" if updates else ""
        return f"TASK {args.get('task_id')}{suffix}"
    if name == "schedule_meeting":
        attendees = ", ".join(args.get("attendees", []))
        return f"MEETING [{_short(args.get('title', ''), 60)}] {args.get('start_at')}->{args.get('end_at')} with {attendees}"
    if name == "observe":
        return "OBSERVE"
    if name == "list_tasks":
        return "TASKS"
    if name == "finish":
        return f"FINISH: {_short(args.get('reason', ''), 80)}"
    return ""


def _tool_call_summary(tool_calls: list[Any]) -> str:
    names = [str(getattr(call, "name", "unknown")) for call in tool_calls]
    counts: dict[str, int] = {}
    for name in names:
        counts[name] = counts.get(name, 0) + 1
    summary = ", ".join(
        f"{name} x{count}" if count > 1 else name for name, count in counts.items()
    )
    return f"model requested {len(names)} tool call(s): {summary}"


def _tool_progress_line(name: str, args: dict[str, Any], result: ToolResult) -> str:
    action = _args_summary(name, args) or name
    summary = _result_summary(name, result)
    if summary:
        return f"{action} — {summary}"
    return action


def _result_summary(name: str, result: ToolResult) -> str:
    if not isinstance(result, dict):
        return f"{len(result)} row(s)"
    if not result.get("ok", True):
        return f"failed: {result.get('error')}"
    if name == "observe":
        return f"current time {_pretty_time(result.get('current_time'))}"
    if name == "advance_time":
        delivered = result.get("delivered_events", [])
        if delivered:
            event_types = ", ".join(event.get("event_type", "event") for event in delivered)
            return f"{_pretty_time(result.get('to'))}; events: {event_types}"
        return f"{_pretty_time(result.get('to'))}; events: none"
    if name == "send_chat":
        replies = result.get("scheduled_reply_ids", [])
        return f"scheduled {len(replies)} reply event(s){_time_cost_summary(result)}"
    if name == "send_email":
        effects = result.get("applied_effects", [])
        return f"applied {len(effects)} effect(s){_time_cost_summary(result)}"
    if name == "read_doc":
        doc = result.get("doc", {})
        return f"{doc.get('title', 'unknown doc')}{_time_cost_summary(result)}"
    if name == "update_doc":
        effects = result.get("applied_effects", [])
        return f"updated {result.get('doc_id')}; applied {len(effects)} effect(s){_time_cost_summary(result)}"
    if name == "schedule_meeting":
        return f"scheduled {result.get('meeting_id')}{_time_cost_summary(result)}"
    if name == "update_task":
        return f"updated{_time_cost_summary(result)}"
    return ""


def _time_cost_summary(result: dict[str, Any]) -> str:
    time_cost = result.get("time_cost")
    if not isinstance(time_cost, dict):
        return ""
    delivered = time_cost.get("delivered_events") or []
    delivered_summary = ""
    if delivered:
        event_types = ", ".join(event.get("event_type", "event") for event in delivered)
        delivered_summary = f"; events: {event_types}"
    return f" (+{time_cost.get('minutes')}m){delivered_summary}"


def _short(value: str, limit: int) -> str:
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    return f"{text[: limit - 3]}..."
