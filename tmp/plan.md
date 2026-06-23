# Implementation Plan

This file is local scratch planning and is intentionally git ignored.

## Technology Choices

- Language: Python
- Storage: SQLite through Python's standard `sqlite3`
- CLI: Python standard `argparse`
- Tests: standard-library `unittest`
- Scenario data: JSON to avoid adding YAML dependencies
- UI: defer until backend works
- API server: defer unless needed
- LLM usage: defer until deterministic backend works; never use for v1 grading or state transitions

Keep dependencies close to zero. The implementation should be easy for a reviewer to clone and run.

## Build Order

### 1. Project Skeleton

- Create `pm_sim/` package
- Create `scenarios/` directory
- Create `tests/` directory if we add tests
- Add `README.md`
- Add `pyproject.toml` or keep simple module commands

Goal: reviewer can see the intended project shape before logic is complex.

### 2. SQLite Schema

Create the minimum tables:

- `sim_state`
- `people`
- `projects`
- `tasks`
- `dependencies`
- `blockers`
- `messages`
- `calendar_events`
- `docs`
- `events`
- `action_log`
- optional later: `eval_results`

Goal: make the world model persistent and inspectable.

### 3. Scenario Seed

Create `scenarios/launch_readiness/scenario.json` with `world.json`, `interactions.json`, and `evaluation.json` includes.

Seed:

- Mushroom Metrics company context
- Executive Health Report project
- Fireflower CRM Friday renewal meeting
- Mario, Luigi, Peach, Daisy, Toad
- tasks, dependencies, docs, initial messages
- hidden CRM sync blocker known by Luigi
- scheduled background events

Goal: `reset` creates the same starting state every time.

### 4. Reset And Observe

Implement:

```text
pm-sim reset
pm-sim observe
pm-sim log
```

Goal: local operator can reset and inspect state before any agent actions.

### 5. Tool Actions

Implement action handlers:

```text
send_chat
send_email
list_tasks
update_task
read_doc
schedule_meeting
```

Each action should:

- validate inputs
- mutate state through the simulator layer
- write to `action_log`
- schedule follow-up events if needed

Goal: agent interacts through tools, not direct DB writes.

### 6. Event Queue And Time

Implement:

```text
advance_time 2h
advance_time until_next_event
advance_time to "Tuesday 09:00"
```

Rules:

- simulated time only changes through `advance_time`
- process due events in deterministic order
- write delivered events and state changes to logs

Goal: show decoupled simulated time and async background activity.

### 7. Coworker Rules

Implement deterministic behavior for:

- Luigi replies about CRM sync risk after delay
- Daisy applies customer pressure and asks for confidence
- Mario pushes for full report unless risk is made clear
- Peach is blocked until scope/export requirements are clarified
- Toad can approve fallback/de-scope if given clear risk

Goal: multiple stateful coworkers with realistic delays and goals.

### 8. Evaluation

Implement `pm-sim evaluate`.

Initial scoring:

- blocker discovered and addressed
- stakeholders updated early
- Peach unblocked / requirements clarified
- fallback or de-scope decision made before deadline
- harmful behavior avoided

Output should show score components and evidence from state/history.

Goal: grading is explainable and resists superficial activity.

### 9. Baseline Rollout

Implement a baseline/no-op evaluation path or document the baseline trajectory.

Goal: make improved outcome comparison obvious.

### 10. README Demo Flow

Add commands a reviewer can run:

```text
reset
observe
read docs/tasks
send chat
advance time
inspect logs
evaluate
```

Goal: clone, run, understand main flow quickly.

### 11. Tests

Add focused tests for:

- reset determinism
- simulated time not moving during actions
- due events delivered on advance
- coworker reply scheduling
- evaluator scoring known good and bad paths

Goal: prove the core semantics do not regress.

Initial `unittest` coverage now exists for reset, hidden information, time advancement, event delivery, and coworker rule output.

### 12. Optional UI/API

Only after backend is solid:

- lightweight read-only UI
- FastAPI wrapper
- richer observability

Do not start here.

### 13. Optional LLM Integration

Only after reset, tools, event delivery, coworker rules, and evaluation work deterministically.

Possible uses:

- generate more natural coworker message wording from fixed facts
- run an automated agent rollout through the CLI/tool layer
- summarize logs for reviewer convenience

Do not use the LLM to decide:

- hidden facts
- event timing
- state transitions
- scoring
- pass/fail outcomes

Environment setup:

```text
cp .env.example .env
# edit .env and set OPENAI_API_KEY
```

Goal: keep the simulator stable and explainable, while leaving a clean path for model-driven demos later.
