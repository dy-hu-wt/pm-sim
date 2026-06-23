from __future__ import annotations

from datetime import datetime
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
            _section("Week Timeline", _week_calendar(data.get("timeline", []))),
            _section("Tasks", _tasks(data.get("tasks", []))),
            _section("Recent Messages", _messages(observation.get("recent_messages", []))),
            _section("Visible Docs", _docs(data.get("docs", []))),
            _section("Debug Logs", _debug_logs(data)),
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
  --purple: #6f4bb2;
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
.week-grid {
  display: grid;
  grid-template-columns: repeat(5, minmax(210px, 1fr));
  gap: 12px;
  padding: 14px;
}
.day-column {
  min-height: 220px;
  background: #f8fafc;
  border: 1px solid var(--line);
  border-radius: 9px;
  overflow: hidden;
}
.day-header {
  padding: 10px 12px;
  border-bottom: 1px solid var(--line);
  background: #ffffff;
}
.day-header strong { display: block; }
.day-header span { color: var(--muted); font-size: 12px; }
.calendar-card {
  margin: 9px;
  padding: 10px;
  border: 1px solid var(--line);
  border-left: 4px solid var(--blue);
  border-radius: 8px;
  background: #ffffff;
}
.calendar-card.event { border-left-color: var(--purple); }
.calendar-card.evidence { border-left-color: var(--good); }
.calendar-card.message { border-left-color: var(--warn); }
.calendar-time { color: var(--muted); font-size: 12px; font-weight: 700; }
.calendar-title { font-weight: 800; margin-top: 3px; }
.calendar-detail { color: var(--muted); margin-top: 4px; font-size: 12px; }
.debug-stack { padding: 14px; display: grid; gap: 10px; }
details {
  border: 1px solid var(--line);
  border-radius: 8px;
  background: #ffffff;
  overflow: hidden;
}
summary {
  cursor: pointer;
  padding: 10px 12px;
  font-weight: 800;
  background: #f8fafc;
}
.outcome-summary {
  padding: 14px 16px 0;
  color: var(--muted);
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
.badge.purple { background: #f0eafd; color: var(--purple); }
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
  .week-grid { grid-template-columns: 1fr; }
}
""".strip()


def _sidebar() -> str:
    links = [
        ("projects", "Projects"),
        ("calendar-obligations", "Calendar"),
        ("known-blockers", "Blockers"),
        ("evaluation", "Evaluation"),
        ("week-timeline", "Week Timeline"),
        ("tasks", "Tasks"),
        ("recent-messages", "Messages"),
        ("visible-docs", "Docs"),
        ("debug-logs", "Debug Logs"),
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
    outcome_title = _human_outcome(final_outcome)
    return (
        '<div class="stat-grid">'
        + _card("Score", f"{evaluation.get('score')} / {evaluation.get('max_score')}", "good" if status == "passed" else "warn")
        + _card("Evidence Rows", str(evaluation.get("evidence_count", 0)))
        + _card("Friday Result", outcome_title)
        + _card("Status", status, "good" if status == "passed" else "warn")
        + "</div>"
        + _outcome_summary(final_outcome)
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
                _human_project_state(metadata),
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


def _week_calendar(entries: list[dict[str, Any]]) -> str:
    days = ["2026-06-22", "2026-06-23", "2026-06-24", "2026-06-25", "2026-06-26"]
    labels = ["Mon", "Tue", "Wed", "Thu", "Fri"]
    grouped = {day: [] for day in days}
    for entry in entries:
        card = _calendar_card(entry)
        if card is None:
            continue
        day = str(entry.get("time", ""))[:10]
        if day in grouped:
            grouped[day].append(card)

    columns = []
    for day, label in zip(days, labels):
        cards = "".join(grouped[day]) or '<p class="empty">No visible activity</p>'
        columns.append(
            '<div class="day-column">'
            f'<div class="day-header"><strong>{label}</strong><span>{_h(day)}</span></div>'
            f"{cards}"
            "</div>"
        )
    return '<div class="week-grid">' + "".join(columns) + "</div>"


def _calendar_card(entry: dict[str, Any]) -> str | None:
    kind = entry.get("kind")
    if kind not in {"action", "event_delivered"}:
        return None

    title = _calendar_title(entry)
    detail = _calendar_detail(entry)
    if not title:
        return None

    card_kind = "event" if kind == "event_delivered" else str(kind or "action")
    return (
        f'<div class="calendar-card {_h(card_kind)}">'
        f'<div class="calendar-time">{_h(_time_only(entry.get("time")))}</div>'
        f'<div class="calendar-title">{title}</div>'
        f'<div class="calendar-detail">{detail}</div>'
        "</div>"
    )


def _calendar_title(entry: dict[str, Any]) -> str:
    kind = entry.get("kind")
    if kind == "action":
        action_type = entry.get("action_type")
        payload = entry.get("payload", {})
        if action_type == "send_chat":
            return f"Chat to {_person_name(payload.get('person_id'))}"
        if action_type == "send_email":
            return f"Email to {_person_name(payload.get('person_id'))}"
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
        if action_type == "reset":
            return ""
        return _human_label(action_type)
    if kind == "event_delivered":
        return _human_event(entry.get("event_type"))
    if kind == "evidence":
        return "Evidence recorded"
    if kind == "message":
        return f"{_person_name(entry.get('sender_id'))} sent {entry.get('channel')}"
    return _human_label(entry.get("title"))


def _calendar_detail(entry: dict[str, Any]) -> str:
    kind = entry.get("kind")
    if kind == "action":
        payload = entry.get("payload", {})
        result = entry.get("result", {})
        action_type = entry.get("action_type")
        if action_type in {"send_chat", "send_email"}:
            text = payload.get("body") or payload.get("subject") or ""
            return _h(_short_text(text, 120))
        if action_type == "read_doc":
            return _h(_doc_name(payload.get("doc_id")))
        if action_type == "update_doc":
            return _h(_doc_name(payload.get("doc_id")))
        if action_type == "update_task":
            return _h(_task_name(payload.get("task_id")))
        if action_type == "advance_time":
            delivered = result.get("delivered_event_ids", [])
            if delivered:
                return _h(f"Delivered {len(delivered)} queued event(s)")
            return _h(str(payload.get("target") or "wait"))
        return _h(_human_label(action_type))
    if kind == "event_delivered":
        return _h(_human_event_detail(entry))
    if kind == "evidence":
        return _h(f"{_human_label(entry.get('evidence_key'))}: {entry.get('note', '')}")
    if kind == "message":
        return _h(_short_text(entry.get("body", ""), 120))
    return _h(entry.get("title", ""))


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


def _debug_logs(data: dict[str, Any]) -> str:
    timeline_html = _timeline(data.get("timeline", []))
    actions_html = _actions(data.get("actions", []))
    events_html = _events(data.get("events", []))
    return (
        '<div class="debug-stack">'
        "<details open><summary>Timeline</summary>"
        f"{timeline_html}"
        "</details>"
        "<details><summary>Action Log</summary>"
        f"{actions_html}"
        "</details>"
        "<details><summary>Event Queue</summary>"
        f"{events_html}"
        "</details>"
        "</div>"
    )


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
    return f'<span class="badge {_h(tone)}">{_h(_human_label(value))}</span>'


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


def _human_outcome(final_outcome: dict[str, Any]) -> str:
    outcome = final_outcome.get("outcome")
    mapping = {
        "draft_mode_beta_shipped": "Draft-mode beta shipped",
        "late_draft_mode": "Draft mode landed late",
        "risky_auto_commenting": "Risky auto-commenting shipped",
        "missed_due_to_blockers": "Launch blocked",
        "no_approved_friday_plan": "No approved Friday plan",
        "koopa_audit_update_ready": "Koopa update ready",
        "koopa_audit_scope_unresolved": "Koopa scope unresolved",
    }
    return mapping.get(str(outcome), _human_label(outcome or "Pending"))


def _outcome_summary(final_outcome: dict[str, Any]) -> str:
    summary = final_outcome.get("summary")
    if not summary:
        return ""
    return f'<div class="outcome-summary">{_h(summary)}</div>'


def _human_project_state(metadata: dict[str, Any]) -> str:
    if metadata.get("final_outcome"):
        return _human_outcome({"outcome": metadata.get("final_outcome")})
    decision = metadata.get("decision")
    if decision:
        return _human_label(decision)
    return ""


def _human_event(event_type: Any) -> str:
    mapping = {
        "coworker_reply": "Coworker replied",
        "meeting_occurs": "Meeting ended",
        "project_deadline": "Project deadline",
        "mario_auto_comment_push": "Mario pushed auto-commenting",
        "peach_design_blocked_escalation": "Peach escalated blocker",
        "daisy_confidence_check": "Daisy asked for confidence update",
        "daisy_private_repo_security_question": "Daisy asked security question",
        "nimbus_launch_mode_question": "Nimbus asked launch-mode question",
        "luigi_proactive_repo_risk": "Luigi resurfaced repo risk",
        "koopa_audit_export_request": "Koopa audit request arrived",
        "thursday_final_readiness_check": "Thursday readiness check",
    }
    return mapping.get(str(event_type), _human_label(event_type))


def _human_event_detail(entry: dict[str, Any]) -> str:
    descriptions = {
        "coworker_reply": "A coworker response became visible.",
        "meeting_occurs": "The meeting ended and a transcript was created.",
        "project_deadline": "A project deadline was reached and the outcome was settled.",
        "mario_auto_comment_push": "Mario pushed for the flashier auto-commenting launch path.",
        "peach_design_blocked_escalation": "Peach flagged that onboarding was still blocked by unclear launch scope.",
        "daisy_confidence_check": "Daisy asked whether the Nimbus beta was still on track.",
        "daisy_private_repo_security_question": "Daisy brought in a same-day customer security question.",
        "nimbus_launch_mode_question": "Nimbus asked for clear launch-mode wording.",
        "luigi_proactive_repo_risk": "Luigi resurfaced repo-sync risk before launch.",
        "koopa_audit_export_request": "Koopa's audit-log export request became visible.",
        "thursday_final_readiness_check": "Daisy asked for the final go/no-go before Friday.",
    }
    event_type = str(entry.get("event_type") or "")
    if event_type in descriptions:
        return descriptions[event_type]

    result = entry.get("result", {})
    if isinstance(result, dict):
        body = result.get("body") or result.get("summary")
        if body:
            return _short_text(body, 130)
        effects = result.get("applied_effects", [])
        if effects:
            return f"Applied {len(effects)} state change(s)"
    return _human_label(entry.get("event_type"))


def _human_label(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    return text.replace("_", " ").replace("-", " ").strip().capitalize()


def _doc_name(value: Any) -> str:
    names = {
        "doc_project_brief": "PR Review Agent beta brief",
        "doc_monday_standup": "Monday standup notes",
        "doc_launch_decision_record": "Friday launch decision record",
        "doc_beta_rollout_template": "Nimbus rollout template",
        "doc_private_repo_security_baseline": "Private repo security baseline",
        "doc_koopa_audit_export_note": "Koopa audit export note",
        "doc_friday_outcome": "Friday outcome",
        "doc_koopa_audit_export_outcome": "Koopa audit export outcome",
    }
    return names.get(str(value), _human_label(value or "document"))


def _task_name(value: Any) -> str:
    names = {
        "task_launch_decision": "Launch-mode decision",
        "task_draft_mode_docs": "Draft-mode onboarding",
        "task_customer_talk_track": "Customer talk track",
        "task_beta_rollout_notes": "Beta rollout notes",
        "task_repo_sync": "Repo sync",
        "task_review_context_pipeline": "Review context pipeline",
        "task_audit_export_feasibility": "Audit export feasibility",
        "task_audit_export_scope": "Audit export scope",
        "task_koopa_status_update": "Koopa status update",
    }
    return names.get(str(value), _human_label(value or "task"))


def _person_name(value: Any) -> str:
    names = {
        "agent": "Agent",
        "mario": "Mario",
        "luigi": "Luigi",
        "peach": "Peach",
        "daisy": "Daisy",
        "toad": "Toad",
    }
    return names.get(str(value), _human_label(value))


def _time_only(value: Any) -> str:
    try:
        return datetime.fromisoformat(str(value)).strftime("%I:%M %p").lstrip("0")
    except ValueError:
        return str(value or "")


def _short_text(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


def _h(value: Any) -> str:
    return escape("" if value is None else str(value))
