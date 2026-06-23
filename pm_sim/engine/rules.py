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


def mentions_any(value: str, terms: set[str] | frozenset[str]) -> bool:
    return any(term in value for term in terms)


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

    match_spec = rule.get("match", rule)
    if not match_text_and_facts(match_spec, normalized_text, state=state):
        return RuleMatch(False)

    conditions = rule.get("when", [])
    if conn is not None and not all_conditions_match(conn, conditions, project_id=project_id):
        return RuleMatch(False)
    if conn is None and state is not None and not state_conditions_match(conditions, state):
        return RuleMatch(False)

    semantic_criteria = rule.get("semantic_match")
    if semantic_criteria:
        if semantic_matcher is None:
            return RuleMatch(False)
        semantic_result = semantic_matcher(semantic_criteria, rule)
        if not semantic_result.get("matches"):
            return RuleMatch(False, semantic_result)
        return RuleMatch(True, semantic_result)

    return RuleMatch(True)


def match_text_and_facts(
    match: dict[str, Any],
    normalized_text: str,
    *,
    state: dict[str, Any] | None = None,
) -> bool:
    terms_any = {normalize_text(term) for term in match.get("terms_any", [])}
    if terms_any and not mentions_any(normalized_text, terms_any):
        return False

    terms_all = {normalize_text(term) for term in match.get("terms_all", [])}
    if terms_all and not all(term in normalized_text for term in terms_all):
        return False

    for group in match.get("term_groups_all", []):
        terms = {normalize_text(term) for term in group}
        if not terms or not mentions_any(normalized_text, terms):
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
