from __future__ import annotations

from datetime import datetime
from typing import Any


def format_output(command: str | None, value: Any) -> str:
    if command == "reset":
        return _format_reset(value)
    if command == "observe":
        return _format_observe(value)
    if command == "list-tasks":
        return _format_tasks(value)
    if command == "read-doc":
        return _format_doc(value)
    if command in {"send-chat", "send-email", "update-task", "schedule-meeting"}:
        return _format_action_result(value)
    if command == "events":
        return _format_events(value)
    if command == "advance-time":
        return _format_advance_time(value)
    if command == "evaluate":
        return _format_evaluate(value)
    if command == "log":
        return _format_log(value)
    return str(value)


def _format_reset(value: dict[str, Any]) -> str:
    return "\n".join(
        [
            "Reset complete",
            f"  Scenario: {value.get('scenario_id')}",
            f"  Time:     {_pretty_time(value.get('current_time'))}",
            f"  DB:       {value.get('db_path')}",
        ]
    )


def _format_observe(value: dict[str, Any]) -> str:
    lines = [
        "Simulation",
        f"  Time:     {_pretty_time(value.get('current_time'))}",
        f"  Scenario: {value.get('scenario_id')}",
        "",
        "Project",
    ]
    for project in value.get("projects", []):
        lines.append(f"  {project['name']}")
        lines.append(f"  Status:   {project['status']}")
        lines.append(f"  Risk:     {_severity(project['risk_level'])}")
        if project.get("deadline"):
            lines.append(f"  Deadline: {_pretty_time(project['deadline'])}")
        if project.get("stakeholder_pressure"):
            lines.append(f"  Pressure: {project['stakeholder_pressure']}")

    lines.append("")
    lines.append("Known Blockers")
    blockers = value.get("known_blockers", [])
    if blockers:
        for blocker in blockers:
            lines.append(
                f"  [{_severity(blocker['severity'])}] {blocker['title']} "
                f"({blocker['status']})"
            )
            if blocker.get("description"):
                lines.append(f"       {blocker['description']}")
    else:
        lines.append("  None")

    lines.append("")
    lines.append("Recent Messages")
    messages = value.get("recent_messages", [])
    if messages:
        for message in messages[:5]:
            subject = f" [{message['subject']}]" if message.get("subject") else ""
            lines.append(
                f"  {_pretty_time(message['sent_at'])}  "
                f"{message['sender_id']} -> {message.get('recipient_id') or 'all'}"
                f"  {message['channel']}{subject}"
            )
            lines.append(f"       {_short(message['body'], 130)}")
    else:
        lines.append("  None")

    lines.append("")
    lines.append("Next Events")
    events = value.get("pending_events", [])
    if events:
        for event in events[:5]:
            lines.append(
                f"  {_pretty_time(event['scheduled_at'])}  "
                f"{event['event_type']}  ({event['id']})"
            )
    else:
        lines.append("  None")

    return "\n".join(lines)


def _format_tasks(tasks: list[dict[str, Any]]) -> str:
    if not tasks:
        return "No tasks."
    lines = ["Tasks"]
    for task in tasks:
        owner = task.get("owner_id") or "unowned"
        blocked = f"Blocked by: {task['blocked_by']}" if task.get("blocked_by") else "Not blocked"
        lines.append(f"  {task['id']}")
        lines.append(f"    {task['title']}")
        lines.append(
            f"    Status: {task['status']} | Priority: {_severity(task['priority'])} | "
            f"Owner: {owner} | Due: {_pretty_time(task.get('due_at'))}"
        )
        lines.append(f"    {blocked}")
    return "\n".join(lines)


def _format_doc(value: dict[str, Any]) -> str:
    if not value.get("ok"):
        return f"Error: {value.get('error')}"
    doc = value["doc"]
    return "\n".join(
        [
            f"{doc['title']}",
            f"  Kind:    {doc['kind']}",
            f"  Updated: {_pretty_time(doc['updated_at'])}",
            "",
            doc["body"],
        ]
    )


def _format_action_result(value: dict[str, Any]) -> str:
    if not value.get("ok"):
        return f"Error: {value.get('error')}"

    if "scheduled_reply_ids" in value:
        lines = ["Chat sent"]
        lines.append(f"  Message ID: {value.get('message_id')}")
        replies = value.get("scheduled_reply_ids") or []
        if replies:
            lines.append("  Scheduled replies:")
            for reply_id in replies:
                lines.append(f"    {reply_id}")
        else:
            lines.append("  Scheduled replies: none")
        return "\n".join(lines)

    if "message_id" in value:
        return "\n".join(["Message sent", f"  Message ID: {value.get('message_id')}"])

    if "task_id" in value:
        return "\n".join(
            [
                "Task updated",
                f"  Task:     {value.get('task_id')}",
                f"  Status:   {value.get('status')}",
                f"  Priority: {value.get('priority')}",
            ]
        )

    if "meeting_id" in value:
        return "\n".join(["Meeting scheduled", f"  Meeting ID: {value.get('meeting_id')}"])

    lines = ["OK"]
    for key, item in value.items():
        if key != "ok":
            lines.append(f"  {key}: {item}")
    return "\n".join(lines)


