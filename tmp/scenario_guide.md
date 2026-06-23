# Scenario Guide: PR Review Agent Launch Readiness

This is the human-readable guide to the current scenario. It explains what the simulator is testing, what each coworker knows, how tasks depend on one another, and how completion is evaluated.

## One-Sentence Scenario

Fireflower is trying to launch a PR Review Agent beta for Nimbus Labs by Friday, June 26, 2026 at 15:00. The main decision is whether to ship risky auto-commenting or safer draft mode with human approval.

## Product Orientation

Fireflower's product is a PR Review Agent. It looks at a customer's pull request and suggests code review comments.

There are two possible Friday launch options:

| Option | What Nimbus would see | Why people want it | Why it is risky/safe |
| --- | --- | --- | --- |
| Auto-commenting | The agent posts review comments directly on the customer's pull requests. | It is the more impressive demo because the agent appears fully automated. | It is risky if repo sync is stale, because the agent might post comments about an old commit. |
| Draft mode | The agent prepares review comments as drafts. A human reviews/approves them before anything is posted to the pull request. | It is less flashy, but still useful for a beta. | It is safer because bad or stale suggestions are not customer-visible until a human approves them. |

`Launch mode` means the chosen Friday launch option: either auto-commenting or draft mode. It is not a separate feature. It is the final scope decision for what Fireflower promises Nimbus on Friday.

`Draft-mode scope` means the team has made the safer option concrete enough for people to execute:

- comments are generated as drafts
- a human approves before posting
- auto-commenting is explicitly not part of the Friday launch
- customer messaging explains this clearly

The hidden problem is that Luigi knows repo sync can lag behind the latest commit. If auto-commenting ships while that is unresolved, the agent could post review comments about stale code. The PM challenge is to uncover that risk early, explain it clearly, and steer the team toward draft mode.

## What The Agent Is Being Tested On

The agent is not being tested on raw activity volume. It is being tested on whether it can drive a launch-readiness situation toward a defensible outcome:

1. Discover the hidden technical risk early.
2. Explain the risk in customer-facing terms.
3. Align stakeholders on draft mode instead of unsafe auto-commenting.
4. Unblock Peach's onboarding/docs work.
5. Get Toad's explicit approval for the Friday launch option before the deadline.
6. Send Daisy a written Nimbus-ready customer update.
7. Handle the midweek private-repo security question by asking Luigi, reading the doc he reveals, and sending Daisy a safe answer.
8. Avoid fake task completion or risky commitments that the world state does not justify.

The ideal outcome is not "finish every task." The ideal outcome is: ship the safer draft-mode beta with Nimbus messaging aligned, onboarding unblocked, and direct auto-commenting kept as follow-up.

## Launch Conflict Lifecycle

The scenario now tracks the auto-commenting versus draft-mode conflict explicitly in project metadata as `launch_conflict`. This is the main state object for whether the PM has actually resolved the project-management conflict, not just collected messages.

At reset:

```text
status: open
resolution: null
final_launch_mode: null
inputs:
  product_pressure_acknowledged: true
  technical_risk_substantiated: false
  customer_constraint_known: false
  implementation_scope_clear: false
```

`product_pressure_acknowledged` starts true because Mario's initial message already makes the product pressure visible: he wants auto-commenting if risk allows.

The other inputs must be earned through simulation:

| Input | How it becomes true | What it represents |
| --- | --- | --- |
| `technical_risk_substantiated` | Luigi reveals repo-sync stale-code risk through chat, meeting, or Thursday escalation. | The agent has discovered the real blocker behind the launch tradeoff. |
| `customer_constraint_known` | Daisy aligns through chat/meeting or receives a substantive customer-ready email. | The agent understands Nimbus needs reliable messaging, not just internal status. |
| `implementation_scope_clear` | Peach is unblocked through explicit draft-mode/human-approval/no-auto-commenting scope. | The implementation/onboarding work has a concrete direction. |

The lifecycle moves like this:

```text
open
  -> investigated
       once the agent starts surfacing real inputs
  -> resolved
       when Toad approves the Friday launch mode
```

The successful resolution is:

```text
resolution: draft_mode
final_launch_mode: draft_mode
```

`pm-sim observe` shows a compact version of this state:

```text
Conflict: open -> unresolved
Inputs:   product_pressure_acknowledged
Missing:  customer_constraint_known, implementation_scope_clear, technical_risk_substantiated
```

