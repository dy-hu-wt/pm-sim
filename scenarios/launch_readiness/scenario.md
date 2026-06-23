# PR Review Agent Beta Launch Readiness

This is the source-of-truth scenario guide. The YAML defines executable state; this document explains the story, workstreams, dependencies, deadlines, and who the PM needs to coordinate with.

![Week calendar](../../docs/assets/week-calendar.svg)

## One-Line Situation

Fireflower wants to give Nimbus Labs a Friday beta of the PR Review Agent. The flashy option is automatic PR comments. The safer option is draft mode: the agent prepares suggestions, but a human approves before anything is posted.

The PM is juggling:

- the main Nimbus Friday launch decision
- implementation unblock and onboarding readiness
- customer-facing security wording
- a smaller Koopa Bank interruption with its own Thursday deadline

## Workstreams

### 1. Nimbus Friday Launch Decision

This is the main stream.

Question:
- What exactly is Fireflower promising Nimbus for Friday?

Primary deadline:
- `Fri 2026-06-26 15:00` Nimbus beta deadline

Decision that must be reached:
- approve `draft mode` for Friday
- do not promise Friday `auto-commenting` unless repo-sync risk is truly resolved

Who matters:
- `Luigi`: source of truth on repo-sync risk
- `Daisy`: source of truth on what Nimbus can safely be promised
- `Toad`: approver for the Friday launch mode
- `Mario`: pushes for broader scope; useful for alignment, not final approval

### 2. Implementation / Onboarding Unblock

This stream depends on the launch decision.

Question:
- Can Peach finish onboarding/docs once Friday scope is explicit?

Primary deadline:
- effectively before Daisy's Thursday readiness path closes; late scope leaves implementation blocked even if approval exists

Unlock condition:
- Peach needs explicit Friday scope: `draft suggestions`, `human approval before posting`, `no Friday auto-commenting`

Who matters:
- `Peach`: blocked owner
- `Toad`: approval authority
- `Luigi` and `Daisy`: provide the rationale that makes the scope defensible

### 3. Nimbus Customer Communication

This is the customer-facing packaging of the launch decision.

Question:
- Has Daisy received wording she can forward or archive?

Primary deadlines:
- before Daisy's Thursday customer update path
- before the Thursday final readiness ask

Required content:
- Friday is `draft mode`
- repo-sync stale-commit risk is why auto-commenting is out of Friday scope
- human approval is required before posting

Who matters:
- `Daisy`: recipient of customer-ready wording
- `Luigi`: technical grounding
- `Toad`: approval grounding

### 4. Nimbus Security Interruption

This is a side stream inside the main Nimbus work.

Question:
- Can Daisy answer Nimbus's private-repo/source-code question from grounded security facts?

Primary deadline:
- `Wed 2026-06-24 14:00` question appears
- should be answered before Thursday readiness communication compounds uncertainty

Required answer:
- private repo source is processed transiently
- raw source is not retained long term
- generated suggestions / metadata may be retained for the beta audit window

Who matters:
- `Luigi`: source of truth owner
- `Daisy`: customer-facing recipient

### 5. Koopa Bank Interruption

This is the overhead / secondary stream. It is smaller than Nimbus, but it has a real deadline and still affects realism and scoring.

Question:
- What can Fireflower safely promise Koopa for Thursday's security review?

Primary deadline:
- `Thu 2026-06-25 16:00` Koopa deadline

Safe scoped answer:
- one-time admin audit-log CSV is feasible for the review
- full self-serve export is follow-up work after the Nimbus launch

Who matters:
- `Luigi`: feasibility source of truth
- `Toad`: scope / tradeoff approval
- `Daisy`: needs customer-ready wording

## Who To Talk To

| Person | Why you talk to them | What they can unlock |
| --- | --- | --- |
| Luigi | Technical truth | stale-commit risk, draft-mode rationale, private-repo security details, Koopa one-time CSV feasibility |
| Daisy | Customer truth | Nimbus reliability constraint, final customer-ready wording path, Koopa customer update path |
| Peach | Implementation readiness | whether onboarding/docs are still blocked by unclear Friday scope |
| Toad | Approval | Friday launch mode approval, Koopa scope approval |
| Mario | Product pressure / alignment | willingness to accept safer Friday scope instead of auto-commenting |

## Causal Map

### Main Nimbus path

`Ask Luigi about repo-sync risk`
-> `stale-commit risk becomes visible`
-> `ask Daisy what Nimbus values`
-> `Daisy surfaces reliability constraint`
-> `ask Toad for approval using grounded risk + customer impact`
-> `draft mode approved`
-> `Peach can be unblocked`
-> `update launch decision record`
-> `email Daisy forwardable Nimbus wording`
-> `Thursday readiness email consolidates the plan`
-> `Friday can end as draft_mode_beta_shipped`

