# Evaluator Semantics

The evaluator scores durable state, not activity volume.

The intended chain is:

```text
agent action
  -> deterministic causal gates
  -> optional semantic text check
  -> world/coworker state mutation
  -> state-derived milestone
  -> component score
```

This is why a plausible message sent too early does not score. If the PM has not discovered the blocker or secured the approval, the communication rule's `requires` conditions fail and no scoring state changes.

## Source Of Truth

Scored milestones should be represented as state:

- coworker state, such as `daisy.customer_update_received`
- world/project state, such as `project.decision`
- fact/blocker visibility, such as `fact_backfill_checksum_mismatch.visible_at`
- task/blocker status when it reflects a real gate

The evaluator derives scored milestones from `milestone_rules`. Direct `record_milestone` effects are rejected for scored keys by scenario validation.

## Semantic Matching

Semantic matching is intentionally narrow. It answers: "does this already-grounded action communicate the authored required ideas and avoid forbidden claims?"

It does not decide whether the PM deserves credit by itself. Causal gates run first:

- required facts must already be discovered
- required approvals must already be recorded
- required customer interruptions must already be visible
- required coworker state must already exist

The default matcher is deterministic so the documented CLI path works in a fresh clone without OpenAI credentials. `PM_SIM_SEMANTIC_MATCHER=llm` enables cached, fail-closed model-backed phrasing equivalence when an operator wants broader paraphrase handling, but the evaluator still scores database state. The semantic cache key includes the matcher mode and model so deterministic and LLM results cannot contaminate each other inside the same SQLite DB.

## Anti-Cheat Matrix

The tests cover these failure modes:

| Failure mode | Expected behavior |
| --- | --- |
| Perfect-sounding customer email before risk discovery | No customer-ready state mutation |
| Security answer before the customer asks | No security-answer score |
| Fake task completion without required blockers/facts | Rejected by task gates or ignored by outcome |
| Unsafe promise while blocker remains unresolved | Harmful-action component loses credit |
| Busywork outreach and task churn | Does not satisfy state-derived milestones |
| Semantic matcher failure | Fails closed and does not mutate scoring state |

This keeps scoring inspectable: reviewers can trace every point back to state in SQLite.
