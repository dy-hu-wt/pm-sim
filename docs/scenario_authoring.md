# Scenario Authoring

`pm-sim` scenarios are data-authored directories. The engine owns storage, time, tools, event delivery, condition evaluation, effects, scoring, and timelines. Scenario YAML owns the company setup, coworker behavior, deadlines, scoring targets, and scripted demo path.

## Required Files

Each scenario directory has this shape:

```text
scenarios/<scenario_id>/
  scenario.yaml      # manifest and includes
  world.yaml         # seeded company/project state
  interactions.yaml  # coworker, event, meeting, and action behavior
  evaluation.yaml    # grading, gates, outcomes, baseline, scripted path
```

`scenario.yaml` is intentionally small:

```yaml
id: billing_migration
name: Billing Migration Readiness
company: Fireflower
start_time: "2026-06-22T09:00:00"
timezone: America/New_York
include:
  - world.yaml
  - interactions.yaml
  - evaluation.yaml
```

Use `pm-sim reset --scenario scenarios/<scenario_id>` to validate and load it.

## World State

`world.yaml` defines the persistent starting state:

- `people`: coworkers, roles, goals, constraints, availability, private knowledge, and behavior notes.
- `coworker_state`: mutable actor memory such as `risk_shared`, `approval_recorded`, or `customer_update_received`.
- `actor_goals`, `actor_workload`, and `actor_commitments`: optional first-class runtime actor
  state for deterministic NPC complexity. If omitted, goals and workload are seeded from
  `people[].goals` and `people[].behavior.current_focus`.
- `projects`: active workstreams with status, risk, stakeholder pressure, deadline, and metadata.
- `facts`: discoverable information, often hidden until chat, email, docs, or events reveal it.
- `tasks` and `dependencies`: visible PM work, owners, due dates, blockers, and priorities.
- `blockers`: known or hidden risks with severity, owner, status, and `visible_at`.
- `docs`: readable or hidden documents.
- `messages`: initial chat/email context.
- `events`: scheduled background events, coworker interruptions, and project deadlines.

Visibility is standardized with nullable `visible_at`: if it is `null`, the item exists in the world but is not visible to the agent yet.

## Interactions

`interactions.yaml` defines how coworkers and background dynamics change the world. Reply behavior
and proactive coworker behavior live in one `actor_behaviors` list:

- `actor_behaviors`: the reusable actor model. `kind: "reply"` entries respond to chat/email;
  `kind: "policy"` entries fire from time plus state. Both use the shared condition and effect
  languages.
- `event_rules`: effects applied when scheduled non-actor events are delivered.
- `meeting_rules`: transcript lines and effects applied when a meeting resolves.
- `action_rules`: effects applied when an agent action matches causal conditions and optional
  semantic criteria.

`actor_behaviors` is the single scenario surface for coworker replies and proactive actor behavior.

```yaml
id: daisy_customer_wording_nudge
kind: policy
person_id: daisy
trigger:
  at: "2026-06-25T09:30:00"
when:
  - not:
      coworker_state:
        person_id: daisy
        key: customer_message_ready
        equals: true
effects:
  - type: create_message
    channel: email
    sender_id: daisy
    recipient_id: agent
    subject: Customer wording risk
    body: I still need written customer-ready wording.
```

## Evaluation

`evaluation.yaml` defines grading and reviewer/demo behavior:

- `agent_brief`: scenario-specific operating guidance for LLM-driven runs, including objective,
  timing guidance, finish criteria, and optional tool hints.
- `grading_rules`: reusable templates that compile into action rules plus state-derived evidence.
- `state_evidence_rules`: how evaluator evidence is derived from coworker/world state.
- `task_gate_rules`: prevents task completion before required state exists.
- `harmful_action_rules`: harmful or suspicious world states the evaluator should penalize.
- `outcome_rules`: classifies project outcome at deadline.
- `evaluation_targets`: point allocation and evidence requirements.
- `baseline`: no-op reference path.
- `scripted_policy`: deterministic demo path using normal tools.

Keep engine behavior out of `agent_brief`. It should orient the agent to the scenario's visible
PM objective and durable-work expectations; it should not reveal hidden facts or evaluator keys.

Effects are reusable state mutations:

```yaml
- type: discover_fact
  fact_id: fact_backfill_checksum_mismatch
- type: update_coworker_state
  person_id: toad
  key: stage_approved
  value: true
- type: update_actor_workload
  person_id: daisy
  load_level: high
- type: add_actor_commitment
  person_id: daisy
  description: Send customer update.
- type: update_project
  project_id: project_billing_migration
  decision: staged_shadow_mode
- type: create_message
  channel: email
  sender_id: daisy
  recipient_id: agent
  body: ...
```

## Multiple Valid Paths

A scenario should not require one golden sequence. Author alternate tool paths by making different surfaces converge on the same state:

- Chat rules can reveal information quickly through `coworker_reply` events.
- Email rules can reveal the same information with email subjects/replies and longer action cost.
- Meeting rules can combine attendees and topic terms, create a transcript, and apply multiple effects at the scheduled end time.

For example, the billing migration scenario allows Luigi to reveal backfill risk by chat, by email, or in a meeting. All three paths set `luigi.backfill_risk_shared = true` and reveal `fact_backfill_checksum_mismatch`, so the evaluator sees the same grounded state rather than a channel-specific checklist.

Meetings must be at least 10 minutes long. This keeps meetings distinct from instant chat while still allowing them to resolve several stakeholder inputs at once when the right people attend.

## Grading Template

Use `grading_rules` for scored communication. A grading rule says: only after prerequisites are true, a matching action mutates coworker state; the evaluator then derives evidence from that state.

```yaml
id: atlas_customer_update
template: grounded_communication
requires:
  - fact_discovered: fact_backfill_checksum_mismatch
  - project_decision:
      project_id: project_billing_migration
      equals: staged_shadow_mode
action:
  type: send_email
  recipient_id: daisy
  match:
    mode: semantic
    intents:
      - id: staged_shadow
        description: The message says the migration will use staged shadow mode.
        signals:
          - staged shadow
          - shadow mode
      - id: invoice_correctness
        description: The message explains the invoice correctness or checksum rationale.
        signals:
          - invoice correctness
          - checksum
    require_all:
      - staged_shadow
      - invoice_correctness
state:
  person_id: daisy
  key: atlas_update_received
  value: true
evidence:
  key: atlas_update_sent
  note: Daisy received grounded Atlas customer wording by email.
```

This keeps scoring causal:

- The agent must discover the risk first.
- The agent must get the decision first.
- The matching email changes Daisy's state.
- The evaluator scores Daisy's state, not raw message text.

## Evaluator Invariant

Scored evidence must be state-derived. The scenario validator rejects direct `add_evaluation_evidence` effects for keys listed in `evaluation_targets`.

Allowed:

```yaml
type: update_coworker_state
person_id: daisy
key: customer_update_received
value: true
```

Then:

```yaml
evidence_key: customer_update_sent
when:
  - coworker_state:
      person_id: daisy
      key: customer_update_received
      equals: true
created_at:
  coworker_state:
    person_id: daisy
    key: customer_update_received
```

Not allowed for scored keys:

```yaml
type: add_evaluation_evidence
key: customer_update_sent
```

## Adding A Scenario Without Python

1. Create `scenarios/<id>/scenario.yaml`, `world.yaml`, `interactions.yaml`, and `evaluation.yaml`.
2. Seed at least one project, several coworkers, hidden facts, blockers, tasks, docs, messages, and deadline events.
3. Add actor behaviors that reveal private facts, mutate coworker state, and model proactive pressure.
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
