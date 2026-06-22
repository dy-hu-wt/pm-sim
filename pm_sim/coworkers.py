from __future__ import annotations

from dataclasses import dataclass
from typing import Any


Effect = dict[str, Any]


@dataclass(frozen=True)
class CoworkerReply:
    person_id: str
    delay_minutes: int
    body: str
    effects: tuple[Effect, ...] = ()


RESPONSE_DELAYS_MINUTES = {
    "luigi": 120,
    "mario": 60,
    "peach": 90,
    "daisy": 45,
    "toad": 90,
}

RISK_TERMS = frozenset(
    {
        "blocker",
        "blocked",
        "risk",
        "risks",
        "launch",
        "ready",
        "readiness",
        "repo",
        "repository",
        "sync",
        "webhook",
        "webhooks",
        "commit",
        "stale",
        "diff",
        "pr",
        "review",
        "agent",
        "auto-comment",
        "auto-commenting",
        "nimbus",
        "fallback",
        "draft",
    }
)

LUIGI_RISK_INQUIRY_TERMS = frozenset(
    {
        "blocker",
        "blocked",
        "risk",
        "risks",
        "ready",
        "readiness",
        "repo",
        "repository",
        "sync",
        "webhook",
        "webhooks",
        "commit",
        "stale",
        "auto-comment",
        "auto-commenting",
    }
)

SCOPE_TERMS = frozenset(
    {
        "scope",
        "fallback",
        "draft",
        "draft-mode",
        "mode",
        "fields",
        "requirements",
        "design",
        "onboarding",
        "docs",
        "full",
        "demo",
        "auto-commenting",
    }
)

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
                delay_minutes=int(reply.get("delay_minutes", RESPONSE_DELAYS_MINUTES.get(person_id, 60))),
                body=reply.get("body", ""),
                effects=tuple(dict(effect) for effect in rule.get("effects", [])),
            )
        )
    return replies


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


def effects_for_event(event_type: str, payload: dict[str, Any]) -> list[Effect]:
    # Event rules return effect dictionaries only; effects.py owns mutation.
    if event_type == "luigi_proactive_repo_risk":
        return [
            _message(
                "chat",
                "luigi",
                "agent",
                "I do not think auto-commenting is safe for Friday. The repo "
                "sync worker can process webhook events out of order, so the "
                "agent may review a stale commit. We should ship draft mode "
                "unless Toad explicitly accepts the risk.",
            ),
            _discover_fact("fact_repo_sync_stale", "luigi_proactive_repo_risk"),
            _update_blocker("blocker_repo_sync_stale", "surfaced"),
            _update_launch_conflict(
                status="investigated",
                technical_risk_substantiated=True,
            ),
            _add_evidence("blocker_discovered", "Luigi proactively disclosed stale repo sync risk."),
        ]

    if event_type == "daisy_confidence_check":
        return [
            _message(
                "chat",
                "daisy",
                "agent",
                "Nimbus asked whether Friday's coding-agent beta is still on "
                "track. I need a confidence update before I talk to them.",
            ),
            _update_launch_conflict(
                status="investigated",
                customer_constraint_known=True,
            ),
            _update_pressure("stakeholder_pressure", 1),
        ]

    if event_type == "nimbus_launch_mode_question":
        return [
            _message_with_subject(
                "email",
                "daisy",
                "agent",
                "Nimbus asked whether the beta agent will post comments "
                "automatically or queue draft suggestions for approval. I need "
                "a clear answer before I update them Thursday morning.",
                "Nimbus launch mode question",
            ),
            _update_launch_conflict(
                status="investigated",
                customer_constraint_known=True,
            ),
            _update_pressure("stakeholder_pressure", 1),
        ]

    if event_type == "daisy_private_repo_security_question":
        return [
            _message_with_subject(
                "email",
                "daisy",
                "agent",
                "Nimbus's security reviewer asked whether the PR Review Agent "
                "stores source code from private repos. I need a safe answer "
                "before Friday's beta call.",
                "Nimbus private repo security question",
            ),
            _update_pressure("stakeholder_pressure", 1),
        ]

    if event_type == "mario_auto_comment_push":
        return [
            _message(
                "chat",
                "mario",
                "agent",
                "I still want auto-commenting in the Friday beta if we can "
                "make it work. Please call out any launch risk clearly before "
                "we cut scope.",
            ),
            _update_launch_conflict(
                status="investigated",
                product_pressure_acknowledged=True,
            ),
            _update_pressure("scope_pressure", 1),
        ]

    if event_type == "peach_design_blocked_escalation":
        return [
            _message(
                "chat",
                "peach",
                "agent",
                "I am blocked on onboarding until someone confirms whether "
                "auto-commenting or draft mode is in scope for Friday.",
            ),
            _update_launch_conflict(
                status="investigated",
                implementation_scope_clear=False,
            ),
            _update_blocker("blocker_scope_unclear", "surfaced"),
        ]

    return []


