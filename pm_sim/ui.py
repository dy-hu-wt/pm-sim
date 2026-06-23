from __future__ import annotations

import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .actions import list_tasks
from .agents.finalize import finalize_to_deadline
from .agents.llm import _agent_brief_text, _load_llm_session, llm_session_state, start_llm_session, step_llm_session
from .agents.scripted import run_scripted_step, scripted_policy_steps
from .db import connect
from .evaluator import evaluate
from .formatters import (
    format_agent_progress_html,
    format_agent_tool_progress,
    format_output,
    format_concept_progress,
)
from .paths import DEFAULT_DB_PATH, DEFAULT_SCENARIO_PATH
from .scenario import load_scenario
from .state import get_state_value, observe, reset, set_state_value
from .timeline import timeline


DEFAULT_UI_HOST = "127.0.0.1"
DEFAULT_UI_PORT = 8765
ProgressFn = Callable[[str], None]


def serve_ui(
    db_path: Path | str = DEFAULT_DB_PATH,
    scenario_path: Path | str = DEFAULT_SCENARIO_PATH,
    *,
    host: str = DEFAULT_UI_HOST,
    port: int = DEFAULT_UI_PORT,
    open_browser: bool = True,
    reset_first: bool = True,
    timeline_limit: int = 120,
    policy: str = "scripted",
    model: str | None = None,
    max_turns: int = 40,
    progress: ProgressFn | None = None,
) -> dict[str, Any]:
    if reset_first:
        reset(db_path, scenario_path)
        if policy == "llm":
            start_llm_session(db_path, scenario_path, model=model)

    server = _UiServer(
        (host, port),
        _Handler,
        db_path=Path(db_path),
        scenario_path=Path(scenario_path),
        timeline_limit=timeline_limit,
        policy=policy,
        model=model,
        max_turns=max_turns,
        progress=progress,
    )
    url = f"http://{host}:{server.server_address[1]}/"

    if open_browser:
        threading.Timer(0.2, lambda: webbrowser.open(url)).start()

    try:
        print(f"UI running at {url}")
        print("Press Ctrl-C to stop.")
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

    return {"ok": True, "url": url, "stopped": True}


class _UiServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address,
        RequestHandlerClass,
        *,
        db_path: Path,
        scenario_path: Path,
        timeline_limit: int,
        policy: str,
        model: str | None,
        max_turns: int,
        progress: ProgressFn | None,
    ):
        super().__init__(server_address, RequestHandlerClass)
        self.db_path = db_path
        self.scenario_path = scenario_path
        self.timeline_limit = timeline_limit
        self.policy = policy
        self.model = model
        self.max_turns = max_turns
        self.progress = progress
        self.step_lock = threading.Lock()
        self.last_log_lines: list[str] = []
        self.run_summary_printed = False


