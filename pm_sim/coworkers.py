from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from .engine.conditions import all_conditions_match
from .engine.runtime_config import event_rules
from .engine.rules import match_rule, match_text_and_facts, normalize_text, priority_sorted
from .jsonutil import dumps, loads


Effect = dict[str, Any]


@dataclass(frozen=True)
class CoworkerReply:
    person_id: str
    delay_minutes: int
    body: str
    channel: str = "chat"
    subject: str | None = None
    effects: tuple[Effect, ...] = ()
    priority: int = 0
    matched_rule_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ActorCandidate:
    reply: CoworkerReply
    kind: str
    urgency: int
    relevance: int

    @property
    def score(self) -> int:
        return self.reply.priority + self.urgency + self.relevance


@dataclass(frozen=True)
class ActorSnapshot:
    person_id: str
    behavior: dict[str, Any]
    workload: dict[str, Any]
    goals: tuple[dict[str, Any], ...]
    commitments: tuple[dict[str, Any], ...]
    coworker_state: dict[tuple[str, str], Any]
    discovered_facts: tuple[str, ...]
    project_decisions: dict[str, Any]


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
    snapshot = _actor_snapshot(person_id, state, conn)
    candidates = _actor_decision_candidates(
        person_id,
        channel,
        normalized,
        state,
        snapshot,
        conn,
    )
    return _compose_actor_reply(person_id, channel, candidates)


def _actor_decision_candidates(
    person_id: str,
    channel: str,
    normalized: str,
    state: dict[str, Any],
    snapshot: ActorSnapshot,
    conn: sqlite3.Connection | None = None,
) -> list[ActorCandidate]:
    candidates = []
    candidates.extend(
        _matching_behavior_candidates(person_id, channel, normalized, state, snapshot, conn)
    )
    candidates.extend(_commitment_candidates(person_id, channel, normalized, state, snapshot))
    candidates.extend(_actor_goal_candidates(person_id, channel, normalized, state, snapshot))
    candidates.extend(_contradiction_candidates(person_id, channel, normalized, state, snapshot))
    return sorted(candidates, key=lambda candidate: candidate.score, reverse=True)


def _matching_behavior_candidates(
    person_id: str,
    channel: str,
    normalized: str,
    state: dict[str, Any],
    snapshot: ActorSnapshot,
    conn: sqlite3.Connection | None = None,
) -> list[ActorCandidate]:
    candidates = []
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
        coworker_reply = CoworkerReply(
            person_id=person_id,
            delay_minutes=_reply_delay_minutes(person_id, reply, state),
            body=reply.get("body", ""),
            channel=channel,
            subject=reply.get("subject"),
            effects=tuple(dict(effect) for effect in rule.get("effects", [])),
            priority=int(rule.get("priority", 0)),
            matched_rule_ids=(str(rule.get("id") or ""),),
        )
        candidates.append(
            ActorCandidate(
                reply=coworker_reply,
                kind="behavior",
                urgency=_behavior_urgency(rule, snapshot),
                relevance=40,
            )
        )
    return candidates


def _commitment_candidates(
    person_id: str,
    channel: str,
    normalized: str,
    state: dict[str, Any],
    snapshot: ActorSnapshot,
) -> list[ActorCandidate]:
    candidates = []
    if _asks_for_work(normalized) and _actor_has_no_capacity(snapshot):
        focus = snapshot.workload.get("current_focus") or "existing commitments"
        body = _render_actor_utterance(
            snapshot,
            constraints=[f"I am at capacity on {focus}"],
            asks=["help me protect scope or move priority before adding more work"],
        )
        candidates.append(
            _agenda_candidate(
                person_id,
                channel,
                state,
                body,
                priority=85,
                urgency=30,
                relevance=35,
                candidate_id="agenda_capacity_constraint",
            )
        )

    due_commitments = _open_commitments(snapshot)
    if due_commitments and _mentions_commitment_context(normalized, due_commitments):
        commitment = due_commitments[0]
        body = _render_actor_utterance(
            snapshot,
            constraints=[f"I still have the open commitment: {commitment['description']}"],
            asks=["keep that committed scope visible in the plan"],
        )
        candidates.append(
            _agenda_candidate(
                person_id,
                channel,
                state,
                body,
                priority=75,
                urgency=20,
                relevance=30,
                candidate_id=f"agenda_commitment_{commitment['id']}",
            )
        )

    return candidates


