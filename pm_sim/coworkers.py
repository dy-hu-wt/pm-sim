from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from .conditions import all_conditions_match
from .jsonutil import loads


Effect = dict[str, Any]


@dataclass(frozen=True)
class CoworkerReply:
    person_id: str
    delay_minutes: int
    body: str
    effects: tuple[Effect, ...] = ()


def replies_for_chat(
    person_id: str, body: str, state: dict[str, Any] | None = None
) -> list[CoworkerReply]:
    person_id = person_id.lower()
    normalized = _normalize(body)
    state = state or {}
    structured_replies = _structured_replies_for_chat(person_id, normalized, state)
    return structured_replies[:1]


def _structured_replies_for_chat(
    person_id: str,
    normalized: str,
    state: dict[str, Any],
) -> list[CoworkerReply]:
    replies = []
    rules = sorted(
        state.get("coworker_rules", []),
        key=lambda rule: int(rule.get("priority", 0)),
        reverse=True,
    )
    for rule in rules:
        if rule.get("channel", "chat") != "chat":
            continue
        if rule.get("person_id", "").lower() != person_id:
            continue
        if not _rule_matches(rule.get("match", rule), normalized, state):
            continue

        reply = rule.get("reply", {})
        replies.append(
            CoworkerReply(
                person_id=person_id,
                delay_minutes=_reply_delay_minutes(person_id, reply, state),
                body=reply.get("body", ""),
                effects=tuple(dict(effect) for effect in rule.get("effects", [])),
            )
        )
    return replies


def _reply_delay_minutes(person_id: str, reply: dict[str, Any], state: dict[str, Any]) -> int:
    if "delay_minutes" in reply:
        return int(reply["delay_minutes"])
    response_delays = state.get("response_delays", {})
    if person_id in response_delays:
        return int(response_delays[person_id])
    raise ValueError(f"No response delay configured for coworker: {person_id}")


def _rule_matches(match: dict[str, Any], normalized: str, state: dict[str, Any]) -> bool:
    terms_any = {_normalize(term) for term in match.get("terms_any", [])}
    if terms_any and not _mentions_any(normalized, terms_any):
        return False

    terms_all = {_normalize(term) for term in match.get("terms_all", [])}
    if terms_all and not all(term in normalized for term in terms_all):
        return False

    for group in match.get("term_groups_all", []):
        terms = {_normalize(term) for term in group}
        if not terms or not _mentions_any(normalized, terms):
            return False

    discovered = set(state.get("discovered_facts", ()))
    required_facts = set(match.get("required_facts", []))
    if required_facts and not required_facts.issubset(discovered):
        return False

    required_facts_any = set(match.get("required_facts_any", []))
    if required_facts_any and not discovered.intersection(required_facts_any):
        return False

    absent_facts = set(match.get("absent_facts", []))
    if absent_facts and discovered.intersection(absent_facts):
        return False

    return True


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
    row = conn.execute("SELECT value FROM sim_state WHERE key = 'event_rules_json'").fetchone()
    return loads(row["value"], []) if row is not None else []


def effects_for_meeting(payload: dict[str, Any], state: dict[str, Any] | None = None) -> list[Effect]:
    attendees = {attendee.lower() for attendee in payload.get("attendees", [])}
    title = payload.get("title", "Meeting")
    normalized_topic = _normalize(title)
    state = state or {}
    transcript_doc_id = payload["transcript_doc_id"]
    calendar_event_id = payload["calendar_event_id"]
    meeting_rules = sorted(
        state.get("meeting_rules", []),
        key=lambda rule: int(rule.get("priority", 0)),
        reverse=True,
    )
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
            "visible": True,
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


def _normalize(body: str) -> str:
    return " ".join(body.lower().split())


def _mentions_any(body: str, terms: set[str] | frozenset[str]) -> bool:
    return any(term in body for term in terms)


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

    topic_terms_any = {_normalize(term) for term in rule.get("topic_terms_any", [])}
    if topic_terms_any and not _mentions_any(normalized_topic, topic_terms_any):
        return False

    topic_terms_all = {_normalize(term) for term in rule.get("topic_terms_all", [])}
    if topic_terms_all and not all(term in normalized_topic for term in topic_terms_all):
        return False

    facts = context["facts"]
    required_facts = set(rule.get("required_facts", []))
    if required_facts and not required_facts.issubset(facts):
        return False

    required_facts_any = set(rule.get("required_facts_any", []))
    if required_facts_any and not facts.intersection(required_facts_any):
        return False

    absent_facts = set(rule.get("absent_facts", []))
    if absent_facts and facts.intersection(absent_facts):
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