class _Handler(BaseHTTPRequestHandler):
    server: _UiServer

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self._send_html(_html())
            return
        if path == "/api/state":
            self._send_json(_state_payload(self.server.db_path, self.server.scenario_path, self.server.timeline_limit))
            return
        self.send_error(404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/reset":
            if self.server.step_lock.locked():
                payload = _state_payload(self.server.db_path, self.server.scenario_path, self.server.timeline_limit)
                payload["busy"] = True
                self._send_json(payload)
                return
            reset(self.server.db_path, self.server.scenario_path)
            if self.server.policy == "llm":
                start_llm_session(self.server.db_path, self.server.scenario_path, model=self.server.model)
            self.server.last_log_lines = []
            self.server.run_summary_printed = False
            self._send_json(_state_payload(self.server.db_path, self.server.scenario_path, self.server.timeline_limit))
            return
        if path == "/api/advance-next":
            if not self.server.step_lock.acquire(blocking=False):
                payload = _state_payload(self.server.db_path, self.server.scenario_path, self.server.timeline_limit)
                payload["busy"] = True
                self._send_json(payload)
                return
            try:
                step_result = _run_next_ui_step(
                    self.server.db_path,
                    self.server.scenario_path,
                    policy=self.server.policy,
                    model=self.server.model,
                    max_turns=self.server.max_turns,
                    progress=self.server.progress,
                )
            finally:
                self.server.step_lock.release()
            payload = _state_payload(self.server.db_path, self.server.scenario_path, self.server.timeline_limit)
            payload["step_result"] = step_result
            payload["done"] = bool(step_result.get("done"))
            if self.server.policy != "llm":
                _emit_new_console_lines(self.server, payload.get("log_lines") or [])
            if payload["done"] and not self.server.run_summary_printed:
                print(format_output("run-agent", _ui_run_summary(self.server)), flush=True)
                self.server.run_summary_printed = True
            self._send_json(payload)
            return
        self.send_error(404)

    def log_message(self, format: str, *args) -> None:
        return

    def _send_html(self, body: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_json(self, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def _state_payload(db_path: Path, scenario_path: Path, timeline_limit: int) -> dict[str, Any]:
    scenario = load_scenario(scenario_path)
    llm_state = llm_session_state(db_path)
    all_entries = timeline(db_path, limit=0)
    entries = all_entries
    if timeline_limit > 0:
        entries = entries[-timeline_limit:]
    display_timeline = []
    for entry in all_entries:
        display = _display_entry(entry)
        if display:
            display_timeline.append(display)
    log_lines = _log_lines(all_entries, llm_state)
    return {
        "scenario": {
            "id": scenario.get("id"),
            "name": scenario.get("name") or scenario.get("id"),
            "company": scenario.get("company", ""),
            "summary": scenario.get("summary", ""),
            "agent_brief": _brief_payload(scenario),
            "start_time": scenario.get("start_time"),
            "project_deadlines": [
                project.get("deadline")
                for project in scenario.get("projects", [])
                if project.get("deadline")
            ],
        },
        "observation": observe(db_path),
        "evaluation": evaluate(db_path, scenario_path),
        "tasks": list_tasks(db_path),
        "timeline": entries,
        "display_timeline": display_timeline,
        "log_lines": log_lines,
        "log_entries": _log_entries(log_lines),
        "authored_schedule": _authored_schedule(scenario),
        "scripted_demo": _scripted_demo_state(db_path, scenario_path),
        "llm_session": llm_state,
    }


def _brief_payload(scenario: dict[str, Any]) -> dict[str, Any]:
    brief = scenario.get("agent_brief") if isinstance(scenario.get("agent_brief"), dict) else {}
    return {
        "objective": brief.get("objective") or scenario.get("summary") or scenario.get("name") or scenario.get("id"),
        "guidance": [item for item in brief.get("guidance", []) if isinstance(item, str)],
        "finish_criteria": [item for item in brief.get("finish_criteria", []) if isinstance(item, str)],
        "prompt": _agent_brief_text(scenario),
    }


def _run_next_ui_step(
    db_path: Path,
    scenario_path: Path,
    *,
    policy: str = "scripted",
    model: str | None = None,
    max_turns: int = 40,
    client: Any | None = None,
    progress: ProgressFn | None = None,
) -> dict[str, Any]:
    if policy == "llm":
        return step_llm_session(
            db_path,
            scenario_path,
            model=model,
            max_turns=max_turns,
            client=client,
            progress=progress,
        )

    steps = scripted_policy_steps(scenario_path)
    conn = connect(db_path)
    try:
        index = int(get_state_value(conn, "ui_scripted_step_index") or "0")
        finalized = get_state_value(conn, "ui_scripted_finalized") == "1"
    finally:
        conn.close()

    if index < len(steps):
        step = steps[index]
        result = run_scripted_step(db_path, step)
        if result.get("ok", True):
            conn = connect(db_path)
            try:
                set_state_value(conn, "ui_scripted_step_index", str(index + 1))
                conn.commit()
            finally:
                conn.close()
        return {
            "ok": result.get("ok", True),
            "done": False,
            "index": index + 1,
            "total": len(steps),
            "name": step.get("name"),
            "tool": step.get("tool"),
            "result": result,
        }

    if not finalized:
        result = finalize_to_deadline(db_path, scenario_path)
        conn = connect(db_path)
        try:
            set_state_value(conn, "ui_scripted_finalized", "1")
            conn.commit()
        finally:
            conn.close()
        return {
            "ok": result.get("ok", True),
            "done": True,
            "index": len(steps),
            "total": len(steps),
            "name": "finalize_to_deadline",
            "tool": "finalize_to_deadline",
            "result": result,
        }

    return {
        "ok": True,
        "done": True,
        "index": len(steps),
        "total": len(steps),
        "name": "complete",
        "tool": "complete",
        "result": {"ok": True},
    }


def _emit_new_console_lines(server: _UiServer, log_lines: list[str]) -> None:
    previous = server.last_log_lines
    start = 0
    if previous and log_lines[: len(previous)] == previous:
        start = len(previous)
    elif previous and previous[-1] in log_lines:
        start = log_lines.index(previous[-1]) + 1
    for line in log_lines[start:]:
        if server.progress is not None:
            server.progress(line)
        else:
            print(f"[ui] {line}", flush=True)
    server.last_log_lines = list(log_lines)


def _ui_run_summary(server: _UiServer) -> dict[str, Any]:
    evaluation = evaluate(server.db_path, server.scenario_path)
    if server.policy == "llm":
        state = _load_llm_session(server.db_path) or {}
        return {
            "ok": evaluation.get("score") == evaluation.get("max_score"),
            "policy": "llm",
            "model": state.get("model"),
            "turns": state.get("turns", 0),
            "finished": bool(state.get("finished")),
            "done": bool(state.get("done")),
            "stop_reason": state.get("stop_reason"),
            "steps": list(state.get("steps", [])),
            "evaluation": evaluation,
        }

    demo = _scripted_demo_state(server.db_path, server.scenario_path)
    steps = [
        {"name": step.get("name"), "ok": True}
        for step in scripted_policy_steps(server.scenario_path)[: demo.get("index", 0)]
    ]
    if demo.get("finalized"):
        steps.append({"name": "finalize_to_deadline", "ok": True})
    return {
        "ok": evaluation.get("score") == evaluation.get("max_score"),
        "policy": "scripted",
        "steps": steps,
        "evaluation": evaluation,
    }


def _scripted_demo_state(db_path: Path, scenario_path: Path) -> dict[str, Any]:
    steps = scripted_policy_steps(scenario_path)
    conn = connect(db_path)
    try:
        index = int(get_state_value(conn, "ui_scripted_step_index") or "0")
        finalized = get_state_value(conn, "ui_scripted_finalized") == "1"
    finally:
        conn.close()
    return {
        "index": min(index, len(steps)),
        "total": len(steps),
        "finalized": finalized,
        "done": index >= len(steps) and finalized,
    }


def _authored_schedule(scenario: dict[str, Any]) -> list[dict[str, str]]:
    rows = []
    for event in scenario.get("events", []):
        rows.append(
            {
                "time": str(event.get("scheduled_at") or ""),
                "kind": "scheduled",
                "title": _label(event.get("event_type")),
                "detail": _schedule_detail(event),
            }
        )
    return sorted(rows, key=lambda item: (item["time"], item["title"]))


def _schedule_detail(event: dict[str, Any]) -> str:
    payload = event.get("payload") or {}
    project_id = payload.get("project_id")
    if project_id:
        return f"Authored scenario event for {_label(project_id)}"
    return "Authored scenario event"


def _display_entry(entry: dict[str, Any]) -> dict[str, str] | None:
    kind = entry.get("kind")
    if kind not in {"action", "event_delivered", "message"}:
        return None
    if kind == "action" and entry.get("action_type") in {"reset", "finalize_to_deadline", "advance_time"}:
        return None
    if kind == "event_delivered" and entry.get("event_type") == "coworker_reply":
        return None

    if kind == "message":
        if str(entry.get("sender_id") or "").lower() == "agent":
            return None
        channel = str(entry.get("channel") or "").lower()
        sender = _label(entry.get("sender_id"))
        recipient = _label(entry.get("recipient_id") or "all")
        if channel == "email" and entry.get("subject"):
            title = str(entry.get("subject"))
        else:
            title = sender
        detail = str(entry.get("body") or "")
        card_kind = "message"
        badge = _message_badge(channel)
        route = f"{sender} -> {recipient}".strip()
        tone = _message_tone(channel)
        direction = "reply"
    elif kind == "event_delivered":
        event_type = str(entry.get("event_type") or "")
        title = _label(event_type)
        detail = "Event delivered"
        card_kind = "event"
        badge = _event_badge(event_type)
        route = ""
        tone = _event_tone(event_type)
        direction = "neutral"
    else:
        action_type = str(entry.get("action_type") or "")
        payload = entry.get("payload") or {}
        result = entry.get("result") or {}
        title = _action_title(action_type, payload)
        detail = _action_detail(action_type, payload, result)
        card_kind = "action"
        badge = _action_badge(action_type)
        route = _action_route(action_type, payload)
        tone = _action_tone(action_type)
        direction = "neutral"

    return {
        "time": str(entry.get("time") or ""),
        "kind": card_kind,
        "badge": badge,
        "route": route,
        "title": title,
        "detail": detail,
        "tone": tone,
        "direction": direction,
    }


def _log_lines(entries: list[dict[str, Any]], llm_state: dict[str, Any]) -> list[str]:
    progress = llm_state.get("progress") or []
    if progress:
        return list(progress)

    lines: list[str] = []
    for entry in entries:
        if entry.get("kind") != "action":
            continue
        action_type = str(entry.get("action_type") or "")
        if action_type in {"reset", "finalize_to_deadline"}:
            continue
        payload = entry.get("payload") or {}
        result = entry.get("result") or {}
        lines.append(
            f"[{_pretty_time(entry.get('time'))}] "
            f"{format_agent_tool_progress(action_type, payload, result)}"
        )
        for match in result.get("concept_matches", []):
            if isinstance(match, dict):
                lines.append(
                    f"[{_pretty_time(entry.get('time'))}] {format_concept_progress(match)}"
                )
    return lines[-80:]


def _log_entries(lines: list[str]) -> list[dict[str, str]]:
    return [
        {
            "text": line,
            "html": format_agent_progress_html(line),
        }
        for line in lines
    ]


def _action_title(action_type: str, payload: dict[str, Any]) -> str:
    if action_type == "send_chat":
        return _label(payload.get("person_id"))
    if action_type == "send_email":
        subject = str(payload.get("subject") or "").strip()
        return subject or f"Email to {_label(payload.get('person_id'))}"
    if action_type == "read_doc":
        return _label(payload.get("doc_id"))
    if action_type == "update_doc":
        return f"Updated {_label(payload.get('doc_id'))}"
    if action_type == "update_task":
        return _label(payload.get("task_id"))
    if action_type == "schedule_meeting":
        return str(payload.get("title") or "Meeting")
    if action_type == "advance_time":
        return "Waited"
    return _label(action_type)


def _action_detail(action_type: str, payload: dict[str, Any], result: dict[str, Any]) -> str:
    if action_type in {"send_chat", "send_email"}:
        return str(payload.get("body") or payload.get("message") or "Message sent")
    if action_type == "read_doc":
        return str(result.get("doc_body") or result.get("doc_title") or "Document")
    if action_type == "update_doc":
        return str(payload.get("body") or "Document updated")
    if action_type == "update_task":
        parts = []
        if payload.get("status"):
            parts.append(f"Status {payload.get('status')}")
        if payload.get("priority"):
            parts.append(f"Priority {payload.get('priority')}")
        return " · ".join(parts) or "Task updated"
    if action_type == "schedule_meeting":
        attendees = payload.get("attendees") or []
        people = ", ".join(_label(person) for person in attendees) if attendees else "No attendees"
        starts_at = payload.get("starts_at") or ""
        return f"{people} · {starts_at}".strip(" ·")
    if action_type == "advance_time":
        return str(payload.get("target") or "Advanced simulated time")
    return _label(action_type)


def _action_badge(action_type: str) -> str:
    if action_type == "send_chat":
        return "CHAT"
    if action_type == "send_email":
        return "EMAIL"
    if action_type == "read_doc":
        return "READ DOC"
    if action_type == "update_doc":
        return "WRITE DOC"
    if action_type == "update_task":
        return "TASK"
    if action_type == "schedule_meeting":
        return "MEETING"
    return _label(action_type).upper()


def _action_tone(action_type: str) -> str:
    if action_type == "send_chat":
        return "chat"
    if action_type == "send_email":
        return "email"
    if action_type in {"read_doc", "update_doc"}:
        return "doc"
    if action_type == "schedule_meeting":
        return "meeting"
    if action_type == "update_task":
        return "task"
    return "neutral"


def _message_tone(channel: str) -> str:
    if channel == "email":
        return "email"
    if channel == "chat":
        return "chat"
    return "neutral"


def _message_badge(channel: str) -> str:
    if channel == "email":
        return "EMAIL REPLY"
    if channel == "chat":
        return "CHAT REPLY"
    return "REPLY"


def _event_badge(event_type: str) -> str:
    if event_type == "project_deadline":
        return "DEADLINE"
    if event_type in {"luigi_proactive_repo_risk", "mario_auto_comment_push"}:
        return "RISK"
    if event_type in {"daisy_confidence_check", "nimbus_launch_mode_question"}:
        return "CUSTOMER"
    if event_type == "daisy_private_repo_security_question":
        return "SECURITY"
    if event_type == "koopa_audit_export_request":
        return "PORTFOLIO"
    if event_type == "peach_design_blocked_escalation":
        return "BLOCKER"
    if event_type == "thursday_final_readiness_check":
        return "READINESS"
    return "EVENT"


def _event_tone(event_type: str) -> str:
    if event_type == "project_deadline":
        return "deadline"
    if event_type == "luigi_proactive_repo_risk":
        return "risk"
    if event_type == "mario_auto_comment_push":
        return "scope"
    if event_type in {"daisy_confidence_check", "nimbus_launch_mode_question"}:
        return "customer"
    if event_type == "daisy_private_repo_security_question":
        return "security"
    if event_type == "koopa_audit_export_request":
        return "portfolio"
    if event_type == "peach_design_blocked_escalation":
        return "blocker"
    if event_type == "thursday_final_readiness_check":
        return "readiness"
    return "neutral"


def _action_route(action_type: str, payload: dict[str, Any]) -> str:
    if action_type in {"send_chat", "send_email"}:
        return f"Agent -> {_label(payload.get('person_id'))}"
    if action_type == "schedule_meeting":
        attendees = payload.get("attendees") or []
        if attendees:
            preview = ", ".join(_label(person) for person in attendees[:4])
            suffix = "..." if len(attendees) > 4 else ""
            return f"With {preview}{suffix}"
    return ""


def _label(value: Any) -> str:
    text = str(value or "")
    for prefix in ("doc_", "task_", "fact_", "blocker_", "project_", "event_"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    return text.replace("_", " ").replace("-", " ").strip().capitalize()


def _pretty_time(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    try:
        import datetime as _dt

        parsed = _dt.datetime.fromisoformat(text)
    except Exception:
        return text
    return parsed.strftime("%a %Y-%m-%d %H:%M")


def _html() -> str:
    return """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PM Sim UI</title>
<style>
:root { --bg:#eef2f6; --panel:#fff; --ink:#17202a; --muted:#637083; --line:#d9dee7; --line-strong:#c4cdd9; --blue:#255c99; --purple:#6f4bb2; --good:#0f7b4f; --warn:#9a5b00; --bad:#aa2f2f; --shadow:0 18px 44px rgba(24,35,52,.10); }
* { box-sizing:border-box; }
html, body { overflow-anchor:none; }
body { margin:0; background:var(--bg); color:var(--ink); font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
main { max-width:1280px; margin:0 auto; padding:24px; }
.top { position:sticky; top:0; z-index:2; display:flex; justify-content:space-between; gap:16px; align-items:center; margin-bottom:16px; padding:12px 14px; border:1px solid var(--line); border-radius:12px; background:rgba(255,255,255,.96); box-shadow:var(--shadow); backdrop-filter:blur(10px); }
.brand strong { display:block; font-size:16px; }
.brand span { color:var(--muted); font-size:12px; }
.controls { display:flex; flex-wrap:wrap; gap:8px; align-items:center; }
button { border:1px solid var(--line); border-radius:8px; padding:8px 12px; background:#fff; color:var(--ink); font-weight:800; cursor:pointer; }
button[disabled] { opacity:.55; cursor:wait; }
button.primary { background:var(--blue); border-color:var(--blue); color:#fff; }
.meter { color:var(--muted); font-weight:800; }
.spinner { width:14px; height:14px; border:2px solid #c7d5e8; border-top-color:var(--blue); border-radius:999px; display:inline-block; animation:spin .8s linear infinite; }
.spinner[hidden] { display:none; }
.hero, section, .card { background:var(--panel); border:1px solid var(--line); border-radius:12px; box-shadow:var(--shadow); }
.hero { display:grid; grid-template-columns:minmax(0,1.15fr) minmax(280px,.85fr); gap:16px; align-items:start; padding:22px; margin-bottom:14px; background:linear-gradient(135deg,#ffffff 0%,#edf5ff 100%); }
.eyebrow { color:var(--blue); text-transform:uppercase; font-size:12px; font-weight:800; letter-spacing:.08em; margin:0 0 4px; }
h1 { margin:0 0 6px; font-size:30px; letter-spacing:0; }
h2 { margin:0; font-size:17px; }
p { margin:0 0 8px; }
.brief-card { border:1px solid var(--line); border-radius:10px; background:rgba(255,255,255,.78); padding:12px; display:grid; gap:8px; }
.brief-title { color:var(--blue); font-size:12px; font-weight:850; text-transform:uppercase; letter-spacing:.06em; }
.brief-objective { font-size:14px; font-weight:800; color:var(--ink); }
.brief-list { display:grid; gap:5px; margin:0; padding:0; list-style:none; color:var(--muted); font-size:12px; }
.brief-list li { padding-left:12px; position:relative; }
.brief-list li::before { content:""; position:absolute; left:0; top:.62em; width:4px; height:4px; border-radius:999px; background:var(--blue); }
.score { font-size:30px; font-weight:850; text-align:right; }
.score span { display:block; color:var(--muted); font-size:12px; text-transform:uppercase; }
.grid { display:grid; gap:12px; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); margin-bottom:14px; }
.card { padding:14px; border-left:4px solid var(--blue); }
.label { color:var(--muted); font-size:12px; font-weight:800; text-transform:uppercase; }
.value { font-size:20px; font-weight:850; margin-top:4px; }
section { margin:14px 0; overflow:hidden; }
.section-head { padding:13px 15px; border-bottom:1px solid var(--line); background:#fbfcfe; }
.playback-controls { display:flex; justify-content:center; align-items:center; flex-wrap:wrap; gap:8px; padding:14px 14px 0; }
.replay { display:grid; grid-template-columns:1fr; gap:12px; padding:14px; align-items:start; }
.calendar-board { display:grid; gap:10px; overflow:auto; max-height:52vh; padding-bottom:4px; overscroll-behavior:contain; overflow-anchor:none; }
.day { min-height:180px; border:1px solid var(--line); border-radius:10px; background:#f8fafc; overflow:hidden; }
.day-head { padding:9px 10px; border-bottom:1px solid var(--line); background:#fff; }
.day-head strong { display:block; }
.day-head span { color:var(--muted); font-size:12px; }
.calendar-event { margin:8px; padding:10px; border:1px solid var(--line); border-left:4px solid var(--blue); border-radius:10px; background:#fff; display:grid; gap:6px; }
.calendar-event.event { border-left-color:#7b61c8; }
.calendar-event.message { border-left-color:#1a8f6a; }
.calendar-event.tone-chat { border-left-color:#b54fd6; }
.calendar-event.tone-email { border-left-color:#2a7fd1; }
.calendar-event.tone-doc { border-left-color:#4a67d6; }
.calendar-event.tone-meeting { border-left-color:#c2861b; }
.calendar-event.tone-task { border-left-color:#6b7280; }
.calendar-event.tone-risk { border-left-color:#1f8a5c; }
.calendar-event.tone-scope { border-left-color:#d97706; }
.calendar-event.tone-customer { border-left-color:#2563eb; }
.calendar-event.tone-security { border-left-color:#0f766e; }
.calendar-event.tone-portfolio { border-left-color:#7c3aed; }
.calendar-event.tone-blocker { border-left-color:#dc2626; }
.calendar-event.tone-readiness { border-left-color:#b45309; }
.calendar-event.tone-deadline { border-left-color:#b91c1c; }
.calendar-event.direction-reply { box-shadow:inset 0 0 0 1px rgba(23,32,42,.03); }
.calendar-event.current { outline:2px solid rgba(37,92,153,.24); background:#f2f7ff; }
.calendar-top { display:flex; justify-content:space-between; align-items:flex-start; gap:8px; }
.calendar-meta { display:flex; align-items:center; gap:8px; min-width:0; flex:1 1 auto; }
.tool-badge { display:inline-flex; align-items:center; border-radius:999px; padding:3px 8px; font-size:11px; font-weight:800; letter-spacing:.04em; background:#eaf1fb; color:var(--blue); }
.calendar-event.direction-reply .tool-badge { box-shadow:inset 0 0 0 1px rgba(255,255,255,.45); }
.calendar-event.message .tool-badge { background:#e7f6f0; color:#136c50; }
.calendar-event.event .tool-badge { background:#f2ebff; color:var(--purple); }
.calendar-event.tone-chat .tool-badge { background:#f6e8fb; color:#8f2db0; }
.calendar-event.tone-email .tool-badge { background:#e8f1fd; color:#1e5fb8; }
.calendar-event.tone-doc .tool-badge { background:#eef0ff; color:#3e57c7; }
.calendar-event.tone-meeting .tool-badge { background:#fff4dc; color:#9a6a0b; }
.calendar-event.tone-task .tool-badge { background:#eef2f7; color:#596579; }
.calendar-event.tone-risk .tool-badge { background:#e6f6ee; color:#166a46; }
.calendar-event.tone-scope .tool-badge { background:#fff0dd; color:#b86700; }
.calendar-event.tone-customer .tool-badge { background:#e8f1ff; color:#215fc4; }
.calendar-event.tone-security .tool-badge { background:#e6f7f5; color:#0f766e; }
.calendar-event.tone-portfolio .tool-badge { background:#f1eaff; color:#6d35d6; }
.calendar-event.tone-blocker .tool-badge { background:#feeceb; color:#bc2d2d; }
.calendar-event.tone-readiness .tool-badge { background:#fff2e3; color:#a85b00; }
.calendar-event.tone-deadline .tool-badge { background:#fee8e8; color:#b42323; }
.calendar-time { font-size:12px; font-weight:800; color:var(--muted); flex:0 0 auto; }
.calendar-title { font-size:14px; font-weight:800; color:var(--ink); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.calendar-event.direction-reply .calendar-title { color:#111827; }
.calendar-detail { display:none; }
.calendar-event button.card-open { all:unset; display:grid; gap:6px; cursor:pointer; width:100%; }
.calendar-event button.card-open:focus-visible { outline:2px solid rgba(37,92,153,.35); outline-offset:2px; border-radius:8px; }
.log-console { max-height:360px; overflow:auto; padding:14px; border:1px solid var(--line); border-radius:10px; background:#101722; color:#d9e7ff; font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace; overscroll-behavior:contain; overflow-anchor:none; }
.log-line { white-space:pre-wrap; border-bottom:1px solid rgba(255,255,255,.06); padding:6px 0; }
.log-line:last-child { border-bottom:none; }
.agent-prefix, .agent-muted, .agent-message.muted { color:#7f8ea3; }
.agent-time, .agent-tool-email { color:#5cc8ff; }
.agent-tool-read { color:#8ab4ff; }
.agent-tool-chat { color:#e7a1ff; }
.agent-tool-meeting, .agent-tool-task, .agent-cost { color:#ffd166; }
.agent-tool-wait, .agent-message.good { color:#67e09c; }
.agent-person { color:#f3f7ff; font-weight:800; }
.helper { color:var(--muted); font-size:12px; margin:8px 14px 0; }
.columns { display:grid; grid-template-columns:repeat(auto-fit,minmax(320px,1fr)); gap:12px; }
.list { padding:14px; display:grid; gap:8px; }
.row { border:1px solid var(--line); border-radius:8px; padding:10px; background:#fff; }
.row.scheduled { border-left:4px solid var(--purple); }
.project-card { border:1px solid var(--line); border-radius:10px; padding:12px; background:#fff; display:grid; gap:8px; }
.project-head { display:grid; gap:4px; }
.project-title { font-size:15px; font-weight:800; }
.project-state { display:flex; flex-wrap:wrap; gap:8px; color:var(--muted); font-size:12px; font-weight:800; }
.project-state strong { color:var(--ink); }
.meta-chip { display:inline-flex; align-items:center; border-radius:999px; padding:3px 8px; font-size:11px; font-weight:800; background:#f1f4f8; color:var(--muted); }
.project-copy { font-size:13px; color:var(--muted); }
.project-footer { display:flex; flex-wrap:wrap; gap:6px; }
.blocker-groups { padding:14px; display:grid; gap:12px; }
.blocker-group { display:grid; gap:8px; }
.blocker-group-title { color:var(--muted); font-size:12px; font-weight:800; text-transform:uppercase; letter-spacing:.04em; }
.blocker-card { border:1px solid var(--line); border-radius:10px; padding:12px; background:#fff; display:grid; gap:6px; }
.blocker-card.resolved { opacity:.72; background:#fbfcfd; }
.blocker-head { display:flex; justify-content:space-between; align-items:flex-start; gap:8px; }
.blocker-title { font-weight:800; }
.blocker-copy { color:var(--muted); font-size:13px; }
.table-wrap { padding:14px; overflow:auto; }
table { width:100%; border-collapse:collapse; font-size:13px; }
th, td { padding:10px 8px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; }
th { color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.04em; }
.task-list { padding:14px; display:grid; gap:6px; }
.task-row { border:1px solid var(--line); border-radius:8px; padding:9px 10px; background:#fff; display:grid; gap:5px; }
.task-row-top { display:flex; justify-content:space-between; gap:10px; align-items:flex-start; }
.task-title { font-size:13px; font-weight:800; }
.task-meta { color:var(--muted); font-size:12px; overflow-wrap:anywhere; }
.score-grid { padding:14px; display:grid; gap:10px; grid-template-columns:repeat(auto-fit,minmax(240px,1fr)); }
.score-card { border:1px solid var(--line); border-radius:10px; padding:12px; background:#fff; display:grid; gap:8px; }
.score-top { display:flex; justify-content:space-between; align-items:flex-start; gap:8px; }
.score-name { font-size:14px; font-weight:800; }
.score-points { font-size:12px; color:var(--muted); font-weight:800; }
.score-note { font-size:13px; color:var(--muted); }
.score-missing { font-size:12px; color:var(--bad); }
.milestone-list { display:grid; gap:8px; margin-top:2px; }
.milestone-item { border:1px solid var(--line); border-radius:8px; padding:9px; background:#fbfcfe; display:grid; gap:5px; }
.milestone-head { display:flex; justify-content:space-between; align-items:flex-start; gap:8px; }
.milestone-key { font-size:12px; font-weight:850; color:var(--ink); overflow-wrap:anywhere; }
.milestone-note { font-size:12px; color:var(--ink); }
.milestone-meta { font-size:11px; color:var(--muted); overflow-wrap:anywhere; }
.milestone-empty { font-size:12px; color:var(--muted); font-style:italic; }
.schedule-grid { padding:14px; display:grid; gap:8px; }
.schedule-card { border:1px solid var(--line); border-left:4px solid var(--purple); border-radius:10px; padding:10px 12px; background:#fff; display:grid; gap:4px; }
.schedule-top { display:flex; justify-content:space-between; gap:8px; align-items:flex-start; }
.schedule-title { font-weight:800; font-size:14px; }
.schedule-detail { font-size:12px; color:var(--muted); }
details.operator { margin:14px 0; border:1px solid var(--line); border-radius:12px; background:#fff; box-shadow:var(--shadow); overflow:hidden; }
details.operator summary { cursor:pointer; padding:13px 15px; font-weight:850; background:#fbfcfe; border-bottom:1px solid var(--line); }
details.operator[open] summary { border-bottom:1px solid var(--line); }
.inspector-section { border-top:1px solid var(--line); }
.inspector-title { padding:13px 15px 0; font-size:15px; font-weight:800; }
.badge { display:inline-block; border-radius:999px; padding:2px 8px; font-size:12px; font-weight:800; background:#eef2f7; }
.good { color:var(--good); } .warn { color:var(--warn); } .bad { color:var(--bad); }
.empty { color:var(--muted); font-style:italic; padding:14px; }
.modal { position:fixed; inset:0; background:rgba(15,24,38,.42); display:flex; align-items:center; justify-content:center; padding:24px; z-index:20; }
.modal[hidden] { display:none; }
.modal-card { width:min(760px, 100%); max-height:min(80vh, 720px); overflow:auto; background:#fff; border:1px solid var(--line); border-radius:14px; box-shadow:var(--shadow); }
.modal-head { display:flex; justify-content:space-between; align-items:flex-start; gap:12px; padding:16px 18px 10px; border-bottom:1px solid var(--line); }
.modal-title { font-size:18px; font-weight:850; }
.modal-route { color:var(--muted); font-size:13px; margin-top:4px; overflow-wrap:anywhere; }
.modal-close { all:unset; cursor:pointer; font-size:22px; line-height:1; color:var(--muted); padding:4px; }
.modal-body { padding:16px 18px 18px; display:grid; gap:12px; }
.modal-block { display:grid; gap:6px; }
.modal-label { color:var(--muted); font-size:12px; font-weight:800; text-transform:uppercase; letter-spacing:.04em; }
.modal-text { font-size:14px; color:var(--ink); white-space:pre-wrap; overflow-wrap:anywhere; }
@keyframes spin { to { transform:rotate(360deg); } }
@media (max-width:800px) { .top { display:block; } .hero { grid-template-columns:1fr; } .controls { margin-top:10px; } .score { text-align:left; margin-top:12px; } }
</style>
</head>
<body>
<main>
  <nav class="top">
    <div class="brand"><strong>PM Sim</strong><span>live operator UI</span></div>
    <div class="controls">
      <span class="spinner" id="spinner" hidden></span>
      <span class="meter" id="meter">loading</span>
    </div>
  </nav>
  <header class="hero">
    <div>
      <p class="eyebrow">Simulation</p>
      <h1 id="title">PM Sim</h1>
      <p id="subtitle"></p>
      <p>Simulated time: <strong id="sim-time"></strong></p>
    </div>
    <div class="brief-card" id="agent-brief"></div>
  </header>
  <section id="playback-section">
    <div class="section-head"><h2>Live Playback</h2></div>
    <p class="helper">Play runs the selected backend policy one step at a time. The calendar shows when things happen; the log shows the actual streamed tool/event flow.</p>
    <div class="playback-controls">
      <button class="primary" id="play">Play</button>
      <button id="step">Step</button>
      <button id="pause">Pause</button>
      <button id="reset">Reset</button>
    </div>
    <div class="replay">
      <div class="calendar-board" id="calendar-board"></div>
      <div class="log-console" id="playback"></div>
    </div>
  </section>
  <div class="columns">
    <section><div class="section-head"><h2>Visible Projects</h2></div><p class="helper">Current project state from SQLite. It changes only when delivered events or actions mutate state.</p><div class="list" id="projects"></div></section>
    <section><div class="section-head"><h2>Known Blockers</h2></div><p class="helper">Only blockers already visible in the run. This is the PM-facing view, not the hidden authored truth.</p><div class="list" id="blockers"></div></section>
  </div>
  <details class="operator">
    <summary>Operator inspector: current evaluation</summary>
    <p class="helper">This is for debugging and grading. It is computed from current visible state and milestones, and is not shown as part of the agent-facing playback.</p>
    <div class="grid" id="summary"></div>
    <div class="score-grid" id="evaluation"></div>
    <div class="inspector-section">
    <div class="inspector-title">Task State</div>
    <p class="helper">Compact work-tracker state for debugging task gates and fake-progress guardrails.</p>
    <div id="tasks"></div>
    </div>
    <div class="inspector-section">
    <div class="inspector-title">Authored Schedule</div>
    <p class="helper">Seeded scenario events for author/debug use. These are not all visible to the agent at the start.</p>
    <div class="schedule-grid" id="schedule"></div>
    </div>
  </details>
</main>
<div class="modal" id="card-modal" hidden>
  <div class="modal-card">
    <div class="modal-head">
      <div>
        <div class="modal-title" id="modal-title"></div>
        <div class="modal-route" id="modal-route"></div>
      </div>
      <button class="modal-close" id="modal-close" aria-label="Close">×</button>
    </div>
    <div class="modal-body">
      <div class="modal-block">
        <div class="modal-label">Time</div>
        <div class="modal-text" id="modal-time"></div>
      </div>
      <div class="modal-block">
        <div class="modal-label">Type</div>
        <div class="modal-text" id="modal-badge"></div>
      </div>
      <div class="modal-block">
        <div class="modal-label">Details</div>
        <div class="modal-text" id="modal-detail"></div>
      </div>
    </div>
  </div>
</div>
<script>
let timer = null;
let stepping = false;

const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, c => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;" }[c]));
const pretty = (value) => {
  if (!value) return "";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  return d.toLocaleString([], { weekday:"short", month:"short", day:"numeric", hour:"numeric", minute:"2-digit" });
};
const statusClass = (value) => {
  const text = String(value ?? "").toLowerCase();
  if (["passed","done","complete","completed","resolved","low","delivered","shipped"].includes(text)) return "good";
  if (["partial","medium","scheduled","pending","ready","in_progress","active"].includes(text)) return "warn";
  if (["missing","failed","blocked","open","high","critical","at_risk"].includes(text)) return "bad";
  return "";
};
const label = (value) => String(value ?? "").replace(/^(doc|task|fact|blocker|project|event)_/, "").replaceAll("_", " ").replaceAll("-", " ").replace(/^./, c => c.toUpperCase());
const dateKey = (value) => String(value || "").slice(0, 10);
const timeOnly = (value) => {
  if (!value) return "";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  return d.toLocaleTimeString([], { hour:"numeric", minute:"2-digit" });
};
const dayLabel = (value) => {
  const d = new Date(`${value}T12:00:00`);
  if (Number.isNaN(d.getTime())) return value;
  return d.toLocaleDateString([], { weekday:"short" });
};
const addDays = (date, days) => {
  const next = new Date(date);
  next.setDate(next.getDate() + days);
  return next;
};
const isoDay = (date) => date.toISOString().slice(0, 10);

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}

function setLoading(active) {
  stepping = active;
  $("spinner").hidden = !active;
  $("play").disabled = active;
  $("step").disabled = active;
  $("reset").disabled = active;
}

function card(labelText, value) {
  return `<div class="card"><div class="label">${esc(labelText)}</div><div class="value">${esc(value)}</div></div>`;
}

function row(title, detail, meta = "") {
  return `<div class="row"><strong>${esc(title)}</strong>${meta ? ` <span class="badge ${statusClass(meta)}">${esc(label(meta))}</span>` : ""}<div>${esc(detail)}</div></div>`;
}

function projectCard(project) {
  const status = project.status || "";
  const risk = project.risk_level || project.risk || "";
  const outcome = project.final_outcome || project.decision || project.outcome_summary || "";
  return `
    <div class="project-card">
      <div class="project-head">
        <div class="project-title">${esc(project.name || "Project")}</div>
        <div class="project-state">
          ${status ? `<span>Project status: <strong class="${statusClass(status)}">${esc(label(status))}</strong></span>` : ""}
          ${risk ? `<span>Risk: <strong class="${statusClass(risk)}">${esc(label(risk))}</strong></span>` : ""}
        </div>
      </div>
      ${project.stakeholder_pressure ? `<div class="project-copy">${esc(project.stakeholder_pressure)}</div>` : ""}
      <div class="project-footer">
        ${project.deadline ? `<span class="meta-chip">Deadline ${esc(pretty(project.deadline))}</span>` : ""}
        ${outcome ? `<span class="meta-chip">${esc(label(outcome))}</span>` : ""}
      </div>
    </div>
  `;
}

function taskCard(task) {
  const meta = [
    `Owner ${label(task.owner_id || "unowned")}`,
    `Priority ${label(task.priority || "")}`,
    task.due_at ? `Due ${pretty(task.due_at)}` : "",
    task.blocked_by ? `Blocked by ${label(task.blocked_by)}` : "",
  ].filter(Boolean).join(" · ");
  return `
    <div class="task-row">
      <div class="task-row-top">
        <div class="task-title">${esc(task.title || "Task")}</div>
        <span class="badge ${statusClass(task.status)}">${esc(label(task.status || ""))}</span>
      </div>
      <div class="task-meta">${esc(meta)}</div>
    </div>
  `;
}

function blockerCard(blocker) {
  const status = blocker.status || "";
  const statusText = status === "surfaced" ? "Known risk" : label(status);
  return `
    <div class="blocker-card ${status === "resolved" ? "resolved" : ""}">
      <div class="blocker-head">
        <div class="blocker-title">${esc(blocker.title || "Blocker")}</div>
        <span class="badge ${statusClass(status)}">${esc(statusText)}</span>
      </div>
      <div class="blocker-copy">${esc(blocker.description || "")}</div>
      ${blocker.severity ? `<div><span class="meta-chip">${esc(label(blocker.severity))} severity</span></div>` : ""}
    </div>
  `;
}

function blockerGroups(blockers) {
  if (!blockers.length) return `<div class="empty">No visible blockers.</div>`;
  const active = blockers.filter(blocker => blocker.status !== "resolved");
  const resolved = blockers.filter(blocker => blocker.status === "resolved");
  return `
    <div class="blocker-groups">
      <div class="blocker-group">
        <div class="blocker-group-title">Active / Known Risks</div>
        ${active.length ? active.map(blockerCard).join("") : `<div class="empty">No active visible blockers.</div>`}
      </div>
      ${resolved.length ? `<div class="blocker-group"><div class="blocker-group-title">Resolved</div>${resolved.map(blockerCard).join("")}</div>` : ""}
    </div>
  `;
}

function evaluationCard(component) {
  const missing = component.missing_milestones || [];
  const milestones = component.milestones || [];
  return `
    <div class="score-card">
      <div class="score-top">
        <div class="score-name">${esc(label(component.key))}</div>
        <div class="score-points">${esc(component.earned)} / ${esc(component.points)}</div>
      </div>
      <div><span class="badge ${statusClass(component.status)}">${esc(label(component.status || ""))}</span></div>
      <div class="score-note">${esc(component.note || "")}</div>
      ${missing.length ? `<div class="score-missing">Missing: ${esc(missing.join(", "))}</div>` : ""}
      <div class="milestone-list">
        ${milestones.length ? milestones.map(milestoneItem).join("") : `<div class="milestone-empty">No causal milestone recorded for this component yet.</div>`}
      </div>
    </div>
  `;
}

function milestoneItem(item) {
  return `
    <div class="milestone-item">
      <div class="milestone-head">
        <div class="milestone-key">${esc(item.key || "")}</div>
        ${item.timing ? `<span class="badge ${statusClass(item.timing)}">${esc(label(item.timing))}</span>` : ""}
      </div>
      <div class="milestone-note">${esc(item.note || "")}</div>
      <div class="milestone-meta">${esc(pretty(item.created_at))} · Source: ${esc(item.source || "")}</div>
    </div>
  `;
}

function scheduleCard(item) {
  return `
    <div class="schedule-card">
      <div class="schedule-top">
        <div class="schedule-title">${esc(item.title || "")}</div>
        <span class="meta-chip">${esc(pretty(item.time))}</span>
      </div>
      <div class="schedule-detail">${esc(item.detail || "")}</div>
    </div>
  `;
}

function modalOpen(item) {
  $("modal-title").textContent = item.title || "";
  $("modal-route").textContent = item.route || "";
  $("modal-time").textContent = pretty(item.time);
  $("modal-badge").textContent = item.badge || item.kind || "";
  $("modal-detail").textContent = item.detail || "";
  $("card-modal").hidden = false;
}

function modalClose() {
  $("card-modal").hidden = true;
}

function renderBrief(brief) {
  if (!brief || !brief.objective) {
    $("agent-brief").innerHTML = `<div class="brief-title">Agent prompt</div><div class="brief-objective">No scenario brief configured.</div>`;
    return;
  }
  const guidance = (brief.guidance || []).slice(0, 3);
  const finish = (brief.finish_criteria || []).slice(0, 2);
  const rows = [
    ...guidance.map(item => `Guidance: ${item}`),
    ...finish.map(item => `Finish: ${item}`),
  ];
  $("agent-brief").innerHTML = `
    <div class="brief-title">Agent prompt</div>
    <div class="brief-objective">${esc(brief.objective)}</div>
    ${rows.length ? `<ul class="brief-list">${rows.map(item => `<li>${esc(item)}</li>`).join("")}</ul>` : ""}
  `;
}

function scenarioDays(scenario, items) {
  const values = [
    scenario.start_time,
    ...(scenario.project_deadlines || []),
    ...items.map(item => item.time)
  ].filter(Boolean);
  const parsed = values
    .map(value => new Date(String(value).slice(0, 10) + "T12:00:00"))
    .filter(date => !Number.isNaN(date.getTime()));
  if (!parsed.length) return [];
  const start = new Date(Math.min(...parsed));
  const end = new Date(Math.max(...parsed));
  const days = [];
  for (let day = start; day <= end; day = addDays(day, 1)) days.push(isoDay(day));
  return days;
}

function renderReplay(items, scenario) {
  const latest = items.length - 1;
  const days = scenarioDays(scenario || {}, items);
  $("calendar-board").style.gridTemplateColumns = days.length
    ? `repeat(${days.length}, minmax(150px, 1fr))`
    : "";
  $("calendar-board").innerHTML = days.length
    ? days.map(day => {
        const cards = items
          .map((item, index) => ({ item, index }))
          .filter(row => dateKey(row.item.time) === day)
          .map(row => `<div class="calendar-event ${esc(row.item.kind)} tone-${esc(row.item.tone || "neutral")} direction-${esc(row.item.direction || "neutral")} ${row.index === latest ? "current" : ""}">
            <button class="card-open" type="button"
              data-time="${esc(row.item.time)}"
              data-badge="${esc(row.item.badge || row.item.kind)}"
              data-route="${esc(row.item.route || "")}"
              data-title="${esc(row.item.title || "")}"
              data-detail="${esc(row.item.detail || "")}">
              <div class="calendar-top">
                <div class="calendar-meta">
                  <span class="tool-badge">${esc(row.item.badge || row.item.kind)}</span>
                </div>
                <div class="calendar-time">${esc(timeOnly(row.item.time))}</div>
              </div>
              <div class="calendar-title">${esc(row.item.title)}</div>
            </button>
          </div>`)
          .join("");
        return `<div class="day"><div class="day-head"><strong>${esc(dayLabel(day))}</strong><span>${esc(day)}</span></div>${cards || `<div class="empty">No visible activity.</div>`}</div>`;
      }).join("")
    : `<div class="empty">No calendar activity yet.</div>`;
}

function render(state) {
  const pageX = window.scrollX;
  const pageY = window.scrollY;
  const scenario = state.scenario || {};
  const obs = state.observation || {};
  const evaluation = state.evaluation || {};
  $("title").textContent = scenario.name || obs.scenario_id || "PM Sim";
  $("subtitle").textContent = scenario.company || "";
  $("sim-time").textContent = pretty(obs.current_time);
  renderBrief(scenario.agent_brief || {});
  const demo = state.scripted_demo || {};
  const llm = state.llm_session || {};
  const modelLabel = llm.model ? ` · model ${llm.model}` : "";
  $("meter").textContent = llm.active
    ? `llm turn ${llm.turns ?? 0}${modelLabel} · ${llm.steps ?? 0} tool step(s) · ${state.display_timeline.length} visible item(s)`
    : `step ${demo.index ?? 0} / ${demo.total ?? 0} · ${state.display_timeline.length} visible item(s)`;

  $("summary").innerHTML = [
    card("Milestones", evaluation.milestone_count ?? 0),
    card("Outcome", label((evaluation.final_outcome || {}).outcome || "pending")),
    card("Status", evaluation.score === evaluation.max_score ? "passed" : "incomplete")
  ].join("");

  renderReplay(state.display_timeline || [], scenario);
  const logEntries = state.log_entries || [];
  const logs = state.log_lines || [];
  const logEl = $("playback");
  const shouldStick = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 24;
  logEl.innerHTML = logEntries.length
    ? logEntries.map(entry => `<div class="log-line">${entry.html || esc(entry.text || "")}</div>`).join("")
    : logs.length
      ? logs.map(line => `<div class="log-line">${esc(line)}</div>`).join("")
    : `<div class="empty">No log output yet.</div>`;
  if (shouldStick) {
    logEl.scrollTop = logEl.scrollHeight;
  }

  $("projects").innerHTML = (obs.projects || []).map(projectCard).join("") || `<div class="empty">No projects.</div>`;
  $("blockers").innerHTML = blockerGroups(obs.known_blockers || []);
  $("schedule").innerHTML = (state.authored_schedule || []).map(scheduleCard).join("") || `<div class="empty">No authored events.</div>`;
  $("evaluation").innerHTML = (evaluation.components || []).map(evaluationCard).join("") || `<div class="empty">No evaluation yet.</div>`;
  const tasks = (state.tasks || []).slice(0, 12);
  $("tasks").innerHTML = tasks.length
    ? `<div class="task-list">${tasks.map(taskCard).join("")}</div>`
    : `<div class="empty">No tasks.</div>`;
  window.scrollTo(pageX, pageY);
}

async function refresh() {
  render(await api("/api/state"));
}

async function step() {
  if (stepping) return;
  setLoading(true);
  try {
    const state = await api("/api/advance-next", { method: "POST" });
    if (state.busy) return;
    render(state);
    if (state.done) pause();
  } finally {
    setLoading(false);
  }
}

function play() {
  pause();
  const loop = async () => {
    await step();
    if (timer) timer = setTimeout(loop, 900);
  };
  timer = setTimeout(loop, 0);
}

function pause() {
  if (timer) clearTimeout(timer);
  timer = null;
}

$("play").addEventListener("click", play);
$("step").addEventListener("click", step);
$("pause").addEventListener("click", pause);
$("reset").addEventListener("click", async () => { pause(); render(await api("/api/reset", { method: "POST" })); });
refresh();
$("calendar-board").addEventListener("click", (event) => {
  const card = event.target.closest(".card-open");
  if (!card) return;
  modalOpen({
    time: card.dataset.time || "",
    badge: card.dataset.badge || "",
    route: card.dataset.route || "",
    title: card.dataset.title || "",
    detail: card.dataset.detail || "",
  });
});
$("modal-close").addEventListener("click", modalClose);
$("card-modal").addEventListener("click", (event) => {
  if (event.target === $("card-modal")) modalClose();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !$("card-modal").hidden) modalClose();
});
</script>
</body>
</html>"""
