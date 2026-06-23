from __future__ import annotations

from datetime import datetime
from html import escape, unescape
from pathlib import Path
from typing import Any

from .actions import list_tasks
from .db import connect, rows_to_dicts
from .evaluator import evaluate
from .jsonutil import loads
from .paths import DEFAULT_DB_PATH, DEFAULT_SCENARIO_PATH
from .scenario import load_scenario
from .state import action_log, event_log, observe
from .timeline import timeline


DEFAULT_UI_PATH = Path("tmp/operator_ui.html")


def generate_report(
    db_path: Path | str = DEFAULT_DB_PATH,
    scenario_path: Path | str = DEFAULT_SCENARIO_PATH,
    output_path: Path | str = DEFAULT_UI_PATH,
    timeline_limit: int = 80,
) -> dict[str, Any]:
    output = Path(output_path)
    scenario = load_scenario(scenario_path)
    observation = observe(db_path)
    evaluation = evaluate(db_path, scenario_path)
    timeline_entries = timeline(db_path, limit=0)
    if timeline_limit > 0:
        timeline_entries = timeline_entries[-timeline_limit:]

    data = {
        "scenario": _scenario_summary(scenario),
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


def _scenario_summary(scenario: dict[str, Any]) -> dict[str, Any]:
    project_deadlines = [
        project.get("deadline")
        for project in scenario.get("projects", [])
        if project.get("deadline")
    ]
    return {
        "id": scenario.get("id"),
        "name": scenario.get("name") or scenario.get("id"),
        "company": scenario.get("company", ""),
        "start_time": scenario.get("start_time"),
        "timezone": scenario.get("timezone"),
        "project_deadlines": project_deadlines,
    }


def _render_html(data: dict[str, Any]) -> str:
    scenario = data["scenario"]
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
            f"<title>{_h(scenario.get('name') or 'PM Sim Operator UI')}</title>",
            "<style>",
            _css(),
            "</style>",
            "</head>",
            "<body>",
            '<div class="app-shell">',
            '<main class="content">',
            _top_nav(),
            '<header class="hero">',
            '<div class="hero-copy">',
            '<p class="eyebrow">PM Sim Operator UI</p>',
            f"<h1>{_h(scenario.get('name') or observation.get('scenario_id', 'scenario'))}</h1>",
            f"<p>{_h(scenario.get('company', ''))}</p>",
            '<p class="muted">Static snapshot of the current SQLite state. Use <span class="mono">pm-sim ui</span> for a fresh live run.</p>',
            f"<p>Simulated time: <strong>{_h(observation.get('current_time'))}</strong></p>",
            "</div>",
            f'<div class="hero-score">{_h(score)}<span>score</span></div>',
            "</header>",
            _summary_cards(evaluation, final_outcome),
            _section("Calendar Playback", _calendar_playback(data.get("timeline", []), scenario)),
            '<div id="overview" class="dashboard-grid">',
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
            _section("Debug Logs", _debug_logs(data)),
            "<script>",
            _script(data.get("timeline", [])),
            "</script>",
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
.app-shell { min-height: 100vh; }
.top-nav {
  position: sticky;
  top: 0;
  z-index: 20;
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 18px;
  margin-bottom: 18px;
  padding: 12px 14px;
  background: rgba(255, 255, 255, 0.94);
  border: 1px solid var(--line);
  border-radius: 12px;
  box-shadow: var(--shadow);
  backdrop-filter: blur(10px);
}
.brand strong { display: block; font-size: 16px; }
.brand span { color: var(--muted); font-size: 12px; }
.nav-links { display: flex; flex-wrap: wrap; gap: 8px; }
.nav-links a {
  color: var(--ink);
  text-decoration: none;
  border: 1px solid var(--line);
  border-radius: 999px;
  padding: 7px 11px;
  font-size: 13px;
  font-weight: 700;
  background: #f8fafc;
}
.nav-links a:hover { background: #e8f1fb; border-color: #bdd3ed; color: var(--blue); }
.content { max-width: 1280px; width: 100%; margin: 0 auto; padding: 28px; }
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
.replay-grid {
  display: grid;
  grid-template-columns: 1fr;
  gap: 14px;
  padding: 14px;
  align-items: start;
}
.week-grid {
  display: grid;
  gap: 12px;
  overflow-x: auto;
  padding-bottom: 4px;
}
.day-column {
  min-height: 180px;
  background: #f8fafc;
  border: 1px solid var(--line);
  border-radius: 9px;
  overflow: visible;
}
.day-header {
  padding: 10px 12px;
  border-bottom: 1px solid var(--line);
  background: #ffffff;
}
.day-header strong { display: block; }
.day-header span { color: var(--muted); font-size: 12px; }
.calendar-card {
  margin: 8px;
  padding: 9px;
  border: 1px solid var(--line);
  border-left: 4px solid var(--blue);
  border-radius: 8px;
  background: #ffffff;
}
.calendar-card.visible { display: block; }
.calendar-card:not(.visible) { display: none; }
.calendar-card.current {
  outline: 2px solid rgba(37, 92, 153, .24);
  background: #f2f7ff;
}
.calendar-card.event { border-left-color: var(--purple); }
.calendar-card.milestone { border-left-color: var(--good); }
.calendar-card.message { border-left-color: var(--warn); }
.calendar-time { color: var(--muted); font-size: 11px; font-weight: 800; }
.calendar-title { font-weight: 800; margin-top: 2px; }
.calendar-detail { color: var(--muted); margin-top: 3px; font-size: 12px; }
.playback-panel { display: grid; gap: 8px; }
.playback-controls {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  justify-content: center;
  padding: 14px 14px 0;
}
.playback-button {
  border: 1px solid var(--line-strong);
  border-radius: 8px;
  background: #ffffff;
  color: var(--ink);
  padding: 8px 12px;
  font-weight: 800;
  cursor: pointer;
}
.playback-button.primary { background: var(--blue); border-color: var(--blue); color: #ffffff; }
.playback-meter { color: var(--muted); font-size: 13px; font-weight: 700; }
.playback-track {
  display: grid;
  gap: 8px;
  max-height: 360px;
  overflow: auto;
  padding-right: 6px;
}
.playback-item {
  display: none;
  border: 1px solid var(--line);
  border-left: 4px solid var(--blue);
  border-radius: 8px;
  background: #ffffff;
  padding: 10px;
}
.playback-item.visible { display: block; }
.playback-item.event { border-left-color: var(--purple); }
.playback-item.current {
  outline: 2px solid rgba(37, 92, 153, .24);
  background: #f2f7ff;
}
.playback-item .meta { color: var(--muted); font-size: 12px; font-weight: 800; }
.playback-item .title { font-weight: 850; margin-top: 2px; }
.playback-item .detail { color: var(--muted); margin-top: 3px; }
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
  .content { padding: 18px; }
  .top-nav { display: block; }
  .nav-links { margin-top: 10px; }
  .hero { display: block; }
  .hero-score { text-align: left; margin-top: 14px; }
  .replay-grid { grid-template-columns: 1fr; }
  .week-grid { grid-template-columns: 1fr; }
}
""".strip()


def _top_nav() -> str:
    links = [
        ("overview", "Overview"),
        ("evaluation", "Evaluation"),
        ("playback", "Playback"),
        ("timeline", "Timeline"),
        ("tasks", "Tasks"),
        ("recent-messages", "Messages"),
        ("visible-docs", "Docs"),
        ("debug-logs", "Debug Logs"),
    ]
    nav = "".join(f'<a href="#{section_id}">{_h(label)}</a>' for section_id, label in links)
    return (
        '<nav class="top-nav">'
        '<div class="brand"><strong>PM Sim</strong><span>operator ui</span></div>'
        f'<div class="nav-links">{nav}</div>'
        "</nav>"
    )


def _summary_cards(evaluation: dict[str, Any], final_outcome: dict[str, Any]) -> str:
    status = _score_status(evaluation)
    outcome_title = _human_outcome(final_outcome)
    return (
        '<div class="stat-grid">'
        + _card("Score", f"{evaluation.get('score')} / {evaluation.get('max_score')}", "good" if status == "passed" else "warn")
        + _card("Milestones", str(evaluation.get("milestone_count", 0)))
        + _card("Outcome", outcome_title)
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
                _human_label(component.get("key")),
                f"{component.get('earned')} / {component.get('points')}",
                _badge(component.get("status"), _tone(component.get("status"))),
                component.get("note"),
            ]
        )
    return _table(["Component", "Score", "Status", "Note"], rows, raw_columns={2})


def _calendar_playback(entries: list[dict[str, Any]], scenario: dict[str, Any]) -> str:
    playback_entries = _playback_entries(entries)
    if not playback_entries:
        return '<p class="empty">No visible activity</p>'
    return (
        '<div class="playback-controls">'
        '<button class="playback-button primary" type="button" data-playback="play">Play</button>'
        '<button class="playback-button" type="button" data-playback="pause">Pause</button>'
        '<button class="playback-button" type="button" data-playback="reset">Reset</button>'
        f'<span class="playback-meter" id="playback-meter">0 / {len(playback_entries)}</span>'
        "</div>"
        '<div class="replay-grid">'
        + _week_calendar(entries, scenario)
        + _playback(playback_entries)
        + "</div>"
    )


def _playback(playback_entries: list[dict[str, str]]) -> str:
    items = []
    for index, entry in enumerate(playback_entries):
        items.append(
            f'<div class="playback-item {_h(entry["kind"])}" data-step="{index}">'
            f'<div class="meta">{_h(entry["time_label"])}</div>'
            f'<div class="title">{_h(entry["title"])}</div>'
            f'<div class="detail">{_h(entry["detail"])}</div>'
            "</div>"
        )
    return (
        '<div class="playback-panel">'
        '<div class="playback-track" id="playback-track">'
        + "".join(items)
        + "</div>"
        "</div>"
    )


def _week_calendar(entries: list[dict[str, Any]], scenario: dict[str, Any]) -> str:
    days = _timeline_days(entries, scenario)
    grouped = {day: [] for day in days}
    for index, entry in enumerate(_playback_entries(entries)):
        day = str(entry.get("time", ""))[:10]
        if day in grouped:
            grouped[day].append(_calendar_card(entry, index))

    columns = []
    for day in days:
        label = _day_label(day)
        cards = "".join(grouped[day]) or '<p class="empty">No visible activity</p>'
        columns.append(
            '<div class="day-column">'
            f'<div class="day-header"><strong>{label}</strong><span>{_h(day)}</span></div>'
            f"{cards}"
            "</div>"
        )
    style = f' style="grid-template-columns: repeat({len(days)}, minmax(150px, 1fr));"'
    return f'<div class="week-grid"{style}>' + "".join(columns) + "</div>"


def _timeline_days(entries: list[dict[str, Any]], scenario: dict[str, Any]) -> list[str]:
    values = [
        scenario.get("start_time"),
        *scenario.get("project_deadlines", []),
        *(entry.get("time") for entry in entries),
    ]
    parsed = [_parse_date(value) for value in values if value]
    if not parsed:
        return []
    start = min(parsed)
    end = max(parsed)
    day_count = (end - start).days + 1
    return [(start + _day_delta(offset)).isoformat() for offset in range(day_count)]


def _day_delta(days: int):
    from datetime import timedelta

    return timedelta(days=days)


def _parse_date(value: Any):
    return datetime.fromisoformat(str(value)[:19]).date()


def _day_label(day: str) -> str:
    try:
        return datetime.fromisoformat(day).strftime("%a")
    except ValueError:
        return day


def _calendar_card(entry: dict[str, str], index: int) -> str:
    return (
        f'<div class="calendar-card {_h(entry["kind"])}" data-step="{index}">'
        f'<div class="calendar-time">{_h(_time_only(entry.get("time")))}</div>'
        f'<div class="calendar-title">{_h(entry["title"])}</div>'
        f'<div class="calendar-detail">{_h(entry["detail"])}</div>'
        "</div>"
    )


def _playback_entries(entries: list[dict[str, Any]]) -> list[dict[str, str]]:
    rows = []
    for entry in entries:
        display = _display_timeline_entry(entry)
        if display is None:
            continue
        rows.append(
            {
                **display,
                "time": str(entry.get("time") or ""),
                "time_label": f"{str(entry.get('time', ''))[:10]} {_time_only(entry.get('time'))}".strip(),
            }
        )
    return rows


def _display_timeline_entry(entry: dict[str, Any]) -> dict[str, str] | None:
    kind = entry.get("kind")
    if kind not in {"action", "event_delivered"}:
        return None
    if kind == "action" and entry.get("action_type") in {"advance_time", "reset", "finalize_to_deadline"}:
        return None
    if kind == "event_delivered" and entry.get("event_type") == "coworker_reply":
        return None

    title = _calendar_title(entry)
    if not title:
        return None
    card_kind = "event" if kind == "event_delivered" else str(kind or "action")
    return {
        "kind": card_kind,
        "title": unescape(title),
        "detail": unescape(_calendar_detail(entry)),
    }


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
    if kind == "milestone":
        return "Milestone recorded"
    if kind == "message":
        return f"{_person_name(entry.get('sender_id'))} sent {entry.get('channel')}"
    return _human_label(entry.get("title"))


def _calendar_detail(entry: dict[str, Any]) -> str:
    kind = entry.get("kind")
    if kind == "action":
        payload = entry.get("payload", {})
        action_type = entry.get("action_type")
        if action_type in {"send_chat", "send_email"}:
            return _h(payload.get("subject") or "Message sent")
        if action_type == "read_doc":
            return _h(_doc_name(payload.get("doc_id")))
        if action_type == "update_doc":
            return _h(_doc_name(payload.get("doc_id")))
        if action_type == "update_task":
            return _h(_task_name(payload.get("task_id")))
        return _h(_human_label(action_type))
    if kind == "event_delivered":
        return _h(_human_event_detail(entry))
    if kind == "milestone":
        return _h(f"{_human_label(entry.get('milestone_id'))}: {entry.get('note', '')}")
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
        "<details><summary>Timeline</summary>"
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


def _script(entries: list[dict[str, Any]]) -> str:
    if not _playback_entries(entries):
        return ""
    return """
(() => {
  const items = Array.from(document.querySelectorAll(".playback-item"));
  const meter = document.getElementById("playback-meter");
  let index = 0;
  let timer = null;

  function render() {
    items.forEach((item, itemIndex) => {
      item.classList.toggle("visible", itemIndex < index);
      item.classList.toggle("current", itemIndex === index - 1);
    });
    document.querySelectorAll(".calendar-card").forEach((item) => {
      const itemIndex = Number(item.dataset.step);
      item.classList.toggle("visible", itemIndex < index);
      item.classList.toggle("current", itemIndex === index - 1);
    });
    if (meter) meter.textContent = `${index} / ${items.length}`;
    const latest = items[Math.max(0, index - 1)];
    if (latest) latest.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  function pause() {
    if (timer) window.clearInterval(timer);
    timer = null;
  }

  function play() {
    pause();
    timer = window.setInterval(() => {
      if (index >= items.length) {
        pause();
        return;
      }
      index += 1;
      render();
    }, 650);
  }

  document.querySelector('[data-playback="play"]')?.addEventListener("click", play);
  document.querySelector('[data-playback="pause"]')?.addEventListener("click", pause);
  document.querySelector('[data-playback="reset"]')?.addEventListener("click", () => {
    pause();
    index = 0;
    render();
  });

  render();
})();
""".strip()


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
    return _human_label(outcome or "Pending")


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
    }
    return mapping.get(str(event_type), _human_label(event_type))


def _human_event_detail(entry: dict[str, Any]) -> str:
    descriptions = {
        "coworker_reply": "A coworker response became visible.",
        "meeting_occurs": "The meeting ended and a transcript was created.",
        "project_deadline": "Deadline settled",
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
    for prefix in ("doc_", "task_", "fact_", "blocker_", "project_", "event_"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    return text.replace("_", " ").replace("-", " ").strip().capitalize()


def _doc_name(value: Any) -> str:
    return _human_label(value or "document")


def _task_name(value: Any) -> str:
    return _human_label(value or "task")


def _person_name(value: Any) -> str:
    return _human_label(value)


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
