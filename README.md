# pm-sim

`pm-sim` is a local project-manager simulation environment. The current backend models a simulated SaaS launch week with persistent SQLite state, scheduled events, coworker rules, internal tool surfaces, and an inspectable action/event log.

The first scenario is Mushroom Metrics launching an Executive Health Report for Fireflower CRM's Friday renewal meeting. The project has stakeholder pressure, task dependencies, hidden CRM sync risk, and a full-report versus fallback-report tradeoff.

## Scenario

The included scenario is `launch_readiness`.

Mushroom Metrics is a B2B SaaS company preparing an Executive Health Report for Fireflower CRM's Friday renewal meeting. The full report depends on a flaky CRM enrichment sync, while a fallback report using reliable internal usage and support data is safer but less complete.

The agent's job is to discover the CRM risk, align Mario, Luigi, Peach, Daisy, and Toad, clarify scope, and improve the Friday launch outcome.

## Setup

Use Python 3.9 or newer. No package install is required for the current backend.

```bash
python3 -m unittest discover -s tests
```

Optional LLM settings can be copied later if model-driven wording or rollout support is added:

```bash
cp .env.example .env
```

## Start

Reset the local SQLite state from the scenario:

```bash
python3 -m pm_sim.cli reset --scenario scenarios/launch_readiness.json
```

This creates `data/current.db`, which is ignored by git.

## Commands

Commands print human-readable output by default. Add `--json` before the command for machine-readable output.

Inspect the current visible state:

```bash
python3 -m pm_sim.cli observe
python3 -m pm_sim.cli --json observe
```

Read tasks and docs:

```bash
python3 -m pm_sim.cli list-tasks
python3 -m pm_sim.cli read-doc doc_project_brief
```

Send messages and update work:

```bash
python3 -m pm_sim.cli send-chat luigi "Any CRM sync blockers for launch?"
python3 -m pm_sim.cli send-email daisy "Friday confidence" "I am checking launch risk and will follow up."
python3 -m pm_sim.cli update-task task_launch_decision --status in_progress
python3 -m pm_sim.cli schedule-meeting "Fallback decision" 2026-06-24T10:00:00 2026-06-24T10:30:00 mario luigi daisy toad
```

Inspect scheduled and delivered background events:

```bash
python3 -m pm_sim.cli events
```

Advance simulated time without using wall-clock time:

```bash
python3 -m pm_sim.cli advance-time 2h
python3 -m pm_sim.cli advance-time until_next_event
```

Inspect the action log:

```bash
python3 -m pm_sim.cli log
```

## Evaluation

The deterministic evaluator is the next backend step. It will score the final state and event/action history against the scenario targets in `scenarios/launch_readiness.json`.

The intended command is:

```bash
python3 -m pm_sim.cli evaluate
```

Until that lands, the core reset, observation, time, event delivery, and coworker-rule behavior are covered by:

```bash
python3 -m unittest discover -s tests
```
