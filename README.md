# pm-sim

`pm-sim` is a local project-manager simulation environment. The current backend models a simulated SaaS launch week with persistent SQLite state, scheduled events, coworker rules, internal tool surfaces, and an inspectable action/event log.

The first scenario is Fireflower launching a PR Review Agent beta for Nimbus Labs. The project has stakeholder pressure, task dependencies, hidden repo-sync risk, and an auto-commenting versus draft-mode tradeoff.

## Scenario

The included scenario is `launch_readiness`.

Fireflower is a B2B SaaS company preparing a PR Review Agent beta for Nimbus Labs. The full beta would auto-post review comments on pull requests, but that depends on repo sync always using the latest commit. A safer draft mode prepares suggestions for human approval before comments are posted. During the week, Nimbus asks whether comments will auto-post, and Daisy needs rollout language before she updates them.

The agent's job is to discover the stale-code risk, align Mario, Luigi, Peach, Daisy, and Toad, clarify scope, and improve the Friday launch outcome.

## Setup

Use Python 3.9 or newer. Create a virtualenv and install the repo in editable mode so the `pm-sim` command is available:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

Then run the tests:

```bash
python -m unittest discover -s tests
```

Optional LLM settings can be copied later if model-driven wording or rollout support is added:

```bash
cp .env.example .env
```

## Start

Reset the local SQLite state from the scenario:

```bash
pm-sim reset --scenario scenarios/launch_readiness.json
```

This creates `data/current.db`, which is ignored by git.

## Quick Happy Path

This is the shortest successful path through the scenario. It demonstrates discovery, stakeholder alignment, draft-mode approval, evaluation, and the Friday deadline outcome; it is not meant to exhaust the whole simulated week.

```bash
pm-sim reset
pm-sim observe
pm-sim read-doc doc_project_brief
pm-sim read-doc doc_beta_rollout_template

pm-sim send-chat luigi "Any repo sync blockers or launch risks for Nimbus?"
pm-sim advance-time 2h

pm-sim send-chat daisy "Repo sync has stale-code risk. Can we message reliable draft mode for Nimbus?"
pm-sim advance-time 45m

pm-sim send-chat peach "Please finalize draft-mode onboarding with human approval and no auto-commenting."
pm-sim advance-time 90m

pm-sim send-chat toad "Repo sync can review stale commits. Approve draft mode for Friday?"
pm-sim advance-time 90m

pm-sim send-email daisy "Nimbus Friday draft-mode update" "Nimbus can see reliable draft-mode suggestions on Friday. Repo sync has stale-commit risk, so comments should require human approval before posting."

pm-sim evaluate

pm-sim advance-time to:2026-06-26T15:00:00
pm-sim read-doc doc_friday_outcome
```

Expected evaluation result before the Friday deadline: `100 / 100`. The important evidence is recorded through delivered coworker reply events plus the final Daisy email: `blocker_discovered`, `stakeholder_alignment`, `customer_message_ready`, `peach_unblocked`, and `draft_mode_approved`. Advancing to Friday then records the final project outcome.

The path demonstrates good PM behavior by turning a hidden technical risk into a clear launch tradeoff, aligning the customer-facing owner, unblocking implementation work, and getting an explicit decision before the deadline.

## Commands

Commands print human-readable output by default. Add `--json` before the command for machine-readable output.

Inspect the current visible state:

```bash
pm-sim observe
pm-sim --json observe
```

Read tasks and docs:

```bash
pm-sim list-tasks
pm-sim read-doc doc_project_brief
pm-sim read-doc doc_beta_rollout_template
```

Send messages and update work:

```bash
pm-sim send-chat luigi "Any repo sync blockers for launch?"
pm-sim send-email daisy "Nimbus Friday draft-mode status" "Repo sync has stale-commit risk. I recommend reliable draft mode for Friday with human approval before posting."
pm-sim update-task task_launch_decision --status in_progress
pm-sim schedule-meeting "Draft-mode decision" 2026-06-24T10:00:00 2026-06-24T10:30:00 mario luigi daisy toad
```

Inspect scheduled and delivered background events:

```bash
pm-sim events
```

Advance simulated time without using wall-clock time:

```bash
pm-sim advance-time 2h
pm-sim advance-time until_next_event
```

Inspect the action log:

```bash
pm-sim log
```

Inspect the combined action, event, message, and evidence timeline:

```bash
pm-sim timeline
pm-sim timeline --limit 20
```

## Evaluation

Run the deterministic evaluator against the current SQLite state:

```bash
pm-sim evaluate
pm-sim evaluate --explain
pm-sim --json evaluate
```

The score comes from the rubric in `scenarios/launch_readiness.json`. It rewards outcomes and state improvements, not raw tool usage or activity volume. Evidence must show that the agent improved the project: discovering blockers, aligning stakeholders, unblocking real work, approving a defensible draft-mode launch, and avoiding harmful state.

Task updates are checked against the surrounding world state to resist reward hacking. For example, marking repo sync complete while the stale-code blocker is unresolved is penalized, and draft-mode onboarding progress only counts when draft-mode scope is confirmed and the scope blocker is resolved.

The backend is covered by:

```bash
python -m unittest discover -s tests
```
