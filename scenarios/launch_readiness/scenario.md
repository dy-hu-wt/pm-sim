# PR Review Agent Beta Launch Readiness

This is the source-of-truth scenario guide. The YAML files define the executable state; this document explains the story, expected dynamics, and how the work can be solved.

## Story

Fireflower is preparing a Friday beta of the PR Review Agent for Nimbus Labs. Mario wants the strongest launch: automatic PR comments. Luigi knows repo sync can sometimes review stale commits when webhook events arrive out of order. The safer Friday option is draft mode: the agent prepares suggestions, but a human approves before anything is posted.

The PM must discover the risk, align the team on a defensible Friday mode, keep Nimbus communication grounded, unblock onboarding/docs, and handle Koopa Bank's smaller audit-log export request without derailing Nimbus.

![Week calendar](../../docs/assets/week-calendar.svg)

## Cast

| Person | Role | What they know or need |
| --- | --- | --- |
| Mario | Product lead | Wants auto-commenting unless the risk is concrete. Will accept draft mode when stale-code risk is substantiated. |
| Luigi | Backend owner | Knows the hidden repo-sync stale-commit risk. Also knows private-repo security details and Koopa one-time CSV feasibility. |
| Peach | Design/onboarding | Blocked until Friday scope is explicit: draft mode, human approval, no auto-commenting on Friday. |
| Daisy | Customer success | Needs written customer-ready wording for Nimbus and Koopa. Values reliable customer promises over flashy scope. |
| Toad | Engineering manager | Can approve the Friday launch mode and Koopa scope, but only when risk, customer impact, and implementation scope are clear. |

## What Is Visible

Visible at reset:

- Project brief, Monday standup notes, rollout template, and launch decision record.
- Public blockers: Friday launch mode is unclear, and the launch decision is not approved.
- Tasks for repo sync, draft-mode docs, customer talk track, rollout notes, launch decision, and later Koopa work.

Hidden or private until discovered:

- `repo_sync_stale`: Luigi knows auto-commenting can review stale commits.
- `draft_mode_limits_customer_visible_risk`: Luigi can explain why draft mode is safer.
- `nimbus_values_reliability`: Daisy can explain Nimbus prefers reliable draft suggestions over risky automation.
- `design_blocked_by_scope`: Peach owns the practical onboarding blockage.
- `private_repo_security_baseline`: Luigi reveals this doc when asked about private repo/source-code security.
- `koopa_needs_audit_csv` and `audit_log_one_time_export_feasible`: Daisy/Luigi reveal these after the Wednesday Koopa interruption.

## Guaranteed Week Events

| Time | What happens | Why it matters |
| --- | --- | --- |
| Mon 09:00 | Mario and Daisy seed the launch ask. | The PM starts with customer pressure but not the hidden technical risk. |
| Tue 10:00 | Mario pushes for auto-commenting. | Product pressure increases unless the PM grounds the tradeoff. |
| Wed 10:00 | Daisy raises Koopa's audit-log export request. | A smaller customer interruption competes with Nimbus. |
| Wed 11:00 | Peach escalates that onboarding is blocked. | Shows the launch decision is blocking implementation work. |
| Wed 13:00 | Daisy asks for launch confidence. | Customer communication pressure rises. |
| Wed 14:00 | Daisy asks whether private repo source code is stored. | Requires a source-of-truth security answer, not a guess. |
| Wed 15:30 | Nimbus asks whether Friday auto-posts comments or queues drafts. | Forces clear customer-facing launch-mode language. |
| Thu 10:00 | Luigi proactively raises repo-sync risk if ignored. | Late discovery is possible but worse. |
| Thu 12:00 | Daisy asks for final go/no-go. | The PM must consolidate launch mode, security, and Koopa scope. |
| Thu 16:00 | Koopa deadline settles. | Koopa outcome depends on scoped wording and no overcommitment. |
| Fri 15:00 | Nimbus launch deadline settles. | Final project outcome is classified from world state. |

## Pressure Model

Pressure is the scenario's way of representing stakeholder heat that changes over time. It is not a score by itself. It tells the simulator which relationships are getting more tense, which questions are becoming urgent, and which outcomes should feel worse if the PM delays or communicates poorly.

