from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Callable

from .conditions import all_conditions_match


SemanticMatcher = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class RuleMatch:
    matches: bool
    semantic: dict[str, Any] | None = None


def normalize_text(value: str) -> str:
    return " ".join(value.lower().split())


def priority_sorted(rules: list[dict[str, Any]], *, default: int = 0) -> list[dict[str, Any]]:
    return sorted(rules, key=lambda rule: int(rule.get("priority", default)), reverse=True)


def match_rule(
    rule: dict[str, Any],
    *,
    normalized_text: str = "",
    context: dict[str, Any] | None = None,
    context_keys: tuple[str, ...] = (),
    conn: sqlite3.Connection | None = None,
    state: dict[str, Any] | None = None,
    project_id: str | None = None,
    semantic_matcher: SemanticMatcher | None = None,
) -> RuleMatch:
    context = context or {}
    for key in context_keys:
        expected = rule.get(key)
        if expected is not None and str(context.get(key, "")).lower() != str(expected).lower():
            return RuleMatch(False)

    match_spec = rule.get("match")
    if not isinstance(match_spec, dict):
        return RuleMatch(False)
    semantic_match_spec = _match_semantic_criteria(match_spec)

    if not match_text_and_facts(
        match_spec,
        normalized_text,
        state=state,
        include_intents=semantic_match_spec is None,
    ):
        return RuleMatch(False)

    conditions = rule.get("when", [])
    if conn is not None and not all_conditions_match(conn, conditions, project_id=project_id):
        return RuleMatch(False)
    if conn is None and state is not None and not state_conditions_match(conditions, state):
        return RuleMatch(False)

    if semantic_match_spec:
        if semantic_matcher is None:
            return RuleMatch(False)
        semantic_result = semantic_matcher(semantic_match_spec, rule)
        if not semantic_result.get("matches"):
            return RuleMatch(False, semantic_result)
        return RuleMatch(True, semantic_result)

    return RuleMatch(True)


def match_text_and_facts(
    match: dict[str, Any],
    normalized_text: str,
    *,
    state: dict[str, Any] | None = None,
    include_intents: bool = True,
) -> bool:
    if include_intents and not _match_intents(match, normalized_text):
        return False

    discovered = set((state or {}).get("discovered_facts", ()))
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


def _match_semantic_criteria(match: dict[str, Any]) -> dict[str, Any] | None:
    mode = str(match.get("mode", "deterministic")).lower()
    if mode not in {"semantic", "llm"}:
        return None
    criteria = _criteria_from_intents(match)
    return criteria if criteria["required"] or criteria["forbidden"] else None


def _criteria_from_intents(match: dict[str, Any]) -> dict[str, Any]:
    intents = _intent_map(match)
    required_ids = _required_intent_ids(match, intents)
    forbidden_ids = _forbidden_intent_ids(match)
    return {
        "required": [
            _semantic_item_from_intent(intents[intent_id])
            for intent_id in required_ids
            if intent_id in intents
        ],
        "forbidden": [
            _semantic_item_from_intent(intents[intent_id])
            for intent_id in forbidden_ids
            if intent_id in intents
        ],
    }


def _semantic_item_from_intent(intent: dict[str, Any]) -> dict[str, Any]:
    return dict(intent)


def _match_intents(match: dict[str, Any], normalized_text: str) -> bool:
    intents = _intent_map(match)
    if not intents:
        return True

    matched = {
        intent_id
        for intent_id, intent in intents.items()
        if _intent_matches(normalized_text, intent)
    }
    required_ids = _required_intent_ids(match, intents)
    if required_ids and not set(required_ids).issubset(matched):
        return False

    require_any = [str(intent_id) for intent_id in match.get("require_any", [])]
    if require_any and not set(require_any).intersection(matched):
        return False

    forbidden_ids = _forbidden_intent_ids(match)
    if forbidden_ids and set(forbidden_ids).intersection(matched):
        return False

    return True


def _intent_map(match: dict[str, Any]) -> dict[str, dict[str, Any]]:
    intents = match.get("intents", [])
    if not isinstance(intents, list):
        return {}
    mapped = {}
    for index, intent in enumerate(intents, start=1):
        if not isinstance(intent, dict):
            continue
        intent_id = str(intent.get("id") or f"intent_{index}")
        mapped[intent_id] = intent
    return mapped


def _required_intent_ids(match: dict[str, Any], intents: dict[str, dict[str, Any]]) -> list[str]:
    if "require_all" in match:
        return [str(intent_id) for intent_id in match.get("require_all", [])]
    return list(intents)


def _forbidden_intent_ids(match: dict[str, Any]) -> list[str]:
    return [
        str(intent_id)
        for intent_id in match.get("forbid", match.get("forbidden", []))
    ]


def _intent_matches(normalized_text: str, intent: dict[str, Any]) -> bool:
    signals = [normalize_text(value) for value in intent.get("signals", [])]
    if signals:
        return any(signal and signal in normalized_text for signal in signals)
    description = normalize_text(str(intent.get("description", "")))
    return bool(description and description in normalized_text)


def state_conditions_match(conditions: list[dict[str, Any]], state: dict[str, Any]) -> bool:
    return all(state_condition_matches(condition, state) for condition in conditions)


def state_condition_matches(condition: dict[str, Any], state: dict[str, Any]) -> bool:
    if not condition:
        return True
    if "not" in condition:
        return not state_condition_matches(condition["not"], state)
    if "all" in condition:
        return all(state_condition_matches(item, state) for item in condition["all"])
    if "any" in condition:
        return any(state_condition_matches(item, state) for item in condition["any"])
    if "coworker_state" in condition:
        spec = condition["coworker_state"]
        person_id = spec["person_id"]
        key = spec["key"]
        values = state.get("coworker_state", {})
        value = values.get((person_id, key), values.get(f"{person_id}.{key}", False))
        if "equals" in spec:
            return value == spec["equals"]
        if "not_equals" in spec:
            return value != spec["not_equals"]
        if spec.get("truthy"):
            return bool(value)
        if spec.get("falsy"):
            return not bool(value)
    return False
