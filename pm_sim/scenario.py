from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


class ScenarioError(ValueError):
    pass


def load_scenario(path: Path | str) -> dict[str, Any]:
    scenario_path = Path(path)
    if scenario_path.is_dir():
        scenario_path = scenario_path / "scenario.json"
    if not scenario_path.exists():
        raise ScenarioError(f"Scenario file not found: {scenario_path}")

    data = _compile_grading_rules(_load_scenario_data(scenario_path))
    _validate_scenario(data, scenario_path)
    return data


def _load_scenario_data(path: Path) -> dict[str, Any]:
    data = _load_json_object(path)
    includes = data.get("include", [])
    if includes is None:
        includes = []
    if not isinstance(includes, list):
        raise ScenarioError(f"{path} include must be a list.")

    merged = {key: value for key, value in data.items() if key != "include"}
    for include in includes:
        if not isinstance(include, str) or not include:
            raise ScenarioError(f"{path} include entries must be non-empty strings.")
        include_path = path.parent / include
        included = _load_json_object(include_path)
        for key, value in included.items():
            if key in merged:
                raise ScenarioError(
                    f"{include_path} defines duplicate scenario key already present in {path}: {key}"
                )
            merged[key] = value
    return merged


def _load_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ScenarioError(f"Scenario include file not found: {path}")
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ScenarioError(f"Scenario file must contain a JSON object: {path}")
    return data


def _compile_grading_rules(data: dict[str, Any]) -> dict[str, Any]:
    grading_rules = data.get("grading_rules", [])
    if not grading_rules:
        return data
    if not isinstance(grading_rules, list):
        raise ScenarioError("grading_rules must be a list.")

    compiled = copy_json_object(data)
    generated_action_ids = {
        f"grading_{_required_string(rule, 'id', 'grading rule')}_action"
        for rule in grading_rules
        if isinstance(rule, dict)
    }
    generated_evidence_keys = {
        _required_string(
            _required_object(rule, "evidence", f"grading rule {rule.get('id')}"),
            "key",
            f"grading rule {rule.get('id')} evidence",
        )
        for rule in grading_rules
        if isinstance(rule, dict)
    }
    action_rules = [
        rule
        for rule in compiled.get("action_rules", [])
        if rule.get("id") not in generated_action_ids
    ]
    state_evidence_rules = [
        rule
        for rule in compiled.get("state_evidence_rules", [])
        if rule.get("evidence_key") not in generated_evidence_keys
    ]
    for rule in grading_rules:
        if not isinstance(rule, dict):
            raise ScenarioError("grading_rules entries must be objects.")
        if rule.get("template") != "grounded_communication":
            raise ScenarioError(f"Unsupported grading rule template: {rule.get('template')}")

        rule_id = _required_string(rule, "id", "grading rule")
        action = _required_object(rule, "action", f"grading rule {rule_id}")
        state = _required_object(rule, "state", f"grading rule {rule_id}")
        evidence = _required_object(rule, "evidence", f"grading rule {rule_id}")

        action_type = _required_string(action, "type", f"grading rule {rule_id} action")
        recipient_id = _required_string(
            action,
            "recipient_id",
            f"grading rule {rule_id} action",
        )
        person_id = _required_string(state, "person_id", f"grading rule {rule_id} state")
        key = _required_string(state, "key", f"grading rule {rule_id} state")
        if "value" not in state:
            raise ScenarioError(f"grading rule {rule_id} state must include value.")
        evidence_key = _required_string(evidence, "key", f"grading rule {rule_id} evidence")
        note = _required_string(evidence, "note", f"grading rule {rule_id} evidence")

        action_rules.append(
            {
                "id": f"grading_{rule_id}_action",
                "action_type": action_type,
                "priority": int(rule.get("priority", 60)),
                "recipient_id": recipient_id,
                "semantic_match": _semantic_match_for_grading_action(action),
                "when": rule.get("requires", []),
                "effects": [
                    {
                        "type": "update_coworker_state",
                        "person_id": person_id,
                        "key": key,
                        "value": state["value"],
                    },
                    *rule.get("effects", []),
                ],
            }
        )
        state_evidence_rules.append(
            {
                "evidence_key": evidence_key,
                "note": note,
                "when": [
                    {
                        "coworker_state": {
                            "person_id": person_id,
                            "key": key,
                            "equals": state["value"],
                        }
                    }
                ],
                "created_at": {
                    "coworker_state": {
                        "person_id": person_id,
                        "key": key,
                    }
                },
            }
        )

    compiled["action_rules"] = action_rules
    compiled["state_evidence_rules"] = state_evidence_rules
    return compiled


