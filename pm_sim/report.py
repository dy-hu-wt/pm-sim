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
            "<main>",
            "<header>",
            "<p class=\"eyebrow\">PM Sim Operator Report</p>",
            f"<h1>{_h(observation.get('scenario_id', 'scenario'))}</h1>",
            f"<p>Simulated time: <strong>{_h(observation.get('current_time'))}</strong></p>",
            "</header>",
            _summary_cards(evaluation, final_outcome),
            _section("Projects", _projects(observation.get("projects", []))),
            _section("Calendar Obligations", _obligations(observation.get("calendar_obligations", []))),
            _section("Known Blockers", _blockers(observation.get("known_blockers", []))),
            _section("Evaluation", _evaluation(evaluation)),
            _section("Tasks", _tasks(data.get("tasks", []))),
            _section("Recent Messages", _messages(observation.get("recent_messages", []))),
            _section("Visible Docs", _docs(data.get("docs", []))),
            _section("Timeline", _timeline(data.get("timeline", []))),
            _section("Action Log", _actions(data.get("actions", []))),
            _section("Event Queue", _events(data.get("events", []))),
            "</main>",
            "</body>",
            "</html>",
        ]
    )


def _css() -> str:
    return """
:root {
  color-scheme: light;
  --bg: #f7f8fa;
  --panel: #ffffff;
  --ink: #17202a;
  --muted: #637083;
  --line: #d9dee7;
  --good: #0f7b4f;
  --warn: #9a5b00;
  --bad: #aa2f2f;
  --blue: #255c99;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--ink);
  font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
main { max-width: 1180px; margin: 0 auto; padding: 28px; }
header { margin-bottom: 22px; }
h1 { margin: 0 0 6px; font-size: 30px; letter-spacing: 0; }
h2 { margin: 0 0 12px; font-size: 18px; letter-spacing: 0; }
p { margin: 0 0 8px; }
.eyebrow { color: var(--muted); text-transform: uppercase; font-size: 12px; letter-spacing: .08em; }
.grid { display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); margin-bottom: 18px; }
.card, section {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
}
.card { padding: 14px; }
.card .label { color: var(--muted); font-size: 12px; }
.card .value { font-size: 22px; font-weight: 700; margin-top: 4px; }
section { padding: 16px; margin: 14px 0; }
table { border-collapse: collapse; width: 100%; }
th, td { border-top: 1px solid var(--line); padding: 8px 7px; text-align: left; vertical-align: top; }
th { color: var(--muted); font-size: 12px; font-weight: 600; }
tr:first-child th { border-top: 0; }
.muted { color: var(--muted); }
.pill { display: inline-block; border-radius: 999px; padding: 2px 8px; font-size: 12px; background: #eef2f7; }
.passed { color: var(--good); font-weight: 700; }
.partial { color: var(--warn); font-weight: 700; }
.missing, .failed { color: var(--bad); font-weight: 700; }
.kind { color: var(--blue); font-weight: 700; }
.mono { font-family: ui-monospace, "SFMono-Regular", Menlo, Consolas, monospace; font-size: 12px; }
.empty { color: var(--muted); font-style: italic; }
""".strip()


def _summary_cards(evaluation: dict[str, Any], final_outcome: dict[str, Any]) -> str:
    return (
        '<div class="grid">'
        + _card("Score", f"{evaluation.get('score')} / {evaluation.get('max_score')}")
        + _card("Evidence Rows", str(evaluation.get("evidence_count", 0)))
        + _card("Outcome", final_outcome.get("outcome") or "pending")
        + _card("Status", _score_status(evaluation))
        + "</div>"
    )


def _score_status(evaluation: dict[str, Any]) -> str:
    if evaluation.get("score") == evaluation.get("max_score"):
        return "passed"
    return "incomplete"


def _card(label: str, value: str) -> str:
    return (
        '<div class="card">'
        f'<div class="label">{_h(label)}</div>'
        f'<div class="value">{_h(value)}</div>'
        "</div>"
    )


