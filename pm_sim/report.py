from __future__ import annotations

from html import escape
from pathlib import Path
from typing import Any

from .actions import list_tasks
from .db import connect, rows_to_dicts
from .evaluator import evaluate
from .jsonutil import loads
from .paths import DEFAULT_DB_PATH, DEFAULT_SCENARIO_PATH
from .state import action_log, event_log, observe
from .timeline import timeline


DEFAULT_REPORT_PATH = Path("tmp/operator_report.html")


def generate_report(
    db_path: Path | str = DEFAULT_DB_PATH,
    scenario_path: Path | str = DEFAULT_SCENARIO_PATH,
    output_path: Path | str = DEFAULT_REPORT_PATH,
    timeline_limit: int = 80,
) -> dict[str, Any]:
    output = Path(output_path)
    observation = observe(db_path)
    evaluation = evaluate(db_path, scenario_path)
    timeline_entries = timeline(db_path, limit=0)
    if timeline_limit > 0:
        timeline_entries = timeline_entries[-timeline_limit:]

    data = {
        "observation": observation,
        "evaluation": evaluation,
        "tasks": list_tasks(db_path),
        "docs": _visible_docs(db_path),
        "timeline": timeline_entries,
        "actions": action_log(db_path, limit=40),
        "events": event_log(db_path, limit=40),
    }

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_render_html(data), encoding="utf-8")

    return {
        "ok": True,
        "path": str(output),
        "current_time": observation.get("current_time"),
        "score": evaluation.get("score"),
        "max_score": evaluation.get("max_score"),
        "timeline_entries": len(timeline_entries),
        "action_log_entries": len(data["actions"]),
        "event_log_entries": len(data["events"]),
    }


def _visible_docs(db_path: Path | str) -> list[dict[str, Any]]:
    conn = connect(db_path)
    try:
        rows = rows_to_dicts(
            conn.execute(
                """
                SELECT id, title, kind, updated_at
                FROM docs
                WHERE visible_at IS NOT NULL
                ORDER BY updated_at DESC, id
                """
            ).fetchall()
        )
        return rows
    finally:
        conn.close()


def _render_html(data: dict[str, Any]) -> str:
    observation = data["observation"]
    evaluation = data["evaluation"]
    final_outcome = evaluation.get("final_outcome") or {}
    score = f"{evaluation.get('score')} / {evaluation.get('max_score')}"

    return "\n".join(
        [
            "<!doctype html>",
            '<html lang="en">',
            "<head>",
            '<meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            "<title>PM Sim Operator Report</title>",
            "<style>",
            _css(),
            "</style>",
            "</head>",
            "<body>",
            '<div class="app-shell">',
            _sidebar(),
            '<main class="content">',
            '<header class="hero">',
            '<div class="hero-copy">',
            '<p class="eyebrow">PM Sim Operator Report</p>',
            f"<h1>{_h(observation.get('scenario_id', 'scenario'))}</h1>",
            f"<p>Simulated time: <strong>{_h(observation.get('current_time'))}</strong></p>",
            "</div>",
            f'<div class="hero-score">{_h(score)}<span>score</span></div>',
            "</header>",
            _summary_cards(evaluation, final_outcome),
            '<div class="dashboard-grid">',
            _section("Projects", _projects(observation.get("projects", []))),
            _section("Calendar Obligations", _obligations(observation.get("calendar_obligations", []))),
            "</div>",
            '<div class="dashboard-grid">',
            _section("Known Blockers", _blockers(observation.get("known_blockers", []))),
            _section("Evaluation", _evaluation(evaluation)),
            "</div>",
            _section("Tasks", _tasks(data.get("tasks", []))),
            _section("Recent Messages", _messages(observation.get("recent_messages", []))),
            _section("Visible Docs", _docs(data.get("docs", []))),
            _section("Timeline", _timeline(data.get("timeline", []))),
            '<div class="dashboard-grid">',
            _section("Action Log", _actions(data.get("actions", []))),
            _section("Event Queue", _events(data.get("events", []))),
            "</div>",
            "</main>",
            "</div>",
            "</body>",
            "</html>",
        ]
    )


