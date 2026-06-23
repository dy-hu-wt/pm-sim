from __future__ import annotations

import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .actions import list_tasks
from .agents.finalize import finalize_to_deadline
from .agents.llm import llm_session_state, start_llm_session, step_llm_session
from .agents.scripted import run_scripted_step, scripted_policy_steps
from .db import connect
from .evaluator import evaluate
from .paths import DEFAULT_DB_PATH, DEFAULT_SCENARIO_PATH
from .scenario import load_scenario
from .state import get_state_value, observe, reset, set_state_value
from .timeline import timeline


DEFAULT_UI_HOST = "127.0.0.1"
DEFAULT_UI_PORT = 8765


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
    ):
        super().__init__(server_address, RequestHandlerClass)
        self.db_path = db_path
        self.scenario_path = scenario_path
        self.timeline_limit = timeline_limit
        self.policy = policy
        self.model = model
        self.max_turns = max_turns
        self.step_lock = threading.Lock()


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
                )
            finally:
                self.server.step_lock.release()
            payload = _state_payload(self.server.db_path, self.server.scenario_path, self.server.timeline_limit)
            payload["step_result"] = step_result
            payload["done"] = bool(step_result.get("done"))
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
    entries = timeline(db_path, limit=0)
    if timeline_limit > 0:
        entries = entries[-timeline_limit:]
    display_timeline = []
    for entry in entries:
        display = _display_entry(entry)
        if display:
            display_timeline.append(display)
    return {
        "scenario": {
            "id": scenario.get("id"),
            "name": scenario.get("name") or scenario.get("id"),
            "company": scenario.get("company", ""),
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
        "log_lines": _log_lines(entries, llm_state),
        "authored_schedule": _authored_schedule(scenario),
        "scripted_demo": _scripted_demo_state(db_path, scenario_path),
        "llm_session": llm_state,
    }


def _run_next_ui_step(
    db_path: Path,
    scenario_path: Path,
    *,
    policy: str = "scripted",
    model: str | None = None,
    max_turns: int = 40,
    client: Any | None = None,
) -> dict[str, Any]:
    if policy == "llm":
        return step_llm_session(
            db_path,
            scenario_path,
            model=model,
            max_turns=max_turns,
            client=client,
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
    if kind not in {"action", "event_delivered"}:
        return None
    if kind == "action" and entry.get("action_type") in {"reset", "finalize_to_deadline"}:
        return None
    if kind == "event_delivered" and entry.get("event_type") == "coworker_reply":
        return None

    if kind == "event_delivered":
        title = _label(entry.get("event_type"))
        detail = "Event delivered"
        card_kind = "event"
    else:
        action_type = str(entry.get("action_type") or "")
        payload = entry.get("payload") or {}
        title = _action_title(action_type, payload)
        detail = _action_detail(action_type, payload)
        card_kind = "action"

    return {
        "time": str(entry.get("time") or ""),
        "kind": card_kind,
        "title": title,
        "detail": detail,
    }


def _log_lines(entries: list[dict[str, Any]], llm_state: dict[str, Any]) -> list[str]:
    progress = llm_state.get("progress") or []
    if progress:
        return list(progress)

    lines: list[str] = []
    for entry in entries:
        kind = entry.get("kind")
        if kind == "action":
            action_type = str(entry.get("action_type") or "")
            if action_type in {"reset", "finalize_to_deadline"}:
                continue
            payload = entry.get("payload") or {}
            detail = _action_detail(action_type, payload)
            title = _action_title(action_type, payload).upper()
            lines.append(f"[{_pretty_time(entry.get('time'))}] {title} - {detail}")
        elif kind == "event_delivered":
            if entry.get("event_type") == "coworker_reply":
                continue
            lines.append(
                f"[{_pretty_time(entry.get('time'))}] EVENT - {_label(entry.get('event_type'))}"
            )
    return lines[-80:]


def _action_title(action_type: str, payload: dict[str, Any]) -> str:
    if action_type == "send_chat":
        return f"Chat to {_label(payload.get('person_id'))}"
    if action_type == "send_email":
        return f"Email to {_label(payload.get('person_id'))}"
    if action_type == "read_doc":
        return "Read document"
    if action_type == "update_doc":
        return "Updated document"
    if action_type == "update_task":
        return "Updated task"
    if action_type == "schedule_meeting":
        return "Scheduled meeting"
    if action_type == "advance_time":
        return "Waited"
    return _label(action_type)


def _action_detail(action_type: str, payload: dict[str, Any]) -> str:
    if action_type in {"send_chat", "send_email"}:
        return str(payload.get("subject") or "Message sent")
    if action_type in {"read_doc", "update_doc"}:
        return _label(payload.get("doc_id"))
    if action_type == "update_task":
        return _label(payload.get("task_id"))
    if action_type == "schedule_meeting":
        return str(payload.get("title") or "Meeting")
    if action_type == "advance_time":
        return str(payload.get("target") or "Advanced simulated time")
    return _label(action_type)


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
body { margin:0; background:var(--bg); color:var(--ink); font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
main { max-width:1280px; margin:0 auto; padding:24px; }
.top { position:sticky; top:0; z-index:2; display:flex; justify-content:space-between; gap:16px; align-items:center; margin-bottom:16px; padding:12px 14px; border:1px solid var(--line); border-radius:12px; background:rgba(255,255,255,.96); box-shadow:var(--shadow); backdrop-filter:blur(10px); }
.brand strong { display:block; font-size:16px; }
.brand span { color:var(--muted); font-size:12px; }
.controls { display:flex; flex-wrap:wrap; gap:8px; align-items:center; }
button { border:1px solid var(--line); border-radius:8px; padding:8px 12px; background:#fff; color:var(--ink); font-weight:800; cursor:pointer; }
button.primary { background:var(--blue); border-color:var(--blue); color:#fff; }
.meter { color:var(--muted); font-weight:800; }
.hero, section, .card { background:var(--panel); border:1px solid var(--line); border-radius:12px; box-shadow:var(--shadow); }
.hero { display:flex; justify-content:space-between; gap:16px; align-items:flex-end; padding:22px; margin-bottom:14px; background:linear-gradient(135deg,#ffffff 0%,#edf5ff 100%); }
.eyebrow { color:var(--blue); text-transform:uppercase; font-size:12px; font-weight:800; letter-spacing:.08em; margin:0 0 4px; }
h1 { margin:0 0 6px; font-size:30px; letter-spacing:0; }
h2 { margin:0; font-size:17px; }
p { margin:0 0 8px; }
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
.calendar-board { display:grid; gap:10px; overflow-x:auto; padding-bottom:4px; }
.day { min-height:180px; border:1px solid var(--line); border-radius:10px; background:#f8fafc; overflow:hidden; }
.day-head { padding:9px 10px; border-bottom:1px solid var(--line); background:#fff; }
.day-head strong { display:block; }
.day-head span { color:var(--muted); font-size:12px; }
.calendar-event { margin:8px; padding:8px; border:1px solid var(--line); border-left:4px solid var(--blue); border-radius:8px; background:#fff; }
.calendar-event.event { border-left-color:var(--purple); }
.calendar-event.current { outline:2px solid rgba(37,92,153,.24); background:#f2f7ff; }
.log-console { max-height:360px; overflow:auto; padding:14px; border:1px solid var(--line); border-radius:10px; background:#101722; color:#d9e7ff; font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace; }
.log-line { white-space:pre-wrap; border-bottom:1px solid rgba(255,255,255,.06); padding:6px 0; }
.log-line:last-child { border-bottom:none; }
.helper { color:var(--muted); font-size:12px; margin:8px 14px 0; }
.columns { display:grid; grid-template-columns:repeat(auto-fit,minmax(320px,1fr)); gap:12px; }
.list { padding:14px; display:grid; gap:8px; }
.row { border:1px solid var(--line); border-radius:8px; padding:10px; background:#fff; }
.row.scheduled { border-left:4px solid var(--purple); }
.table-wrap { padding:14px; overflow:auto; }
table { width:100%; border-collapse:collapse; font-size:13px; }
th, td { padding:10px 8px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; }
th { color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.04em; }
details.operator { margin:14px 0; border:1px solid var(--line); border-radius:12px; background:#fff; box-shadow:var(--shadow); overflow:hidden; }
details.operator summary { cursor:pointer; padding:13px 15px; font-weight:850; background:#fbfcfe; border-bottom:1px solid var(--line); }
details.operator[open] summary { border-bottom:1px solid var(--line); }
.badge { display:inline-block; border-radius:999px; padding:2px 8px; font-size:12px; font-weight:800; background:#eef2f7; }
.good { color:var(--good); } .warn { color:var(--warn); } .bad { color:var(--bad); }
.empty { color:var(--muted); font-style:italic; padding:14px; }
@media (max-width:800px) { .top,.hero { display:block; } .controls { margin-top:10px; } .score { text-align:left; margin-top:12px; } }
</style>
</head>
<body>
<main>
  <nav class="top">
    <div class="brand"><strong>PM Sim</strong><span>live operator UI</span></div>
    <div class="controls">
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
  <section><div class="section-head"><h2>Tasks</h2></div><div class="table-wrap" id="tasks"></div></section>
  <details class="operator">
    <summary>Operator inspector: current evaluation</summary>
    <p class="helper">This is for debugging and grading. It is computed from current visible state/evidence and is not shown as part of the agent-facing playback.</p>
    <div class="grid" id="summary"></div>
    <div class="list" id="evaluation"></div>
    <div class="section-head"><h2>Authored Schedule</h2></div>
    <p class="helper">Seeded scenario events for author/debug use. These are not all visible to the agent at the start.</p>
    <div class="list" id="schedule"></div>
  </details>
</main>
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

function card(labelText, value) {
  return `<div class="card"><div class="label">${esc(labelText)}</div><div class="value">${esc(value)}</div></div>`;
}

function row(title, detail, meta = "") {
  return `<div class="row"><strong>${esc(title)}</strong>${meta ? ` <span class="badge ${statusClass(meta)}">${esc(label(meta))}</span>` : ""}<div>${esc(detail)}</div></div>`;
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
  const days = scenarioDays(scenario || {}, items);
  $("calendar-board").style.gridTemplateColumns = days.length
    ? `repeat(${days.length}, minmax(150px, 1fr))`
    : "";
  $("calendar-board").innerHTML = days.length
    ? days.map(day => {
        const cards = items
          .map((item, index) => ({ item, index }))
          .filter(row => dateKey(row.item.time) === day)
          .map(row => `<div class="calendar-event ${esc(row.item.kind)} ${row.index === latest ? "current" : ""}"><div class="time">${esc(timeOnly(row.item.time))}</div><div class="title">${esc(row.item.title)}</div><div class="detail">${esc(row.item.detail)}</div></div>`)
          .join("");
        return `<div class="day"><div class="day-head"><strong>${esc(dayLabel(day))}</strong><span>${esc(day)}</span></div>${cards || `<div class="empty">No visible activity.</div>`}</div>`;
      }).join("")
    : `<div class="empty">No calendar activity yet.</div>`;
}

function render(state) {
  const scenario = state.scenario || {};
  const obs = state.observation || {};
  const evaluation = state.evaluation || {};
  $("title").textContent = scenario.name || obs.scenario_id || "PM Sim";
  $("subtitle").textContent = scenario.company || "";
  $("sim-time").textContent = pretty(obs.current_time);
  const demo = state.scripted_demo || {};
  const llm = state.llm_session || {};
  $("meter").textContent = llm.active
    ? `llm turn ${llm.turns ?? 0} · ${llm.steps ?? 0} tool step(s) · ${state.display_timeline.length} visible item(s)`
    : `step ${demo.index ?? 0} / ${demo.total ?? 0} · ${state.display_timeline.length} visible item(s)`;

  $("summary").innerHTML = [
    card("Evidence found", evaluation.evidence_count ?? 0),
    card("Outcome", label((evaluation.final_outcome || {}).outcome || "pending")),
    card("Status", evaluation.score === evaluation.max_score ? "passed" : "incomplete")
  ].join("");

  renderReplay(state.display_timeline || [], scenario);
  const logs = state.log_lines || [];
  $("playback").innerHTML = logs.length
    ? logs.map(line => `<div class="log-line">${esc(line)}</div>`).join("")
    : `<div class="empty">No log output yet.</div>`;
  $("playback").lastElementChild?.scrollIntoView({ block: "nearest" });

  $("projects").innerHTML = (obs.projects || []).map(project => row(project.name, `${project.stakeholder_pressure || ""} Deadline: ${pretty(project.deadline)}`, project.status)).join("") || `<div class="empty">No projects.</div>`;
  $("blockers").innerHTML = (obs.known_blockers || []).map(blocker => row(blocker.title, blocker.description || "", blocker.status)).join("") || `<div class="empty">No visible blockers.</div>`;
  $("schedule").innerHTML = (state.authored_schedule || []).map(item => `<div class="row scheduled"><strong>${esc(item.title)}</strong> <span class="badge">${esc(timeOnly(item.time))}</span><div>${esc(pretty(item.time))}</div><div>${esc(item.detail)}</div></div>`).join("") || `<div class="empty">No authored events.</div>`;
  $("evaluation").innerHTML = (evaluation.components || []).map(component => row(label(component.key), component.note || "", `${component.earned} / ${component.points}`)).join("") || `<div class="empty">No evaluation yet.</div>`;
  const tasks = (state.tasks || []).slice(0, 12);
  $("tasks").innerHTML = tasks.length ? `
    <table>
      <thead><tr><th>Task</th><th>Status</th><th>Owner</th><th>Priority</th><th>Due</th></tr></thead>
      <tbody>
        ${tasks.map(task => `<tr><td>${esc(task.title)}</td><td><span class="badge ${statusClass(task.status)}">${esc(label(task.status))}</span></td><td>${esc(task.owner_id || "unowned")}</td><td>${esc(label(task.priority || ""))}</td><td>${esc(pretty(task.due_at))}</td></tr>`).join("")}
      </tbody>
    </table>
  ` : `<div class="empty">No tasks.</div>`;
}

async function refresh() {
  render(await api("/api/state"));
}

async function step() {
  if (stepping) return;
  stepping = true;
  try {
    const state = await api("/api/advance-next", { method: "POST" });
    if (state.busy) return;
    render(state);
    if (state.done) pause();
  } finally {
    stepping = false;
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
</script>
</body>
</html>"""
