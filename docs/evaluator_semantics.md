# Evaluator Semantics

The evaluator scores durable state, not activity volume.

The intended chain is:

```text
agent action
  -> deterministic causal gates
  -> optional concept text check
  -> world/coworker state mutation
  -> state-derived milestone
  -> component score
```

This is why a plausible message sent too early does not score. If the PM has not discovered the blocker or secured the approval, the matching `action_check.requires` conditions fail and no scoring state changes.

## Source Of Truth

Scored milestones should be represented as state:

- coworker state, such as `daisy.customer_update_received`
- world/project state, such as `project.decision`
- fact/blocker visibility, such as `fact_repo_sync_stale.visible_at`
- task/blocker status when it reflects a real gate

The evaluator derives scored milestones from `milestone_rules`. Direct `record_milestone` effects are rejected for scored keys by scenario validation.

`action_checks` are the authoring shortcut for grounded actions. They compile into action matching, evidence promotion, and milestone derivation rules, so scenario authors do not have to repeat that boilerplate for every scored email or document update.

## Concept Matching

Concept matching is intentionally narrow. It answers: "does this already-grounded action communicate the authored required ideas and avoid forbidden claims?"

It does not decide whether the PM deserves credit by itself. Causal gates run first:

- required facts must already be discovered
- required approvals must already be recorded
- required customer interruptions must already be visible
- required coworker state must already exist

Concept matching has two runtime modes. `PM_SIM_CONCEPT_MODE=llm` is the default: it is LLM-backed, requires `OPENAI_API_KEY`, returns per-concept booleans and rationales, and fails closed on missing credentials, invalid output, missing concept IDs, or contradictory top-level results. `PM_SIM_CONCEPT_MODE=local` is a deterministic fallback for reproducible local review and CI. It is intentionally not a robust semantic grader; it uses authored exemplars and token overlap, so it can miss good paraphrases or accept wording that is close to the examples. In both modes, a match records `action_evidence`; separate deterministic promotion rules re-check causal gates before mutating coworker state or pressure. The evaluator still scores database state. The concept-match cache key includes matcher mode, model, criteria, text, and rule id, so LLM and local results cannot contaminate one another.

## Anti-Cheat Matrix

The tests cover these failure modes:

| Failure mode | Expected behavior |
| --- | --- |
| Perfect-sounding customer email before risk discovery | No customer-ready state mutation |
| Security answer before the customer asks | No security-answer score |
| Fake task completion without required blockers/facts | Rejected by task gates or ignored by outcome |
| Unsafe promise while blocker remains unresolved | Harmful-action component loses credit |
| Busywork outreach and task churn | Does not satisfy state-derived milestones |
| Concept matcher failure | Fails closed and does not mutate scoring state |

This keeps scoring inspectable: reviewers can trace every point back to state in SQLite.