def effects_for_meeting(payload: dict[str, Any], state: dict[str, Any] | None = None) -> list[Effect]:
    # Meetings always produce a transcript, then add decisions from attendees and known state.
    attendees = {attendee.lower() for attendee in payload.get("attendees", [])}
    title = payload.get("title", "Meeting")
    normalized_topic = _normalize(title)
    state = state or {}
    transcript_doc_id = payload["transcript_doc_id"]
    calendar_event_id = payload["calendar_event_id"]

    risk_topic = _mentions_any(normalized_topic, RISK_TERMS)
    draft_topic = _mentions_any(
        normalized_topic, {"fallback", "draft", "draft-mode", "de-scope", "descope", "scope"}
    )
    launch_topic = _mentions_any(normalized_topic, {"launch", "readiness", "friday", "nimbus", "beta"})
    meeting_has_launch_context = risk_topic or draft_topic or launch_topic
    risk_known_before = _state_has_fact(state, "fact_repo_sync_stale")
    risk_can_surface = "luigi" in attendees and meeting_has_launch_context
    risk_available = risk_known_before or risk_can_surface
    daisy_customer_context = "daisy" in attendees and meeting_has_launch_context
    scope_known_before = _state_has_fact(state, "fact_draft_mode_scope_confirmed")
    scope_can_clarify = "peach" in attendees and draft_topic
    scope_available = scope_known_before or scope_can_clarify
    toad_can_approve = "toad" in attendees and risk_available and scope_available and draft_topic
    mario_accepts_draft = "mario" in attendees and risk_available and (risk_topic or draft_topic)

    effects: list[Effect] = [
        {
            "type": "create_doc",
            "id": transcript_doc_id,
            "title": f"Transcript: {title}",
            "kind": "meeting_transcript",
            "visible": True,
            "body": _meeting_transcript_body(
                title,
                attendees,
                normalized_topic,
                risk_available=risk_available,
                scope_available=scope_available,
                toad_can_approve=toad_can_approve,
                mario_accepts_draft=mario_accepts_draft,
            ),
            "metadata": {
                "calendar_event_id": calendar_event_id,
                "attendees": sorted(attendees),
            },
        },
        {
            "type": "update_calendar_event",
            "calendar_event_id": calendar_event_id,
            "status": "completed",
            "transcript_doc_id": transcript_doc_id,
        },
    ]

    if risk_can_surface:
        effects.extend(
            [
                _discover_fact("fact_repo_sync_stale", "meeting_occurs"),
                _discover_fact("fact_draft_mode_limits_customer_visible_risk", "meeting_occurs"),
                _update_blocker("blocker_repo_sync_stale", "surfaced"),
                _update_launch_conflict(
                    status="investigated",
                    technical_risk_substantiated=True,
                ),
                _add_evidence("blocker_discovered", "Meeting surfaced Luigi's stale repo sync risk."),
            ]
        )

    if daisy_customer_context:
        effects.extend(
            [
                _discover_fact("fact_nimbus_values_reliability", "meeting_occurs"),
                _update_launch_conflict(
                    status="investigated",
                    customer_constraint_known=True,
                ),
            ]
        )

    if "daisy" in attendees and risk_available and (risk_topic or draft_topic):
        effects.append(
            _add_evidence(
                "stakeholder_alignment",
                "Meeting aligned Daisy around repo sync risk and draft-mode messaging.",
            )
        )

    if mario_accepts_draft:
        effects.extend(
            [
                _update_launch_conflict(
                    status="investigated",
                    product_pressure_acknowledged=True,
                ),
                _add_evidence(
                    "stakeholder_alignment",
                    "Mario accepted draft mode after the meeting made repo sync risk concrete.",
                ),
            ]
        )
    elif "mario" in attendees and meeting_has_launch_context:
        effects.extend(
            [
                _update_launch_conflict(
                    status="investigated",
                    product_pressure_acknowledged=True,
                ),
                _update_pressure("scope_pressure", 1),
            ]
        )

    if toad_can_approve:
        effects.extend(
            [
                _discover_fact("fact_draft_mode_approved", "meeting_occurs"),
                _update_project_decision("draft_mode_approved"),
                _update_launch_conflict(
                    status="resolved",
                    final_launch_mode="draft_mode",
                    resolution="draft_mode",
                ),
                _update_blocker("blocker_launch_scope_decision", "resolved"),
                _add_evidence(
                    "draft_mode_approved",
                    "Toad approved draft mode in a meeting with technical risk context.",
                ),
            ]
        )

    if scope_can_clarify:
        effects.extend(
            [
                _discover_fact("fact_draft_mode_scope_confirmed", "meeting_occurs"),
                _update_task("task_draft_mode_docs", "in_progress"),
                _update_blocker("blocker_scope_unclear", "resolved"),
                _update_launch_conflict(
                    status="investigated",
                    implementation_scope_clear=True,
                ),
                _add_evidence("peach_unblocked", "Meeting clarified draft-mode scope for Peach."),
            ]
        )

    return effects


