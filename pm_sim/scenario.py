from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


class ScenarioError(ValueError):
    pass


def load_scenario(path: Path | str) -> dict[str, Any]:
    scenario_path = Path(path)
    if not scenario_path.exists():
        raise ScenarioError(f"Scenario file not found: {scenario_path}")

    data = json.loads(scenario_path.read_text())
    _validate_scenario(data, scenario_path)
    return data


def _validate_scenario(data: dict[str, Any], path: Path) -> None:
    required = ["id", "start_time", "people", "projects"]
    missing = [key for key in required if key not in data]
    if missing:
        raise ScenarioError(f"{path} is missing required keys: {', '.join(missing)}")

    if not isinstance(data["people"], list):
        raise ScenarioError("Scenario key 'people' must be a list.")

    if not isinstance(data["projects"], list):
        raise ScenarioError("Scenario key 'projects' must be a list.")

    start_time = _parse_datetime(data["start_time"], "start_time")
    people = _ids(data, "people")
    projects = _ids(data, "projects")
    facts = _ids(data, "facts")
    tasks = _ids(data, "tasks")
    blockers = _ids(data, "blockers")
    docs = _ids(data, "docs")
    _ids(data, "dependencies")
    _ids(data, "messages")
    _ids(data, "calendar_events")
    _ids(data, "events")

    valid_actors = people | {"agent"}

    for project in data["projects"]:
        _require_string(project, "id", "projects")

    for person in data["people"]:
        _require_string(person, "id", "people")

    for fact in data.get("facts", []):
        _validate_owner(fact, "fact", valid_actors)

    for task in data.get("tasks", []):
        _require_project(task, "task", projects)
        _validate_owner(task, "task", valid_actors)
        blocker_id = task.get("blocked_by")
        if blocker_id and blocker_id not in blockers:
            raise ScenarioError(f"Task {task.get('id')} references unknown blocker: {blocker_id}")

    for dependency in data.get("dependencies", []):
        _require_project(dependency, "dependency", projects)
        for key in ("upstream_task_id", "downstream_task_id"):
            task_id = dependency.get(key)
            if task_id not in tasks:
                raise ScenarioError(
                    f"Dependency {dependency.get('id')} references unknown {key}: {task_id}"
                )

    for blocker in data.get("blockers", []):
        _require_project(blocker, "blocker", projects)
        _validate_owner(blocker, "blocker", valid_actors)

    for doc in data.get("docs", []):
        metadata = doc.get("metadata", {})
        owner_id = doc.get("owner_id") or (
            metadata.get("owner_id") if isinstance(metadata, dict) else None
        )
        if owner_id and owner_id not in valid_actors:
            raise ScenarioError(f"Doc {doc.get('id')} references unknown owner_id: {owner_id}")

    for message in data.get("messages", []):
        for key in ("sender_id", "recipient_id"):
            actor_id = message.get(key)
            if actor_id and actor_id not in valid_actors:
                raise ScenarioError(
                    f"Message {message.get('id')} references unknown {key}: {actor_id}"
                )

    for calendar_event in data.get("calendar_events", []):
        _parse_datetime(calendar_event.get("start_at"), f"calendar event {calendar_event.get('id')} start_at")
        _parse_datetime(calendar_event.get("end_at"), f"calendar event {calendar_event.get('id')} end_at")
        for attendee in calendar_event.get("attendees", []):
            if attendee not in valid_actors:
                raise ScenarioError(
                    f"Calendar event {calendar_event.get('id')} references unknown attendee: {attendee}"
                )

    for event in data.get("events", []):
        scheduled_at = _parse_datetime(
            event.get("scheduled_at"),
            f"event {event.get('id')} scheduled_at",
        )
        if scheduled_at < start_time:
            raise ScenarioError(
                f"Event {event.get('id')} is scheduled before scenario start_time."
            )
        project_id = event.get("payload", {}).get("project_id")
        if project_id and project_id not in projects:
            raise ScenarioError(f"Event {event.get('id')} references unknown project_id: {project_id}")

    for key, target in data.get("evaluation_targets", {}).items():
        points = target.get("points")
        if not isinstance(points, int) or points <= 0:
            raise ScenarioError(f"Evaluation target {key} must have positive integer points.")
        evidence_keys = target.get("evidence_keys")
        if evidence_keys is not None:
            if not isinstance(evidence_keys, list) or not evidence_keys:
                raise ScenarioError(f"Evaluation target {key} evidence_keys must be a non-empty list.")
            invalid = [value for value in evidence_keys if not isinstance(value, str) or not value.strip()]
            if invalid:
                raise ScenarioError(f"Evaluation target {key} evidence_keys must be non-empty strings.")

    for rule in data.get("coworker_rules", []):
        _validate_coworker_rule(rule, people, docs, facts, projects, blockers, tasks)