def _format_events(events: list[dict[str, Any]]) -> str:
    if not events:
        return "No events."
    lines = ["Events"]
    for event in events:
        delivered = _pretty_time(event.get("delivered_at")) if event.get("delivered_at") else "not delivered"
        lines.append(
            f"  {_pretty_time(event['scheduled_at'])}  {event['event_type']}  "
            f"[{event['status']}]"
        )
        lines.append(f"    ID: {event['id']}")
        lines.append(f"    Delivered: {delivered}")
    return "\n".join(lines)


def _format_advance_time(value: dict[str, Any]) -> str:
    lines = [
        "Advanced Simulated Time",
        f"  From: {_pretty_time(value.get('from'))}",
        f"  To:   {_pretty_time(value.get('to'))}",
        "",
        "Delivered Events",
    ]
    delivered = value.get("delivered_events", [])
    if delivered:
        for event in delivered:
            lines.append(f"  {event['event_type']} ({event['id']})")
            effects = event.get("result", {}).get("applied_effects", [])
            if effects:
                lines.append("    Effects:")
                for effect in effects:
                    lines.append(f"      - {_format_effect(effect)}")
    else:
        lines.append("  None")
    return "\n".join(lines)


def _format_log(entries: list[dict[str, Any]]) -> str:
    if not entries:
        return "No log entries."
    lines = ["Action log"]
    for entry in entries:
        lines.append(
            f"  {_pretty_time(entry['created_at'])}  "
            f"{entry['actor']}  {entry['action_type']}  ({entry['id']})"
        )
    return "\n".join(lines)


def _format_evaluate(value: dict[str, Any]) -> str:
    if not value.get("ok"):
        return f"Error: {value.get('error')}"

    lines = [
        "Evaluation",
        f"  Scenario: {value.get('scenario_id')}",
        f"  Score:    {value.get('score')} / {value.get('max_score')}",
        f"  Evidence: {value.get('evidence_count')}",
        "",
        "Components",
    ]
    for component in value.get("components", []):
        lines.append(
            f"  {component['key']}: {component['earned']} / {component['points']} "
            f"[{component['status']}]"
        )
        lines.append(f"    {component.get('note')}")
        for evidence in component.get("evidence", []):
            lines.append(
                f"    - {evidence['key']} at {_pretty_time(evidence['created_at'])}: "
                f"{evidence['note']}"
            )
        for harm in component.get("detected_harms", []):
            lines.append(f"    - {harm}")

    baseline = value.get("baseline") or {}
    if baseline:
        lines.extend(
            [
                "",
                "Baseline",
                f"  {baseline.get('description')}",
                f"  {baseline.get('expected_outcome')}",
            ]
        )

    return "\n".join(lines)


def _format_effect(effect: dict[str, Any]) -> str:
    effect_type = effect.get("type")
    if effect_type == "create_message":
        return f"created message {effect.get('id')}"
    if effect_type == "create_doc":
        return f"created doc {effect.get('id')}"
    if effect_type == "update_calendar_event":
        return f"updated calendar event {effect.get('calendar_event_id')}"
    if effect_type == "discover_fact":
        return f"discovered fact {effect.get('fact_id')}"
    if effect_type == "update_blocker":
        return f"updated blocker {effect.get('blocker_id')} -> {effect.get('status')}"
    if effect_type == "update_task":
        return f"updated task {effect.get('task_id')}"
    if effect_type == "update_project":
        return f"updated project {effect.get('project_id')}"
    if effect_type == "add_evaluation_evidence":
        deduped = " (already recorded)" if effect.get("deduped") else ""
        return f"added evidence {effect.get('key')}{deduped}"
    if effect_type == "update_metric":
        return f"updated metric {effect.get('metric')} -> {effect.get('value')}"
    return str(effect)


def _pretty_time(value: str | None) -> str:
    if not value:
        return "n/a"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    return parsed.strftime("%a %Y-%m-%d %H:%M")


def _severity(value: str | None) -> str:
    if not value:
        return "UNKNOWN"
    return value.upper()


def _short(value: str, limit: int = 110) -> str:
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return f"{text[: limit - 3]}..."