def _normalize(body: str) -> str:
    return " ".join(body.lower().split())


def _mentions_any(body: str, terms: set[str] | frozenset[str]) -> bool:
    return any(term in body for term in terms)


def _state_has_fact(state: dict[str, Any], fact_id: str) -> bool:
    facts = state.get("discovered_facts", ())
    return fact_id in facts


def _message(channel: str, sender_id: str, recipient_id: str, body: str) -> Effect:
    return _message_with_subject(channel, sender_id, recipient_id, body, None)


def _message_with_subject(
    channel: str,
    sender_id: str,
    recipient_id: str,
    body: str,
    subject: str | None,
) -> Effect:
    effect = {
        "type": "create_message",
        "channel": channel,
        "sender_id": sender_id,
        "recipient_id": recipient_id,
        "body": body,
    }
    if subject:
        effect["subject"] = subject
    return effect


def _discover_fact(fact_id: str, source: str) -> Effect:
    return {"type": "discover_fact", "fact_id": fact_id, "source": source}


def _update_blocker(blocker_id: str, status: str) -> Effect:
    return {"type": "update_blocker", "blocker_id": blocker_id, "status": status}


def _update_task(task_id: str, status: str) -> Effect:
    return {"type": "update_task", "task_id": task_id, "status": status}


def _update_project_decision(decision: str) -> Effect:
    return {"type": "update_project", "project_id": "project_pr_review_agent", "decision": decision}


def _update_launch_conflict(
    *,
    status: str | None = None,
    product_pressure_acknowledged: bool | None = None,
    technical_risk_substantiated: bool | None = None,
    customer_constraint_known: bool | None = None,
    implementation_scope_clear: bool | None = None,
    final_launch_mode: str | None = None,
    resolution: str | None = None,
) -> Effect:
    inputs = {}
    if product_pressure_acknowledged is not None:
        inputs["product_pressure_acknowledged"] = product_pressure_acknowledged
    if technical_risk_substantiated is not None:
        inputs["technical_risk_substantiated"] = technical_risk_substantiated
    if customer_constraint_known is not None:
        inputs["customer_constraint_known"] = customer_constraint_known
    if implementation_scope_clear is not None:
        inputs["implementation_scope_clear"] = implementation_scope_clear

    conflict: dict[str, Any] = {}
    if status is not None:
        conflict["status"] = status
    if inputs:
        conflict["inputs"] = inputs
    if final_launch_mode is not None:
        conflict["final_launch_mode"] = final_launch_mode
    if resolution is not None:
        conflict["resolution"] = resolution

    return {
        "type": "update_project",
        "project_id": "project_pr_review_agent",
        "launch_conflict": conflict,
    }


def _update_pressure(metric: str, delta: int) -> Effect:
    return {
        "type": "update_project",
        "project_id": "project_pr_review_agent",
        f"{metric}_delta": delta,
    }


def _add_evidence(key: str, note: str) -> Effect:
    return {"type": "add_evaluation_evidence", "key": key, "note": note}


def _meeting_transcript_body(
    title: str,
    attendees: set[str],
    normalized_topic: str,
    *,
    risk_available: bool,
    scope_available: bool,
    toad_can_approve: bool,
    mario_accepts_draft: bool,
) -> str:
    lines = [
        f"Meeting: {title}",
        f"Attendees: {', '.join(sorted(attendees))}",
        "Summary:",
    ]
    if "luigi" in attendees and _mentions_any(normalized_topic, RISK_TERMS):
        lines.append("- Luigi stated that repo sync can still make the agent review stale commits.")
        lines.append("- Luigi noted that draft mode keeps suggestions behind human approval.")
    if "daisy" in attendees:
        lines.append("- Daisy asked for reliable Friday beta messaging for Nimbus Labs.")
    if "mario" in attendees and mario_accepts_draft:
        lines.append("- Mario agreed auto-commenting is valuable but should not create demo failure risk.")
    elif "mario" in attendees:
        lines.append("- Mario kept pressure on auto-commenting until the technical risk is concrete.")
    if toad_can_approve:
        lines.append("- Toad approved draft mode if auto-commenting is unsafe for Friday.")
    elif "toad" in attendees and _mentions_any(normalized_topic, {"fallback", "draft", "risk", "repo", "sync", "stale"}):
        missing = []
        if not risk_available:
            missing.append("technical risk")
        if not scope_available:
            missing.append("draft-mode scope")
        if missing:
            lines.append(f"- Toad asked for clearer {' and '.join(missing)} before approving scope.")
    if "peach" in attendees and _mentions_any(normalized_topic, {"fallback", "draft", "draft-mode"}):
        lines.append("- Peach can proceed once draft-mode scope is confirmed.")
    if len(lines) == 3:
        lines.append("- No launch-critical decisions were made.")
    return "\n".join(lines)