def _css() -> str:
    return """
:root {
  color-scheme: light;
  --bg: #f4f6f8;
  --panel: #ffffff;
  --ink: #17202a;
  --muted: #637083;
  --line: #d9dee7;
  --line-strong: #c6ceda;
  --good: #0f7b4f;
  --warn: #9a5b00;
  --bad: #aa2f2f;
  --blue: #255c99;
  --nav: #111827;
  --nav-muted: #aeb7c6;
  --shadow: 0 16px 40px rgba(24, 35, 52, 0.08);
}
* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--ink);
  font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
.app-shell { display: grid; grid-template-columns: 240px minmax(0, 1fr); min-height: 100vh; }
.sidebar {
  position: sticky;
  top: 0;
  align-self: start;
  height: 100vh;
  padding: 24px 18px;
  background: var(--nav);
  color: #ffffff;
}
.brand { margin-bottom: 28px; }
.brand strong { display: block; font-size: 18px; }
.brand span { color: var(--nav-muted); font-size: 12px; }
.nav a {
  display: block;
  color: var(--nav-muted);
  text-decoration: none;
  border-radius: 7px;
  padding: 8px 10px;
  margin: 3px 0;
}
.nav a:hover { background: rgba(255, 255, 255, 0.08); color: #ffffff; }
.content { max-width: 1280px; width: 100%; padding: 28px; }
.hero {
  display: flex;
  justify-content: space-between;
  gap: 20px;
  align-items: flex-end;
  margin-bottom: 18px;
  padding: 24px;
  background: linear-gradient(135deg, #ffffff 0%, #eef5ff 100%);
  border: 1px solid var(--line);
  border-radius: 12px;
  box-shadow: var(--shadow);
}
h1 { margin: 0 0 6px; font-size: 32px; letter-spacing: 0; }
h2 { margin: 0; font-size: 17px; letter-spacing: 0; }
p { margin: 0 0 8px; }
.eyebrow { color: var(--blue); text-transform: uppercase; font-size: 12px; font-weight: 700; letter-spacing: .08em; }
.hero-score {
  min-width: 150px;
  text-align: right;
  font-size: 30px;
  font-weight: 800;
}
.hero-score span {
  display: block;
  color: var(--muted);
  font-size: 12px;
  font-weight: 600;
  text-transform: uppercase;
}
.stat-grid {
  display: grid;
  gap: 12px;
  grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
  margin-bottom: 16px;
}
.dashboard-grid {
  display: grid;
  gap: 14px;
  grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
}
.stat-card, section {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 10px;
  box-shadow: var(--shadow);
}
.stat-card {
  padding: 16px;
  border-left: 4px solid var(--blue);
}
.stat-card.good { border-left-color: var(--good); }
.stat-card.warn { border-left-color: var(--warn); }
.stat-card .label { color: var(--muted); font-size: 12px; font-weight: 700; text-transform: uppercase; }
.stat-card .value { font-size: 21px; font-weight: 800; margin-top: 5px; word-break: break-word; }
section { padding: 0; margin: 14px 0; overflow: hidden; }
.section-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 14px 16px;
  border-bottom: 1px solid var(--line);
  background: #fbfcfe;
}
.section-kicker { color: var(--muted); font-size: 12px; }
.section-body { padding: 0; }
.table-wrap { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; min-width: 680px; }
th, td { border-top: 1px solid var(--line); padding: 10px 11px; text-align: left; vertical-align: top; }
thead th { border-top: 0; background: #f8fafc; position: sticky; top: 0; z-index: 1; }
th { color: var(--muted); font-size: 12px; font-weight: 700; text-transform: uppercase; }
tbody tr:hover { background: #f9fbff; }
.muted { color: var(--muted); }
.badge, .pill {
  display: inline-block;
  border-radius: 999px;
  padding: 3px 9px;
  font-size: 12px;
  font-weight: 700;
  white-space: nowrap;
  background: #eef2f7;
  color: #3d4b5f;
}
.badge.good { background: #e7f6ef; color: var(--good); }
.badge.warn { background: #fff3d6; color: var(--warn); }
.badge.bad { background: #fde8e8; color: var(--bad); }
.badge.blue { background: #e8f1fb; color: var(--blue); }
.passed { color: var(--good); font-weight: 700; }
.partial { color: var(--warn); font-weight: 700; }
.missing, .failed { color: var(--bad); font-weight: 700; }
.kind { color: var(--blue); font-weight: 700; }
.mono { font-family: ui-monospace, "SFMono-Regular", Menlo, Consolas, monospace; font-size: 12px; }
.empty { color: var(--muted); font-style: italic; padding: 16px; }
@media (max-width: 860px) {
  .app-shell { grid-template-columns: 1fr; }
  .sidebar { position: static; height: auto; }
  .nav { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 4px; }
  .content { padding: 18px; }
  .hero { display: block; }
  .hero-score { text-align: left; margin-top: 14px; }
}
""".strip()


