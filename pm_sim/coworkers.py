from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from .engine.conditions import all_conditions_match
from .engine.runtime_config import event_rules
from .engine.rules import match_rule, match_text_and_facts, normalize_text, priority_sorted


Effect = dict[str, Any]


@dataclass(frozen=True)
class CoworkerReply:
    person_id: str
    delay_minutes: int
    body: str
    channel: str = "chat"
    subject: str | None = None
    effects: tuple[Effect, ...] = ()


def replies_for_chat(
    person_id: str,
    body: str,
    state: dict[str, Any] | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[CoworkerReply]:
    return replies_for_message(person_id, "chat", None, body, state, conn)


def replies_for_email(
    person_id: str,
    subject: str,
    body: str,
    state: dict[str, Any] | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[CoworkerReply]:
    return replies_for_message(person_id, "email", subject, body, state, conn)


def replies_for_message(
    person_id: str,
    channel: str,
    subject: str | None,
    body: str,
    state: dict[str, Any] | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[CoworkerReply]:
    person_id = person_id.lower()
    channel = channel.lower()
    normalized = normalize_text(f"{subject or ''} {body}")
    state = state or {}
    structured_replies = _structured_replies_for_channel(
        person_id,
        channel,
        normalized,
        state,
        conn,
    )
    return structured_replies[:1]


def _structured_replies_for_channel(
    person_id: str,
    channel: str,
    normalized: str,
    state: dict[str, Any],
    conn: sqlite3.Connection | None = None,
) -> list[CoworkerReply]:
    replies = []
    rules = priority_sorted(_reply_behaviors(state))
    for rule in rules:
        if rule.get("channel", "chat").lower() != channel:
            continue
        if rule.get("person_id", "").lower() != person_id:
            continue
        if not match_rule(
            rule,
            normalized_text=normalized,
            conn=conn,
            state=state,
        ).matches:
            continue

        reply = rule.get("reply", {})
        replies.append(
            CoworkerReply(
                person_id=person_id,
                delay_minutes=_reply_delay_minutes(person_id, reply, state),
                body=reply.get("body", ""),
                channel=channel,
                subject=reply.get("subject"),
                effects=tuple(dict(effect) for effect in rule.get("effects", [])),
            )
        )
    return replies


def _reply_behaviors(state: dict[str, Any]) -> list[dict[str, Any]]:
    actor_behaviors = state.get("actor_behaviors")
    if isinstance(actor_behaviors, list):
        return [
            behavior
            for behavior in actor_behaviors
            if isinstance(behavior, dict) and behavior.get("kind") == "reply"
        ]
    return []


def _reply_delay_minutes(person_id: str, reply: dict[str, Any], state: dict[str, Any]) -> int:
    if "delay_minutes" in reply:
        return int(reply["delay_minutes"])
    response_delays = state.get("response_delays", {})
    if person_id in response_delays:
        return int(response_delays[person_id])
    raise ValueError(f"No response delay configured for coworker: {person_id}")


def effects_for_event(
    conn: sqlite3.Connection,
    event_type: str,
    payload: dict[str, Any],
) -> list[Effect]:
    effects: list[Effect] = []
    for rule in _event_rules(conn):
        if rule.get("event_type") != event_type:
            continue
        if not all_conditions_match(
            conn,
            rule.get("when", []),
            project_id=payload.get("project_id"),
        ):
            continue
        effects.extend(dict(effect) for effect in rule.get("effects", []))
    return effects


def _event_rules(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return event_rules(conn)


def effects_for_meeting(payload: dict[str, Any], state: dict[str, Any] | None = None) -> list[Effect]:
    attendees = {attendee.lower() for attendee in payload.get("attendees", [])}
    title = payload.get("title", "Meeting")
    normalized_topic = normalize_text(title)
    state = state or {}
    transcript_doc_id = payload["transcript_doc_id"]
    calendar_event_id = payload["calendar_event_id"]
    meeting_rules = priority_sorted(state.get("meeting_rules", []))
    context = {
        "facts": set(state.get("discovered_facts", ())),
        "evidence_keys": set(state.get("evidence_keys", ())),
    }
    matched_rules = []
    rule_effects: list[Effect] = []
    transcript_lines: list[str] = []

    for rule in meeting_rules:
        if not _meeting_rule_matches(rule, attendees, normalized_topic, context):
            continue
        matched_rules.append(rule.get("id"))
        effects = [dict(effect) for effect in rule.get("effects", [])]
        rule_effects.extend(effects)
        transcript_lines.extend(rule.get("transcript_lines", []))
        _update_meeting_context(context, effects)

    effects: list[Effect] = [
        {
            "type": "create_doc",
            "id": transcript_doc_id,
            "title": f"Transcript: {title}",
            "kind": "meeting_transcript",
            "body": _meeting_transcript_body(title, attendees, transcript_lines),
            "metadata": {
                "calendar_event_id": calendar_event_id,
                "attendees": sorted(attendees),
                "matched_rules": matched_rules,
            },
        },
        {
            "type": "update_calendar_event",
            "calendar_event_id": calendar_event_id,
            "status": "completed",
            "transcript_doc_id": transcript_doc_id,
        },
        *rule_effects,
    ]
    return effects


def _meeting_rule_matches(
    rule: dict[str, Any],
    attendees: set[str],
    normalized_topic: str,
    context: dict[str, set[str]],
) -> bool:
    required_attendees = {attendee.lower() for attendee in rule.get("required_attendees", [])}
    if required_attendees and not required_attendees.issubset(attendees):
        return False

    attendees_any = {attendee.lower() for attendee in rule.get("attendees_any", [])}
    if attendees_any and not attendees.intersection(attendees_any):
        return False

    if not match_text_and_facts(rule.get("topic_match", {}), normalized_topic):
        return False

    facts = context["facts"]
    fact_match = {
        "required_facts": rule.get("required_facts", []),
        "required_facts_any": rule.get("required_facts_any", []),
        "absent_facts": rule.get("absent_facts", []),
    }
    if not match_text_and_facts(fact_match, "", state={"discovered_facts": facts}):
        return False

    evidence_keys = context["evidence_keys"]
    required_evidence = set(rule.get("required_evidence", []))
    if required_evidence and not required_evidence.issubset(evidence_keys):
        return False

    absent_evidence = set(rule.get("absent_evidence", []))
    if absent_evidence and evidence_keys.intersection(absent_evidence):
        return False

    return True


def _update_meeting_context(context: dict[str, set[str]], effects: list[Effect]) -> None:
    for effect in effects:
        if effect.get("type") == "discover_fact":
            context["facts"].add(effect["fact_id"])
        elif effect.get("type") == "add_evaluation_evidence":
            context["evidence_keys"].add(effect["key"])


def _meeting_transcript_body(
    title: str,
    attendees: set[str],
    transcript_lines: list[str],
) -> str:
    lines = [
        f"Meeting: {title}",
        f"Attendees: {', '.join(sorted(attendees))}",
        "Summary:",
    ]
    lines.extend(transcript_lines)
    if not transcript_lines:
        lines.append("- No launch-critical decisions were made.")
    return "\n".join(lines)
