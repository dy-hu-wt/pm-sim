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

## Quick Happy Path

This is the shortest successful path through the scenario. It demonstrates discovery, stakeholder alignment, fallback approval, evaluation, and the Friday deadline outcome; it is not meant to exhaust the whole simulated week.

```bash
python3 -m pm_sim.cli reset
python3 -m pm_sim.cli observe
python3 -m pm_sim.cli read-doc doc_project_brief

python3 -m pm_sim.cli send-chat luigi "Any CRM sync blockers or launch risks for Fireflower?"
python3 -m pm_sim.cli advance-time 2h

python3 -m pm_sim.cli send-chat daisy "CRM sync is risky. Can we message a reliable fallback for Fireflower?"
python3 -m pm_sim.cli advance-time 45m

python3 -m pm_sim.cli send-chat peach "Please finalize the fallback using usage and support data without CRM fields."
python3 -m pm_sim.cli advance-time 90m

python3 -m pm_sim.cli send-chat toad "CRM vendor sync is timing out. Approve fallback report for Friday?"
python3 -m pm_sim.cli advance-time 90m

python3 -m pm_sim.cli evaluate

python3 -m pm_sim.cli advance-time to:2026-06-26T15:00:00
python3 -m pm_sim.cli read-doc doc_friday_outcome
```

Expected evaluation result before the Friday deadline: `100 / 100`. The important evidence is recorded through delivered coworker reply events: `blocker_discovered`, `stakeholder_alignment`, `peach_unblocked`, and `fallback_approved`. Advancing to Friday then records the final project outcome.

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
python3 -m pm_sim.cli send-email daisy "Fireflower Friday fallback status" "CRM sync has vendor timeout risk. I recommend a reliable fallback for Friday using usage and support data."
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

Run the deterministic evaluator against the current SQLite state:

```bash
python3 -m pm_sim.cli evaluate
python3 -m pm_sim.cli --json evaluate
```

The score comes from the rubric in `scenarios/launch_readiness.json`. It rewards outcomes and state improvements, not raw tool usage or activity volume. Evidence must show that the agent improved the project: discovering blockers, aligning stakeholders, unblocking real work, approving a defensible fallback, and avoiding harmful state.

Task updates are checked against the surrounding world state to resist reward hacking. For example, marking CRM enrichment complete while the CRM blocker is unresolved is penalized, and fallback design progress only counts when fallback scope is confirmed and the scope blocker is resolved.

The backend is covered by:

```bash
python3 -m unittest discover -s tests
```