### Security side path

`Daisy asks about private-repo/source-code handling`
-> `ask Luigi / get security baseline doc revealed`
-> `read the doc`
-> `email Daisy a grounded answer`

### Koopa side path

`Daisy raises Koopa audit-log export request`
-> `ask Luigi if one-time CSV is feasible`
-> `ask Toad to scope Koopa to one-time CSV, not self-serve`
-> `email Daisy the scoped update`

## Guaranteed Events By Stream

### Main Nimbus stream

| Time | Event | What it pressures |
| --- | --- | --- |
| Mon 09:00 | Mario and Daisy seed the launch ask | Starts launch pressure before the PM knows the hidden technical risk |
| Tue 10:00 | Mario pushes for auto-commenting | Raises pressure toward risky broader scope |
| Wed 11:00 | Peach escalates blocked onboarding | Shows unclear scope is now blocking real implementation work |
| Wed 13:00 | Daisy asks for launch confidence | Forces progress on customer-facing clarity |
| Wed 15:30 | Nimbus asks whether Friday auto-posts or queues drafts | Forces precise launch-mode wording |
| Thu 10:00 | Luigi proactively raises repo-sync risk if ignored | Late but still recoverable risk discovery |
| Thu 12:00 | Daisy asks for final go/no-go | Forces a consolidated readiness answer |
| Fri 15:00 | Nimbus deadline settles | Final main-project outcome is derived |

### Security side stream

| Time | Event | What it pressures |
| --- | --- | --- |
| Wed 14:00 | Daisy asks whether private repo source is stored | Requires a source-of-truth answer, not a guess |

### Koopa overhead stream

| Time | Event | What it pressures |
| --- | --- | --- |
| Wed 10:00 | Daisy raises Koopa's audit-log export request | Introduces portfolio tradeoff pressure |
| Thu 16:00 | Koopa deadline settles | Penalizes overpromising or ignoring the interruption |

## What Starts Visible vs Hidden

### Starts visible

- project brief
- Monday standup notes
- rollout template
- launch decision record
- visible blockers about unclear launch mode / missing approval
- task tracker rows for launch decision, rollout notes, customer wording, and later Koopa work

### Starts hidden

- `repo_sync_stale`: Luigi knows auto-commenting can review an older commit
- `draft_mode_limits_customer_visible_risk`: Luigi can explain why draft mode is safer
- `nimbus_values_reliability`: Daisy can reveal Nimbus prefers reliability over flashy automation
- `design_blocked_by_scope`: Peach owns the practical onboarding blockage
- `audit_log_one_time_export_feasible`: Luigi can confirm one-time CSV feasibility for Koopa

### Starts private but revealable

- `doc_private_repo_security_baseline`
- `doc_koopa_audit_export_note`

Usually revealed by asking the right owner first.

## Tasks And What They Actually Mean

| Task | What it really represents | Depends on | How it becomes legitimately solvable |
| --- | --- | --- | --- |
| Stabilize repo sync for latest commit reviews | Underlying technical fix for stale-commit risk | Luigi's hidden blocker | Usually not finished this week; the realistic PM move is choosing draft mode while it remains unresolved |
| Decide auto-commenting versus draft mode | The core Friday scope decision | Luigi risk + Daisy customer constraint + Toad approval | Ground the tradeoff, then get Toad's explicit approval |
| Finalize draft-mode onboarding | Peach's onboarding/docs work | explicit Friday scope | Tell Peach the approved mode and constraints, or use a valid meeting outcome |
| Email Daisy the Nimbus beta talk track | durable customer-facing wording | approved launch mode + grounded risk rationale | Send an email Daisy can forward/archive |
| Prepare Nimbus beta rollout notes | durable internal decision artifact | launch decision + rationale | Update the decision record with the full approved package |
| Clarify audit log export scope for Koopa Bank | Koopa tradeoff decision | Luigi feasibility + Toad scope approval | Keep scope to one-time CSV, defer self-serve |
| Confirm one-time CSV export feasibility | technical feasibility check | Luigi | Ask Luigi after Koopa appears |
| Email Daisy the Koopa Bank status update | durable customer-facing Koopa wording | Luigi feasibility + Toad scoping | Send a grounded email; do not promise self-serve this week |

## Best Meeting Use

Use a meeting when:
- the PM needs one decision from multiple stakeholders at once
- the right attendees are available
- the goal is launch-mode approval or shared scope alignment

Do not default to a meeting when:
- Daisy needs customer-ready wording
- a security answer just needs Luigi's source-of-truth doc/input
- Koopa just needs a tightly scoped feasibility + approval chain

