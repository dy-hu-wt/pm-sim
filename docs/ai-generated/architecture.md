# Architecture Notes

This file is a short reviewer-facing mirror of `docs/spec.md`. It exists only as a compact secondary note.

## Summary

`pm-sim` is a single-node simulation backend. A scenario reset loads authored YAML into SQLite. Tool actions mutate the run only through explicit effects. Simulated time advances through action cost, waiting, meetings, and event delivery. The evaluator scores durable state, not activity volume.

## Runtime Flow

```text
scenario files
-> SQLite state
-> CLI or UI tool action
-> time advance
-> event delivery
-> effects
-> evaluation
```

## Scenario Layout

```text
scenarios/launch_readiness/scenario.yaml
scenarios/launch_readiness/world.yaml
scenarios/launch_readiness/interactions.yaml
scenarios/launch_readiness/evaluation.yaml
```

## Key Boundaries

- SQLite is the source of truth for an active run.
- Coworkers are deterministic stateful actors.
- LLM use is narrow and limited to concept matching for authored communication checks.
- Scoring comes from world state and coworker state, not raw text alone.
- The browser UI is optional and reads the same backend state as the CLI.

For details, read `docs/spec.md`.