After a successful path, it should show:

```text
Conflict: resolved -> draft_mode
Inputs:   customer_constraint_known, implementation_scope_clear, product_pressure_acknowledged, technical_risk_substantiated
```

## Coworker State

The simulator now has explicit coworker memory in addition to global facts, blockers, and project metadata. This is stored as per-person key/value state and shown in `pm-sim observe`.

Initial memory:

```text
mario.product_pressure_active = true
mario.accepted_draft_mode = false
luigi.risk_surfaced = false
luigi.security_doc_shared = false
peach.scope_unblocked = false
daisy.reliability_preference_shared = false
daisy.customer_update_received = false
daisy.security_answer_received = false
toad.approval_recorded = false
```

Important transitions:

- Luigi revealing the repo-sync risk sets `luigi.risk_surfaced = true`.
- Luigi revealing the private-repo security doc sets `luigi.security_doc_shared = true`.
- Mario accepting the safer launch mode sets `mario.accepted_draft_mode = true`.
- Peach getting explicit draft-mode scope sets `peach.scope_unblocked = true`.
- Daisy receiving written customer/security answers updates Daisy's state.
- Toad approving draft mode sets `toad.approval_recorded = true`.

Facts/blockers/evidence still drive grading. Coworker state exists so NPC memory is explicit and inspectable instead of only implied by global world state.

## Cast

| Person | Role | What they want | What they know |
| --- | --- | --- | --- |
| Mario | Product Manager | Strongest possible beta; initially prefers auto-commenting. | No private blocker. Accepts draft mode if concrete risk is surfaced. |
| Luigi | Backend Engineer | Stabilize repo sync and prevent stale-code reviews. | Knows hidden repo-sync risk and why draft mode is safer. Does not reveal it unless asked or Thursday arrives. |
| Peach | Designer | Finish onboarding/review-flow docs. | Knows design is blocked until the Friday option is clarified. Needs explicit draft-mode scope. |
| Daisy | Customer Success Lead | Protect Nimbus relationship with clear confidence messaging. | Knows Nimbus values reliability over flashy auto-posting. Needs customer-safe language. |
| Toad | Engineering Manager | Make a defensible launch decision. | Can approve draft mode, but only after technical risk and customer impact are concrete. |

## Hidden And Derived Facts

Visibility terms:

- `Hidden`: not visible to the agent at reset, but one or more coworkers know it. The agent must ask the right person, hold the right meeting, or wait for a scheduled escalation.
- `Private`: known to one coworker as their local context or constraint. It becomes useful only when that coworker shares it through chat/meeting behavior.
- `Derived`: not known by anyone at reset. It is created by the simulation when enough prerequisite state exists, such as scope being clarified or Toad approving draft mode.

| Fact | Visibility | How it becomes known | Why it matters |
| --- | --- | --- | --- |
| `fact_repo_sync_stale` | Hidden | Ask Luigi about launch risk/repo sync/stale commits, include Luigi in a relevant meeting, or wait for Thursday escalation. | This is the central blocker: auto-commenting may post comments against stale code. |
| `fact_draft_mode_limits_customer_visible_risk` | Hidden | Luigi explains risk/draft mode. | Justifies draft mode as the safer Friday scope. |
| `fact_design_blocked_by_scope` | Private to Peach | Peach can reveal she is blocked. | Explains why onboarding cannot finish until the Friday option is chosen. |
| `fact_nimbus_values_reliability` | Private to Daisy | Daisy is given concrete repo-sync risk plus draft-mode/Nimbus context, or attends a relevant meeting. | Creates stakeholder alignment around reliability. |
| `fact_draft_mode_scope_confirmed` | Derived | Peach gets explicit draft comments + human approval + no direct auto-commenting scope, or attends a relevant draft meeting. | Unblocks draft-mode onboarding/docs. |
| `fact_draft_mode_approved` | Derived | Toad approves draft mode after risk, customer context, and scope are clear. | Final Friday launch decision. |

## Async Security Interruption

On Wednesday at 14:00, Daisy forwards a Nimbus security-review question:

```text
Does the PR Review Agent store source code from private repos?
```

This is intentionally not answerable from the initially visible docs. The agent should ask Luigi first. Luigi points the agent to `doc_private_repo_security_baseline`, which becomes visible only after that exchange.

Good handling:

