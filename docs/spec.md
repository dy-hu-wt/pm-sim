# Project Manager Simulation Environment

This file is the concise architecture note for the current implementation.

## Core Model

The simulator runs a work week against one SQLite database. A scenario reset loads authored YAML, validates references, and writes the initial world state into SQLite. From that point on, SQLite is the source of truth for the run.

The main loop is:

```text
scenario files -> SQLite state -> tool action -> time advance -> event delivery -> effects -> evaluation
```

The agent never writes state directly. Every action goes through the simulator layer and produces explicit effects.

## Time

Simulated time is independent from wall-clock latency. Model inference can take seconds or minutes without moving the work week.

Time moves only when:

- a tool action has a fixed effort cost,
- the operator or agent explicitly advances time,
- a scheduled meeting reaches its end time,
- due events are delivered.

Current logical action costs:

```text
send_chat        5m
send_email      10m
read_doc        15m
update_doc      20m
schedule_meeting 5m
update_task      1m
```

Coworker reply delays are separate from action cost and only count inside authored coworker availability windows.

## Tool Surfaces

The simulator exposes workplace tools rather than direct storage operations:

- chat
- email
- docs
- tasks
- calendar / meetings
- timeline / observation
- evaluation

These are different interfaces over the same state.

## State Ownership

The scenario authors:

- people
- coworker state
- projects
- tasks and dependencies
- blockers
- docs
- scheduled events
- reply rules
- proactive policies
- meeting rules
- action-triggered rules
- evaluation rules

The runtime owns:

- current simulated time
- delivered messages
- delivered events
- visibility timestamps
- task/project/blocker mutations
- action logs
- evaluation evidence

`visibility_scope` is authored metadata. `visible_at` is runtime state. Facts, blockers, and docs can exist in the world before they become visible to the PM.

## Coworkers

Coworkers are stateful deterministic actors. They are not free-form LLM agents.

Each coworker can have:

- role
- goals
- response delay
- availability
- current focus
- mutable memory in `coworker_state`
- direct reply rules
- proactive policy rules

This gives the simulator realism without making grading depend on unconstrained model behavior.

## Events

Asynchronous activity is modeled through a deterministic event queue.

Examples:

- coworker replies
- customer interruptions
- deadline events
- meeting transcript generation
- proactive coworker nudges

When time advances, the engine delivers all due events in deterministic order and applies their effects.

## Effects And Conditions

The runtime uses two generic interpreters:

- conditions decide whether a rule can fire
- effects mutate state

That keeps scenario-specific logic in YAML instead of Python branches. The same effect system handles direct actions, coworker replies, proactive policies, scheduled events, and meetings.

Coworker replies are generated as structured candidates first. Each candidate has authored conditions, fallback text, and effects. By default the runtime selects candidates deterministically. With `PM_SIM_COWORKER_MODE=llm`, a model may choose and rephrase only from the allowed candidate IDs. The engine validates the selected IDs and applies only the selected candidates' authored effects; model text cannot create facts, resolve blockers, complete tasks, or award score.

## Evaluation

The evaluator scores durable state, not surface activity.

The intended chain is:

```text
action
-> deterministic prerequisites
-> optional concept check
-> world/coworker state mutation
-> milestone derivation
-> component score
```

LLM use is intentionally bounded. Concept matching checks whether a grounded communication action expresses the authored required ideas and avoids forbidden ones. Optional coworker LLM mode chooses and phrases among already-valid coworker response candidates. Neither path decides project truth, task completion, blocker status, or final score by itself.

Scoring comes from state such as:

- discovered facts
- resolved blockers
- project decisions
- task movement behind real gates
- coworker state showing that the right recipient actually received the update

## Scenario Layout

Each scenario is split by concern:

```text
scenario.yaml      manifest
world.yaml         initial state
events.yaml        scheduled interruption behavior
policies.yaml      proactive coworker behavior
replies.yaml       direct reply behavior
meetings.yaml      meeting behavior
actions.yaml       action-triggered authored checks
evaluation.yaml    scoring, outcomes, baseline, scripted path
scenario.md        human-readable guide
```

This is the current answer to “how does this scale?” The engine stays generic; scenario semantics live in authored data.

## Operator Surfaces

The CLI is the primary interface.

Useful commands:

```text
pm-sim reset
pm-sim observe
pm-sim timeline
pm-sim read-doc
pm-sim evaluate --explain
pm-sim run-agent --policy scripted --reset
pm-sim run-agent --policy llm --reset --max-turns 80
pm-sim ui --policy llm --max-turns 80
```

The browser UI is optional. It reads the same SQLite state and advances the same backend event queue. It does not own separate simulation state.
