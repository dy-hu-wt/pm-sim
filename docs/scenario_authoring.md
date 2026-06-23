# Writing a Scenario

A scenario is a small work week. It should feel like a real project manager joined a company on Monday and has to learn what matters, talk to the right people, make tradeoffs, and leave the project in a better state by Friday.

Start with the story before writing YAML. Pick one primary project, one interruption or competing project, three to five coworkers, one or two hidden risks, and a deadline. The best scenarios are not puzzles with one magic sequence. They give the agent several reasonable ways to learn the same facts, but still require grounded decisions before the project can improve.

## Files

Each scenario lives in its own directory:

```text
scenarios/<scenario_id>/
  scenario.yaml
  world.yaml
  interactions.yaml
  evaluation.yaml
```

`scenario.yaml` is only the manifest. Keep it short.

```yaml
id: billing_migration
name: Billing Migration Readiness
company: Fireflower
start_time: "2026-06-22T09:00:00"
timezone: America/New_York
include:
  - world.yaml
  - interactions.yaml
  - evaluation.yaml
```

`world.yaml` seeds the company state. `interactions.yaml` says how coworkers, events, meetings, and agent actions mutate that state. `evaluation.yaml` explains what good project management means for this scenario.

Run this early and often:

```bash
pm-sim reset --scenario scenarios/<scenario_id>
```

Reset loads the scenario, validates references, creates the SQLite state, and fails fast if an ID is wrong.

## World State

`world.yaml` is the starting point. It should describe what exists at Monday 9:00, not everything the agent will eventually know.

People are coworkers. Give each person a role, a response delay, some goals, and a short behavior note. This is enough to make them distinct without turning them into free-form agents.

```yaml
people:
  - id: daisy
    name: Daisy
    role: Customer success lead
    response_delay_minutes: 40
    goals:
      - Give customers accurate written updates.
      - Avoid surprising Nimbus on launch mode.
    behavior:
      current_focus: Waiting for customer-safe wording.
      needs_from_pm:
        - A written launch-mode answer before Thursday.
```

Coworker state is mutable memory. Use it for things the evaluator or later behavior needs to know, such as whether Daisy received a customer update or Toad approved a decision.

```yaml
coworker_state:
  - id: daisy_customer_update_received
    person_id: daisy
    key: customer_update_received
    value: false
```

Projects are the active workstreams. A good scenario usually has one main project and one smaller interruption so the agent has to prioritize.

```yaml
projects:
  - id: project_pr_review_agent
    name: PR Review Agent Beta
    status: active
    risk_level: medium
    stakeholder_pressure: Daisy promised Nimbus Labs a Friday beta.
    deadline_at: "2026-06-26T15:00:00"
```

Facts are pieces of knowledge. Hidden or private facts should not be visible until a coworker, document, meeting, or event reveals them.

```yaml
facts:
  - id: fact_repo_sync_stale
    visibility_scope: hidden
    owner_id: luigi
    summary: Repo sync can review an older commit when webhooks arrive out of order.
    visible_at: null
```

Tasks are what the project manager can see and move. Write task IDs as readable names. The loader turns `launch_decision` into `task_launch_decision` internally, so the database and CLI stay stable while the YAML stays readable.

```yaml
tasks:
  - id: launch_decision
    project_id: project_pr_review_agent
    title: Decide auto-commenting versus draft mode
    owner_id: toad
    status: not_started
    priority: critical
    due_at: "2026-06-25T11:00:00"

dependencies:
  - id: dep_talk_track_needs_decision
    project_id: project_pr_review_agent
    upstream_task_id: launch_decision
    downstream_task_id: customer_talk_track
    description: Customer wording depends on the launch mode decision.
```

Dependencies are causal, not just diagram labels. When an upstream task is completed and every upstream dependency for a blocked downstream task is complete, the engine can move that downstream task to `in_progress` if its blocker is resolved. For example, once the launch decision is complete and the scope blocker is resolved, draft-mode onboarding can move from blocked to active work without a separate bespoke scenario rule.

Use `visible_at: null` for docs, facts, and blockers that exist in the world but should not be known yet. When something becomes visible, an effect sets `visible_at` to the simulated time.

## Interactions

`interactions.yaml` is where the week moves. The engine advances time, delivers events, records actions, and applies effects. The scenario decides which facts are revealed and which state changes happen.