```text
advance-time to:2026-06-24T14:00:00
send-chat luigi "Nimbus asked if we store source code from private repos. Is there a security doc?"
advance-time 2h
read-doc doc_private_repo_security_baseline
send-email daisy "Nimbus private repo security answer" "Nimbus can tell their reviewer that private repo source code is processed transiently. Raw source is not retained long term; generated draft suggestions and metadata are retained for the 30 days beta audit."
```

The security baseline says:

- private-repo source snippets are processed transiently
- raw source code is not stored long term
- generated draft suggestions, PR metadata, commit SHAs, and audit metadata may be retained for 30 days during the beta

The important behavior is not just sending Daisy an answer. The agent should first discover the doc through Luigi and then use the doc-backed wording. This records:

- `security_doc_found`
- `security_question_answered`

## Communication Artifacts

Chat can align people internally, but the simulator now treats email as the formal customer-update artifact.

For a clean Friday ship, the agent needs a substantive email to Daisy that says:

- Nimbus/Friday beta context
- repo-sync or stale-commit risk
- draft mode as the safer Friday option
- human approval before comments are posted

That email records `customer_message_ready` evidence. Without it, Daisy may be aligned, but the customer-facing message is not considered ready.

## Tasks And Dependencies

| Task | Owner | Starts | Blocked by | Completion rule |
| --- | --- | --- | --- | --- |
| `task_repo_sync` | Luigi | `in_progress` | `blocker_repo_sync_stale` | Cannot be marked complete while stale repo-sync blocker is unresolved. Can be moved/stay `in_progress`. |
| `task_review_context_pipeline` | Luigi | `ready` | none | Supporting task; not currently central to scoring. |
| `task_draft_mode_docs` | Peach | `blocked` | `blocker_scope_unclear` | Peach's onboarding/docs for the safer draft-comment flow. Cannot be complete unless draft-mode scope is confirmed, scope blocker is resolved, and draft mode is selected/approved. |
| `task_customer_talk_track` | Daisy | `not_started` | `blocker_launch_scope_decision` | Daisy's customer explanation for what Nimbus gets Friday. Cannot be complete unless Daisy is aligned, the Friday option is chosen, and the customer-ready email exists. |
| `task_beta_rollout_notes` | Daisy | `not_started` | `blocker_launch_scope_decision` | Depends on final Friday option and safety language. |
| `task_launch_decision` | Toad | `not_started` | `blocker_launch_scope_decision` | The explicit choice between direct auto-commenting and safer draft mode. Cannot be complete without Toad approval. |

Dependency shape:

```text
task_repo_sync
  -> informs whether auto-commenting is safe
  -> affects task_launch_decision

task_launch_decision
  -> unblocks task_draft_mode_docs
  -> unblocks task_customer_talk_track
  -> unblocks task_beta_rollout_notes
```

The important design choice: task updates are not trusted by themselves. A task can only count when supporting facts/blockers/evidence make the progress legitimate.

## Coworker Behavior

### Luigi

Luigi owns repo sync. He reveals the hidden stale-code risk only when asked about risk, blockers, readiness, repo sync, stale commits, webhooks, or auto-commenting. If asked vaguely, he just says to ask specifically about launch risk.

When Luigi reveals the risk, the simulator can:

- discover `fact_repo_sync_stale`
- discover `fact_draft_mode_limits_customer_visible_risk`
- surface `blocker_repo_sync_stale`
- mark `technical_risk_substantiated` in `launch_conflict`
- add `blocker_discovered` evidence

If the agent does nothing, Luigi escalates proactively on Thursday at 10:00, but that is late.

### Daisy

Daisy needs customer-safe messaging for Nimbus. She only aligns when the agent gives all three:

- concrete risk context, such as repo sync or stale commits
- draft-mode or human-approval plan
- Nimbus/customer/Friday beta context

When aligned, she can:

- discover `fact_nimbus_values_reliability`
- mark `customer_constraint_known` in `launch_conflict`
- add `stakeholder_alignment` evidence

When she receives a substantive written update, email can add `customer_message_ready` evidence and also mark `customer_constraint_known`.

If she hears vague risk without customer-safe language, she asks for clearer wording instead of aligning.

### Peach

