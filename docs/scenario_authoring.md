# Scenario Authoring

`pm-sim` scenarios are data-authored directories. The engine owns storage, time, tools, event delivery, condition evaluation, effects, scoring, and timelines. Scenario JSON owns the company setup, coworker behavior, deadlines, scoring targets, and scripted demo path.

## Required Files

Each scenario directory has this shape:

```text
scenarios/<scenario_id>/
  scenario.json   # manifest and includes
  world.json      # seeded company/project state
  rules.json      # behavior, grading, outcomes, scripted path
```

`scenario.json` is intentionally small:

```json
{
  "id": "billing_migration",
  "name": "Billing Migration Readiness",
  "company": "Fireflower",
  "start_time": "2026-06-22T09:00:00",
  "timezone": "America/New_York",
  "include": ["world.json", "rules.json"]
}
```

Use `pm-sim reset --scenario scenarios/<scenario_id>` to validate and load it.

## World State

`world.json` defines the persistent starting state:

- `people`: coworkers, roles, goals, constraints, availability, private knowledge, and behavior notes.
- `coworker_state`: mutable actor memory such as `risk_shared`, `approval_recorded`, or `customer_update_received`.
- `projects`: active workstreams with status, risk, stakeholder pressure, deadline, and metadata.
- `facts`: discoverable information, often hidden until chat, email, docs, or events reveal it.
- `tasks` and `dependencies`: visible PM work, owners, due dates, blockers, and priorities.
- `blockers`: known or hidden risks with severity, owner, status, and `visible_at`.
- `docs`: readable or hidden documents.
- `messages`: initial chat/email context.
- `events`: scheduled background events, coworker interruptions, and project deadlines.

Visibility is standardized with nullable `visible_at`: if it is `null`, the item exists in the world but is not visible to the agent yet.

## Rules

`rules.json` defines how the world changes:

- `coworker_rules`: deterministic replies by person and channel. Rules can depend on hidden facts, visible facts, and coworker state.
- `event_rules`: effects applied when scheduled events are delivered.
- `action_rules`: effects applied when an agent action matches causal conditions and optional semantic criteria.
- `grading_rules`: reusable templates that compile into action rules plus state-derived evidence.
- `state_evidence_rules`: how evaluator evidence is derived from coworker/world state.
- `task_gate_rules`: prevents task completion before required state exists.
- `outcome_rules`: classifies project outcome at deadline.
- `evaluation_targets`: point allocation and evidence requirements.
- `scripted_policy`: deterministic demo path using normal tools.

Effects are reusable state mutations:

```json
{"type": "discover_fact", "fact_id": "fact_backfill_checksum_mismatch"}
{"type": "update_coworker_state", "person_id": "toad", "key": "stage_approved", "value": true}
{"type": "update_project", "project_id": "project_billing_migration", "decision": "staged_shadow_mode"}
{"type": "create_message", "channel": "email", "sender_id": "daisy", "recipient_id": "agent", "body": "..."}
```

## Grading Template

Use `grading_rules` for scored communication. A grading rule says: only after prerequisites are true, a matching action mutates coworker state; the evaluator then derives evidence from that state.

```json
{
  "id": "atlas_customer_update",
  "template": "grounded_communication",
  "requires": [
    {"fact_discovered": "fact_backfill_checksum_mismatch"},
    {"project_decision": {"project_id": "project_billing_migration", "equals": "staged_shadow_mode"}}
  ],
  "action": {
    "type": "send_email",
    "recipient_id": "daisy",
    "required_semantics": [
      {"description": "staged shadow mode", "signals_any": ["staged shadow", "shadow mode"]},
      {"description": "invoice correctness rationale", "signals_any": ["invoice correctness", "checksum"]}
    ]
  },
  "state": {
    "person_id": "daisy",
    "key": "atlas_update_received",
    "value": true
  },
  "evidence": {
    "key": "atlas_update_sent",
    "note": "Daisy received grounded Atlas customer wording by email."
  }
}
```

This keeps scoring causal:

- The agent must discover the risk first.
- The agent must get the decision first.
- The matching email changes Daisy's state.
- The evaluator scores Daisy's state, not raw message text.

## Evaluator Invariant

Scored evidence must be state-derived. The scenario validator rejects direct `add_evaluation_evidence` effects for keys listed in `evaluation_targets`.

Allowed:

```json
{"type": "update_coworker_state", "person_id": "daisy", "key": "customer_update_received", "value": true}
```

Then:

```json
{
  "evidence_key": "customer_update_sent",
  "when": [
    {"coworker_state": {"person_id": "daisy", "key": "customer_update_received", "equals": true}}
  ],
  "created_at": {"coworker_state": {"person_id": "daisy", "key": "customer_update_received"}}
}
```

Not allowed for scored keys:

```json
{"type": "add_evaluation_evidence", "key": "customer_update_sent"}
```

## Adding A Scenario Without Python

1. Create `scenarios/<id>/scenario.json`, `world.json`, and `rules.json`.
2. Seed at least one project, several coworkers, hidden facts, blockers, tasks, docs, messages, and deadline events.
3. Add coworker rules that reveal private facts and mutate coworker state.
4. Add `grading_rules` or `state_evidence_rules` so score comes from state.
5. Add `outcome_rules` for project deadlines.
6. Add a `baseline.commands` block and a `scripted_policy`.
7. Run:

```bash
pm-sim reset --scenario scenarios/<id>
pm-sim advance-time to:2026-06-26T15:00:00
pm-sim evaluate --scenario scenarios/<id> --explain
pm-sim run-agent --policy scripted --reset --scenario scenarios/<id>
python -m unittest discover -s tests
```

If those commands pass without Python changes, the scenario is using the reusable engine surface.
