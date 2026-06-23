# pm-sim

`pm-sim` is a local project-manager simulation environment. It runs a simulated work week against persistent SQLite state, scheduled events, stateful coworkers, workplace tools, and an evaluator that scores durable project outcomes instead of activity volume.

The repository ships two authored scenarios:

- `scenarios/launch_readiness/`: Fireflower prepares a Friday PR Review Agent beta for Nimbus Labs while handling a smaller Koopa Bank interruption.
- `scenarios/support_inbox_move/`: Poppy moves support from an old shared inbox to a new help desk, with two equal readiness streams: saved replies and VIP email routing.

## Project Structure

Each scenario lives in its own directory.

```text
scenario.yaml      manifest and include list
world.yaml         people, projects, tasks, facts, blockers, docs, events
interactions.yaml  event, policy, reply, meeting, and action behavior rules
evaluation.yaml    scoring, outcome rules, baseline, scripted path
scenario.md        human-readable scenario guide
```

Supporting docs:

- `docs/spec.md`: architecture and runtime semantics
- `docs/scenario_authoring.md`: how to write a new scenario
- `docs/evaluator_semantics.md`: grading model and anti-cheat rules

## Setup

Use Python 3.9+.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[llm]"
python -m unittest discover -s tests
```

Concept matching has two runtime modes:

- `PM_SIM_CONCEPT_MODE=llm`:
  default; LLM-backed, cached, fail-closed
- `PM_SIM_CONCEPT_MODE=local`:
  deterministic local matcher for reproducible no-key review

`OPENAI_API_KEY` is required for `PM_SIM_CONCEPT_MODE=llm` and for `pm-sim run-agent --policy llm`. No-key runs can still be fully scored in `PM_SIM_CONCEPT_MODE=local`.

Coworker replies use deterministic candidate selection in every mode. Set `PM_SIM_COWORKER_MODE=deterministic` for fully local/offline replay. In the default `llm` mode, the model only rephrases the already-selected fallback response using the coworker's `voice`; it cannot choose different facts, effects, approvals, blockers, docs, or dates. Invalid model output falls back to deterministic text.

```bash
cp .env.example .env
```

Set `OPENAI_API_KEY` in `.env`.

Relevant model settings:

- `OPENAI_MODEL`: agent model default
- `PM_SIM_CONCEPT_MODE`: `llm` or `local`
- `PM_SIM_CONCEPT_MODEL`: concept-match model override
- `PM_SIM_COWORKER_MODE`: `deterministic` or `llm`
- `PM_SIM_COWORKER_MODEL`: coworker renderer model override
- `--model`: per-run override for `run-agent` or `ui`

## Quickstart

Reset the scenario:

```bash
pm-sim reset --scenario scenarios/launch_readiness
```

Inspect the starting state:

```bash
pm-sim observe
pm-sim read-doc doc_project_brief
pm-sim timeline --limit 20
```

Run the LLM agent:

```bash
pm-sim run-agent --policy llm --reset --max-turns 80
```

Open the operator UI:

```bash
pm-sim ui --policy llm --max-turns 80
```

Run the scripted reference path:

```bash
PM_SIM_CONCEPT_MODE=local pm-sim run-agent --policy scripted --reset
```

Run the scripted reference path with LLM concept matching:

```bash
pm-sim run-agent --policy scripted --reset
```

That command uses the default `PM_SIM_CONCEPT_MODE=llm`, so it requires `OPENAI_API_KEY`.

Run the second scenario:

```bash
PM_SIM_CONCEPT_MODE=local pm-sim run-agent --policy scripted --scenario scenarios/support_inbox_move --reset
```

Evaluate the current state:

```bash
pm-sim evaluate --explain
```

`evaluate` uses the active scenario recorded in the current DB. Pass `--scenario` only when you intentionally want to override that.

## Expected Results

Baseline, with no meaningful PM work, should score `15 / 120`.

```bash
pm-sim reset
pm-sim advance-time to:2026-06-26T15:00:00
pm-sim evaluate --explain
```

The launch scripted reference path should reach `120 / 120`. The support-inbox scripted reference path should reach `100 / 100`.

The LLM path is not guaranteed to get full score. That is expected. The environment is meant to expose tradeoffs, missed sequencing, and late communication, not guarantee a perfect run.

Use `PM_SIM_CONCEPT_MODE=local` for reproducible no-key scripted review. The local matcher is deterministic and useful for smoke tests, but it is exemplar/token based; robust semantic scoring should use the LLM matcher plus deterministic causal gates.

## Main Commands

Inspect:

```bash
pm-sim observe
pm-sim timeline
```

Work:

```bash
pm-sim read-doc <doc_id>
pm-sim send-chat <person_id> "<body>"
pm-sim send-email <person_id> "<subject>" "<body>"
pm-sim update-doc <doc_id> "<body>"
pm-sim update-task <task_id> --status in_progress
pm-sim schedule-meeting "<title>" <start_iso> <end_iso> <attendee...>
```

Move time:

```bash
pm-sim advance-time 2h
pm-sim advance-time until_next_event
pm-sim advance-time to:2026-06-25T12:00:00
```

Run agents:

```bash
PM_SIM_CONCEPT_MODE=local pm-sim run-agent --policy scripted --reset
pm-sim run-agent --policy llm --reset --max-turns 80
pm-sim ui --policy llm --max-turns 80
```

Global flags can go before or after the subcommand:

```bash
pm-sim --db tmp/demo.sqlite observe
pm-sim observe --db tmp/demo.sqlite
```

## How Scoring Works

The evaluator scores state, not message count.

High-level rubric:

- blocker discovery: did the PM surface the real repo-sync risk?
- stakeholder communication: did Daisy receive durable customer-ready wording?
- task improvement: did approval and scope unblock real work?
- risk handling: did the PM document the decision and close the Thursday readiness loop?
- security interruption: did Daisy get a grounded private-repo answer?
- portfolio tradeoff: did Koopa stay scoped without derailing Nimbus?
- harmful actions: did the PM avoid fake completion, unsafe promises, and noisy outreach?

The full breakdown for the launch scenario is in `scenarios/launch_readiness/scenario.md`. The scoring semantics and anti-cheat invariants are in `docs/evaluator_semantics.md`.

## Design Boundaries

- The simulator owns all mutable run state in SQLite.
- Time advances only through action cost, explicit waiting, meetings, and event delivery.
- Coworkers are deterministic stateful actors, not free-form autonomous LLM agents.
- The agent-facing `observe` tool exposes public workplace state, not private persona internals, hidden event rules, voice hints, or scoring internals.
- LLM use is narrow: concept matching checks whether already-grounded communication contains the authored required ideas and avoids forbidden claims.
- The evaluator awards credit from world state and coworker state, not from raw text alone.

## Documentation Map

- Read `docs/spec.md` to understand runtime semantics and system boundaries.
- Read `docs/scenario_authoring.md` to add or modify scenarios.
- Read `scenarios/launch_readiness/scenario.md` or `scenarios/support_inbox_move/scenario.md` to understand a scenario story, deadlines, and scoring path.