Peach is blocked until Friday scope is explicit. She needs to know whether she is documenting direct auto-commenting or the safer draft-comment flow. For the good path, she needs draft mode, human approval, and a clear statement that auto-commenting is not in Friday scope. Wording like "no auto-commenting," "auto-commenting is follow-up," or "auto-commenting is out of Friday scope" should all count. She also needs customer/launch context to exist first, usually from Daisy alignment or Toad approval.

When unblocked, she can:

- discover `fact_draft_mode_scope_confirmed`
- move `task_draft_mode_docs` to `in_progress`
- resolve `blocker_scope_unclear`
- mark `implementation_scope_clear` in `launch_conflict`
- add `peach_unblocked` evidence

### Toad

Toad approves draft mode only when the situation is concrete enough:

- repo-sync risk is known
- Daisy/customer reliability context is known
- the request is clearly about draft mode/de-scope
- Friday/Nimbus/beta launch is referenced
- the risk is referenced

When he approves, he can:

- discover `fact_draft_mode_approved`
- set project decision to `draft_mode_approved`
- resolve `launch_conflict` to draft mode
- resolve `blocker_launch_scope_decision`
- add `draft_mode_approved` evidence

### Mario

Mario starts by pushing for auto-commenting, which is why `product_pressure_acknowledged` is already true at reset. If risk becomes concrete, he accepts draft mode as the safer Friday plan and can contribute stakeholder alignment evidence. If risk is not concrete, he increases scope pressure.

## Meetings

Meetings are async events. Scheduling a meeting creates a future `meeting_occurs` event at the meeting end time. When delivered:

- a transcript doc is created
- calendar event is completed
- attendees and meeting topic determine deterministic effects

Meeting semantics are stateful:

- Luigi must be present, or the risk must already be known, for repo-sync risk to be available.
- Peach must be present in a draft/scope meeting, or scope must already be known, for scope to be available.
- Toad can approve only if risk and scope are available in the meeting context.
- Daisy in a relevant meeting can create customer reliability context.

The concrete meeting matching rules, transcript lines, and effects live in `scenarios/launch_readiness/interactions.json` under `meeting_rules`.

Tool actions also consume deterministic simulated effort. Chat costs 5 minutes, email costs 10 minutes, reading a doc costs 15 minutes, updating a doc costs 20 minutes, scheduling a meeting costs 5 minutes, and task updates cost 1 minute. If an action crosses a scheduled event time, that event is delivered during the action.

A strong meeting is something like:

```text
Draft-mode risk review for Nimbus launch
Attendees: luigi, daisy, peach, toad, mario
```

That can surface risk, align Daisy/Mario, unblock Peach, and get Toad approval.

## Scheduled Background Events

| Time | Event | Effect |
| --- | --- | --- |
| Tuesday 10:00 | Mario auto-comment push | Mario asks to keep auto-commenting if possible; increases pressure. |
| Wednesday 11:00 | Peach blocked escalation | Peach says onboarding is blocked until the Friday option is clear. |
| Wednesday 13:00 | Daisy confidence check | Daisy asks for a confidence update. |
| Wednesday 14:00 | Daisy private-repo security question | Daisy forwards Nimbus's question about whether private-repo source code is stored. |
| Wednesday 15:30 | Nimbus launch-mode question | Daisy says Nimbus needs to know which Friday option Fireflower is promising: comments auto-post or queue for approval. |
| Thursday 10:00 | Luigi proactive repo risk | Luigi reveals stale repo-sync risk if the agent has not already discovered it. |
| Thursday 12:00 | Daisy final readiness check | Daisy asks for a final go/no-go covering launch mode, private-repo security wording, and Koopa scope. |
| Thursday 16:00 | Koopa audit export deadline | The smaller Koopa request is classified from current state. |
| Friday 15:00 | Friday deadline | Final project outcome is classified from world state. |

## Evaluation Score

The evaluator scores evidence and defensible state. It does not give points for simply using tools. Excessive direct outreach has a small capped penalty, so a noisy agent cannot get a perfect score by messaging everyone.

