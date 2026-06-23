from __future__ import annotations

import copy
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml


class ScenarioError(ValueError):
    pass


RESOURCE_PREFIXES = {
    "projects": "project_",
    "facts": "fact_",
    "blockers": "blocker_",
    "docs": "doc_",
    "tasks": "task_",
    "pressures": "pressure_",
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
    "pressure_id": "pressures",
}

CONDITION_REF_SECTIONS = {
    "project_decision": "projects",
    "blocker_status": "blockers",
    "task_status": "tasks",
    "pressure_at_least": "pressures",
    "pressure_at_most": "pressures",
}


def load_scenario(path: Path | str) -> dict[str, Any]:
    scenario_path = Path(path)
    if scenario_path.is_dir():
        scenario_path = scenario_path / "scenario.yaml"
    if not scenario_path.exists():
        raise ScenarioError(f"Scenario file not found: {scenario_path}")
    if scenario_path.suffix not in {".yaml", ".yml"}:
        raise ScenarioError(f"Scenario files must be YAML: {scenario_path}")

    from .compile import compile_action_checks, compile_behaviors
    from .validate import validate_scenario

    data = normalize_author_references(_load_scenario_data(scenario_path))
    data = compile_action_checks(data)
    data = compile_behaviors(data)
    data["_scenario_path"] = str(scenario_path.resolve())
    validate_scenario(data, scenario_path)
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
    _reject_legacy_behavior_keys(merged, path)
    return merged


def _reject_legacy_behavior_keys(data: dict[str, Any], path: Path) -> None:
    legacy_keys = {
        "behaviors": "event_behaviors, policy_behaviors, reply_behaviors, meeting_behaviors, or action_behaviors",
        "actor_behaviors": "policy_behaviors or reply_behaviors",
        "event_rules": "event_behaviors",
        "meeting_rules": "meeting_behaviors",
        "action_rules": "action_behaviors or action_checks",
        "grading_rules": "action_checks",
    }
    for key, replacement in legacy_keys.items():
        if key in data:
            raise ScenarioError(
                f"{path} uses legacy behavior key {key}; use {replacement} instead."
            )


def _load_yaml_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ScenarioError(f"Scenario include file not found: {path}")
    if path.suffix not in {".yaml", ".yml"}:
        raise ScenarioError(f"Scenario include files must be YAML: {path}")
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise ScenarioError(f"Scenario file must contain a YAML object: {path}")
    return data


def normalize_author_references(data: dict[str, Any]) -> dict[str, Any]:
    normalized = copy_object(data)
    aliases = _resource_aliases(normalized)
    _canonicalize_resource_ids(normalized, aliases)
    _rewrite_resource_references(normalized, aliases)
    return normalized


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


def required_object(row: dict[str, Any], key: str, label: str) -> dict[str, Any]:
    value = row.get(key)
    if not isinstance(value, dict):
        raise ScenarioError(f"{label} must include object {key}.")
    return value


def required_string(row: dict[str, Any], key: str, label: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value:
        raise ScenarioError(f"{label} must include string {key}.")
    return value


def parse_datetime(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ScenarioError(f"Scenario {label} must be an ISO datetime string.")
    try:
        return datetime.fromisoformat(value)
    except ValueError as error:
        raise ScenarioError(f"Scenario {label} is not a valid ISO datetime: {value}") from error