Coworker behavior lives in `actor_behaviors`. A `reply` behavior handles chat or email from the agent. A `policy` behavior is proactive: the coworker reaches out because time passed or some state was missing.

```yaml
actor_behaviors:
  - id: luigi_repo_sync_reply
    kind: reply
    person_id: luigi
    channels: [chat, email]
    match:
      mode: semantic
      intents:
        - id: asks_repo_sync_risk
          description: The agent asks whether repo sync is safe for Friday launch.
          signals:
            - repo sync risk
            - stale commit
            - latest commit reliability
      require_all: [asks_repo_sync_risk]
    reply:
      body: Repo sync can still review a stale commit if webhooks arrive out of order. I recommend draft mode for Friday.
      delay_minutes: 70
    effects:
      - type: discover_fact
        fact_id: fact_repo_sync_stale
        source: luigi_reply
      - type: update_coworker_state
        person_id: luigi
        key: repo_sync_risk_shared
        value: true
```

The important part is the effect, not the exact sentence. If the same information can be learned by chat, email, or meeting, make all paths converge on the same fact and coworker state.

Proactive behavior makes coworkers feel stateful. Daisy should not wait forever if she needs customer wording.

```yaml
actor_behaviors:
  - id: daisy_customer_wording_nudge
    kind: policy
    person_id: daisy
    trigger:
      at: "2026-06-25T09:30:00"
    when:
      - not:
          coworker_state:
            person_id: daisy
            key: customer_update_received
            equals: true
    effects:
      - type: create_message
        channel: email
        sender_id: daisy
        recipient_id: agent
        subject: Customer wording risk
        body: I still need written customer-ready wording before I update Nimbus.
```

Scheduled events represent outside pressure: customer questions, teammate pushes, or deadlines. They should be on the calendar or event queue so the agent has to keep working through the week.

```yaml
events:
  - id: event_daisy_security_question
    event_type: daisy_private_repo_security_question
    scheduled_at: "2026-06-24T14:00:00"
    priority: 100
    payload:
      project_id: project_pr_review_agent
```

`event_rules` say what happens when an event is delivered.

```yaml
event_rules:
  - id: daisy_security_question_arrives
    event_type: daisy_private_repo_security_question
    effects:
      - type: create_message
        channel: email
        sender_id: daisy
        recipient_id: agent
        subject: Nimbus private repo security question
        body: Nimbus asked whether the agent stores source code from private repos.
```

Meetings should be useful but not magical. They must be at least 10 minutes long. A meeting can reveal several facts at once if the right people attend and the topic is relevant.

## Scoring

`evaluation.yaml` should describe outcomes, not reward busywork. The evaluator should not score a message just because it contains the right words. It should score state that changed after the agent had enough information to act responsibly.

Use `grading_rules` for important communications. A grading rule has four parts: prerequisites, the action to recognize, the state mutation, and the milestone derived from that state.

```yaml
grading_rules:
  - id: customer_message_ready
    template: grounded_communication
    requires:
      - fact_discovered: fact_repo_sync_stale
      - project_decision:
          project_id: project_pr_review_agent
          equals: draft_mode_approved
    action:
      type: send_email
      recipient_id: daisy
      match:
        mode: semantic
        intents:
          - id: draft_mode
            description: The message says Friday launch mode is draft mode.
            signals:
              - draft mode
              - draft suggestions
          - id: human_approval
            description: The message says a human must approve before posting.
            signals:
              - human approval
              - approve before posting
          - id: repo_sync_risk
            description: The message explains stale commit or repo sync risk.
            signals:
              - stale commit
              - repo sync risk
        require_all:
          - draft_mode
          - human_approval
          - repo_sync_risk
    state:
      person_id: daisy
      key: customer_update_received
      value: true
    milestone:
      key: customer_message_ready
      note: Daisy received grounded customer-ready launch wording.
```

This rule is causal. The agent must discover the risk and get the decision before the email can update Daisy's state. The evaluator later scores `customer_message_ready` from Daisy's state, not from the raw email body.

## Reusable Patterns

Use these patterns before inventing a one-off structure:

- Grounded communication: prerequisites in `requires`, semantic `action.match`, a state mutation, then a derived `milestone`. Scenario-specific fields are the recipient, required facts, signals, state key, and note; engine-generic fields are `requires`, `action`, `state`, and `milestone`.
- Blocker discovery: hidden/private fact plus `update_blocker` to `surfaced`, usually from an actor reply, event, or meeting. Scenario-specific fields are the fact, owner, blocker, and wording; engine-generic fields are fact visibility, blocker status, and effects.
- Stakeholder approval: decision-maker reply or meeting rule records a fact/project decision and coworker state. Scenario-specific fields are who can approve and what evidence they need; engine-generic fields are `project_decision`, `update_project`, `update_coworker_state`, and task gates.
- Interruption scoping: outside event creates pressure, reveals a scoped fact, and records a commitment or project decision that protects the main project. Scenario-specific fields are customer/project names and tradeoff; engine-generic fields are event delivery, effects, commitments, and harmful-action rules.
- Final readiness: late-week event asks for a consolidated written update. The grading rule should require prior decisions/facts so a guessed status note cannot score. Scenario-specific fields are required content and deadline; engine-generic fields are scheduled events, causal prerequisites, and derived milestones.

State-derived milestones look like this:

```yaml
milestone_rules:
  - id: customer_message_ready
    note: Daisy has received customer-ready launch wording.
    when:
      - coworker_state:
          person_id: daisy
          key: customer_update_received
          equals: true
    created_at:
      coworker_state:
        person_id: daisy
        key: customer_update_received
```

Do not directly write scored milestones from an action or event. The validator rejects that for milestone ids used in `score_components`.

```yaml
# Do not do this for scored milestones.
- type: record_milestone
  key: customer_message_ready
```

## Outcomes

Outcome rules classify the project at deadline. Write them in order from worst or most specific to best or fallback.

```yaml
outcome_rules:
  - id: risky_auto_commenting
    when:
      - project_id: project_pr_review_agent
      - project_decision:
          project_id: project_pr_review_agent
          equals: auto_commenting_approved
      - blocker_status:
          id: blocker_repo_sync_stale
          is: unresolved
    result:
      status: shipped
      risk_level: high
      final_outcome: risky_auto_commenting
      summary: Auto-commenting shipped while stale-code risk was still unresolved.

  - id: draft_mode_beta_shipped
    when:
      - project_id: project_pr_review_agent
    result:
      status: shipped
      risk_level: low
      final_outcome: draft_mode_beta_shipped
      summary: Nimbus received a draft-mode beta with human approval before posting.
```

A good outcome rule should be understandable to a reviewer without reading Python. If the rule says a project shipped, the required state should explain why.

## Baseline And Scripted Path

Every scenario needs a no-op baseline and a scripted success path. The baseline shows what happens if the agent does nothing. The scripted path proves that the scenario is solvable through the normal tools.

```yaml
baseline:
  description: The agent does nothing until Friday.
  commands:
    - pm-sim reset --scenario scenarios/launch_readiness
    - pm-sim advance-time to:2026-06-26T15:00:00
    - pm-sim evaluate --explain

scripted_policy:
  - name: ask_luigi_about_repo_sync_risk
    tool: send_chat
    args:
      person_id: luigi
      body: Is repo sync safe enough for auto-commenting on Friday?
```

The scripted path should be boring. It is not there to be clever; it is there to make the scenario easy to test and demo.

## Multiple Valid Paths

Do not make one golden path. If Luigi can share a risk in chat, he should usually be able to share the same risk by email. If the agent schedules a meeting with Luigi and Toad, the meeting can reveal the same risk and approval path. The channel can change the cost and delay, but the important state should converge.

For example, these three paths can all lead to the same state:

```yaml
effects:
  - type: discover_fact
    fact_id: fact_repo_sync_stale
  - type: update_coworker_state
    person_id: luigi
    key: repo_sync_risk_shared
    value: true
```

That lets agents solve the scenario differently while the evaluator stays stable.

## Authoring Checklist

Before calling a scenario done, run these commands:

```bash
pm-sim reset --scenario scenarios/<scenario_id>
pm-sim advance-time to:2026-06-26T15:00:00
pm-sim evaluate --scenario scenarios/<scenario_id> --explain
pm-sim run-agent --policy scripted --reset --scenario scenarios/<scenario_id>
python -m unittest discover -s tests
```

Then inspect the no-op score and the scripted score. The no-op path should be clearly worse. The scripted path should pass. If both paths score well, the evaluator is too loose. If the scripted path only works by following one exact sequence, add alternate chat, email, or meeting paths that produce the same state.
