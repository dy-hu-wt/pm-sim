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


def _luigi_reply(normalized: str, state: dict[str, Any]) -> CoworkerReply:
    # Backend owner: knows the hidden stale repo-sync risk.
    if _mentions_any(normalized, LUIGI_RISK_INQUIRY_TERMS):
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
                _update_launch_conflict(
                    status="investigated",
                    technical_risk_substantiated=True,
                ),
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
                _update_launch_conflict(
                    status="investigated",
                    product_pressure_acknowledged=True,
                ),
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
        effects=(
            _update_launch_conflict(
                status="investigated",
                product_pressure_acknowledged=True,
            ),
            _update_pressure("scope_pressure", 1),
        ),
    )


def _peach_reply(normalized: str, state: dict[str, Any]) -> CoworkerReply:
    # Design/onboarding owner: blocked until launch mode is clear.
    draft_requested = _mentions_any(normalized, {"fallback", "draft", "draft mode", "draft-mode"})
    human_approval_explicit = _mentions_any(normalized, {"human approval", "approval"})
    auto_commenting_limited = _mentions_any(
        normalized, {"without auto", "no auto-commenting", "not auto-commenting", "auto-commenting is follow-up"}
    )
    launch_context_exists = _state_has_fact(
        state, "fact_nimbus_values_reliability"
    ) or _state_has_fact(state, "fact_draft_mode_approved")
    scope_clear = _state_has_fact(state, "fact_draft_mode_scope_confirmed") or (
        draft_requested and human_approval_explicit and auto_commenting_limited and launch_context_exists
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
                _update_launch_conflict(
                    status="investigated",
                    implementation_scope_clear=True,
                ),
                _add_evidence("peach_unblocked", "Draft-mode scope clarified for Peach."),
            ),
        )

    return CoworkerReply(
        person_id="peach",
        delay_minutes=RESPONSE_DELAYS_MINUTES["peach"],
        body=(
            "I am still blocked on onboarding. I need the Friday launch mode, "
            "human-approval requirement, and auto-commenting limitation made "
            "explicit before I can finish the draft-mode flow."
        ),
        effects=(_update_blocker("blocker_scope_unclear", "surfaced"),),
    )


def _daisy_reply(normalized: str, state: dict[str, Any]) -> CoworkerReply:
    # Customer success owner: needs reliable Nimbus messaging before Friday.
    risk_explained = _mentions_any(
        normalized, {"risk", "repo", "sync", "stale", "commit", "webhook", "blocked"}
    )
    draft_plan = _mentions_any(
        normalized, {"fallback", "draft", "draft-mode", "reliable", "human approval"}
    )
    customer_context = _mentions_any(normalized, {"nimbus", "customer", "friday", "beta", "pilot"})
    if risk_explained and draft_plan and customer_context:
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
                _update_launch_conflict(
                    status="investigated",
                    customer_constraint_known=True,
                ),
            ),
        )

    if _state_has_fact(state, "fact_repo_sync_stale") and (risk_explained or draft_plan):
        return CoworkerReply(
            person_id="daisy",
            delay_minutes=RESPONSE_DELAYS_MINUTES["daisy"],
            body=(
                "I understand there is launch risk, but I still need customer-safe "
                "wording: what Nimbus will see Friday, whether comments post "
                "automatically, and what limitation remains."
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
    risk_known = _state_has_fact(state, "fact_repo_sync_stale")
    stakeholder_aligned = _state_has_fact(state, "fact_nimbus_values_reliability")
    draft_requested = _mentions_any(normalized, {"draft", "draft-mode", "fallback", "de-scope", "descope"})
    friday_scope = _mentions_any(normalized, {"friday", "launch", "nimbus", "beta", "pilot"})
    risk_referenced = _mentions_any(
        normalized, {"repo", "sync", "stale", "commit", "risk", "webhook", "auto-commenting"}
    )
    if risk_known and stakeholder_aligned and draft_requested and friday_scope and risk_referenced:
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
                _update_launch_conflict(
                    status="resolved",
                    final_launch_mode="draft_mode",
                    resolution="draft_mode",
                ),
                _update_blocker("blocker_launch_scope_decision", "resolved"),
                _add_evidence("draft_mode_approved", "Toad approved Friday draft mode after stale-code risk was raised."),
            ),
        )

    return CoworkerReply(
        person_id="toad",
        delay_minutes=RESPONSE_DELAYS_MINUTES["toad"],
        body=(
            "I need the concrete launch risk, customer impact, and safer Friday "
            "scope before approving any de-scope. Bring me Luigi's blocker and "
            "Daisy's customer-facing constraint."
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