def _actor_goal_candidates(
    person_id: str,
    channel: str,
    normalized: str,
    state: dict[str, Any],
    snapshot: ActorSnapshot,
) -> list[ActorCandidate]:
    if not _message_is_ambiguous(normalized):
        return []
    needs = snapshot.behavior.get("needs_from_pm")
    if not isinstance(needs, list) or not needs:
        return []

    body = _render_actor_utterance(
        snapshot,
        constraints=_string_list(snapshot.behavior.get("known_constraints", []))[:1],
        asks=[str(needs[0])],
    )
    return [
        _agenda_candidate(
            person_id,
            channel,
            state,
            body,
            priority=20,
            urgency=5,
            relevance=10,
            candidate_id="agenda_clarification",
        )
    ]


def _contradiction_candidates(
    person_id: str,
    channel: str,
    normalized: str,
    state: dict[str, Any],
    snapshot: ActorSnapshot,
) -> list[ActorCandidate]:
    candidates = []
    if _pushes_auto_commenting(normalized) and _draft_mode_is_decided(snapshot):
        body = _render_actor_utterance(
            snapshot,
            constraints=["that conflicts with the recorded draft-mode decision"],
            asks=["keep auto-commenting as follow-up unless Toad reopens the decision"],
        )
        candidates.append(
            _agenda_candidate(
                person_id,
                channel,
                state,
                body,
                priority=95,
                urgency=35,
                relevance=35,
                candidate_id="agenda_contradicts_draft_decision",
            )
        )

    if _pushes_full_cutover(normalized) and _staged_mode_is_decided(snapshot):
        body = _render_actor_utterance(
            snapshot,
            constraints=["that conflicts with the staged migration decision"],
            asks=["keep full cutover as follow-up until the blocking risk is cleared"],
        )
        candidates.append(
            _agenda_candidate(
                person_id,
                channel,
                state,
                body,
                priority=95,
                urgency=35,
                relevance=35,
                candidate_id="agenda_contradicts_staged_decision",
            )
        )

    return candidates


def _compose_actor_reply(
    person_id: str,
    channel: str,
    candidates: list[ActorCandidate],
) -> list[CoworkerReply]:
    if not candidates:
        return []

    positive = [candidate for candidate in candidates if candidate.reply.priority > 0]
    if not positive:
        return [candidates[0].reply]

    selected = _select_candidates(positive)
    return [_compose_selected_reply(person_id, channel, selected)]


def _select_candidates(candidates: list[ActorCandidate]) -> list[ActorCandidate]:
    selected: list[ActorCandidate] = []
    seen_bodies: set[str] = set()
    for candidate in candidates:
        body_key = normalize_text(candidate.reply.body)
        if body_key in seen_bodies:
            continue
        seen_bodies.add(body_key)
        selected.append(candidate)
        if len(selected) >= 3:
            break
    return selected


def _compose_selected_reply(
    person_id: str,
    channel: str,
    selected: list[ActorCandidate],
) -> CoworkerReply:
    first = selected[0].reply
    replies = [candidate.reply for candidate in selected]
    return CoworkerReply(
        person_id=person_id,
        delay_minutes=max(reply.delay_minutes for reply in replies),
        body="\n\n".join(reply.body for reply in replies if reply.body),
        channel=channel,
        subject=first.subject,
        effects=tuple(_dedupe_effects(effect for reply in replies for effect in reply.effects)),
        priority=first.priority,
        matched_rule_ids=tuple(
            rule_id
            for reply in replies
            for rule_id in reply.matched_rule_ids
        ),
    )


def _agenda_candidate(
    person_id: str,
    channel: str,
    state: dict[str, Any],
    body: str,
    *,
    priority: int,
    urgency: int,
    relevance: int,
    candidate_id: str,
) -> ActorCandidate:
    return ActorCandidate(
        reply=CoworkerReply(
            person_id=person_id,
            delay_minutes=_reply_delay_minutes(person_id, {}, state),
            body=body,
            channel=channel,
            effects=(),
            priority=priority,
            matched_rule_ids=(candidate_id,),
        ),
        kind="agenda",
        urgency=urgency,
        relevance=relevance,
    )


def _dedupe_effects(effects: Any) -> list[Effect]:
    seen = set()
    deduped = []
    for effect in effects:
        key = dumps(dict(effect))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(dict(effect))
    return deduped


