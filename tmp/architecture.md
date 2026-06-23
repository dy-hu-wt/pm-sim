# Architecture Notes

This is a short working map of the simulator layers. Keep it updated when core flow changes.

For the product scenario itself, read `tmp/scenario_guide.md`.

## Layer Model

```text
scenario JSON -> SQLite schema/state -> CLI -> actions -> engine/time -> actor/event rules -> effects -> evaluator
```

## Files

- `scenarios/launch_readiness/scenario.json`
  - Scenario manifest: identity, start time, and includes for `world.json`, `interactions.json`, and `evaluation.json`.

- `scenarios/launch_readiness/world.json`
  - Authored starting world: people, project, tasks, blockers, facts, docs, initial messages, and scheduled events.

- `scenarios/launch_readiness/interactions.json`
  - Authored interaction rules: actor replies, autonomous actor policies, background event rules, meeting rules, and action-derived effects.

- `scenarios/launch_readiness/evaluation.json`
  - Authored grading rules: agent brief, task gates, state evidence rules, harmful-action rules, outcome rules, and scoring rubric.
  - Scenario load validates references before DB reset: owners, projects, dependencies, event payload project IDs, duplicate IDs, event times, and evaluation target shape.

- `pm_sim/schema.sql`
  - Database structure only. It defines tables; it does not define scenario content.

- `pm_sim/state.py`
  - Reset/load/observe layer.
  - `reset` applies `schema.sql`, loads scenario JSON into SQLite, and records the starting action log entry.
  - Stores authored rule sets such as `actor_behaviors`, `action_rules`, `event_rules`, `meeting_rules`, task gates, state evidence rules, outcome rules, and scripted demo steps in scenario data so the active DB owns scenario-specific deterministic behavior.

- `pm_sim/cli.py`
  - Thin command router. Parses arguments, calls actions/state/time/evaluator functions, formats output.

- `pm_sim/actions.py`
  - Agent/operator tools: chat, email, read/update docs, task updates, meetings.
  - Actions are synchronous. They write immediate records and may schedule future events.
  - Agent action costs are fixed. Chat reply delays are scheduled as working minutes inside the recipient coworker's authored availability windows.
  - Task completion is constrained by world state. For example, repo sync cannot be completed while its blocker is unresolved, draft-mode docs need scope plus approval, and launch/customer tasks need the relevant stakeholder decisions.

- `pm_sim/engine/time.py`
  - Simulated clock and event delivery.
  - Time only changes through `advance_time`.
  - Due events and due autonomous coworker policies are delivered in deterministic order.
  - `actor_behaviors` with `kind: "policy"` can fire when time crosses a trigger and their state conditions match. They are used for deterministic autonomy: proactive nudges, escalations, and memory updates that do not depend on the agent asking the right question.
  - The Friday deadline event classifies the final project outcome from world state: decision, stakeholder alignment, onboarding readiness, blocker status, timing, and risky auto-commenting commitments.

- `pm_sim/engine/rules.py`
  - Shared deterministic rule interpreter for action rules, actor replies, and meeting matching.
  - Owns text normalization, trigger terms, required/absent fact checks, causal `when` condition checks, priority ordering, and semantic-match handoff.

- `pm_sim/engine/runtime_config.py`
  - Loads active scenario rule sets from SQLite state.
  - Gives callers already-filtered or ordered rules such as action rules, event rules, meeting rules, outcome rules, task gates, response delays, and actor behavior lists.

- `pm_sim/engine/conditions.py`
  - Shared condition language for task gates, action rules, event rules, outcome rules, and evaluator explanations.

- `pm_sim/agents/finalize.py`
  - Shared run-harness finalization.
  - After an agent policy stops taking actions, the runner advances the world to the scenario deadline before final evaluation. This keeps the agent stop reason separate from the final Friday outcome.

- `pm_sim/coworkers.py`
  - Deterministic scenario behavior.
  - Coworker rules decide what effects should happen, but do not mutate the database directly.
  - Structured `actor_behaviors` with `kind: "reply"` handle direct chat/email replies.
  - Structured `actor_behaviors` with `kind: "policy"` handle autonomous coworker behavior over time/state.
  - Structured `action_rules` handle action-derived email/doc effects.
  - Structured `event_rules` handle proactive background event effects.
  - Structured `meeting_rules` handle meeting transcript lines and coordination effects.

- `pm_sim/engine/effects.py`
  - Shared mutation layer for event/coworker outputs.
  - Applies effect dictionaries such as `create_message`, `discover_fact`, `update_blocker`, `update_task`, `create_doc`, and `add_evaluation_evidence`.

- `pm_sim/evaluator.py`
  - Scores outcomes from evidence plus final state.
  - Rewards project improvement, not raw activity volume.
  - Before the Friday deadline, it can report readiness score without a final outcome.
  - After the Friday deadline event delivers, it includes the classified final outcome from project metadata.

## Evidence

`evaluation_evidence` rows are score-relevant receipts.

Example:

```text
evidence_key: blocker_discovered
note: Luigi disclosed stale repo sync risk.
created_at: 2026-06-22T11:00:00
source: event:event_coworker_reply_6
```

The evaluator checks evidence keys such as:

- `blocker_discovered`
- `stakeholder_alignment`
- `peach_unblocked`
- `draft_mode_approved`

Some evidence is explicit in `evaluation_evidence`. Some is derived from consistent state, such as task progress only counting when the relevant fact is known and blocker is resolved.

## Typical Flow

```text
reset
  -> schema creates tables
  -> scenario JSON loads initial world into DB

send-chat luigi "Any launch risk?"
  -> actions.py inserts agent message
  -> coworkers.py returns deterministic reply content/effects
  -> actions.py schedules the coworker reply event inside Luigi's working hours

advance-time until_next_event
  -> engine/time.py delivers the next due event or autonomous actor policy
  -> engine/effects.py creates Luigi message, discovers fact, surfaces blocker, records evidence

evaluate
  -> evaluator.py reads evidence and state
  -> scores against the scenario rubric
```

Agent runs add one final harness step after the action loop:

```text
agent stops
  -> finalize_to_deadline advances to project deadline as operator-owned settlement
  -> Friday deadline event writes final outcome
  -> evaluator grades the settled Friday state
```

## Maintenance Rule

When adding a new simulator behavior, keep this boundary clear:

```text
actions/engine time decide when something happens
actor/event rules decide what should happen
engine effects mutate the database
evaluator scores only evidence and defensible state
```