def _sidebar() -> str:
    links = [
        ("projects", "Projects"),
        ("calendar-obligations", "Calendar"),
        ("known-blockers", "Blockers"),
        ("evaluation", "Evaluation"),
        ("tasks", "Tasks"),
        ("recent-messages", "Messages"),
        ("visible-docs", "Docs"),
        ("timeline", "Timeline"),
        ("action-log", "Action Log"),
        ("event-queue", "Event Queue"),
    ]
    nav = "".join(f'<a href="#{section_id}">{_h(label)}</a>' for section_id, label in links)
    return (
        '<aside class="sidebar">'
        '<div class="brand"><strong>PM Sim</strong><span>operator view</span></div>'
        f'<nav class="nav">{nav}</nav>'
        "</aside>"
    )


def _summary_cards(evaluation: dict[str, Any], final_outcome: dict[str, Any]) -> str:
    status = _score_status(evaluation)
    return (
        '<div class="stat-grid">'
        + _card("Score", f"{evaluation.get('score')} / {evaluation.get('max_score')}", "good" if status == "passed" else "warn")
        + _card("Evidence Rows", str(evaluation.get("evidence_count", 0)))
        + _card("Outcome", final_outcome.get("outcome") or "pending")
        + _card("Status", status, "good" if status == "passed" else "warn")
        + "</div>"
    )


def _score_status(evaluation: dict[str, Any]) -> str:
    if evaluation.get("score") == evaluation.get("max_score"):
        return "passed"
    return "incomplete"


def _card(label: str, value: str, tone: str = "") -> str:
    class_name = f"stat-card {tone}".strip()
    return (
        f'<div class="{class_name}">'
        f'<div class="label">{_h(label)}</div>'
        f'<div class="value">{_h(value)}</div>'
        "</div>"
    )


def _section(title: str, body: str) -> str:
    section_id = _section_id(title)
    return (
        f'<section id="{section_id}">'
        '<div class="section-header">'
        f"<h2>{_h(title)}</h2>"
        '<span class="section-kicker">live state</span>'
        "</div>"
        f'<div class="section-body">{body}</div>'
        "</section>"
    )


def _projects(projects: list[dict[str, Any]]) -> str:
    rows = []
    for project in projects:
        metadata = loads(project.get("metadata_json"), {})
        rows.append(
            [
                project.get("name"),
                _badge(project.get("status"), _tone(project.get("status"))),
                _badge(project.get("risk_level"), _tone(project.get("risk_level"))),
                project.get("deadline"),
                project.get("stakeholder_pressure"),
                metadata.get("final_outcome") or metadata.get("decision") or "",
            ]
        )
    return _table(
        ["Project", "Status", "Risk", "Deadline", "Pressure", "Decision/Outcome"],
        rows,
        raw_columns={1, 2},
    )


def _obligations(obligations: list[dict[str, Any]]) -> str:
    rows = [
        [
            item.get("start_at"),
            item.get("title"),
            _badge(item.get("kind"), "blue"),
            _badge(item.get("status", "scheduled"), _tone(item.get("status", "scheduled"))),
        ]
        for item in obligations
    ]
    return _table(["Time", "Obligation", "Kind", "Status"], rows, raw_columns={2, 3})


def _blockers(blockers: list[dict[str, Any]]) -> str:
    rows = [
        [
            item.get("title"),
            _badge(item.get("severity"), _tone(item.get("severity"))),
            _badge(item.get("status"), _tone(item.get("status"))),
            item.get("description"),
        ]
        for item in blockers
    ]
    return _table(["Blocker", "Severity", "Status", "Description"], rows, raw_columns={1, 2})


