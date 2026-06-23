from __future__ import annotations

from typing import Any

from .load import ScenarioError, copy_object, required_object, required_string


def compile_grading_rules(data: dict[str, Any]) -> dict[str, Any]:
    grading_rules = data.get("grading_rules", [])
    if not grading_rules:
        return data
    if not isinstance(grading_rules, list):
        raise ScenarioError("grading_rules must be a list.")

    compiled = copy_object(data)
    generated_promotion_ids = {
        f"grading_{required_string(rule, 'id', 'grading rule')}_promotion"
        for rule in grading_rules
        if isinstance(rule, dict)
    }
    generated_milestone_keys = {
        required_string(
            required_object(rule, "milestone", f"grading rule {rule.get('id')}"),
            "key",
            f"grading rule {rule.get('id')} milestone",
        )
        for rule in grading_rules
        if isinstance(rule, dict)
    }
    action_rules = []
    milestone_rules = [
        rule
        for rule in compiled.get("milestone_rules", [])
        if rule.get("id") not in generated_milestone_keys
    ]
    evidence_promotion_rules = [
        rule
        for rule in compiled.get("evidence_promotion_rules", [])
        if rule.get("id") not in generated_promotion_ids
    ]
    for rule in grading_rules:
        if not isinstance(rule, dict):
            raise ScenarioError("grading_rules entries must be objects.")
        if rule.get("template") != "grounded_communication":
            raise ScenarioError(f"Unsupported grading rule template: {rule.get('template')}")

        rule_id = required_string(rule, "id", "grading rule")
        action = required_object(rule, "action", f"grading rule {rule_id}")
        state = required_object(rule, "state", f"grading rule {rule_id}")
        milestone = required_object(rule, "milestone", f"grading rule {rule_id}")

        action_type = required_string(action, "type", f"grading rule {rule_id} action")
        recipient_id = required_string(action, "recipient_id", f"grading rule {rule_id} action")
        person_id = required_string(state, "person_id", f"grading rule {rule_id} state")
        key = required_string(state, "key", f"grading rule {rule_id} state")
        if "value" not in state:
            raise ScenarioError(f"grading rule {rule_id} state must include value.")
        milestone_key = required_string(milestone, "key", f"grading rule {rule_id} milestone")
        note = required_string(milestone, "note", f"grading rule {rule_id} milestone")
        evidence_key = f"grading_{rule_id}"

        action_rules.append(
            {
                "id": f"{evidence_key}_action",
                "action_type": action_type,
                "priority": int(rule.get("priority", 60)),
                "recipient_id": recipient_id,
                "match": match_for_grading_action(action),
                "when": rule.get("requires", []),
                "effects": [
                    {
                        "type": "record_action_evidence",
                        "key": evidence_key,
                        "action_type": action_type,
                        "person_id": person_id,
                        "state_key": key,
                        "state_value": state["value"],
                    },
                ],
            }
        )
        evidence_promotion_rules.append(
            {
                "id": f"{evidence_key}_promotion",
                "priority": int(rule.get("priority", 60)),
                "when": [
                    *rule.get("requires", []),
                    {
                        "action_evidence": {
                            "key": evidence_key,
                            "status": "pending",
                        }
                    },
                ],
                "effects": [
                    {
                        "type": "update_coworker_state",
                        "person_id": person_id,
                        "key": key,
                        "value": state["value"],
                    },
                    *rule.get("effects", []),
                    {
                        "type": "mark_action_evidence_promoted",
                        "key": evidence_key,
                    },
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
    compiled["evidence_promotion_rules"] = evidence_promotion_rules
    compiled["milestone_rules"] = milestone_rules
    return compiled


def compile_behaviors(data: dict[str, Any]) -> dict[str, Any]:
    compiled = copy_object(data)
    actor_behaviors: list[dict[str, Any]] = []
    event_rules = behavior_group(compiled, "event_behaviors")
    meeting_rules = behavior_group(compiled, "meeting_behaviors")
    action_rules = [
        *compiled.get("action_rules", []),
        *behavior_group(compiled, "action_behaviors"),
    ]
    seen = set()

    for behavior in behavior_group(compiled, "policy_behaviors"):
        row = dict(behavior)
        row["kind"] = "policy"
        actor_behaviors.append(row)

    reply_behaviors = compiled.get("reply_behaviors", {})
    if reply_behaviors is None:
        reply_behaviors = {}
    if not isinstance(reply_behaviors, dict):
        raise ScenarioError("reply_behaviors must be an object keyed by person id.")
    for person_id, behaviors in reply_behaviors.items():
        if not isinstance(person_id, str) or not person_id:
            raise ScenarioError("reply_behaviors keys must be non-empty person ids.")
        if not isinstance(behaviors, list):
            raise ScenarioError(f"reply_behaviors.{person_id} must be a list.")
        for behavior in behaviors:
            if not isinstance(behavior, dict):
                raise ScenarioError(f"reply_behaviors.{person_id} entries must be objects.")
            row = dict(behavior)
            row["kind"] = "reply"
            row["person_id"] = person_id
            actor_behaviors.append(row)

    for group_name, group in (
        ("event_behaviors", event_rules),
        ("policy_behaviors", actor_behaviors),
        ("meeting_behaviors", meeting_rules),
        ("action_behaviors", action_rules),
    ):
        for behavior in group:
            behavior_id = behavior.get("id")
            if not isinstance(behavior_id, str) or not behavior_id:
                raise ScenarioError(f"{group_name} entries must include string id.")
            if behavior_id in seen:
                raise ScenarioError(f"Behavior groups have duplicate id: {behavior_id}")
            seen.add(behavior_id)

    for key in (
        "event_behaviors",
        "policy_behaviors",
        "reply_behaviors",
        "meeting_behaviors",
        "action_behaviors",
    ):
        compiled.pop(key, None)
    compiled["actor_behaviors"] = actor_behaviors
    compiled["event_rules"] = event_rules
    compiled["meeting_rules"] = meeting_rules
    compiled["action_rules"] = action_rules
    return compiled


def behavior_group(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    behaviors = data.get(key, [])
    if behaviors is None:
        return []
    if not isinstance(behaviors, list):
        raise ScenarioError(f"{key} must be a list.")
    for behavior in behaviors:
        if not isinstance(behavior, dict):
            raise ScenarioError(f"{key} entries must be objects.")
    return [dict(behavior) for behavior in behaviors]


def match_for_grading_action(action: dict[str, Any]) -> dict[str, Any]:
    match = action.get("match")
    if isinstance(match, dict):
        return {"mode": "concept_match", **match}
    raise ScenarioError("grading rule action must include match.")
