from __future__ import annotations

import copy
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml


class ScenarioError(ValueError):
    pass


def load_scenario(path: Path | str) -> dict[str, Any]:
    scenario_path = Path(path)
    if scenario_path.is_dir():
        scenario_path = scenario_path / "scenario.yaml"
    if not scenario_path.exists():
        raise ScenarioError(f"Scenario file not found: {scenario_path}")
    if scenario_path.suffix not in {".yaml", ".yml"}:
        raise ScenarioError(f"Scenario files must be YAML: {scenario_path}")

    data = _normalize_author_references(_load_scenario_data(scenario_path))
    data = _compile_grading_rules(data)
    data = _compile_behaviors(data)
    _validate_scenario(data, scenario_path)
    return data


def _load_scenario_data(path: Path) -> dict[str, Any]:
    data = _load_yaml_object(path)
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
        included = _load_yaml_object(include_path)
        for key, value in included.items():
            if key in merged:
                raise ScenarioError(
                    f"{include_path} defines duplicate scenario key already present in {path}: {key}"
                )
            merged[key] = value
    return merged


def _load_yaml_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ScenarioError(f"Scenario include file not found: {path}")
    if path.suffix not in {".yaml", ".yml"}:
        raise ScenarioError(f"Scenario include files must be YAML: {path}")
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise ScenarioError(f"Scenario file must contain a YAML object: {path}")
    return data


