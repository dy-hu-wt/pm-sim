# Writing a Scenario

A scenario is one PM work week. It should start from a believable Monday morning state, hide some important information, force tradeoffs during the week, and let the evaluator measure whether the PM actually improved the outcome.

Start with the story, not the YAML. Pick a believable week shape: one main project with an interruption, two equal workstreams, or another small PM coordination problem. Keep the first version narrow enough that a reviewer can explain the people, hidden facts, deadlines, and scoring path in a few minutes.

## File Layout

Each scenario lives in its own directory.

```text
scenarios/<scenario_id>/
  scenario.md
  scenario.yaml
  world.yaml
  interactions.yaml
  evaluation.yaml
```

`scenario.yaml` is just the manifest.

```yaml
id: launch_readiness
name: PR Review Agent Beta Launch Readiness
company: Fireflower
start_time: "2026-06-22T09:00:00"
timezone: America/New_York
include:
  - world.yaml
  - interactions.yaml
  - evaluation.yaml
```

`scenario.md` is the human guide. Keep the executable logic in YAML and the explanation in prose.

## World State

`world.yaml` describes Monday at 09:00. It should contain only what already exists at reset time.

Typical sections:

- `people`
- `coworker_state`
- `projects`
- `pressures`
- `facts`
- `tasks`
- `dependencies`
- `blockers`
- `docs`
- `events`

Use readable IDs in authored YAML. The loader keeps runtime IDs stable while allowing scenario files to stay legible.

Use hidden facts and hidden docs when the PM should have to ask the right person or wait for the right interruption.

Use `coworker_state` for durable actor memory, not for every detail in the world. Good uses are things like:

- Daisy received a customer-ready update
- Toad recorded approval
- Peach is unblocked
- Luigi surfaced the risk

Use `pressures` for mutable stakeholder pressure that should rise or fall during the week.

## Interaction Rules

`interactions.yaml` contains behavior rules that mutate or schedule simulated world state. Keep evaluation and scoring rules out of this file.

Top-level groups:

- `event_behaviors`: scheduled interruptions and deadline-time behavior, such as customer questions, leadership nudges, and project settlement events.
- `policy_behaviors`: proactive coworker behavior when time passed or a dependency is still missing.
- `reply_behaviors`: direct replies to agent chat and email. This is the main place where the PM learns information through conversation.
- `meeting_behaviors`: useful meeting outcomes. A meeting should not be magical. It should work because the right people attended, the topic was relevant, and the prerequisites were satisfied.
- `action_behaviors`: action-triggered authored checks that do not belong in reply or meeting logic, such as “the decision record was written with the required grounded content.”

Keep these groups convergent. If chat, email, and meetings can all teach the same fact, they should all land on the same fact ID or state key.

## Matching

There are two kinds of matching.

Deterministic matching routes stable behavior. It is used for coworker reply candidates, meetings, and similar authored rules where you want predictability.

Coworker replies always start from authored candidates. Candidate selection is deterministic in every mode. `PM_SIM_COWORKER_MODE=llm` is the default and lets a model rephrase the already-selected fallback text using `people[].behavior.voice`. `PM_SIM_COWORKER_MODE=deterministic` renders the selected fallback text locally for offline replay. In both modes, the model cannot add effects or make unsupported facts true. Keep each candidate's effects complete and deterministic because they remain the source of truth. `voice` is private runtime authoring data; the agent should infer coworker style from actual messages, not from `observe`.

`concept_match` is used when an action’s wording matters. It should be used narrowly. It is not the scorer by itself. It only answers whether an already-grounded action communicates the authored required ideas and avoids forbidden ones.

Runtime modes:

- `PM_SIM_CONCEPT_MODE=llm`:
  default; LLM-backed, cached, fail-closed
- `PM_SIM_CONCEPT_MODE=local`:
  deterministic local matcher for reproducible review and CI

The local matcher is a review aid, not the quality bar for nuanced language. Keep causal gates deterministic, keep concepts small, and use LLM matching when the wording itself needs semantic judgment.

The safe pattern is:

```text
deterministic prerequisites first
concept match second
state mutation third
scoring from state last
```

Do not award points directly from raw text.

## Evaluation

`evaluation.yaml` should describe what good PM work changes in the world.

Prefer scoring these things:

- blocker discovered
- approval recorded
- customer owner updated
- blocked implementation unblocked
- risky scope deferred
- interruption handled without overcommitting
- readiness confirmed before deadline

Use state-derived milestones. A scored milestone should be traceable to project state, fact visibility, blocker state, or coworker state.

Avoid scoring busywork such as:

- sending many messages
- editing a task without causal justification
- writing nice-sounding text before the PM has enough information

## Authoring Checklist

Before calling a scenario done, verify these questions:

- Can the PM learn each critical fact through more than one reasonable path?
- Does each major score component map to durable state?
- Are there clear deadlines and visible consequences for missing them?
- Do coworkers have distinct roles and different useful information?
- Can a bad but plausible PM strategy fail for concrete reasons?
- Does the test suite cover at least one good path and a few bad paths?

## Working Loop

Author a little, then reset and test:

```bash
pm-sim reset --scenario scenarios/<scenario_id>
python -m unittest discover -s tests
```

If the scenario is hard to explain in a short `scenario.md`, it is usually too tangled in YAML too.