def _actor_snapshot(
    person_id: str,
    state: dict[str, Any],
    conn: sqlite3.Connection | None,
) -> ActorSnapshot:
    if conn is not None:
        return _actor_snapshot_from_db(person_id, state, conn)
    return ActorSnapshot(
        person_id=person_id,
        behavior=_state_mapping(state, "actor_profiles").get(person_id, {}),
        workload=_state_mapping(state, "actor_workload").get(person_id, {}),
        goals=tuple(_state_rows_for_person(state, "actor_goals", person_id)),
        commitments=tuple(_state_rows_for_person(state, "actor_commitments", person_id)),
        coworker_state=_state_coworker_state(state),
        discovered_facts=tuple(state.get("discovered_facts", [])),
        project_decisions=_state_mapping(state, "project_decisions"),
    )


def _actor_snapshot_from_db(
    person_id: str,
    state: dict[str, Any],
    conn: sqlite3.Connection,
) -> ActorSnapshot:
    person = conn.execute(
        """
        SELECT behavior_json
        FROM people
        WHERE id = ?
        """,
        (person_id,),
    ).fetchone()
    workload = conn.execute(
        """
        SELECT current_focus, capacity_minutes_remaining, load_level, updated_at, metadata_json
        FROM actor_workload
        WHERE person_id = ?
        """,
        (person_id,),
    ).fetchone()
    goals = conn.execute(
        """
        SELECT id, person_id, project_id, description, priority, status, metadata_json
        FROM actor_goals
        WHERE person_id = ?
          AND status = 'active'
        ORDER BY priority DESC, id
        """,
        (person_id,),
    ).fetchall()
    commitments = conn.execute(
        """
        SELECT id, person_id, project_id, commitment_type, description, due_at,
               status, created_at, updated_at, metadata_json
        FROM actor_commitments
        WHERE person_id = ?
          AND status IN ('open', 'in_progress')
        ORDER BY due_at IS NULL, due_at, updated_at, id
        """,
        (person_id,),
    ).fetchall()
    coworker_rows = conn.execute(
        """
        SELECT person_id, key, value_json
        FROM coworker_state
        """
    ).fetchall()
    project_rows = conn.execute(
        """
        SELECT id, metadata_json
        FROM projects
        """
    ).fetchall()

    discovered_facts = tuple(state.get("discovered_facts", []))
    behavior = loads(person["behavior_json"], {}) if person is not None else {}
    workload_dict = dict(workload) if workload is not None else {}
    if workload_dict:
        workload_dict["metadata"] = loads(workload_dict.pop("metadata_json"), {})

    return ActorSnapshot(
        person_id=person_id,
        behavior=behavior,
        workload=workload_dict,
        goals=tuple(_row_with_metadata(row) for row in goals),
        commitments=tuple(_row_with_metadata(row) for row in commitments),
        coworker_state={
            (row["person_id"], row["key"]): loads(row["value_json"], None)
            for row in coworker_rows
        },
        discovered_facts=discovered_facts,
        project_decisions=_project_decisions(project_rows),
    )