def _compile_grading_rules(data: dict[str, Any]) -> dict[str, Any]:
    grading_rules = data.get("grading_rules", [])
    if not grading_rules:
        return data
    if not isinstance(grading_rules, list):
        raise ScenarioError("grading_rules must be a list.")

    compiled = copy_object(data)
    generated_action_ids = {
        f"grading_{_required_string(rule, 'id', 'grading rule')}_action"
        for rule in grading_rules
        if isinstance(rule, dict)
    }
    generated_milestone_keys = {
        _required_string(
            _required_object(rule, "milestone", f"grading rule {rule.get('id')}"),
            "key",
            f"grading rule {rule.get('id')} milestone",
        )
        for rule in grading_rules
        if isinstance(rule, dict)
    }
    action_rules = [
        rule
        for rule in compiled.get("action_rules", [])
        if rule.get("id") not in generated_action_ids
    ]
    milestone_rules = [
        rule
        for rule in compiled.get("milestone_rules", [])
        if rule.get("id") not in generated_milestone_keys
    ]
    for rule in grading_rules:
        if not isinstance(rule, dict):
            raise ScenarioError("grading_rules entries must be objects.")
        if rule.get("template") != "grounded_communication":
            raise ScenarioError(f"Unsupported grading rule template: {rule.get('template')}")

        rule_id = _required_string(rule, "id", "grading rule")
        action = _required_object(rule, "action", f"grading rule {rule_id}")
        state = _required_object(rule, "state", f"grading rule {rule_id}")
        milestone = _required_object(rule, "milestone", f"grading rule {rule_id}")

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
        milestone_key = _required_string(milestone, "key", f"grading rule {rule_id} milestone")
        note = _required_string(milestone, "note", f"grading rule {rule_id} milestone")

        action_rules.append(
            {
                "id": f"grading_{rule_id}_action",
                "action_type": action_type,
                "priority": int(rule.get("priority", 60)),
                "recipient_id": recipient_id,
                "match": _match_for_grading_action(action),
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
        milestone_rules.append(
            {
                "id": milestone_key,
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
    compiled["milestone_rules"] = milestone_rules
    return compiled


def _compile_behaviors(data: dict[str, Any]) -> dict[str, Any]:
    behaviors = data.get("behaviors", [])
    if not behaviors:
        return data
    if not isinstance(behaviors, list):
        raise ScenarioError("behaviors must be a list.")

    compiled = copy_object(data)
    actor_behaviors = list(compiled.get("actor_behaviors", []))
    event_rules = list(compiled.get("event_rules", []))
    meeting_rules = list(compiled.get("meeting_rules", []))
    action_rules = list(compiled.get("action_rules", []))
    seen = set()

    for behavior in behaviors:
        if not isinstance(behavior, dict):
            raise ScenarioError("behaviors entries must be objects.")
        behavior_id = behavior.get("id")
        if not isinstance(behavior_id, str) or not behavior_id:
            raise ScenarioError("Behavior must have a string id.")
        if behavior_id in seen:
            raise ScenarioError(f"Behaviors have duplicate id: {behavior_id}")
        seen.add(behavior_id)

        kind = behavior.get("kind")
        row = {key: value for key, value in behavior.items() if key != "kind"}
        if kind in {"reply", "policy"}:
            row["kind"] = kind
            actor_behaviors.append(row)
        elif kind == "event":
            event_rules.append(row)
        elif kind == "meeting":
            meeting_rules.append(row)
        elif kind == "action":
            action_rules.append(row)
        else:
            raise ScenarioError(f"Behavior {behavior_id} has unsupported kind: {kind}")

    compiled.pop("behaviors", None)
    compiled["actor_behaviors"] = actor_behaviors
    compiled["event_rules"] = event_rules
    compiled["meeting_rules"] = meeting_rules
    compiled["action_rules"] = action_rules
    return compiled


def _normalize_author_references(data: dict[str, Any]) -> dict[str, Any]:
    normalized = copy_object(data)
    aliases = _resource_aliases(normalized)
    _canonicalize_resource_ids(normalized, aliases)
    _rewrite_resource_references(normalized, aliases)
    return normalized


RESOURCE_PREFIXES = {
    "projects": "project_",
    "facts": "fact_",
    "blockers": "blocker_",
    "docs": "doc_",
    "tasks": "task_",
}

RESOURCE_REF_KEYS = {
    "project_id": "projects",
    "fact_id": "facts",
    "fact_discovered": "facts",
    "fact_ids": "facts",
    "private_fact_ids": "facts",
    "required_facts": "facts",
    "required_facts_any": "facts",
    "absent_facts": "facts",
    "blocker_id": "blockers",
    "blocked_by": "blockers",
    "doc_id": "docs",
    "task_id": "tasks",
    "upstream_task_id": "tasks",
    "downstream_task_id": "tasks",
}

CONDITION_REF_SECTIONS = {
    "project_decision": "projects",
    "blocker_status": "blockers",
    "task_status": "tasks",
}


def _resource_aliases(data: dict[str, Any]) -> dict[str, dict[str, str]]:
    return {
        section: _section_aliases(data.get(section, []), prefix)
        for section, prefix in RESOURCE_PREFIXES.items()
    }


def _section_aliases(items: list[dict[str, Any]], prefix: str) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for item in items:
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id:
            continue
        canonical = item_id if item_id.startswith(prefix) else f"{prefix}{item_id}"
        names = {item_id, canonical}
        if canonical.startswith(prefix):
            names.add(canonical.removeprefix(prefix))
        for key in ("key", "slug", "ref"):
            value = item.get(key)
            if isinstance(value, str) and value:
                names.add(value)
        for name in names:
            existing = aliases.get(name)
            if existing and existing != canonical:
                raise ScenarioError(
                    f"Ambiguous scenario reference {name!r}: {existing} and {canonical}"
                )
            aliases[name] = canonical
    return aliases


def _canonicalize_resource_ids(
    data: dict[str, Any],
    aliases: dict[str, dict[str, str]],
) -> None:
    for section, section_aliases in aliases.items():
        for item in data.get(section, []):
            item_id = item.get("id")
            if isinstance(item_id, str) and item_id in section_aliases:
                item["id"] = section_aliases[item_id]


def _rewrite_resource_references(value: Any, aliases: dict[str, dict[str, str]]) -> None:
    if isinstance(value, list):
        for item in value:
            _rewrite_resource_references(item, aliases)
        return
    if not isinstance(value, dict):
        return
    for key, item in value.items():
        section = RESOURCE_REF_KEYS.get(key)
        if section:
            value[key] = _resolve_resource_value(item, aliases[section])
            continue
        condition_section = CONDITION_REF_SECTIONS.get(key)
        if condition_section and isinstance(item, dict):
            item_id = item.get("id")
            if isinstance(item_id, str):
                item["id"] = _resolve_resource_reference(item_id, aliases[condition_section])
            project_id = item.get("project_id")
            if isinstance(project_id, str):
                item["project_id"] = _resolve_resource_reference(project_id, aliases["projects"])
            _rewrite_resource_references(item, aliases)
            continue
        if key == "first_time_at_or_after" and isinstance(item, dict):
            fact_id = item.get("fact_id")
            if isinstance(fact_id, str):
                item["fact_id"] = _resolve_resource_reference(fact_id, aliases["facts"])
            _rewrite_resource_references(item, aliases)
            continue
        _rewrite_resource_references(item, aliases)


def _resolve_resource_reference(resource_id: str, aliases: dict[str, str]) -> str:
    return aliases.get(resource_id, resource_id)


def _resolve_resource_value(value: Any, aliases: dict[str, str]) -> Any:
    if isinstance(value, str):
        return _resolve_resource_reference(value, aliases)
    if isinstance(value, list):
        return [_resolve_resource_value(item, aliases) for item in value]
    return value


def copy_object(data: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(data)


def _match_for_grading_action(action: dict[str, Any]) -> dict[str, Any]:
    match = action.get("match")
    if isinstance(match, dict):
        return {"mode": "semantic", **match}
    raise ScenarioError("grading rule action must include match.")


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
    _ids(data, "actor_behaviors")
    _ids(data, "actor_goals")
    _ids(data, "actor_commitments")

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

    for goal in data.get("actor_goals", []):
        if goal.get("person_id") not in people:
            raise ScenarioError(f"Actor goal {goal.get('id')} references unknown person_id: {goal.get('person_id')}")
        project_id = goal.get("project_id")
        if project_id and project_id not in projects:
            raise ScenarioError(f"Actor goal {goal.get('id')} references unknown project_id: {project_id}")
        _require_string(goal, "description", "actor_goals")

    for row in data.get("actor_workload", []):
        if row.get("person_id") not in people:
            raise ScenarioError(f"Actor workload references unknown person_id: {row.get('person_id')}")

    for commitment in data.get("actor_commitments", []):
        if commitment.get("person_id") not in people:
            raise ScenarioError(
                f"Actor commitment {commitment.get('id')} references unknown person_id: {commitment.get('person_id')}"
            )
        project_id = commitment.get("project_id")
        if project_id and project_id not in projects:
            raise ScenarioError(
                f"Actor commitment {commitment.get('id')} references unknown project_id: {project_id}"
            )
        _require_string(commitment, "description", "actor_commitments")

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

    for key, target in data.get("score_components", {}).items():
        points = target.get("points")
        if not isinstance(points, int) or points <= 0:
            raise ScenarioError(f"Score component {key} must have positive integer points.")
        milestones = target.get("milestones")
        if milestones is not None:
            if not isinstance(milestones, list) or not milestones:
                raise ScenarioError(f"Score component {key} milestones must be a non-empty list.")
            invalid = [value for value in milestones if not isinstance(value, str) or not value.strip()]
            if invalid:
                raise ScenarioError(f"Score component {key} milestones must be non-empty strings.")

    response_delays = {
        person["id"]: person.get("response_delay_minutes")
        for person in data.get("people", [])
    }

    _validate_actor_behaviors(
        data.get("actor_behaviors", []),
        people,
        docs,
        facts,
        projects,
        blockers,
        tasks,
        valid_actors,
        response_delays,
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
    _validate_scored_milestones_are_state_derived(data)
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


def _validate_actor_reply_behavior(
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
        raise ScenarioError("Actor reply behavior must have a string id.")
    person_id = rule.get("person_id")
    if person_id not in people:
        raise ScenarioError(f"Actor behavior {rule_id} references unknown person_id: {person_id}")
    channels = rule.get("channels")
    if channels is not None:
        if not isinstance(channels, list) or not channels:
            raise ScenarioError(f"Actor behavior {rule_id} channels must be a non-empty list.")
        invalid = [
            channel
            for channel in channels
            if not isinstance(channel, str) or channel not in {"chat", "email"}
        ]
        if invalid:
            raise ScenarioError(f"Actor behavior {rule_id} channels must contain chat or email.")
    channel = rule.get("channel")
    if channel is not None and channel not in {"chat", "email"}:
        raise ScenarioError(f"Actor behavior {rule_id} channel must be chat or email.")
    match = rule.get("match", rule)
    if not isinstance(match, dict):
        raise ScenarioError(f"Actor behavior {rule_id} match must be an object.")
    _validate_match_spec(match, f"Actor behavior {rule_id}", facts=facts)
    _validate_conditions(
        rule.get("when", []),
        label=f"Actor behavior {rule_id}",
        facts=facts,
        valid_actors=valid_actors,
    )

    reply = rule.get("reply", {})
    if not isinstance(reply.get("body"), str) or not reply["body"].strip():
        raise ScenarioError(f"Actor behavior {rule_id} reply.body must be a non-empty string.")
    delay = reply.get("delay_minutes", response_delays.get(person_id))
    if not isinstance(delay, int) or delay <= 0:
        raise ScenarioError(
            f"Actor behavior {rule_id} must define positive integer reply.delay_minutes "
            f"or use a person with response_delay_minutes."
        )

    _validate_effects(
        rule.get("effects", []),
        label=f"Actor behavior {rule_id}",
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


def _validate_actor_policy_behavior(
    behavior: dict[str, Any],
    people: set[str],
    docs: set[str],
    facts: set[str],
    projects: set[str],
    blockers: set[str],
    tasks: set[str],
    valid_actors: set[str],
) -> None:
    behavior_id = behavior["id"]
    person_id = behavior.get("person_id")
    if person_id not in people:
        raise ScenarioError(
            f"Actor behavior {behavior_id} references unknown person_id: {person_id}"
        )

    trigger = behavior.get("trigger")
    if not isinstance(trigger, dict):
        raise ScenarioError(f"Actor behavior {behavior_id} trigger must be an object.")
    trigger_at = trigger.get("at") or trigger.get("at_or_after")
    if not isinstance(trigger_at, str) or not trigger_at:
        raise ScenarioError(
            f"Actor behavior {behavior_id} trigger must include at or at_or_after."
        )
    _parse_datetime(trigger_at, f"Actor behavior {behavior_id} trigger")

    _validate_conditions(
        behavior.get("when", []),
        label=f"Actor behavior {behavior_id}",
        facts=facts,
        valid_actors=valid_actors,
    )

    effects = behavior.get("effects", [])
    if not isinstance(effects, list) or not effects:
        raise ScenarioError(f"Actor behavior {behavior_id} effects must be a non-empty list.")
    _validate_effects(
        effects,
        label=f"Actor behavior {behavior_id}",
        docs=docs,
        facts=facts,
        projects=projects,
        blockers=blockers,
        tasks=tasks,
        valid_actors=valid_actors,
    )


def _validate_actor_behaviors(
    behaviors: list[dict[str, Any]],
    people: set[str],
    docs: set[str],
    facts: set[str],
    projects: set[str],
    blockers: set[str],
    tasks: set[str],
    valid_actors: set[str],
    response_delays: dict[str, Any],
) -> None:
    if not isinstance(behaviors, list):
        raise ScenarioError("actor_behaviors must be a list.")
    seen = set()
    for behavior in behaviors:
        behavior_id = behavior.get("id")
        if not isinstance(behavior_id, str) or not behavior_id:
            raise ScenarioError("Actor behavior must have a string id.")
        if behavior_id in seen:
            raise ScenarioError(f"Actor behaviors have duplicate id: {behavior_id}")
        seen.add(behavior_id)

        kind = behavior.get("kind")
        if kind == "reply":
            _validate_actor_reply_behavior(
                behavior,
                people,
                docs,
                facts,
                projects,
                blockers,
                tasks,
                valid_actors,
                response_delays,
            )
            continue
        if kind == "policy":
            _validate_actor_policy_behavior(
                behavior,
                people,
                docs,
                facts,
                projects,
                blockers,
                tasks,
                valid_actors,
            )
            continue
        raise ScenarioError(f"Actor behavior {behavior_id} has unsupported kind: {kind}")


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

        _validate_string_list(rule.get("transcript_lines", []), f"Meeting rule {rule_id} transcript_lines")
        topic_match = rule.get("topic_match")
        if topic_match is not None:
            if not isinstance(topic_match, dict):
                raise ScenarioError(f"Meeting rule {rule_id} topic_match must be an object.")
            _validate_match_spec(topic_match, f"Meeting rule {rule_id} topic_match", facts=facts)

        for key in ("required_facts", "required_facts_any", "absent_facts"):
            _validate_string_list(rule.get(key, []), f"Meeting rule {rule_id} {key}")
            for fact_id in rule.get(key, []):
                if fact_id not in facts:
                    raise ScenarioError(
                        f"Meeting rule {rule_id} references unknown {key} fact: {fact_id}"
                    )

        for key in ("required_milestones", "absent_milestones"):
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

        match = rule.get("match")
        if not isinstance(match, dict):
            raise ScenarioError(f"Action rule {rule_id} match must be an object.")
        _validate_match_spec(match, f"Action rule {rule_id}", facts=facts)

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


def _validate_match_spec(spec: dict[str, Any], label: str, *, facts: set[str]) -> None:
    mode = spec.get("mode", "deterministic")
    if mode not in {"deterministic", "semantic", "llm"}:
        raise ScenarioError(f"{label} match.mode must be deterministic, semantic, or llm.")

    for key in ("required_facts", "required_facts_any", "absent_facts"):
        _validate_string_list(spec.get(key, []), f"{label} {key}")
        for fact_id in spec.get(key, []):
            if fact_id not in facts:
                raise ScenarioError(f"{label} references unknown {key} fact: {fact_id}")

    intents = spec.get("intents", [])
    if intents is None:
        intents = []
    if not isinstance(intents, list):
        raise ScenarioError(f"{label} match.intents must be a list.")
    intent_ids = set()
    for index, intent in enumerate(intents, start=1):
        intent_label = f"{label} match intent {index}"
        if not isinstance(intent, dict):
            raise ScenarioError(f"{intent_label} must be an object.")
        intent_id = intent.get("id")
        if not isinstance(intent_id, str) or not intent_id:
            raise ScenarioError(f"{intent_label} must include string id.")
        if intent_id in intent_ids:
            raise ScenarioError(f"{label} has duplicate match intent id: {intent_id}")
        intent_ids.add(intent_id)
        if not isinstance(intent.get("description"), str) or not intent.get("description"):
            raise ScenarioError(f"{intent_label} must include description.")
        _validate_string_list(intent.get("signals", []), f"{intent_label} signals")

    for key in ("require_all", "require_any", "forbid", "forbidden"):
        _validate_string_list(spec.get(key, []), f"{label} match.{key}")
        for intent_id in spec.get(key, []):
            if intent_id not in intent_ids:
                raise ScenarioError(f"{label} match.{key} references unknown intent: {intent_id}")


def _validate_scored_milestones_are_state_derived(data: dict[str, Any]) -> None:
    scored_keys = {
        key
        for target in data.get("score_components", {}).values()
        for key in target.get("milestones", [])
    }
    if not scored_keys:
        return

    allowed_state_keys = {
        rule.get("id")
        for rule in data.get("milestone_rules", [])
        if isinstance(rule, dict)
    }
    missing = sorted(scored_keys - allowed_state_keys)
    if missing:
        raise ScenarioError(
            "Scored milestones must be derived from milestone_rules: "
            + ", ".join(missing)
        )

    for section in (
        "actor_behaviors",
        "event_rules",
        "meeting_rules",
        "action_rules",
        "task_gate_rules",
        "harmful_action_rules",
    ):
        for rule in data.get(section, []):
            rule_id = rule.get("id", "<unknown>")
            for effect in rule.get("effects", []):
                if (
                    effect.get("type") == "record_milestone"
                    and effect.get("key") in scored_keys
                ):
                    raise ScenarioError(
                        f"{section} {rule_id} directly writes scored milestone "
                        f"{effect.get('key')}; use state mutation plus milestone_rules."
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
    if "milestone_exists" in condition:
        if not isinstance(condition["milestone_exists"], str) or not condition["milestone_exists"]:
            raise ScenarioError(f"{label} milestone_exists condition must be a non-empty string.")
    if "message_exists" in condition:
        spec = condition["message_exists"]
        if not isinstance(spec, dict):
            raise ScenarioError(f"{label} message_exists condition must be an object.")
        for key in ("sender_id", "recipient_id"):
            actor_id = spec.get(key)
            if actor_id is not None and actor_id not in valid_actors:
                raise ScenarioError(f"{label} references unknown message {key}: {actor_id}")
        match = spec.get("match")
        if match is not None:
            if not isinstance(match, dict):
                raise ScenarioError(f"{label} message_exists match must be an object.")
            _validate_match_spec(match, f"{label} message_exists", facts=facts)


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
        if effect_type == "update_actor_workload":
            person_id = effect.get("person_id")
            if person_id not in valid_actors or person_id == "agent":
                raise ScenarioError(f"{label} references unknown actor workload person_id: {person_id}")
        if effect_type == "add_actor_commitment":
            person_id = effect.get("person_id")
            if person_id not in valid_actors or person_id == "agent":
                raise ScenarioError(f"{label} references unknown actor commitment person_id: {person_id}")
            project_id = effect.get("project_id")
            if project_id and project_id not in projects:
                raise ScenarioError(f"{label} references unknown actor commitment project_id: {project_id}")
            if not isinstance(effect.get("description"), str) or not effect["description"]:
                raise ScenarioError(f"{label} has invalid actor commitment description.")
        if effect_type == "update_actor_commitment":
            commitment_id = effect.get("id")
            if not isinstance(commitment_id, str) or not commitment_id:
                raise ScenarioError(f"{label} has invalid actor commitment id.")
        if effect_type == "update_actor_goal":
            goal_id = effect.get("id")
            if not isinstance(goal_id, str) or not goal_id:
                raise ScenarioError(f"{label} has invalid actor goal id.")
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
        if effect_type == "record_milestone":
            key = effect.get("key")
            if not isinstance(key, str) or not key.strip():
                raise ScenarioError(f"{label} has invalid milestone key.")


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