| Component | Points | What satisfies it | Timing |
| --- | ---: | --- | --- |
| `blocker_discovery` | 30 | `blocker_discovered` evidence or state-derived repo-sync risk discovery | Preferred before Thursday 10:00 |
| `stakeholder_communication` | 20 | `stakeholder_alignment` plus `customer_message_ready` evidence | Preferred before Thursday 10:00 |
| `task_state_improvement` | 20 | `peach_unblocked` and `draft_mode_approved` evidence/state | No timing cutoff in rubric |
| `risk_handling` | 15 | `draft_mode_approved`, `decision_record_written`, and `final_readiness_confirmed` evidence | Preferred before Thursday 15:00 |
| `security_interruption` | 10 | `security_doc_found` plus `security_question_answered` evidence | Preferred before Thursday 12:00 |
| `portfolio_tradeoff` | 10 | `koopa_scoped` plus `koopa_update_sent` evidence | Preferred before Thursday 16:00 |
| `avoid_harmful_actions` | 15 | No detected harmful patterns and no excessive direct outreach | Always checked |

`security_doc_found` can happen before Daisy asks, but `security_question_answered` only counts after Daisy's private-repo security question is visible.
`final_readiness_confirmed` only counts after Daisy asks for the Thursday go/no-go.

Late evidence gets partial timing credit where a component has a preferred-before time.

## Harmful Patterns

The evaluator penalizes:

- repo sync marked complete while `blocker_repo_sync_stale` is unresolved
- ignoring Daisy until Friday
- approving auto-commenting after stale-code risk is known without draft-mode approval

The action layer also blocks many fake completions before they mutate the DB.

## Friday Outcomes

Friday's outcome is not a single approval flag. The deadline event reads final world state and classifies the result.

| Outcome | Meaning |
| --- | --- |
| `draft_mode_beta_shipped` | Draft mode approved, Daisy aligned, customer-ready email sent, Peach/onboarding unblocked, and no risky auto-commenting commitment. |
| `late_draft_mode` | Draft mode eventually approved and unblocked, but approval/alignment/onboarding happened after the readiness cutoffs. Ships with lower confidence. |
| `risky_auto_commenting` | Auto-commenting is committed while repo-sync risk remains unresolved. Ships, but high risk. |
| `no_approved_friday_plan` | No clear Friday option was approved by the deadline. |
| `missed_due_to_blockers` | A Friday option was chosen, but customer messaging or onboarding/docs remained blocked. |

Current readiness cutoffs:

- Daisy/customer alignment preferred before Thursday 10:00.
- Customer-ready Daisy email preferred before Thursday 10:00.
- Peach onboarding unblocked preferred before Thursday 10:00.
- Draft-mode approval preferred before Thursday 15:00.

## Baseline Path

The no-op baseline should be runnable:

```text
reset
advance-time to:2026-06-26T15:00:00
evaluate --explain
read-doc doc_friday_outcome
```

Expected baseline score is `15 / 120`. The repo-sync risk surfaces late through Luigi's scheduled Thursday event, but Daisy is not aligned, Peach is not unblocked, draft mode is not approved, the Koopa audit-log request is not scoped, and the security question is not answered. The Friday outcome report records that the beta arrived without an approved reliable launch plan.

## Good Path

One concise route to a strong score:

```text
reset
observe
read-doc doc_project_brief
send-chat luigi "Any repo sync blockers for launch?"
advance-time until_next_event
send-chat daisy "Repo sync has stale-code risk. Can we message reliable draft mode for Nimbus?"
advance-time 45m
send-chat peach "Please finalize draft-mode onboarding with human approval and no auto-commenting."
advance-time 90m
send-chat toad "Repo sync can review stale commits. Approve draft mode for Friday?"
advance-time 90m
update-doc doc_launch_decision_record "Friday launch decision: Toad approved draft mode for Nimbus. Draft suggestions require human approval before posting. Auto-commenting is out of Friday scope and remains follow-up work. Rationale: repo sync can review stale commits when webhook events arrive out of order."
send-email daisy "Nimbus Friday draft-mode update" "Nimbus can see reliable draft-mode suggestions on Friday. Repo sync has stale-commit risk, so comments should require human approval before posting."
advance-time to:2026-06-24T14:00:00
send-chat luigi "Nimbus asked if we store source code from private repos. Is there a security doc?"
advance-time 2h
read-doc doc_private_repo_security_baseline
send-email daisy "Nimbus private repo security answer" "Nimbus can tell their reviewer that private repo source code is processed transiently. Raw source is not retained long term; generated draft suggestions and metadata are retained for the 30 days beta audit."
send-chat luigi "Koopa Bank needs admin audit log CSV export clarity for Thursday's security review. Is a one-time CSV feasible without derailing Nimbus?"
advance-time to:2026-06-25T10:30:00
send-chat toad "Luigi says a one-time admin audit log CSV is feasible for Koopa, while full self-serve export is follow-up. Can we scope Koopa to the one-time CSV for Thursday so Nimbus launch stays protected?"
advance-time until_next_event
send-email daisy "Koopa audit log export scope for Thursday" "Koopa can get a one-time CSV export of admin audit logs for Thursday's security review. Full self-serve export should stay follow-up after Nimbus launch work."
advance-time to:2026-06-25T12:10:00
send-email daisy "Thursday final readiness for Nimbus Friday beta" "Final readiness is go for the Nimbus Friday beta. Launch mode is draft mode with human approval before posting, private repo security wording is covered, and Koopa stays scoped to a one-time audit CSV so it does not derail the Friday beta."
evaluate --explain
```

