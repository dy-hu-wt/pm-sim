from __future__ import annotations

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
    if command == "log":
        return _format_log(value)
    return str(value)


def _format_reset(value: dict[str, Any]) -> str:
    return "\n".join(
        [
            "Reset complete.",
            f"Scenario: {value.get('scenario_id')}",
            f"Current time: {value.get('current_time')}",
            f"Database: {value.get('db_path')}",
        ]
    )


def _format_observe(value: dict[str, Any]) -> str:
    lines = [
        f"Time: {value.get('current_time')}",
        f"Scenario: {value.get('scenario_id')}",
        "",
        "Projects",
    ]
    for project in value.get("projects", []):
        lines.append(
            f"- {project['name']} [{project['status']}, risk: {project['risk_level']}]"
        )
        if project.get("deadline"):
            lines.append(f"  Deadline: {project['deadline']}")
        if project.get("stakeholder_pressure"):
            lines.append(f"  Pressure: {project['stakeholder_pressure']}")

    lines.append("")
    lines.append("Known blockers")
    blockers = value.get("known_blockers", [])
    if blockers:
        for blocker in blockers:
            lines.append(f"- {blocker['title']} [{blocker['severity']}, {blocker['status']}]")
    else:
        lines.append("- None")

    lines.append("")
    lines.append("Recent messages")
    messages = value.get("recent_messages", [])
    if messages:
        for message in messages[:5]:
            subject = f" ({message['subject']})" if message.get("subject") else ""
            lines.append(
                f"- {message['sent_at']} {message['sender_id']} -> "
                f"{message.get('recipient_id') or 'all'}{subject}: {_short(message['body'])}"
            )
    else:
        lines.append("- None")

    lines.append("")
    lines.append("Next events")
    events = value.get("pending_events", [])
    if events:
        for event in events[:5]:
            lines.append(f"- {event['scheduled_at']} {event['event_type']} ({event['id']})")
    else:
        lines.append("- None")

    return "\n".join(lines)


def _format_tasks(tasks: list[dict[str, Any]]) -> str:
    if not tasks:
        return "No tasks."
    lines = ["Tasks"]
    for task in tasks:
        owner = task.get("owner_id") or "unowned"
        blocked = f", blocked by {task['blocked_by']}" if task.get("blocked_by") else ""
        lines.append(
            f"- {task['id']}: {task['title']} "
            f"[{task['status']}, {task['priority']}, owner: {owner}{blocked}]"
        )
    return "\n".join(lines)


def _format_doc(value: dict[str, Any]) -> str:
    if not value.get("ok"):
        return f"Error: {value.get('error')}"
    doc = value["doc"]
    return "\n".join(
        [
            f"{doc['title']} ({doc['kind']})",
            f"Updated: {doc['updated_at']}",
            "",
            doc["body"],
        ]
    )


def _format_action_result(value: dict[str, Any]) -> str:
    if not value.get("ok"):
        return f"Error: {value.get('error')}"

    lines = ["OK"]
    for key, item in value.items():
        if key == "ok":
            continue
        lines.append(f"{key}: {item}")
    return "\n".join(lines)


def _format_events(events: list[dict[str, Any]]) -> str:
    if not events:
        return "No events."
    lines = ["Events"]
    for event in events:
        delivered = event.get("delivered_at") or "not delivered"
        lines.append(
            f"- {event['scheduled_at']} {event['event_type']} "
            f"[{event['status']}, delivered: {delivered}] ({event['id']})"
        )
    return "\n".join(lines)


def _format_advance_time(value: dict[str, Any]) -> str:
    lines = [
        f"Advanced time: {value.get('from')} -> {value.get('to')}",
        "Delivered events:",
    ]
    delivered = value.get("delivered_events", [])
    if delivered:
        for event in delivered:
            lines.append(f"- {event['event_type']} ({event['id']})")
    else:
        lines.append("- None")
    return "\n".join(lines)


def _format_log(entries: list[dict[str, Any]]) -> str:
    if not entries:
        return "No log entries."
    lines = ["Action log"]
    for entry in entries:
        lines.append(
            f"- {entry['created_at']} {entry['actor']} {entry['action_type']} ({entry['id']})"
        )
    return "\n".join(lines)


def _short(value: str, limit: int = 110) -> str:
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return f"{text[: limit - 3]}..."