In this scenario:
- meeting can help on `launch mode / scope decision`
- email is the right surface for `Daisy-facing durable wording`
- doc update is the right surface for `decision record / durable internal artifact`

## Bad Paths

- Discovering the repo-sync risk only on Thursday is recoverable, but it compresses Daisy and Peach.
- Promising Friday auto-commenting after stale-risk discovery is harmful.
- Emailing Daisy guessed security wording before Luigi/doc grounding does not count.
- Marking blocked tasks complete without the underlying state does not help and may be penalized.
- Promising Koopa full self-serve export this week is a bad portfolio tradeoff even if Nimbus is otherwise on track.

## Scoring

The evaluator scores `120` points total:

| Component | Points | What it is really checking |
| --- | ---: | --- |
| Blocker discovery | 30 | Did the PM surface the real technical risk from a grounded source? |
| Stakeholder communication | 20 | Did Daisy both explain Nimbus's need and receive usable forwardable wording? |
| Task state improvement | 20 | Did approval/scope actually unblock work, especially Peach? |
| Risk handling | 15 | Did the PM document the decision and close the Thursday readiness loop? |
| Security interruption | 10 | Did the PM answer the private-repo question from source-of-truth state? |
| Portfolio tradeoff | 10 | Did the PM handle Koopa without overcommitting or derailing Nimbus? |
| Avoid harmful actions | 15 | Did the PM avoid fake progress and unsafe commitments? |

The score is evidence/state based. The evaluator rewards durable state improvements, not raw tool usage. A message only counts when the required prior state is already true and the message content matches the required concept.

`Blocker discovery (30 points):` The stale repo-sync risk must enter visible state from a grounded source.

- Valid paths include:
  - Luigi explains webhook-ordering / stale-commit risk.
  - A relevant meeting transcript surfaces the same issue.
  - A late proactive event reveals it.
- Preferred before Thursday morning.
- Generic "there may be risk" language does not count.

`Stakeholder communication (20 points):` Daisy must first share the customer constraint, then receive usable Nimbus wording.

- Required:
  - Daisy surfaces that Nimbus values reliability over risky automation.
  - Daisy receives the final Nimbus wording by email, not just chat.
  - The email covers Friday draft mode.
  - The email states that human approval is required before posting.
  - The email explains that repo-sync stale-commit risk is why auto-commenting is deferred.

`Task state improvement (20 points):` Work only counts when underlying state changes.

- Required:
  - Peach is unblocked with explicit Friday scope.
  - That scope means draft suggestions, human approval before posting, and no Friday auto-commenting.
  - Toad approves draft mode based on concrete risk and customer impact.
- Updating task rows alone does not count.

`Risk handling (15 points):` The PM must close the loop on the Friday launch package.

- Required:
  - Draft mode is approved.
  - The launch decision record is written.
  - Daisy receives the Thursday final readiness answer.
  - The decision record includes the approved mode, Toad's approval, human approval before posting, auto-commenting as follow-up, and the stale repo-sync rationale.
- The final readiness note can be one consolidated email or chat, but it must be grounded in already discovered launch, security, and Koopa state.

`Security interruption (10 points):` Daisy's private-repo question must be answered from a source of truth.

- Required:
  - The question is routed to the source-of-truth owner.
  - The private-repo security baseline is revealed and read before answering.
  - Daisy receives safe wording that raw source is not retained long term.
  - Daisy receives the beta-audit-window retention constraint for generated suggestions / metadata.
- Guessing before the doc or owner answer is visible does not score.

`Portfolio tradeoff (10 points):` Koopa must be handled without stealing the week from Nimbus or triggering an unsafe promise.

- Required:
  - Koopa is scoped to a one-time admin audit-log CSV for the Thursday review.
  - Full self-serve export remains follow-up after Nimbus launch work.
  - Daisy receives a customer-ready Koopa update after Luigi feasibility and Toad scope alignment are grounded.
- Promising full self-serve export this week is harmful even if one-time CSV is also mentioned.

`Avoid harmful actions (15 points):` The evaluator resists superficial or unsafe play.

- Penalized:
  - fake task completion
  - unsafe customer commitments
  - risky auto-commenting promises after stale-risk discovery
  - excessive direct outreach

## Final Outcomes

Friday is derived from final world state, not from one action.

- `draft_mode_beta_shipped`: approved draft mode, grounded customer messaging, implementation unblocked, no unsafe auto-commenting commitment
- `draft_mode_shipped_with_customer_pressure`: technically shipped, but customer confidence remained too high-risk or too late
- `late_draft_mode`: correct decision, too late for confident execution
- `risky_auto_commenting`: broader scope pushed despite unresolved stale-risk
- `missed_due_to_blockers`: decision existed, but implementation/docs/customer path stayed blocked
- `no_approved_friday_plan`: no defensible approved plan by deadline
