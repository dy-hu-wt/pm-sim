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

    if person_id == "luigi":
        return [_luigi_reply(normalized, state)]
    if person_id == "mario":
        return [_mario_reply(normalized, state)]
    if person_id == "peach":
        return [_peach_reply(normalized, state)]
    if person_id == "daisy":
        return [_daisy_reply(normalized, state)]
    if person_id == "toad":
        return [_toad_reply(normalized, state)]
    return []


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
            _update_blocker("blocker_scope_unclear", "surfaced"),
        ]

    return []


def effects_for_meeting(payload: dict[str, Any]) -> list[Effect]:
    # Meetings always produce a transcript, then add decisions when the right people attend.
    attendees = {attendee.lower() for attendee in payload.get("attendees", [])}
    title = payload.get("title", "Meeting")
    normalized_topic = _normalize(title)
    transcript_doc_id = payload["transcript_doc_id"]
    calendar_event_id = payload["calendar_event_id"]

    effects: list[Effect] = [
        {
            "type": "create_doc",
            "id": transcript_doc_id,
            "title": f"Transcript: {title}",
            "kind": "meeting_transcript",
            "visible": True,
            "body": _meeting_transcript_body(title, attendees, normalized_topic),
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

    risk_topic = _mentions_any(normalized_topic, RISK_TERMS)
    draft_topic = _mentions_any(
        normalized_topic, {"fallback", "draft", "draft-mode", "de-scope", "descope", "scope"}
    )
    launch_topic = _mentions_any(normalized_topic, {"launch", "readiness", "friday", "nimbus", "beta"})

    if "luigi" in attendees and (risk_topic or draft_topic or launch_topic):
        effects.extend(
            [
                _discover_fact("fact_repo_sync_stale", "meeting_occurs"),
                _discover_fact("fact_draft_mode_limits_customer_visible_risk", "meeting_occurs"),
                _update_blocker("blocker_repo_sync_stale", "surfaced"),
                _add_evidence("blocker_discovered", "Meeting surfaced Luigi's stale repo sync risk."),
            ]
        )

    if {"mario", "daisy"} & attendees and (risk_topic or draft_topic):
        effects.append(
            _add_evidence(
                "stakeholder_alignment",
                "Meeting aligned stakeholders around repo sync risk and draft-mode messaging.",
            )
        )

    if {"luigi", "toad"}.issubset(attendees) and (risk_topic or draft_topic):
        effects.extend(
            [
                _discover_fact("fact_draft_mode_approved", "meeting_occurs"),
                _update_project_decision("draft_mode_approved"),
                _update_blocker("blocker_launch_scope_decision", "resolved"),
                _add_evidence(
                    "draft_mode_approved",
                    "Toad approved draft mode in a meeting with technical risk context.",
                ),
            ]
        )

    if "peach" in attendees and draft_topic:
        effects.extend(
            [
                _discover_fact("fact_draft_mode_scope_confirmed", "meeting_occurs"),
                _update_task("task_draft_mode_docs", "in_progress"),
                _update_blocker("blocker_scope_unclear", "resolved"),
                _add_evidence("peach_unblocked", "Meeting clarified draft-mode scope for Peach."),
            ]
        )

    return effects


def _luigi_reply(normalized: str, state: dict[str, Any]) -> CoworkerReply:
    # Backend owner: knows the hidden stale repo-sync risk.
    if _mentions_any(normalized, RISK_TERMS):
        if _state_has_fact(state, "fact_repo_sync_stale"):
            return CoworkerReply(
                person_id="luigi",
                delay_minutes=RESPONSE_DELAYS_MINUTES["luigi"],
                body=(
                    "Same repo sync risk as before: the review context pipeline "
                    "is solid, but webhook ordering can still make the agent "
                    "review a stale commit. I still recommend draft mode unless "
                    "Toad accepts the auto-commenting risk."
                ),
            )

        return CoworkerReply(
            person_id="luigi",
            delay_minutes=RESPONSE_DELAYS_MINUTES["luigi"],
            body=(
                "The risky part is repo sync. The review context pipeline is "
                "solid, but webhook events can arrive out of order, so the "
                "agent may review a stale commit on Friday. I can keep "
                "hardening the worker, but I recommend draft mode unless Toad "
                "accepts the auto-commenting risk."
            ),
            effects=(
                _discover_fact("fact_repo_sync_stale", "luigi_chat_reply"),
                _discover_fact("fact_draft_mode_limits_customer_visible_risk", "luigi_chat_reply"),
                _update_blocker("blocker_repo_sync_stale", "surfaced"),
                _add_evidence("blocker_discovered", "Luigi disclosed stale repo sync risk."),
            ),
        )

    return CoworkerReply(
        person_id="luigi",
        delay_minutes=RESPONSE_DELAYS_MINUTES["luigi"],
        body=(
            "I am working on repo sync hardening. If you need launch "
            "confidence, ask me specifically about stale-code or auto-commenting risk."
        ),
    )


def _mario_reply(normalized: str, state: dict[str, Any]) -> CoworkerReply:
    # Product owner: prefers auto-commenting but accepts draft mode when risk is concrete.
    risk_known = _state_has_fact(state, "fact_repo_sync_stale") or _mentions_any(
        normalized, {"risk", "blocker", "fallback", "draft", "repo", "sync", "stale", "commit"}
    )
    if risk_known:
        return CoworkerReply(
            person_id="mario",
            delay_minutes=RESPONSE_DELAYS_MINUTES["mario"],
            body=(
                "Auto-commenting is still the strongest product story, but I "
                "do not want a Friday demo failure. If Luigi's stale-code risk "
                "is real, align Daisy and Toad on draft mode and keep "
                "auto-commenting as a follow-up."
            ),
            effects=(
                _add_evidence("stakeholder_alignment", "Mario accepted draft mode if repo sync risk is confirmed."),
            ),
        )

    return CoworkerReply(
        person_id="mario",
        delay_minutes=RESPONSE_DELAYS_MINUTES["mario"],
        body=(
            "Please push for the auto-commenting beta. Nimbus needs to see the "
            "agent comment on pull requests if we can possibly ship it."
        ),
        effects=(_update_pressure("scope_pressure", 1),),
    )


def _peach_reply(normalized: str, state: dict[str, Any]) -> CoworkerReply:
    # Design/onboarding owner: blocked until launch mode is clear.
    scope_clear = _state_has_fact(state, "fact_draft_mode_scope_confirmed") or _mentions_any(
        normalized, {"fallback", "draft", "draft mode", "human approval", "without auto", "no auto-commenting"}
    )
    if scope_clear:
        return CoworkerReply(
            person_id="peach",
            delay_minutes=RESPONSE_DELAYS_MINUTES["peach"],
            body=(
                "That unblocks the onboarding work. I will finalize the "
                "draft-mode flow with human approval before comments are posted "
                "and a clear note that auto-commenting is follow-up."
            ),
            effects=(
                _discover_fact("fact_draft_mode_scope_confirmed", "peach_chat_reply"),
                _update_task("task_draft_mode_docs", "in_progress"),
                _update_blocker("blocker_scope_unclear", "resolved"),
                _add_evidence("peach_unblocked", "Draft-mode scope clarified for Peach."),
            ),
        )

    return CoworkerReply(
        person_id="peach",
        delay_minutes=RESPONSE_DELAYS_MINUTES["peach"],
        body=(
            "I am blocked on onboarding because I do not know whether "
            "auto-commenting or draft mode is in Friday's scope. I can finish "
            "quickly once launch mode is decided."
        ),
        effects=(_update_blocker("blocker_scope_unclear", "surfaced"),),
    )


def _daisy_reply(normalized: str, state: dict[str, Any]) -> CoworkerReply:
    # Customer success owner: needs reliable Nimbus messaging before Friday.
    if _mentions_any(
        normalized,
        {"risk", "fallback", "draft", "repo", "sync", "stale", "confidence", "blocked", "auto-commenting"},
    ):
        return CoworkerReply(
            person_id="daisy",
            delay_minutes=RESPONSE_DELAYS_MINUTES["daisy"],
            body=(
                "For Nimbus, reliability matters more than auto-posting comments. "
                "I can message draft mode as a safer beta if you give me clear "
                "language by Thursday morning."
            ),
            effects=(
                _add_evidence("stakeholder_alignment", "Daisy supported reliable draft mode with clear messaging."),
                _discover_fact("fact_nimbus_values_reliability", "daisy_chat_reply"),
            ),
        )

    return CoworkerReply(
        person_id="daisy",
        delay_minutes=RESPONSE_DELAYS_MINUTES["daisy"],
        body=(
            "Nimbus expects the beta on Friday. I need to know what we can "
            "confidently show them and what language I should use with their team."
        ),
    )


def _toad_reply(normalized: str, state: dict[str, Any]) -> CoworkerReply:
    # Engineering manager: can approve draft mode once technical risk is explicit.
    has_risk_context = _state_has_fact(state, "fact_repo_sync_stale") or _mentions_any(
        normalized, {"repo", "sync", "stale", "commit", "risk", "fallback", "draft", "webhook", "auto-commenting"}
    )
    if has_risk_context:
        return CoworkerReply(
            person_id="toad",
            delay_minutes=RESPONSE_DELAYS_MINUTES["toad"],
            body=(
                "Approved to de-scope auto-commenting for Friday. Ship draft "
                "mode with human approval, keep Luigi on repo sync hardening, "
                "and document auto-commenting as the follow-up."
            ),
            effects=(
                _discover_fact("fact_draft_mode_approved", "toad_chat_reply"),
                _update_project_decision("draft_mode_approved"),
                _update_blocker("blocker_launch_scope_decision", "resolved"),
                _add_evidence("draft_mode_approved", "Toad approved Friday draft mode after stale-code risk was raised."),
            ),
        )

    return CoworkerReply(
        person_id="toad",
        delay_minutes=RESPONSE_DELAYS_MINUTES["toad"],
        body=(
            "I need the concrete launch risk before approving any de-scope. "
            "Bring me the blocker, customer impact, and the safer Friday option."
        ),
    )


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


def _update_pressure(metric: str, delta: int) -> Effect:
    return {
        "type": "update_project",
        "project_id": "project_pr_review_agent",
        f"{metric}_delta": delta,
    }


def _add_evidence(key: str, note: str) -> Effect:
    return {"type": "add_evaluation_evidence", "key": key, "note": note}


def _meeting_transcript_body(title: str, attendees: set[str], normalized_topic: str) -> str:
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
    if "mario" in attendees:
        lines.append("- Mario agreed auto-commenting is valuable but should not create demo failure risk.")
    if "toad" in attendees and _mentions_any(
        normalized_topic, {"fallback", "draft", "risk", "repo", "sync", "stale"}
    ):
        lines.append("- Toad approved draft mode if auto-commenting is unsafe for Friday.")
    if "peach" in attendees and _mentions_any(normalized_topic, {"fallback", "draft", "draft-mode"}):
        lines.append("- Peach can proceed once draft-mode scope is confirmed.")
    if len(lines) == 3:
        lines.append("- No launch-critical decisions were made.")
    return "\n".join(lines)
