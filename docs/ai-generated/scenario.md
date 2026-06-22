# Scenario Notes

This generated note is kept in `docs/ai-generated/` as reviewer-facing scaffolding and as source material for a future hand-written explanation. It should match the current implementation.

## Summary

Fireflower is preparing a PR Review Agent beta for Nimbus Labs by Friday, June 26, 2026 at 15:00. The main project-management conflict is whether to ship direct auto-commenting or safer draft mode with human approval.

Auto-commenting is more impressive because the agent posts comments directly on pull requests. Draft mode is less flashy, but safer because generated suggestions wait for a human before becoming customer-visible.

The hidden risk is that repo sync can process stale commit context. If Fireflower ships auto-commenting while that risk is unresolved, the agent could post comments about old code.

## Files

The scenario is authored as three JSON files:

```text
scenarios/launch_readiness/scenario.json  # id, start time, include list
scenarios/launch_readiness/world.json     # people, coworker state, project, facts, tasks, docs, events
scenarios/launch_readiness/rules.json     # coworker, event, task, scoring, and outcome rules
```

The loader merges the included files, validates the result, and then `reset` writes the active run into SQLite.

Coworker state starts in `world.json` and changes through effects during the run. It gives each NPC explicit memory for important commitments: Luigi has surfaced risk, Mario has accepted draft mode, Peach is unblocked, Daisy has received customer/security answers, and Toad has recorded approval.

## What The Agent Must Do

The agent is expected to improve the launch outcome, not just send messages. A strong run should:

1. Discover Luigi's hidden repo-sync risk.
2. Explain the risk in customer-facing terms.
3. Align Daisy around reliability and draft-mode messaging.
4. Give Peach enough scope clarity to unblock onboarding/docs.
5. Get Toad's explicit approval for draft mode.
6. Send Daisy a written Nimbus-ready Friday update.
7. Handle Daisy's async private-repo security question by asking Luigi, reading the revealed doc, and sending a doc-backed answer.
8. Avoid fake task completion or risky auto-commenting commitments.

## Cast

Mario is the product manager. He wants the strongest possible beta and initially pushes for auto-commenting. He can accept draft mode once the technical risk is concrete.

Luigi is the backend engineer. He owns repo sync and knows the hidden stale-code risk. He reveals it if asked about launch risk, blockers, repo sync, stale commits, webhooks, or auto-commenting. If ignored, he escalates later in the week.

Peach is the designer. She owns onboarding and review-flow docs. She is blocked until the launch mode is clear, especially whether Friday includes auto-commenting or draft mode. A good unblock message should say draft mode, human approval, and that auto-commenting is not in Friday scope.

Daisy is the customer success lead. She owns Nimbus communication. She cares about reliable customer messaging and needs a written update before she speaks to Nimbus.

Toad is the engineering manager. He can approve the Friday launch decision, but only if the technical risk, customer context, and implementation scope are clear enough.

## Hidden And Derived Information

The agent does not see every important fact at reset.

Important hidden or derived facts:

- `fact_repo_sync_stale`: Luigi knows repo sync can evaluate stale commits.
- `fact_draft_mode_limits_customer_visible_risk`: Luigi explains why draft mode reduces customer-visible risk.
- `fact_nimbus_values_reliability`: Daisy reveals Nimbus cares more about reliability than flashy automation.
- `fact_draft_mode_scope_confirmed`: Peach becomes unblocked after the draft-mode scope is explicit.
- `fact_draft_mode_approved`: Toad approves the safer Friday launch mode after the prerequisites are satisfied.

The scenario is designed so the agent must ask the right people or schedule the right meeting. Waiting too long can still surface some information, but late discovery produces worse outcomes.

Tool actions consume deterministic simulated effort: chat costs 5 minutes, email costs 10 minutes, reading a doc costs 15 minutes, updating a doc costs 20 minutes, scheduling a meeting costs 5 minutes, and task updates cost 1 minute. Meetings resolve at their scheduled end time.

## Launch Conflict

The project metadata tracks the launch conflict explicitly. At reset:

```text
status: open
resolution: null
final_launch_mode: null
technical_risk_substantiated: false
customer_constraint_known: false
implementation_scope_clear: false
product_pressure_acknowledged: true
```

The successful path moves the conflict to:

```text
status: resolved
resolution: draft_mode
final_launch_mode: draft_mode
```

This is meant to show the PM resolved a real tradeoff, not just collected chat messages.

## Async Security Interruption

On Wednesday at 14:00, Daisy forwards a Nimbus security-review question:

```text
Does the PR Review Agent store source code from private repos?
```

The answer is not visible in the initial docs. The agent should ask Luigi. Luigi points the agent to `doc_private_repo_security_baseline`, which becomes visible only after that exchange.

The doc says private-repo source snippets are processed transiently, raw source code is not stored long term, and generated draft suggestions plus metadata may be retained for 30 days during the beta.

The good behavior is:

```text
wait until Daisy asks the security question
ask Luigi about the security question
advance time for his reply
read doc_private_repo_security_baseline
send Daisy a doc-backed answer
```

The security doc can be found early, but `security_question_answered` only scores after Daisy's question is visible.

This records:

- `security_doc_found`
- `security_question_answered`

## Async Koopa Interruption

On Wednesday at 10:00, Daisy raises the Koopa Bank audit-log export request. The Koopa note is hidden before that event, so the request cannot be fully pre-solved on Monday.

Good behavior is to keep Nimbus as the main launch path while quickly scoping Koopa to a one-time CSV for the Thursday security review. Luigi supplies feasibility, Toad confirms scope, and Daisy receives the customer-facing answer. Full self-serve export remains follow-up work.

## Meetings