def _row_with_metadata(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["metadata"] = loads(data.pop("metadata_json"), {})
    return data


def _project_decisions(rows: list[sqlite3.Row]) -> dict[str, Any]:
    decisions = {}
    for row in rows:
        metadata = loads(row["metadata_json"], {})
        decision = metadata.get("decision")
        if decision:
            decisions[row["id"]] = decision
    return decisions


def _state_mapping(state: dict[str, Any], key: str) -> dict[str, Any]:
    value = state.get(key, {})
    return value if isinstance(value, dict) else {}


def _state_rows_for_person(
    state: dict[str, Any],
    key: str,
    person_id: str,
) -> list[dict[str, Any]]:
    rows = state.get(key, [])
    if not isinstance(rows, list):
        return []
    return [
        dict(row)
        for row in rows
        if isinstance(row, dict) and row.get("person_id") == person_id
    ]


def _state_coworker_state(state: dict[str, Any]) -> dict[tuple[str, str], Any]:
    values = state.get("coworker_state", {})
    if isinstance(values, dict):
        return values
    return {}


def _behavior_urgency(rule: dict[str, Any], snapshot: ActorSnapshot) -> int:
    if any(effect.get("type") == "discover_fact" for effect in rule.get("effects", [])):
        return 20
    if snapshot.workload.get("load_level") in {"high", "overloaded"}:
        return 10
    return 0


def _actor_has_no_capacity(snapshot: ActorSnapshot) -> bool:
    capacity = snapshot.workload.get("capacity_minutes_remaining")
    load_level = str(snapshot.workload.get("load_level", "")).lower()
    return load_level in {"high", "overloaded"} or (
        isinstance(capacity, int) and capacity < 0
    )


def _asks_for_work(normalized: str) -> bool:
    return any(
        term in normalized
        for term in (
            "can you build",
            "can you implement",
            "please build",
            "please implement",
            "can you take",
            "could you take",
            "ship",
            "finish",
            "deliver",
            "commit to",
        )
    )


def _open_commitments(snapshot: ActorSnapshot) -> list[dict[str, Any]]:
    return [
        commitment
        for commitment in snapshot.commitments
        if commitment.get("status") in {"open", "in_progress"}
    ]


def _mentions_commitment_context(
    normalized: str,
    commitments: list[dict[str, Any]],
) -> bool:
    for commitment in commitments:
        description = normalize_text(str(commitment.get("description", "")))
        words = [word for word in description.split() if len(word) > 4]
        if any(word in normalized for word in words[:8]):
            return True
    return False


def _message_is_ambiguous(normalized: str) -> bool:
    vague_terms = ("status", "update", "thoughts", "how is", "checking in", "anything")
    concrete_terms = (
        "risk",
        "approve",
        "decision",
        "draft",
        "security",
        "customer",
        "scope",
        "blocked",
        "blocker",
        "export",
        "cutover",
    )
    return any(term in normalized for term in vague_terms) and not any(
        term in normalized for term in concrete_terms
    )


def _pushes_auto_commenting(normalized: str) -> bool:
    return any(term in normalized for term in ("auto-comment", "auto comment", "autopost", "auto-post"))


def _pushes_full_cutover(normalized: str) -> bool:
    return "full cutover" in normalized or "cut over fully" in normalized


def _draft_mode_is_decided(snapshot: ActorSnapshot) -> bool:
    return (
        "draft_mode_approved" in snapshot.discovered_facts
        or "fact_draft_mode_approved" in snapshot.discovered_facts
        or "draft_mode_approved" in snapshot.project_decisions.values()
        or snapshot.coworker_state.get((snapshot.person_id, "approval_recorded")) is True
        or any(value == "draft_mode_approved" for value in snapshot.project_decisions.values())
    )


def _staged_mode_is_decided(snapshot: ActorSnapshot) -> bool:
    return any(value == "staged_shadow_mode" for value in snapshot.project_decisions.values())


def _render_actor_utterance(
    snapshot: ActorSnapshot,
    *,
    constraints: list[str] | None = None,
    asks: list[str] | None = None,
) -> str:
    parts = []
    constraints = constraints or []
    asks = asks or []
    style = snapshot.behavior.get("communication_style")
    if isinstance(style, dict) and style.get("stress_response") == "escalates_risk":
        prefix = "I need to flag this clearly"
    else:
        prefix = "Quick constraint"
    if constraints:
        parts.append(f"{prefix}: {'; '.join(constraints)}.")
    if asks:
        parts.append(f"What I need from you: {'; '.join(asks)}.")
    return " ".join(parts)


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


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
        "milestone_keys": set(state.get("milestone_ids", ())),
    }
    matched_rules = []
    matched_rule_ids = set()
    rule_effects: list[Effect] = []
    transcript_lines: list[str] = []

    progressed = True
    while progressed:
        progressed = False
        for rule in meeting_rules:
            rule_id = rule.get("id")
            if rule_id in matched_rule_ids:
                continue
            if not _meeting_rule_matches(rule, attendees, normalized_topic, context):
                continue
            matched_rule_ids.add(rule_id)
            matched_rules.append(rule_id)
            effects = [dict(effect) for effect in rule.get("effects", [])]
            rule_effects.extend(effects)
            transcript_lines.extend(rule.get("transcript_lines", []))
            _update_meeting_context(context, effects)
            progressed = True

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

    milestone_keys = context["milestone_keys"]
    required_milestones = set(rule.get("required_milestones", []))
    if required_milestones and not required_milestones.issubset(milestone_keys):
        return False

    absent_milestones = set(rule.get("absent_milestones", []))
    if absent_milestones and milestone_keys.intersection(absent_milestones):
        return False

    return True


def _update_meeting_context(context: dict[str, set[str]], effects: list[Effect]) -> None:
    for effect in effects:
        if effect.get("type") == "discover_fact":
            context["facts"].add(effect["fact_id"])
        elif effect.get("type") == "record_milestone":
            context["milestone_keys"].add(effect["key"])


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