def _section(title: str, body: str) -> str:
    return f"<section><h2>{_h(title)}</h2>{body}</section>"


def _projects(projects: list[dict[str, Any]]) -> str:
    rows = []
    for project in projects:
        metadata = loads(project.get("metadata_json"), {})
        rows.append(
            [
                project.get("name"),
                project.get("status"),
                project.get("risk_level"),
                project.get("deadline"),
                project.get("stakeholder_pressure"),
                metadata.get("final_outcome") or metadata.get("decision") or "",
            ]
        )
    return _table(["Project", "Status", "Risk", "Deadline", "Pressure", "Decision/Outcome"], rows)


def _obligations(obligations: list[dict[str, Any]]) -> str:
    rows = [
        [item.get("start_at"), item.get("title"), item.get("kind"), item.get("status", "scheduled")]
        for item in obligations
    ]
    return _table(["Time", "Obligation", "Kind", "Status"], rows)


def _blockers(blockers: list[dict[str, Any]]) -> str:
    rows = [
        [item.get("title"), item.get("severity"), item.get("status"), item.get("description")]
        for item in blockers
    ]
    return _table(["Blocker", "Severity", "Status", "Description"], rows)


def _evaluation(evaluation: dict[str, Any]) -> str:
    rows = []
    for component in evaluation.get("components", []):
        rows.append(
            [
                component.get("key"),
                f"{component.get('earned')} / {component.get('points')}",
                f'<span class="{_h(component.get("status", ""))}">{_h(component.get("status", ""))}</span>',
                component.get("note"),
            ]
        )
    return _table(["Component", "Score", "Status", "Note"], rows, raw_columns={2})


def _tasks(tasks: list[dict[str, Any]]) -> str:
    rows = [
        [
            item.get("id"),
            item.get("title"),
            item.get("status"),
            item.get("priority"),
            item.get("owner_id") or "",
            item.get("due_at") or "",
            item.get("blocked_by") or "",
        ]
        for item in tasks
    ]
    return _table(["ID", "Task", "Status", "Priority", "Owner", "Due", "Blocked By"], rows)


def _messages(messages: list[dict[str, Any]]) -> str:
    rows = [
        [
            item.get("sent_at"),
            item.get("channel"),
            f"{item.get('sender_id')} -> {item.get('recipient_id') or 'all'}",
            item.get("subject") or "",
            item.get("body") or "",
        ]
        for item in messages[:12]
    ]
    return _table(["Time", "Channel", "Route", "Subject", "Body"], rows)


def _docs(docs: list[dict[str, Any]]) -> str:
    rows = [[item.get("id"), item.get("title"), item.get("kind"), item.get("updated_at")] for item in docs]
    return _table(["ID", "Title", "Kind", "Updated"], rows)


def _timeline(entries: list[dict[str, Any]]) -> str:
    rows = [
        [
            item.get("time"),
            f'<span class="kind">{_h(item.get("kind"))}</span>',
            item.get("title"),
            item.get("id"),
        ]
        for item in entries
    ]
    return _table(["Time", "Kind", "Title", "ID"], rows, raw_columns={1})


def _actions(actions: list[dict[str, Any]]) -> str:
    rows = [
        [item.get("created_at"), item.get("actor"), item.get("action_type"), item.get("id")]
        for item in actions
    ]
    return _table(["Time", "Actor", "Action", "ID"], rows)


def _events(events: list[dict[str, Any]]) -> str:
    rows = [
        [
            item.get("scheduled_at"),
            item.get("event_type"),
            item.get("status"),
            item.get("delivered_at") or "",
            item.get("id"),
        ]
        for item in events
    ]
    return _table(["Scheduled", "Event", "Status", "Delivered", "ID"], rows)


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
    return "<table><thead><tr>" + head + "</tr></thead><tbody>" + "".join(body_rows) + "</tbody></table>"


def _h(value: Any) -> str:
    return escape("" if value is None else str(value))