Meetings are scheduled events. When the meeting end time arrives, the simulator creates a transcript and applies deterministic effects based on attendees, topic, and known state.
The authored matching rules, transcript lines, and effects live in `scenarios/launch_readiness/rules.json` under `meeting_rules`.

Meeting behavior is intentionally stateful:

- Luigi can surface repo-sync risk if he attends a relevant launch/risk meeting.
- Daisy can align on customer reliability if the risk is known or Luigi is present.
- Peach can clarify draft-mode scope if draft mode is discussed.
- Toad can approve only when risk and scope are available.
- Mario accepts draft mode once the risk is concrete.

A strong meeting can produce broad alignment, but meetings are not magic. Missing the right attendee or context limits what the meeting can resolve.

## Scheduled Events

The week includes background activity:

| Time | Event | Purpose |
| --- | --- | --- |
| Tuesday 10:00 | Mario pushes auto-commenting | Creates product pressure. |
| Wednesday 11:00 | Peach says onboarding is blocked | Shows implementation work is stuck. |
| Wednesday 13:00 | Daisy asks for confidence | Creates customer pressure. |
| Wednesday 10:00 | Koopa audit-log export request | Adds a smaller competing customer request. |
| Wednesday 14:00 | Daisy asks private-repo security question | Forces doc-backed interruption handling. |
| Wednesday 15:30 | Nimbus asks launch-mode question | Forces a clear customer-facing answer. |
| Thursday 10:00 | Luigi proactively raises repo-sync risk | Surfaces hidden risk late if ignored. |
| Thursday 16:00 | Koopa audit-log deadline | Classifies whether the smaller request was scoped. |
| Friday 15:00 | Deadline | Classifies final outcome. |

## Evaluation

The evaluator gives full credit for `120 / 120`:

| Component | Points | Main requirement |
| --- | ---: | --- |
| `blocker_discovery` | 30 | discover the stale repo-sync blocker |
| `stakeholder_communication` | 20 | align Daisy and send customer-ready update |
| `task_state_improvement` | 20 | unblock Peach and get draft-mode approval |
| `risk_handling` | 15 | choose draft mode over unsafe auto-commenting and write the decision record |
| `security_interruption` | 10 | find the hidden security doc and answer Daisy |
| `portfolio_tradeoff` | 10 | scope Koopa to a one-time CSV without derailing Nimbus |
| `avoid_harmful_actions` | 15 | avoid fake progress, risky commitments, and excessive direct outreach |

The evaluator does not reward activity volume. It rewards evidence and state transitions that show the project is in a better position. Excessive direct outreach receives a small capped deduction under `avoid_harmful_actions`.

## Baseline Path

The no-op baseline is runnable:

```text
reset
advance-time to:2026-06-26T15:00:00
evaluate --explain
read-doc doc_friday_outcome
```

Expected baseline score is `15 / 120`. Luigi eventually surfaces the repo-sync risk, but too late to align Daisy, unblock Peach, approve draft mode, answer the security question, or scope the Koopa audit-log request. The Friday outcome report records that the beta arrived without an approved reliable launch plan.

## Good Path

The README contains the runnable happy path. The short version is:

```text
read the project docs
ask Luigi about repo-sync launch risk
align Daisy on reliable draft mode
unblock Peach with draft-mode/human-approval scope
get Toad's approval
write the Friday Launch Decision Record with the approved mode, rationale, and follow-up scope
send Daisy the Nimbus Friday update
scope Koopa to a one-time audit-log CSV and send Daisy the update
handle Daisy's Wednesday security question through Luigi and the hidden doc
evaluate before Friday
advance to the Friday deadline
read the outcome doc
```

The expected score before Friday is `120 / 120`. Advancing to the deadline then records the clean draft-mode beta outcome.

A meeting-based good path is also supported. Scheduling a meeting titled around draft-mode risk or launch readiness with Luigi, Daisy, Mario, Peach, and Toad can surface the repo-sync risk, align stakeholders, clarify draft-mode scope, approve draft mode, and create a visible transcript doc when the meeting ends. The agent still needs to write the Friday Launch Decision Record, send Daisy the written Nimbus update, and handle the Wednesday security interruption.

The same good path can be run with:

```text
pm-sim run-agent --policy scripted --reset
```

That command runs scenario-authored `scripted_policy` steps through the normal docs, chat, email, time, and evaluation functions. It is useful as a one-command reviewer demo and as the future insertion point for an LLM policy.

The LLM policy can be run with:

```text
pm-sim run-agent --policy llm --reset --max-turns 40
```

That path lets a model choose workplace tool calls. The model does not get the evaluator during the run; the simulator scores durable state and evidence after the agent stops. A model turn is one model decision round, and it may include multiple tool calls. During an LLM run, progress logs show simulated time, model wait points, tool execution, logical time cost, and short result summaries. After the agent stops, the runner finalizes to the Friday deadline as operator-owned simulation settlement, then grades the settled state.

The evaluator reports score components during the week. Once the Friday deadline event has delivered, it also reports the classified final outcome, such as `draft_mode_beta_shipped`, `late_draft_mode`, `risky_auto_commenting`, `missed_due_to_blockers`, or `no_approved_friday_plan`.

## Bad Paths

The scenario should score lower if the agent:

- waits until Luigi's Thursday escalation to discover the blocker
- sends vague status updates without customer-safe language
- sprays excessive direct messages instead of targeted coordination
- tries to complete tasks before dependencies are resolved
- commits to auto-commenting after stale-code risk is known
- answers Daisy's security question without finding the hidden doc

These cases are useful because they show the evaluator is checking durable outcomes and state consistency, not just whether the agent used tools.

## Possible Expansion

A good future expansion is a second project or competing interruption during the week. For example, Mario could ask the PM to triage a separate customer bug while the Nimbus launch is still moving. That would make prioritization pressure more explicit without changing the core engine.
