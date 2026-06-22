# Project Manager Simulation Environment

This is the design baseline for the implementation. The goal is to build the backend first: simulation engine, state, events, tools, coworkers, and evaluation. UI and APIs can come later if they help the demo.

## What The Environment Must Demonstrate

### 1. Time Advances Through A Simulated Work Week

The environment has its own clock. It is not tied to real time.

If an LLM takes 5 seconds or 5 minutes to choose an action, the simulated week does not move forward during that wait. Time moves because simulated work takes time, not because inference was slow.

The agent can advance time because waiting is a real project-management action. The operator can also advance time during a manual demo.

Tool actions have deterministic logical costs:

```text
send_chat: 5 minutes
send_email: 10 minutes
read_doc: 15 minutes
schedule_meeting: 5 minutes to schedule; the meeting resolves at its scheduled end time
update_task: 1 minute
```

Examples:

```text
advance_time 30m
advance_time 2h
advance_time until_next_event
advance_time to "Tuesday 09:00"
```

The week should jump between meaningful events and action costs: replies, meetings, deadlines, escalations, reading, communication, and final evaluation. It does not need minute-by-minute ticks.

### 2. The Agent Interacts Through Internal Tools

The agent should interact through workplace tools, not direct database access.

Initial tools:

- chat
- email
- calendar
- task tracking
- docs
- meeting notes or transcripts

These are different surfaces over one shared company state.

Example actions:

```text
send_chat(person_id, body)
send_email(to, subject, body)
list_tasks()
update_task(task_id, status, priority)
read_doc(doc_id)
schedule_meeting(attendees, time, topic)
advance_time(duration)
```

### 3. Coworkers Have Goals, Constraints, And Background Activity

Coworkers are part of the environment. They are not the agent being graded.

They should have:

- role
- goals
- constraints
- availability
- response delays
- private knowledge
- behavior rules

For the first version, coworker behavior should be deterministic at the state-transition level. I do not want grading-critical facts to depend on an LLM improvising.

An LLM could later help turn a known fact into more natural wording, but it should not decide the fact.

Example:

```text
Luigi, backend engineer
- owns the auth migration
- knows the vendor API is flaky
- replies after about 2 simulated hours
- reveals the blocker if asked about launch risk or auth status
- proactively escalates concern if ignored until Thursday
```

### 4. The Agent Must Manage The Project

The scenario should require more than checking boxes. The agent should need to:

- discover blockers
- resolve conflicts
- prioritize tradeoffs
- communicate risk
- keep the project moving

The agent should not see everything upfront. Some facts should live in docs, tasks, meeting notes, emails, or coworker private knowledge.

### 5. The System Evaluates Improved Outcomes

The evaluator should score outcomes and decisions, not activity volume. It can apply a small capped penalty for excessive outreach, because spraying messages is different from targeted coordination.

The main comparison is:

```text
baseline project trajectory vs. agent-driven project trajectory
```

The baseline can be a no-op or default rollout. The agent rollout should be better if the agent discovers blockers, improves the critical path, communicates risk, and makes reasonable tradeoffs.

Possible score components:

```text
30% blocker discovery and resolution
20% stakeholder communication
20% task/project state improvement
15% risk handling
15% avoiding harmful or superficial actions, including excessive direct outreach
```

This is RL-adjacent because the simulator has state, actions, transitions, observations, and rewards. The goal is not to train an RL policy for v1. The goal is an agent evaluation environment with defensible grading.

## Systems Problems To Make Legible

### State Transitions

Every meaningful action should have a clear before/after effect on state.

Examples:

- sending chat creates a message and may schedule a reply
- updating a task changes ownership, status, or priority
- resolving a blocker changes project risk
- missing a deadline may increase stakeholder pressure

The simulator owns state mutation. Tool actions and event handlers should go through the simulator layer instead of editing storage directly.

### Event Delivery

The simulator should use an event queue.

Agent actions happen synchronously first and consume their deterministic action cost. Background activity happens through scheduled events, including events that become due during an action's time cost.

Synchronous examples:

- send a chat
- send an email
- update a task
- edit a doc
- schedule a meeting

Asynchronous examples:

- coworker replies after a delay
- coworker proactively reaches out about a risk
- meeting transcript appears after a meeting
- a blocker gets worse if ignored
- a stakeholder escalates after missed communication

When time advances, the engine processes all due events in deterministic order. Events can mutate state and schedule more events.

The system should record:

- when an event was created
- when it is due
- whether it has been delivered
- what handler processed it
- what state changed

### Discoverability Of Information

The agent should have to use the environment well to learn what matters.

Some information should be visible in public docs or tasks. Other information should require asking the right coworker, reading a meeting transcript, or noticing a stakeholder message.

This matters because project management is partly about knowing what to ask and where to look.

### Long-Horizon Consistency

The state should remain consistent across the simulated week.

If Luigi says the vendor API is blocked on Monday, that fact should still matter on Wednesday. If a task is reassigned, later events should reflect the new owner.

