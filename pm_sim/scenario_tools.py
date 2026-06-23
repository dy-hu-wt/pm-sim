from __future__ import annotations

from pathlib import Path
from typing import Any

from .paths import DEFAULT_SCENARIO_PATH
from .scenario import load_scenario


def lint_scenario(scenario_path: Path | str = DEFAULT_SCENARIO_PATH) -> dict[str, Any]:
    scenario = load_scenario(scenario_path)
    warnings = _lint_warnings(scenario)
    return {
        "ok": not warnings,
        "scenario_id": scenario["id"],
        "name": scenario.get("name") or scenario["id"],
        "counts": _scenario_counts(scenario),
        "warnings": warnings,
        "score_links": _score_links(scenario),
        "behavior_primitives": _behavior_primitives(scenario),
    }


def scenario_graph(scenario_path: Path | str = DEFAULT_SCENARIO_PATH) -> dict[str, Any]:
    scenario = load_scenario(scenario_path)
    nodes: list[dict[str, str]] = []
    edges: list[dict[str, str]] = []

    for project in scenario.get("projects", []):
        nodes.append({"id": project["id"], "type": "project", "label": project["name"]})

    for task in scenario.get("tasks", []):
        nodes.append({"id": task["id"], "type": "task", "label": task["title"]})
        edges.append({"from": task["project_id"], "to": task["id"], "type": "owns_task"})
        if task.get("blocked_by"):
            edges.append({"from": task["blocked_by"], "to": task["id"], "type": "blocks"})

    for dependency in scenario.get("dependencies", []):
        edges.append(
            {
                "from": dependency["upstream_task_id"],
                "to": dependency["downstream_task_id"],
                "type": "depends_on",
            }
        )

    for blocker in scenario.get("blockers", []):
        nodes.append({"id": blocker["id"], "type": "blocker", "label": blocker["title"]})
        edges.append({"from": blocker["project_id"], "to": blocker["id"], "type": "has_blocker"})

    for rule in scenario.get("grading_rules", []):
        rule_id = f"grading:{rule['id']}"
        nodes.append({"id": rule_id, "type": "grading_rule", "label": rule["id"]})
        for ref in _condition_refs(rule.get("requires", [])):
            edges.append({"from": ref, "to": rule_id, "type": "requires"})
        state = rule.get("state", {})
        if state:
            state_id = f"state:{state.get('person_id')}.{state.get('key')}"
            nodes.append({"id": state_id, "type": "coworker_state", "label": state_id[6:]})
            edges.append({"from": rule_id, "to": state_id, "type": "mutates"})

    for component_id, component in scenario.get("score_components", {}).items():
        component_node = f"score:{component_id}"
        nodes.append({"id": component_node, "type": "score_component", "label": component_id})
        for milestone_id in component.get("milestones", []):
            milestone_node = f"milestone:{milestone_id}"
            nodes.append({"id": milestone_node, "type": "milestone", "label": milestone_id})
            edges.append({"from": milestone_node, "to": component_node, "type": "scores"})

    return {
        "ok": True,
        "scenario_id": scenario["id"],
        "nodes": _dedupe_nodes(nodes),
        "edges": edges,
    }


def _scenario_counts(scenario: dict[str, Any]) -> dict[str, int]:
    return {
        "people": len(scenario.get("people", [])),
        "projects": len(scenario.get("projects", [])),
        "tasks": len(scenario.get("tasks", [])),
        "dependencies": len(scenario.get("dependencies", [])),
        "blockers": len(scenario.get("blockers", [])),
        "facts": len(scenario.get("facts", [])),
        "docs": len(scenario.get("docs", [])),
        "events": len(scenario.get("events", [])),
        "actor_behaviors": len(scenario.get("actor_behaviors", [])),
        "grading_rules": len(scenario.get("grading_rules", [])),
        "score_components": len(scenario.get("score_components", {})),
    }


def _lint_warnings(scenario: dict[str, Any]) -> list[str]:
    warnings = []
    if not scenario.get("baseline"):
        warnings.append("Scenario has no no-op baseline.")
    if not scenario.get("scripted_policy"):
        warnings.append("Scenario has no scripted success path.")
    if not any(behavior.get("kind") == "policy" for behavior in scenario.get("actor_behaviors", [])):
        warnings.append("Scenario has no state-driven actor policy behavior.")
    if not scenario.get("dependencies"):
        warnings.append("Scenario has no task dependencies.")

    scored = {
        milestone
        for component in scenario.get("score_components", {}).values()
        for milestone in component.get("milestones", [])
    }
    rule_milestones = {rule.get("id") for rule in scenario.get("milestone_rules", [])}
    if missing := sorted(scored - rule_milestones):
        warnings.append("Scored milestones without milestone_rules: " + ", ".join(missing))

    return warnings


def _score_links(scenario: dict[str, Any]) -> list[dict[str, Any]]:
    links = []
    grading_by_milestone = {
        (rule.get("milestone") or {}).get("key"): rule.get("id")
        for rule in scenario.get("grading_rules", [])
    }
    for component_id, component in scenario.get("score_components", {}).items():
        for milestone_id in component.get("milestones", []):
            links.append(
                {
                    "component": component_id,
                    "milestone": milestone_id,
                    "grading_rule": grading_by_milestone.get(milestone_id),
                }
            )
    return links


def _behavior_primitives(scenario: dict[str, Any]) -> dict[str, int]:
    counts = {"reply": 0, "policy": 0}
    for behavior in scenario.get("actor_behaviors", []):
        kind = behavior.get("kind")
        if kind in counts:
            counts[kind] += 1
    return counts


def _condition_refs(conditions: list[dict[str, Any]]) -> list[str]:
    refs = []
    for condition in conditions:
        if "fact_discovered" in condition:
            refs.append(condition["fact_discovered"])
        elif "project_decision" in condition:
            refs.append(condition["project_decision"]["project_id"])
        elif "coworker_state" in condition:
            spec = condition["coworker_state"]
            refs.append(f"state:{spec['person_id']}.{spec['key']}")
        elif "message_exists" in condition:
            refs.append("messages")
        elif "any" in condition:
            refs.extend(_condition_refs(condition["any"]))
        elif "all" in condition:
            refs.extend(_condition_refs(condition["all"]))
        elif "not" in condition:
            refs.extend(_condition_refs([condition["not"]]))
    return refs


def _dedupe_nodes(nodes: list[dict[str, str]]) -> list[dict[str, str]]:
    by_id = {}
    for node in nodes:
        by_id.setdefault(node["id"], node)
    return list(by_id.values())
