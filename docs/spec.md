# Project Manager Simulation Environment

This is the design baseline for the implementation. The goal is to build the backend first: simulation engine, state, events, tools, coworkers, and evaluation. UI and APIs can come later if they help the demo.

## What The Environment Must Demonstrate

### 1. Time Advances Through A Simulated Work Week

The environment has its own clock. It is not tied to real time.

If an LLM takes 5 seconds or 5 minutes to choose an action, the simulated week does not move forward during that wait. Time only moves through an explicit `advance_time` action.

The agent can advance time because waiting is a real project-management action. The operator can also advance time during a manual demo.

Examples:

```text
advance_time 30m
advance_time 2h
advance_time until_next_event
advance_time to "Tuesday 09:00"
```

The week should jump between meaningful events: replies, meetings, deadlines, escalations, and the final evaluation. It does not need minute-by-minute ticks.

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

The evaluator should score outcomes and decisions, not activity volume.

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
15% avoiding harmful or superficial actions
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

Agent actions happen synchronously first. Background activity happens later through scheduled events.

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

The score should not depend on vague style judgments.

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

- people
- projects
- tasks
- hidden facts
- documents
- initial messages
- scheduled events
- evaluation targets

Python behavior rules can handle logic that data alone cannot express. The goal is for a second scenario to reuse the same engine, tools, event queue, and evaluator structure.

### Operator Workflow

The reviewer should be able to run the system locally with documented commands.

Possible CLI shape:

```text
pm-sim reset
pm-sim observe
pm-sim act send-chat --to luigi --body "Any launch blockers?"
pm-sim advance-time 2h
pm-sim log
pm-sim evaluate
```

A UI is optional. If added, it should stay lightweight and should not hide the simulation semantics.

## Initial Scenario

Working scenario: launch readiness week.

Company: Mushroom Metrics, a small B2B SaaS company that helps customer success teams monitor account health.

Product context:

Customers use Mushroom Metrics to track product adoption, support risk, renewal risk, and expansion opportunities.

Project: launch an Executive Health Report feature for a pilot customer by Friday.

The report is meant to help customer success leaders prepare for executive renewal meetings. It combines product usage trends, seat adoption, support ticket volume, renewal risk, CRM account tier, and a customer health summary.

The pilot customer, Fireflower CRM, has a renewal meeting on Friday. Daisy from customer success promised they would have the report ready for that meeting.

Core risk:

The full report depends on a CRM enrichment sync for renewal date and account tier. That sync is flaky. Internal product usage and support data are reliable.

The PM has to help the team choose between:

```text
Full report:
  more valuable for the renewal conversation, but higher demo failure risk

Fallback report:
  uses reliable internal usage/support data only, less complete but safer for Friday
```

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