Persistent state plus an event/action log should make this inspectable.

### Defensible Grading

The score should be explainable from state and history.

The evaluator should point to concrete evidence:

- blocker discovered at a specific time
- stakeholder informed before or after a deadline
- task dependency resolved or left unresolved
- risk increased or decreased
- harmful actions taken or avoided

The score should not depend on vague style judgments. Any communication penalty should be tied to observable counts or state, and should be small compared with outcome evidence.

I do not plan to use model-based verification in the first version. The evaluator should inspect the database, event log, and final project state directly.

## Core Building Blocks

### Backend First

The first implementation should focus on the backend simulator:

- scenario loading
- SQLite state
- action handlers
- event queue
- coworker behavior rules
- evaluator
- CLI/operator commands

UI, APIs, and richer observability can come after the simulation semantics work.

### Storage

I plan to use local SQLite.

The world state should include:

- people
- project
- milestones
- tasks
- dependencies
- blockers
- chat
- email
- calendar
- docs
- meeting transcripts
- scheduled events
- action log

SQLite is local, inspectable, easy to reset, and enough for a single-node simulator.

### Scenario Authoring

The first scenario should not be a pile of one-off code.

Scenario data should define most of the setup:

- a `scenario.json` manifest with `include` entries
- a `world.json` starting-state file
- a `rules.json` behavior/grading file
- people
- projects
- tasks
- hidden facts
- documents
- initial messages
- scheduled events
- evaluation targets
- state evidence rules
- task gate rules
- harmful-action rules
- background event rules
- outcome rules

Python behavior rules can handle logic that data alone cannot express, but scenario-specific scoring, outcome, and proactive event semantics should be data-authored where possible. The current implementation uses a small reusable condition language for task gates, state-derived evidence rows, harmful-action rules, background event rules, and Friday outcome classification.

The defensible split is:

```text
Reusable engine:
  storage, tool actions, event delivery, timelines, effect application,
  condition evaluation, coworker rule matching, action logs, task gates,
  state-derived evidence, harm checks, background event rules, outcome rules,
  and evaluator scoring

Scenario-specific v1 data:
  people, facts, docs, tasks, blockers, events, coworker rules, task gates,
  state evidence rules, harm rules, background event rules, and outcome rules

Remaining boundary:
  meeting behavior still has some PR Review Agent-specific Python.
  The next scaling step is making meeting effects use the same declarative
  rule style before adding a second scenario.
```

The next scaling step is not a giant prompt. It is reducing the remaining meeting special cases so a second scenario can reuse the same engine without custom Python branches for every project.

### Operator Workflow

The reviewer should be able to run the system locally with documented commands.

Possible CLI shape:

```text
pm-sim reset
pm-sim observe
pm-sim read-doc doc_project_brief
pm-sim send-chat luigi "Any repo sync blockers for launch?"
pm-sim advance-time 2h
pm-sim log
pm-sim evaluate
pm-sim run-agent --policy scripted --reset
pm-sim run-agent --policy llm --reset --max-turns 40
```

A UI is optional. If added, it should stay lightweight and should not hide the simulation semantics.

## Initial Scenario

Working scenario: launch readiness week.

Company: Fireflower, a small B2B SaaS company that sells developer workflow tools.

Product context:

Customers use Fireflower to review pull requests faster, catch common issues earlier, and reduce review backlogs.

Project: launch a PR Review Agent beta for a pilot customer by Friday.

The agent is meant to read pull request diffs and prepare useful review suggestions. The most impressive version auto-posts comments directly on PRs.

The pilot customer, Nimbus Labs, expects a Friday beta demo. Daisy promised they would see something useful and reliable.

Nimbus also asks mid-week whether the beta will post comments automatically or queue draft suggestions for approval. That creates stakeholder pressure if the launch mode is still unclear.

Core risk:

Auto-commenting depends on repo sync always reviewing the latest commit. That sync path is flaky because webhook events can arrive out of order. Draft suggestions with human approval are reliable.

The PM has to help the team choose between:

```text
Auto-commenting beta:
  more impressive for the pilot, but higher customer-visible failure risk

Draft-mode beta:
  suggestions require human approval before posting, less flashy but safer for Friday
```

The scenario includes a rollout-note template and a separate rollout-notes task so the agent can inspect written guidance, not only chat with coworkers.

The PM starts on Monday. There is stakeholder pressure, a hidden backend blocker, unclear task ownership, and a Friday decision point.

Possible coworkers:

- Luigi: backend engineer
- Mario: product manager
- Peach: designer
- Daisy: customer success lead
- Toad: engineering manager

The successful path should involve discovering the blocker, clarifying ownership, communicating risk, and making a reasonable launch tradeoff.

## Open Choices

These should be decided before coding too much:

- exact database schema
- exact CLI commands
- exact scenario facts
- final scoring rubric
- whether any UI is worth adding after the backend works
- whether any LLM-generated wording is worth including
