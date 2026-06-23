# Pressure Model

`stakeholder_pressure` on a project is static human-readable context. Mutable pressure belongs in the `pressures` table and should be authored in scenario data.

```yaml
pressures:
  - id: nimbus_customer_confidence
    project_id: pr_review_agent
    owner_id: daisy
    kind: customer_update
    intensity: 5
    min_intensity: 1
    max_intensity: 10
    reason: Daisy needs grounded customer-ready wording before her Thursday update.
    due_at: "2026-06-25T10:00:00"
```

Intensity is a `1-10` scale. Events, actor policies, meetings, and grounded action rules can mutate pressure through generic effects:

```yaml
- type: increase_pressure
  pressure_id: nimbus_customer_confidence
  by: 3
  reason: Customer-ready wording missed Daisy's Thursday update window.

- type: lower_pressure
  pressure_id: nimbus_customer_confidence
  to: 2
  reason: Daisy received grounded customer-ready wording.
```

Outcome and behavior rules can branch on pressure using the shared condition language:

```yaml
- pressure_at_most:
    id: nimbus_customer_confidence
    intensity: 3
```

Do not add new ad hoc fields such as `stakeholder_pressure_delta` or `scope_pressure_delta` to project metadata. If a scenario needs pressure to affect behavior or outcome, create a named pressure row and mutate that row.