def _evaluation(evaluation: dict[str, Any]) -> str:
    rows = []
    for component in evaluation.get("components", []):
        rows.append(
            [
                component.get("key"),
                f"{component.get('earned')} / {component.get('points')}",
                _badge(component.get("status"), _tone(component.get("status"))),
                component.get("note"),
            ]
        )
    return _table(["Component", "Score", "Status", "Note"], rows, raw_columns={2})


def _tasks(tasks: list[dict[str, Any]]) -> str:
    rows = [
        [
            item.get("id"),
            item.get("title"),
            _badge(item.get("status"), _tone(item.get("status"))),
            _badge(item.get("priority"), _tone(item.get("priority"))),
            item.get("owner_id") or "",
            item.get("due_at") or "",
            item.get("blocked_by") or "",
        ]
        for item in tasks
    ]
    return _table(["ID", "Task", "Status", "Priority", "Owner", "Due", "Blocked By"], rows, raw_columns={2, 3})


def _messages(messages: list[dict[str, Any]]) -> str:
    rows = [
        [
            item.get("sent_at"),
            _badge(item.get("channel"), "blue"),
            f"{item.get('sender_id')} -> {item.get('recipient_id') or 'all'}",
            item.get("subject") or "",
            item.get("body") or "",
        ]
        for item in messages[:12]
    ]
    return _table(["Time", "Channel", "Route", "Subject", "Body"], rows, raw_columns={1})


def _docs(docs: list[dict[str, Any]]) -> str:
    rows = [
        [item.get("id"), item.get("title"), _badge(item.get("kind"), "blue"), item.get("updated_at")]
        for item in docs
    ]
    return _table(["ID", "Title", "Kind", "Updated"], rows, raw_columns={2})


def _timeline(entries: list[dict[str, Any]]) -> str:
    rows = [
        [
            item.get("time"),
            _badge(item.get("kind"), "blue"),
            item.get("title"),
            item.get("id"),
        ]
        for item in entries
    ]
    return _table(["Time", "Kind", "Title", "ID"], rows, raw_columns={1})


def _actions(actions: list[dict[str, Any]]) -> str:
    rows = [
        [item.get("created_at"), item.get("actor"), _badge(item.get("action_type"), "blue"), item.get("id")]
        for item in actions
    ]
    return _table(["Time", "Actor", "Action", "ID"], rows, raw_columns={2})


def _events(events: list[dict[str, Any]]) -> str:
    rows = [
        [
            item.get("scheduled_at"),
            item.get("event_type"),
            _badge(item.get("status"), _tone(item.get("status"))),
            item.get("delivered_at") or "",
            item.get("id"),
        ]
        for item in events
    ]
    return _table(["Scheduled", "Event", "Status", "Delivered", "ID"], rows, raw_columns={2})


def _table(headers: list[str], rows: list[list[Any]], raw_columns: set[int] | None = None) -> str:
    if not rows:
        return '<p class="empty">None</p>'
    raw_columns = raw_columns or set()
    head = "".join(f"<th>{_h(header)}</th>" for header in headers)
    body_rows = []
    for row in rows:
        cells = []
        for index, value in enumerate(row):
            text = "" if value is None else str(value)
            rendered = text if index in raw_columns else _h(text)
            cells.append(f"<td>{rendered}</td>")
        body_rows.append("<tr>" + "".join(cells) + "</tr>")
    return (
        '<div class="table-wrap"><table><thead><tr>'
        + head
        + "</tr></thead><tbody>"
        + "".join(body_rows)
        + "</tbody></table></div>"
    )


def _badge(value: Any, tone: str = "neutral") -> str:
    return f'<span class="badge {_h(tone)}">{_h(value)}</span>'


def _tone(value: Any) -> str:
    normalized = str(value or "").lower()
    if normalized in {"passed", "done", "complete", "completed", "resolved", "low", "delivered"}:
        return "good"
    if normalized in {"partial", "medium", "scheduled", "pending", "ready", "in_progress", "active"}:
        return "warn"
    if normalized in {"missing", "failed", "blocked", "open", "high", "critical", "at_risk"}:
        return "bad"
    return "neutral"


def _section_id(title: str) -> str:
    return title.lower().replace(" ", "-")


def _h(value: Any) -> str:
    return escape("" if value is None else str(value))