def _ids(data: dict[str, Any], section: str) -> set[str]:
    seen = set()
    for item in data.get(section, []):
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id:
            raise ScenarioError(f"Scenario section {section} has an item without a string id.")
        if item_id in seen:
            raise ScenarioError(f"Scenario section {section} has duplicate id: {item_id}")
        seen.add(item_id)
    return seen


def _require_string(item: dict[str, Any], key: str, section: str) -> None:
    if not isinstance(item.get(key), str) or not item[key]:
        raise ScenarioError(f"Scenario section {section} has an item without a string {key}.")


def _require_project(item: dict[str, Any], label: str, projects: set[str]) -> None:
    project_id = item.get("project_id")
    if project_id not in projects:
        raise ScenarioError(f"{label.title()} {item.get('id')} references unknown project_id: {project_id}")


def _validate_owner(item: dict[str, Any], label: str, valid_actors: set[str]) -> None:
    owner_id = item.get("owner_id")
    if owner_id and owner_id not in valid_actors:
        raise ScenarioError(f"{label.title()} {item.get('id')} references unknown owner_id: {owner_id}")


def _validate_coworker_rule(
    rule: dict[str, Any],
    people: set[str],
    docs: set[str],
    facts: set[str],
    projects: set[str],
    blockers: set[str],
    tasks: set[str],
) -> None:
    rule_id = rule.get("id")
    if not isinstance(rule_id, str) or not rule_id:
        raise ScenarioError("Coworker rule must have a string id.")
    person_id = rule.get("person_id")
    if person_id not in people:
        raise ScenarioError(f"Coworker rule {rule_id} references unknown person_id: {person_id}")
    match = rule.get("match", rule)
    if not isinstance(match, dict):
        raise ScenarioError(f"Coworker rule {rule_id} match must be an object.")
    _validate_string_list(match.get("terms_any", []), f"Coworker rule {rule_id} terms_any")
    _validate_string_list(match.get("terms_all", []), f"Coworker rule {rule_id} terms_all")
    for group in match.get("term_groups_all", []):
        _validate_string_list(group, f"Coworker rule {rule_id} term group")
    for key in ("required_facts", "required_facts_any", "absent_facts"):
        for fact_id in match.get(key, []):
            if fact_id not in facts:
                raise ScenarioError(f"Coworker rule {rule_id} references unknown {key} fact: {fact_id}")

    reply = rule.get("reply", {})
    if not isinstance(reply.get("body"), str) or not reply["body"].strip():
        raise ScenarioError(f"Coworker rule {rule_id} reply.body must be a non-empty string.")

    for effect in rule.get("effects", []):
        effect_type = effect.get("type")
        if effect_type == "reveal_doc" and effect.get("doc_id") not in docs:
            raise ScenarioError(
                f"Coworker rule {rule_id} references unknown reveal_doc doc_id: {effect.get('doc_id')}"
            )
        if effect_type == "discover_fact" and effect.get("fact_id") not in facts:
            raise ScenarioError(
                f"Coworker rule {rule_id} references unknown discover_fact fact_id: {effect.get('fact_id')}"
            )
        if effect_type == "update_project" and effect.get("project_id") not in projects:
            raise ScenarioError(
                f"Coworker rule {rule_id} references unknown update_project project_id: {effect.get('project_id')}"
            )
        if effect_type == "update_blocker" and effect.get("blocker_id") not in blockers:
            raise ScenarioError(
                f"Coworker rule {rule_id} references unknown update_blocker blocker_id: {effect.get('blocker_id')}"
            )
        if effect_type == "update_task" and effect.get("task_id") not in tasks:
            raise ScenarioError(
                f"Coworker rule {rule_id} references unknown update_task task_id: {effect.get('task_id')}"
            )
        if effect_type == "add_evaluation_evidence":
            key = effect.get("key")
            if not isinstance(key, str) or not key.strip():
                raise ScenarioError(f"Coworker rule {rule_id} has invalid evaluation evidence key.")


def _validate_string_list(values: Any, label: str) -> None:
    if values is None:
        return
    if not isinstance(values, list):
        raise ScenarioError(f"{label} must be a list.")
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ScenarioError(f"{label} must contain non-empty strings.")


def _parse_datetime(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ScenarioError(f"Scenario {label} must be an ISO datetime string.")
    try:
        return datetime.fromisoformat(value)
    except ValueError as error:
        raise ScenarioError(f"Scenario {label} is not a valid ISO datetime: {value}") from error
