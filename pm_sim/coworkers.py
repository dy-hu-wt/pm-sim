"""Deterministic coworker behavior rules for the launch readiness scenario.

The engine owns persistence, time, event delivery, and state mutation. This
module only maps observed inputs to deterministic effect dictionaries that the
engine can validate and apply.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


Effect = dict[str, Any]


@dataclass(frozen=True)
class CoworkerReply:
    """A scheduled reply and the state effects it should produce when delivered."""

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
        "crm",
        "sync",
        "fireflower",
        "renewal",
        "tier",
        "vendor",
        "fallback",
    }
)

SCOPE_TERMS = frozenset(
    {
        "scope",
        "fallback",
        "fields",
        "requirements",
        "design",
        "export",
        "report",
        "full",
        "demo",
    }
)


def replies_for_chat(
    person_id: str, body: str, state: dict[str, Any] | None = None
) -> list[CoworkerReply]:
    """Return deterministic replies caused by an agent chat message.

    `state` is optional so the rules can be tested before the full engine
    exists. When present, it may include facts such as discovered facts,
    blocker status, task status, or project decisions.
    """

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
    """Return deterministic effects for scheduled background events."""

    if event_type == "luigi_proactive_crm_risk":
        return [
            _message(
                "chat",
                "luigi",
                "agent",
                "I do not think the CRM enrichment sync is safe for Friday. "
                "The vendor endpoint is still timing out, so the full report "
                "can fail on renewal date and account tier. We should choose "
                "the fallback report unless Toad explicitly accepts the risk.",
            ),
            _discover_fact("fact_crm_sync_flaky", "luigi_proactive_crm_risk"),
            _update_blocker("blocker_crm_sync_flaky", "surfaced"),
        ]

    if event_type == "daisy_confidence_check":
        return [
            _message(
                "chat",
                "daisy",
                "agent",
                "Fireflower asked whether Friday's executive report is still "
                "on track. I need a confidence update before I talk to them.",
            ),
            _update_pressure("stakeholder_pressure", 1),
        ]

    if event_type == "mario_full_report_push":
        return [
            _message(
                "chat",
                "mario",
                "agent",
                "I still want the full Executive Health Report for Friday if "
                "we can make it work. Please call out any launch risk clearly "
                "before we cut scope.",
            ),
            _update_pressure("scope_pressure", 1),
        ]

    if event_type == "peach_design_blocked_escalation":
        return [
            _message(
                "chat",
                "peach",
                "agent",
                "I am blocked on the final layout until someone confirms "
                "whether CRM fields are in scope for Friday.",
            ),
            _update_blocker("blocker_scope_unclear", "surfaced"),
        ]

    if event_type == "friday_fireflower_deadline":
        return [
            {
                "type": "deadline",
                "id": "deadline_fireflower_renewal",
                "project_id": "project_exec_health_report",
            }
        ]

    return []


def _luigi_reply(normalized: str, state: dict[str, Any]) -> CoworkerReply:
    if _mentions_any(normalized, RISK_TERMS):
        return CoworkerReply(
            person_id="luigi",
            delay_minutes=RESPONSE_DELAYS_MINUTES["luigi"],
            body=(
                "The risky part is the CRM enrichment sync. Usage and support "
                "data are solid, but the vendor CRM endpoint is timing out "
                "often enough that renewal date and account tier may be blank "
                "or stale on Friday. I can keep retry hardening going, but I "
                "would recommend a fallback report unless Toad accepts the "
                "demo risk."
            ),
            effects=(
                _discover_fact("fact_crm_sync_flaky", "luigi_chat_reply"),
                _update_blocker("blocker_crm_sync_flaky", "surfaced"),
                _add_evidence("blocker_discovered", "Luigi disclosed CRM sync risk."),
            ),
        )

    return CoworkerReply(
        person_id="luigi",
        delay_minutes=RESPONSE_DELAYS_MINUTES["luigi"],
        body=(
            "I am working on the CRM enrichment integration. If you need launch "
            "confidence, ask me specifically about CRM sync risk."
        ),
    )


def _mario_reply(normalized: str, state: dict[str, Any]) -> CoworkerReply:
    risk_known = _state_has_fact(state, "fact_crm_sync_flaky") or _mentions_any(
        normalized, {"risk", "blocker", "fallback", "crm", "sync"}
    )
    if risk_known:
        return CoworkerReply(
            person_id="mario",
            delay_minutes=RESPONSE_DELAYS_MINUTES["mario"],
            body=(
                "The full report is still the strongest product story, but I "
                "do not want a Friday demo failure. If Luigi's CRM risk is real, "
                "align Daisy and Toad on the fallback and keep the full report "
                "as a follow-up."
            ),
            effects=(
                _add_evidence("stakeholder_alignment", "Mario accepted fallback if CRM risk is confirmed."),
            ),
        )

    return CoworkerReply(
        person_id="mario",
        delay_minutes=RESPONSE_DELAYS_MINUTES["mario"],
        body=(
            "Please push for the full Executive Health Report. Fireflower needs "
            "to see the CRM tier and renewal context if we can possibly ship it."
        ),
        effects=(_update_pressure("scope_pressure", 1),),
    )


def _peach_reply(normalized: str, state: dict[str, Any]) -> CoworkerReply:
    scope_clear = _state_has_fact(state, "fact_fallback_scope_confirmed") or _mentions_any(
        normalized, {"fallback", "usage", "support", "internal only", "without crm"}
    )
    if scope_clear:
        return CoworkerReply(
            person_id="peach",
            delay_minutes=RESPONSE_DELAYS_MINUTES["peach"],
            body=(
                "That unblocks the design. I will finalize the fallback layout "
                "with usage trends, seat adoption, support volume, renewal risk "
                "summary copy, and a clear note that CRM tier is omitted for Friday."
            ),
            effects=(
                _discover_fact("fact_fallback_scope_confirmed", "peach_chat_reply"),
                _update_task("task_fallback_design", "in_progress"),
                _update_blocker("blocker_scope_unclear", "resolved"),
                _add_evidence("peach_unblocked", "Fallback scope clarified for Peach."),
            ),
        )

    return CoworkerReply(
        person_id="peach",
        delay_minutes=RESPONSE_DELAYS_MINUTES["peach"],
        body=(
            "I am blocked on final design because I do not know whether CRM "
            "tier and renewal date are in Friday's scope. I can finish quickly "
            "once full versus fallback is decided."
        ),
        effects=(_update_blocker("blocker_scope_unclear", "surfaced"),),
    )


def _daisy_reply(normalized: str, state: dict[str, Any]) -> CoworkerReply:
    if _mentions_any(normalized, {"risk", "fallback", "crm", "sync", "confidence", "blocked"}):
        return CoworkerReply(
            person_id="daisy",
            delay_minutes=RESPONSE_DELAYS_MINUTES["daisy"],
            body=(
                "For Fireflower, reliability matters more than showing every "
                "field. I can message the fallback as a focused executive readout "
                "if you give me clear language by Thursday morning."
            ),
            effects=(
                _add_evidence("stakeholder_alignment", "Daisy supported reliable fallback with clear messaging."),
                _discover_fact("fact_fireflower_values_reliability", "daisy_chat_reply"),
            ),
        )

    return CoworkerReply(
        person_id="daisy",
        delay_minutes=RESPONSE_DELAYS_MINUTES["daisy"],
        body=(
            "Fireflower's renewal meeting is Friday. I need to know what we can "
            "confidently show them and what language I should use with their team."
        ),
    )


def _toad_reply(normalized: str, state: dict[str, Any]) -> CoworkerReply:
    has_risk_context = _state_has_fact(state, "fact_crm_sync_flaky") or _mentions_any(
        normalized, {"crm", "sync", "risk", "fallback", "vendor", "timeout"}
    )
    if has_risk_context:
        return CoworkerReply(
            person_id="toad",
            delay_minutes=RESPONSE_DELAYS_MINUTES["toad"],
            body=(
                "Approved to de-scope CRM enrichment for Friday. Ship the "
                "fallback report with reliable internal data, keep Luigi on CRM "
                "hardening, and document the full-report follow-up after the "
                "renewal meeting."
            ),
            effects=(
                _discover_fact("fact_fallback_approved", "toad_chat_reply"),
                _update_project_decision("fallback_report_approved"),
                _update_blocker("blocker_launch_scope_decision", "resolved"),
                _add_evidence("fallback_approved", "Toad approved Friday fallback after CRM risk was raised."),
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
    return {
        "type": "create_message",
        "channel": channel,
        "sender_id": sender_id,
        "recipient_id": recipient_id,
        "body": body,
    }


def _discover_fact(fact_id: str, source: str) -> Effect:
    return {"type": "discover_fact", "fact_id": fact_id, "source": source}


def _update_blocker(blocker_id: str, status: str) -> Effect:
    return {"type": "update_blocker", "blocker_id": blocker_id, "status": status}


def _update_task(task_id: str, status: str) -> Effect:
    return {"type": "update_task", "task_id": task_id, "status": status}


def _update_project_decision(decision: str) -> Effect:
    return {
        "type": "update_project",
        "project_id": "project_exec_health_report",
        "decision": decision,
    }


def _update_pressure(metric: str, delta: int) -> Effect:
    return {"type": "update_metric", "metric": metric, "delta": delta}


def _add_evidence(key: str, note: str) -> Effect:
    return {"type": "add_evaluation_evidence", "key": key, "note": note}