def copy_json_object(data: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(data))


def _semantic_match_for_grading_action(action: dict[str, Any]) -> dict[str, Any]:
    semantic_match = action.get("semantic_match")
    if isinstance(semantic_match, dict):
        return semantic_match
    required = action.get("required_semantics", [])
    forbidden = action.get("forbidden_semantics", [])
    return {"required": required, "forbidden": forbidden}


def _required_object(row: dict[str, Any], key: str, label: str) -> dict[str, Any]:
    value = row.get(key)
    if not isinstance(value, dict):
        raise ScenarioError(f"{label} must include object {key}.")
    return value


def _required_string(row: dict[str, Any], key: str, label: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value:
        raise ScenarioError(f"{label} must include string {key}.")
    return value


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
    _ids(data, "coworker_state")

    valid_actors = people | {"agent"}

    for project in data["projects"]:
        _require_string(project, "id", "projects")

    for person in data["people"]:
        _require_string(person, "id", "people")
        response_delay = person.get("response_delay_minutes")
        if not isinstance(response_delay, int) or response_delay <= 0:
            raise ScenarioError(
                f"Person {person.get('id')} must define positive integer response_delay_minutes."
            )

    for row in data.get("coworker_state", []):
        if row.get("person_id") not in people:
            raise ScenarioError(
                f"Coworker state {row.get('id')} references unknown person_id: {row.get('person_id')}"
            )
        _require_string(row, "key", "coworker_state")
        if "value" not in row:
            raise ScenarioError(f"Coworker state {row.get('id')} is missing value.")

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

    response_delays = {
        person["id"]: person.get("response_delay_minutes")
        for person in data.get("people", [])
    }

    for rule in data.get("coworker_rules", []):
        _validate_coworker_rule(
            rule,
            people,
            docs,
            facts,
            projects,
            blockers,
            tasks,
            valid_actors,
            response_delays,
        )

    _validate_coworker_policies(
        data.get("coworker_policies", []),
        people,
        docs,
        facts,
        projects,
        blockers,
        tasks,
        valid_actors,
    )

    event_types = {
        event.get("event_type")
        for event in data.get("events", [])
        if isinstance(event.get("event_type"), str)
    }
    _validate_event_rules(
        data.get("event_rules", []),
        event_types,
        docs,
        facts,
        projects,
        blockers,
        tasks,
        valid_actors,
    )
    _validate_meeting_rules(
        data.get("meeting_rules", []),
        people,
        docs,
        facts,
        projects,
        blockers,
        tasks,
        valid_actors,
    )
    _validate_action_rules(
        data.get("action_rules", []),
        people,
        docs,
        facts,
        projects,
        blockers,
        tasks,
        valid_actors,
    )
    _validate_scored_evidence_is_state_derived(data)
    _validate_scripted_policy(data.get("scripted_policy", []), people, docs, tasks)


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
    valid_actors: set[str],
    response_delays: dict[str, Any],
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
    _validate_conditions(
        rule.get("when", []),
        label=f"Coworker rule {rule_id}",
        facts=facts,
        valid_actors=valid_actors,
    )

    reply = rule.get("reply", {})
    if not isinstance(reply.get("body"), str) or not reply["body"].strip():
        raise ScenarioError(f"Coworker rule {rule_id} reply.body must be a non-empty string.")
    delay = reply.get("delay_minutes", response_delays.get(person_id))
    if not isinstance(delay, int) or delay <= 0:
        raise ScenarioError(
            f"Coworker rule {rule_id} must define positive integer reply.delay_minutes "
            f"or use a person with response_delay_minutes."
        )

    _validate_effects(
        rule.get("effects", []),
        label=f"Coworker rule {rule_id}",
        docs=docs,
        facts=facts,
        projects=projects,
        blockers=blockers,
        tasks=tasks,
        valid_actors=valid_actors,
    )


def _validate_event_rules(
    rules: list[dict[str, Any]],
    event_types: set[str],
    docs: set[str],
    facts: set[str],
    projects: set[str],
    blockers: set[str],
    tasks: set[str],
    valid_actors: set[str],
) -> None:
    seen = set()
    for rule in rules:
        rule_id = rule.get("id")
        if not isinstance(rule_id, str) or not rule_id:
            raise ScenarioError("Event rule must have a string id.")
        if rule_id in seen:
            raise ScenarioError(f"Event rules have duplicate id: {rule_id}")
        seen.add(rule_id)

        event_type = rule.get("event_type")
        if event_type not in event_types:
            raise ScenarioError(f"Event rule {rule_id} references unknown event_type: {event_type}")
        effects = rule.get("effects", [])
        if not isinstance(effects, list) or not effects:
            raise ScenarioError(f"Event rule {rule_id} effects must be a non-empty list.")
        _validate_effects(
            effects,
            label=f"Event rule {rule_id}",
            docs=docs,
            facts=facts,
            projects=projects,
            blockers=blockers,
            tasks=tasks,
            valid_actors=valid_actors,
        )


def _validate_coworker_policies(
    policies: list[dict[str, Any]],
    people: set[str],
    docs: set[str],
    facts: set[str],
    projects: set[str],
    blockers: set[str],
    tasks: set[str],
    valid_actors: set[str],
) -> None:
    if not isinstance(policies, list):
        raise ScenarioError("coworker_policies must be a list.")
    seen = set()
    for policy in policies:
        policy_id = policy.get("id")
        if not isinstance(policy_id, str) or not policy_id:
            raise ScenarioError("Coworker policy must have a string id.")
        if policy_id in seen:
            raise ScenarioError(f"Coworker policies have duplicate id: {policy_id}")
        seen.add(policy_id)

        person_id = policy.get("person_id")
        if person_id not in people:
            raise ScenarioError(
                f"Coworker policy {policy_id} references unknown person_id: {person_id}"
            )

        trigger = policy.get("trigger")
        if not isinstance(trigger, dict):
            raise ScenarioError(f"Coworker policy {policy_id} trigger must be an object.")
        trigger_at = trigger.get("at") or trigger.get("at_or_after")
        if not isinstance(trigger_at, str) or not trigger_at:
            raise ScenarioError(
                f"Coworker policy {policy_id} trigger must include at or at_or_after."
            )
        _parse_datetime(trigger_at, f"Coworker policy {policy_id} trigger")

        _validate_conditions(
            policy.get("when", []),
            label=f"Coworker policy {policy_id}",
            facts=facts,
            valid_actors=valid_actors,
        )

        effects = policy.get("effects", [])
        if not isinstance(effects, list) or not effects:
            raise ScenarioError(f"Coworker policy {policy_id} effects must be a non-empty list.")
        _validate_effects(
            effects,
            label=f"Coworker policy {policy_id}",
            docs=docs,
            facts=facts,
            projects=projects,
            blockers=blockers,
            tasks=tasks,
            valid_actors=valid_actors,
        )


def _validate_meeting_rules(
    rules: list[dict[str, Any]],
    people: set[str],
    docs: set[str],
    facts: set[str],
    projects: set[str],
    blockers: set[str],
    tasks: set[str],
    valid_actors: set[str],
) -> None:
    seen = set()
    for rule in rules:
        rule_id = rule.get("id")
        if not isinstance(rule_id, str) or not rule_id:
            raise ScenarioError("Meeting rule must have a string id.")
        if rule_id in seen:
            raise ScenarioError(f"Meeting rules have duplicate id: {rule_id}")
        seen.add(rule_id)

        for key in ("required_attendees", "attendees_any"):
            _validate_string_list(rule.get(key, []), f"Meeting rule {rule_id} {key}")
            for attendee in rule.get(key, []):
                if attendee not in people:
                    raise ScenarioError(
                        f"Meeting rule {rule_id} references unknown {key} attendee: {attendee}"
                    )

        for key in ("topic_terms_any", "topic_terms_all", "transcript_lines"):
            _validate_string_list(rule.get(key, []), f"Meeting rule {rule_id} {key}")

        for key in ("required_facts", "required_facts_any", "absent_facts"):
            _validate_string_list(rule.get(key, []), f"Meeting rule {rule_id} {key}")
            for fact_id in rule.get(key, []):
                if fact_id not in facts:
                    raise ScenarioError(
                        f"Meeting rule {rule_id} references unknown {key} fact: {fact_id}"
                    )

        for key in ("required_evidence", "absent_evidence"):
            _validate_string_list(rule.get(key, []), f"Meeting rule {rule_id} {key}")

        effects = rule.get("effects", [])
        if effects is not None:
            if not isinstance(effects, list):
                raise ScenarioError(f"Meeting rule {rule_id} effects must be a list.")
            _validate_effects(
                effects,
                label=f"Meeting rule {rule_id}",
                docs=docs,
                facts=facts,
                projects=projects,
                blockers=blockers,
                tasks=tasks,
                valid_actors=valid_actors,
            )


def _validate_action_rules(
    rules: list[dict[str, Any]],
    people: set[str],
    docs: set[str],
    facts: set[str],
    projects: set[str],
    blockers: set[str],
    tasks: set[str],
    valid_actors: set[str],
) -> None:
    seen = set()
    for rule in rules:
        rule_id = rule.get("id")
        if not isinstance(rule_id, str) or not rule_id:
            raise ScenarioError("Action rule must have a string id.")
        if rule_id in seen:
            raise ScenarioError(f"Action rules have duplicate id: {rule_id}")
        seen.add(rule_id)

        action_type = rule.get("action_type")
        if action_type not in {"send_chat", "send_email", "update_doc"}:
            raise ScenarioError(f"Action rule {rule_id} has unsupported action_type: {action_type}")

        person_id = rule.get("person_id")
        if person_id is not None and person_id not in people:
            raise ScenarioError(f"Action rule {rule_id} references unknown person_id: {person_id}")
        recipient_id = rule.get("recipient_id")
        if recipient_id is not None and recipient_id not in valid_actors:
            raise ScenarioError(f"Action rule {rule_id} references unknown recipient_id: {recipient_id}")
        doc_id = rule.get("doc_id")
        if doc_id is not None and doc_id not in docs:
            raise ScenarioError(f"Action rule {rule_id} references unknown doc_id: {doc_id}")

        for key in ("terms_any", "terms_all"):
            _validate_string_list(rule.get(key, []), f"Action rule {rule_id} {key}")
        for group in rule.get("term_groups_all", []):
            _validate_string_list(group, f"Action rule {rule_id} term group")
        if "semantic_match" in rule:
            _validate_semantic_match(rule["semantic_match"], f"Action rule {rule_id}")

        _validate_conditions(
            rule.get("when", []),
            label=f"Action rule {rule_id}",
            facts=facts,
            valid_actors=valid_actors,
        )

        effects = rule.get("effects", [])
        if not isinstance(effects, list):
            raise ScenarioError(f"Action rule {rule_id} effects must be a list.")
        _validate_effects(
            effects,
            label=f"Action rule {rule_id}",
            docs=docs,
            facts=facts,
            projects=projects,
            blockers=blockers,
            tasks=tasks,
            valid_actors=valid_actors,
        )


def _validate_semantic_match(spec: Any, label: str) -> None:
    if not isinstance(spec, dict):
        raise ScenarioError(f"{label} semantic_match must be an object.")
    for key in ("required", "forbidden"):
        items = spec.get(key, [])
        if not isinstance(items, list):
            raise ScenarioError(f"{label} semantic_match {key} must be a list.")
        for index, item in enumerate(items, start=1):
            item_label = f"{label} semantic_match {key} item {index}"
            if isinstance(item, str):
                if not item:
                    raise ScenarioError(f"{item_label} must be non-empty.")
                continue
            if not isinstance(item, dict):
                raise ScenarioError(f"{item_label} must be a string or object.")
            if not isinstance(item.get("description"), str) or not item.get("description"):
                raise ScenarioError(f"{item_label} must include description.")
            for list_key in ("signals_any", "signals_all"):
                _validate_string_list(item.get(list_key, []), f"{item_label} {list_key}")
            for group in item.get("signal_groups_all", []):
                _validate_string_list(group, f"{item_label} signal group")


def _validate_scored_evidence_is_state_derived(data: dict[str, Any]) -> None:
    scored_keys = {
        key
        for target in data.get("evaluation_targets", {}).values()
        for key in target.get("evidence_keys", [])
    }
    if not scored_keys:
        return

    allowed_state_keys = {
        rule.get("evidence_key")
        for rule in data.get("state_evidence_rules", [])
        if isinstance(rule, dict)
    }
    missing = sorted(scored_keys - allowed_state_keys)
    if missing:
        raise ScenarioError(
            "Evaluation evidence keys must be derived from state_evidence_rules: "
            + ", ".join(missing)
        )

    for section in (
        "event_rules",
        "coworker_rules",
        "coworker_policies",
        "meeting_rules",
        "action_rules",
        "task_gate_rules",
        "harmful_action_rules",
    ):
        for rule in data.get(section, []):
            rule_id = rule.get("id", "<unknown>")
            for effect in rule.get("effects", []):
                if (
                    effect.get("type") == "add_evaluation_evidence"
                    and effect.get("key") in scored_keys
                ):
                    raise ScenarioError(
                        f"{section} {rule_id} directly writes scored evidence "
                        f"{effect.get('key')}; use state mutation plus state_evidence_rules."
                    )


def _validate_scripted_policy(
    steps: list[dict[str, Any]],
    people: set[str],
    docs: set[str],
    tasks: set[str],
) -> None:
    if not isinstance(steps, list):
        raise ScenarioError("scripted_policy must be a list.")
    for index, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            raise ScenarioError(f"scripted_policy step {index} must be an object.")
        name = step.get("name")
        if not isinstance(name, str) or not name:
            raise ScenarioError(f"scripted_policy step {index} must have a string name.")
        tool = step.get("tool")
        args = step.get("args", {})
        if not isinstance(args, dict):
            raise ScenarioError(f"scripted_policy step {name} args must be an object.")
        if tool in {"read_doc", "update_doc"} and args.get("doc_id") not in docs:
            raise ScenarioError(
                f"scripted_policy step {name} references unknown doc_id: {args.get('doc_id')}"
            )
        if tool in {"send_chat", "send_email"} and args.get("person_id") not in people:
            raise ScenarioError(
                f"scripted_policy step {name} references unknown person_id: {args.get('person_id')}"
            )
        if tool == "update_task" and args.get("task_id") not in tasks:
            raise ScenarioError(
                f"scripted_policy step {name} references unknown task_id: {args.get('task_id')}"
            )
        if tool == "schedule_meeting":
            for attendee in args.get("attendees", []):
                if attendee not in people:
                    raise ScenarioError(
                        f"scripted_policy step {name} references unknown attendee: {attendee}"
                    )
        if tool not in {
            "advance_time",
            "read_doc",
            "schedule_meeting",
            "send_chat",
            "send_email",
            "update_doc",
            "update_task",
        }:
            raise ScenarioError(f"scripted_policy step {name} has unsupported tool: {tool}")


def _validate_conditions(
    conditions: list[dict[str, Any]],
    *,
    label: str,
    facts: set[str],
    valid_actors: set[str],
) -> None:
    if not isinstance(conditions, list):
        raise ScenarioError(f"{label} when must be a list.")
    for condition in conditions:
        _validate_condition(condition, label=label, facts=facts, valid_actors=valid_actors)


def _validate_condition(
    condition: dict[str, Any],
    *,
    label: str,
    facts: set[str],
    valid_actors: set[str],
) -> None:
    if not isinstance(condition, dict):
        raise ScenarioError(f"{label} condition must be an object.")
    for key in ("all", "any"):
        if key in condition:
            _validate_conditions(
                condition[key],
                label=label,
                facts=facts,
                valid_actors=valid_actors,
            )
    if "not" in condition:
        _validate_condition(
            condition["not"],
            label=label,
            facts=facts,
            valid_actors=valid_actors,
        )
    if "fact_discovered" in condition and condition["fact_discovered"] not in facts:
        raise ScenarioError(
            f"{label} references unknown fact_discovered fact: {condition['fact_discovered']}"
        )
    if "message_exists" in condition:
        spec = condition["message_exists"]
        if not isinstance(spec, dict):
            raise ScenarioError(f"{label} message_exists condition must be an object.")
        for key in ("sender_id", "recipient_id"):
            actor_id = spec.get(key)
            if actor_id is not None and actor_id not in valid_actors:
                raise ScenarioError(f"{label} references unknown message {key}: {actor_id}")
        for key in ("terms_any", "terms_all"):
            _validate_string_list(spec.get(key, []), f"{label} message_exists {key}")


def _validate_effects(
    effects: list[dict[str, Any]],
    *,
    label: str,
    docs: set[str],
    facts: set[str],
    projects: set[str],
    blockers: set[str],
    tasks: set[str],
    valid_actors: set[str],
) -> None:
    for effect in effects:
        effect_type = effect.get("type")
        if effect_type == "reveal_doc" and effect.get("doc_id") not in docs:
            raise ScenarioError(
                f"{label} references unknown reveal_doc doc_id: {effect.get('doc_id')}"
            )
        if effect_type == "discover_fact" and effect.get("fact_id") not in facts:
            raise ScenarioError(
                f"{label} references unknown discover_fact fact_id: {effect.get('fact_id')}"
            )
        if effect_type == "update_project" and effect.get("project_id") not in projects:
            raise ScenarioError(
                f"{label} references unknown update_project project_id: {effect.get('project_id')}"
            )
        if effect_type == "update_coworker_state":
            person_id = effect.get("person_id")
            if person_id not in valid_actors or person_id == "agent":
                raise ScenarioError(
                    f"{label} references unknown update_coworker_state person_id: {person_id}"
                )
            values = effect.get("values")
            if values is None:
                if not isinstance(effect.get("key"), str) or not effect["key"]:
                    raise ScenarioError(f"{label} has invalid update_coworker_state key.")
            elif not isinstance(values, dict) or not values:
                raise ScenarioError(f"{label} has invalid update_coworker_state values.")
        if effect_type == "update_blocker" and effect.get("blocker_id") not in blockers:
            raise ScenarioError(
                f"{label} references unknown update_blocker blocker_id: {effect.get('blocker_id')}"
            )
        if effect_type == "update_task" and effect.get("task_id") not in tasks:
            raise ScenarioError(
                f"{label} references unknown update_task task_id: {effect.get('task_id')}"
            )
        if effect_type == "create_message":
            sender_id = effect.get("sender_id")
            recipient_id = effect.get("recipient_id")
            if sender_id not in valid_actors:
                raise ScenarioError(f"{label} references unknown message sender_id: {sender_id}")
            if recipient_id and recipient_id not in valid_actors:
                raise ScenarioError(f"{label} references unknown message recipient_id: {recipient_id}")
        if effect_type == "add_evaluation_evidence":
            key = effect.get("key")
            if not isinstance(key, str) or not key.strip():
                raise ScenarioError(f"{label} has invalid evaluation evidence key.")


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