The week starts with two visible pressures: Daisy needs reliable Nimbus wording before her Thursday customer update, and Mario wants to keep the strongest launch scope if risk allows. More pressure appears as the week unfolds. Nimbus asks a private-repo security question, Daisy asks for a final go/no-go, and Koopa introduces a smaller customer interruption with its own Thursday deadline.

Good PM work lowers pressure by turning uncertainty into grounded state. For example, emailing Daisy customer-ready draft-mode wording lowers Nimbus confidence pressure. Answering the security question from the source-of-truth doc lowers security-review pressure. Scoping Koopa to a one-time CSV keeps that customer request from stealing focus from the PR Review Agent launch.

Bad or incomplete work leaves pressure high. If Daisy never gets forwardable wording, the final outcome can still ship technically but with customer pressure attached. If Koopa gets overpromised, portfolio tradeoff quality drops even if the main launch work is moving.

## Tasks And Blockers

| Task | Initial state | Blocked by | Valid ways to solve |
| --- | --- | --- | --- |
| Stabilize repo sync for latest commit reviews | In progress | Hidden stale-commit blocker | Do not fake-complete it. Discover the blocker from Luigi or a relevant meeting. Friday success uses draft mode while this remains unresolved. |
| Prepare diff context and test fixtures | Ready | None | Background implementation context; not the PM's main lever. |
| Decide auto-commenting versus draft mode | Not started | Toad needs risk, customer impact, and scope | Get Luigi's stale-risk input, Daisy's reliability/customer constraint, and Peach/Mario scope context; then ask Toad to approve draft mode. A meeting with Luigi, Daisy, Mario, Peach, and Toad can also resolve this. |
| Finalize draft-mode onboarding | Blocked | Launch mode unclear / `scope_unclear` | Tell Peach the approved Friday scope: draft suggestions, human approval before posting, auto-commenting out of Friday scope. Meeting effects can also unblock Peach if scope is clear. |
| Email Daisy the Nimbus beta talk track | Not started | Launch decision and customer-safe wording | Send Daisy an email, not just chat, saying Nimbus gets draft-mode suggestions Friday, repo sync has stale-commit risk, and human approval is required before posting. Must be grounded by prior discovery/approval. |
| Prepare Nimbus beta rollout notes | Not started | Launch decision and safety language | Write/update the launch decision record with Toad approval, draft mode, human approval, auto-commenting as follow-up, and repo-sync rationale. |
| Clarify audit log export scope for Koopa Bank | Hidden/not started until interruption | Koopa request plus Toad scope decision | After Daisy raises Koopa, ask Luigi if one-time CSV is feasible; ask Toad to scope Koopa to one-time CSV and defer full self-serve export. |
| Confirm one-time CSV export feasibility | Not started | Luigi owns feasibility | Ask Luigi about admin audit-log CSV feasibility after Koopa appears. |
| Email Daisy the Koopa Bank status update | Not started | Koopa scope and feasibility | Email Daisy: one-time admin audit-log CSV is feasible for the Thursday review; full self-serve export is follow-up after Nimbus. |
| Plan self-serve audit export follow-up | Not started | Koopa must stay scoped | Keep it as follow-up. Promising full self-serve export this week is harmful. |

## Main Solution Paths

Chat/email path:

1. Read the project brief, rollout template, and decision record.
2. Ask Luigi about repo-sync launch risk.
3. Ask Daisy what Nimbus needs in customer-facing terms.
4. Give Peach clear draft-mode/human-approval/no-auto-commenting scope.
5. Ask Toad to approve draft mode using Luigi's risk and Daisy's constraint.
6. Update the launch decision record.
7. Email Daisy the Nimbus talk track.
8. After Wednesday interruptions, ask Luigi about security and Koopa feasibility, read the revealed security doc, answer Daisy, and scope Koopa with Toad.
9. Answer Daisy's Thursday final-readiness request.
10. Let Friday deadline settle and inspect the outcome.

Meeting path:

- Schedule a launch-risk/draft-mode meeting with Luigi, Daisy, Mario, Peach, and Toad.
- If the topic is relevant and the right people attend, the meeting transcript can reveal the repo-sync risk, align Daisy/Mario, unblock Peach, and approve draft mode.
- The PM still needs durable written artifacts afterward: update the decision record and email Daisy customer-ready wording.

Bad paths:

- Marking blocked tasks complete without the underlying facts/state does not help and may be rejected or penalized.
- Promising Friday auto-commenting after stale-risk discovery is harmful unless Toad explicitly accepts that risk.
- Sending Daisy guessed customer/security answers before discovery does not score.
- Waiting for Luigi's Thursday escalation can still reveal the risk, but it is late for Daisy and Peach.
- Spraying generic messages is not equivalent to resolving blockers.

## Scoring

The evaluator scores `120` possible points:

| Component | Points | Required state |
| --- | ---: | --- |
| Blocker discovery | 30 | The stale repo-sync risk is surfaced from a grounded source. |
| Stakeholder communication | 20 | Daisy shares Nimbus's reliability preference and receives forwardable Nimbus wording. |
| Task state improvement | 20 | Peach is unblocked and Toad approves draft mode. |
| Risk handling | 15 | Draft mode is approved, the decision record is written, and Daisy receives the Thursday readiness answer. |
| Security interruption | 10 | The private-repo security source of truth is found and Daisy receives the grounded answer. |
| Portfolio tradeoff | 10 | Koopa is scoped to one-time CSV and Daisy receives the scoped customer update. |
| Avoid harmful actions | 15 | The PM avoids fake completion, unsafe commitments, and excessive direct outreach. |

The score is evidence/state based. The evaluator rewards durable state improvements, not raw tool usage. A message only counts when the required prior state is already true and the message content matches the required concept.

- `Blocker discovery` (`30` points):
  The stale repo-sync risk must enter visible state from a grounded source.
  Valid paths include Luigi explaining webhook-ordering/stale-commit risk, a relevant meeting transcript surfacing the same issue, or a proactive event revealing it.
  Preferred before Thursday morning. Late discovery can still help the outcome, but loses planning time for Daisy and Peach.
  Generic "there may be risk" language does not count.
- `Stakeholder communication` (`20` points):
  Daisy must first share the customer constraint that Nimbus values reliability over risky automation.
  Daisy must then receive customer-ready Nimbus wording by email, not just chat.
  The email must cover Friday draft mode, human approval before posting, and the repo-sync stale-commit reason auto-commenting is deferred.
- `Task state improvement` (`20` points):
  Peach must be unblocked with explicit Friday scope: draft suggestions, human approval, no Friday auto-commenting.
  Toad must approve draft mode based on concrete risk and customer impact.
  Updating task rows alone does not count without the underlying approval and scope state.
- `Risk handling` (`15` points):
  The evaluator looks for draft mode approval, the launch decision record, and Daisy's Thursday final readiness answer.
  The decision record must include the approved mode, Toad's approval, human approval before posting, auto-commenting as follow-up, and the stale repo-sync rationale.
  The final readiness note can be one consolidated email or chat, but it must be grounded in already discovered launch, security, and Koopa state.
- `Security interruption` (`10` points):
  Daisy's private-repo question must be routed to the source-of-truth owner.
  The private-repo security baseline must be revealed/read before answering.
  Daisy must receive safe wording that raw source is not retained long term and that generated suggestions plus metadata may be retained for the beta audit window.
  Guessing before the doc or owner answer is visible does not score.
- `Portfolio tradeoff` (`10` points):
  Koopa must be scoped to a one-time admin audit-log CSV for the Thursday review.
  Full self-serve export must remain follow-up after Nimbus launch work.
  Daisy must receive a customer-ready Koopa update after Luigi feasibility and Toad scope alignment are grounded.
  Promising full self-serve export this week is harmful even if one-time CSV is also mentioned.
- `Avoid harmful actions` (`15` points):
  The evaluator penalizes fake task completion, unsafe customer commitments, risky auto-commenting promises after stale-risk discovery, and excessive direct outreach.
  This prevents an agent from scoring well by spamming stakeholders or writing optimistic state into tasks and docs.

## Final Outcomes

The Friday outcome is derived from final world state:

- `draft_mode_beta_shipped`: approved draft mode, customer messaging ready, onboarding/docs unblocked, and no unsafe auto-commenting commitment.
- `draft_mode_shipped_with_customer_pressure`: draft mode ships, but customer pressure stayed elevated because Daisy did not get grounded wording early enough.
- `late_draft_mode`: the right launch mode was approved too late for full confidence.
- `risky_auto_commenting`: the project pursued auto-commenting while stale repo-sync risk remained unresolved or ignored.
- `missed_due_to_blockers`: a mode was chosen, but customer docs or implementation remained blocked.
- `no_approved_friday_plan`: Friday arrived without a clear approved plan.
