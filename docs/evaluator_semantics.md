# Evaluator Semantics

The evaluator scores durable state, not activity volume.

The intended chain is:

```text
agent action
  -> deterministic causal gates
  -> optional semantic text check
  -> world/coworker state mutation
  -> state-derived evidence
  -> component score
```

This is why a plausible message sent too early does not score. If the PM has not discovered the blocker or secured the approval, the communication rule's `requires` conditions fail and no scoring state changes.

## Source Of Truth

Scored milestones should be represented as state:

- coworker state, such as `daisy.customer_update_received`
- world/project state, such as `project.decision`
- fact/blocker visibility, such as `fact_backfill_checksum_mismatch.visible_at`
- task/blocker status when it reflects a real gate

The evaluator derives evidence from `state_evidence_rules`. Direct `add_evaluation_evidence` effects are rejected for scored keys by scenario validation.

## Semantic Matching

Semantic matching is intentionally narrow. It answers: "does this already-grounded action communicate the authored required ideas and avoid forbidden claims?"

It does not decide whether the PM deserves credit by itself. Causal gates run first:

- required facts must already be discovered
- required approvals must already be recorded
- required customer interruptions must already be visible
- required coworker state must already exist

The default matcher is deterministic and offline. `PM_SIM_SEMANTIC_MATCHER=llm` can use a cached, fail-closed lightweight model for phrasing equivalence, but the evaluator still scores database state.

## Anti-Cheat Matrix

The tests cover these failure modes:

| Failure mode | Expected behavior |
| --- | --- |
| Perfect-sounding customer email before risk discovery | No customer-ready state mutation |
| Security answer before the customer asks | No security-answer score |
| Fake task completion without required blockers/facts | Rejected by task gates or ignored by outcome |
| Unsafe promise while blocker remains unresolved | Harmful-action component loses credit |
| Busywork outreach and task churn | Does not satisfy state-derived evidence |
| Semantic matcher failure | Fails closed and does not mutate scoring state |

This keeps scoring inspectable: reviewers can trace every point back to state in SQLite.