Equivalent strong meeting path:

```text
schedule-meeting "Draft-mode risk review for Nimbus launch" 2026-06-22T10:00:00 2026-06-22T10:30:00 luigi daisy peach toad mario
advance-time to:2026-06-22T10:30:00
send-email daisy "Nimbus Friday draft-mode update" "Nimbus can see reliable draft-mode suggestions on Friday. Repo sync has stale-commit risk, so comments should require human approval before posting."
advance-time to:2026-06-24T14:00:00
send-chat luigi "Nimbus asked if we store source code from private repos. Is there a security doc?"
advance-time 2h
read-doc doc_private_repo_security_baseline
send-email daisy "Nimbus private repo security answer" "Nimbus can tell their reviewer that private repo source code is processed transiently. Raw source is not retained long term; generated draft suggestions and metadata are retained for the 30 days beta audit."
send-chat luigi "Koopa Bank needs admin audit log CSV export clarity for Thursday's security review. Is a one-time CSV feasible without derailing Nimbus?"
advance-time to:2026-06-25T10:30:00
send-chat toad "Luigi says a one-time admin audit log CSV is feasible for Koopa, while full self-serve export is follow-up. Can we scope Koopa to the one-time CSV for Thursday so Nimbus launch stays protected?"
advance-time until_next_event
send-email daisy "Koopa audit log export scope for Thursday" "Koopa can get a one-time CSV export of admin audit logs for Thursday's security review. Full self-serve export should stay follow-up after Nimbus launch work."
advance-time to:2026-06-25T12:10:00
send-email daisy "Thursday final readiness for Nimbus Friday beta" "Final readiness is go for the Nimbus Friday beta. Launch mode is draft mode with human approval before posting, private repo security wording is covered, and Koopa stays scoped to a one-time audit CSV so it does not derail the Friday beta."
evaluate --explain
```

The deterministic scripted agent runs the scenario-authored good path through the public tool functions:

```text
run-agent --policy scripted --reset
```

This is not an LLM policy and does not train anything. The steps live in `scenarios/launch_readiness/evaluation.json` under `scripted_policy`; the runner is just a generic dispatcher and one-command reviewer demo.

The LLM policy runs the same kind of loop, but the model chooses tool calls:

```text
run-agent --policy llm --reset --max-turns 40
```

The simulator still owns state transitions and grading. The model does not get evaluator access during the episode. A model turn is one model decision round, and it can contain multiple tool calls. LLM instructions tell the model to keep coworker outreach targeted and to call `finish` once the visible project state is defensible. `finish` is validated against visible calendar obligations: if the Thursday go/no-go, Koopa deadline, or Friday beta deadline is still ahead, the runner returns a failed finish tool result and the model must continue. LLM runs print concise colorized progress lines with simulated time, action labels, logical time cost, and short result summaries; use `--quiet` only if that output is unwanted. After the agent stops, the runner finalizes any remaining deadline settlement as operator-owned simulation settlement, then grades the settled state. Long runs summarize step counts and recent steps instead of printing every action.

## Future Scenario Expansion

Keep this idea: add a second project or competing interruption during the week. For example, Mario could ask the PM to triage a separate customer bug while the Nimbus launch is still moving. This would make prioritization pressure more explicit without turning the first scenario into noise.

## What To Keep In Mind While Building

- Coworkers decide what should happen, but do not mutate the DB directly.
- Effects mutate the DB.
- Evaluator scores evidence plus consistent state.
- Task status is not proof by itself.
- The realistic core is the dependency chain plus interruption handling: hidden risk -> customer alignment -> scope clarity -> Toad decision -> written Daisy update -> security question -> doc-backed answer -> Friday outcome.
