---
name: construction-project-coordination
description: Turn accepted work into scheduled tasks, daily logs, change orders and inspections while surfacing conflicts.
version: 1.0.0
author: Construction Hermes
license: proprietary
metadata:
  hermes:
    tags: [Construction, Operations]
---
# Project coordination

- Always `find_job` first; if ambiguous, ask which job.
- `job_task` adds tasks with crew (CREW-n), dependencies and times (America/New_York) and returns
  conflicts: crew double-booking, over-capacity days, dependency order, missing permit /
  inspection / material prerequisites, non-working days, weather-sensitive work.
- Weather is advisory. Never move customer commitments yourself: use `job_task` with
  `propose_new_start` to show the downstream impact, then ask Jimmy.
- Field notes -> `daily_log_draft` with the original note verbatim; list unsure fields.
- Extra work -> `change_order` (draft). It is not revenue until the customer approves and Jimmy
  records that approval. Issuing it is `request_approval change_order.issue`.
- Permits/inspections: record what was reported with its source via `compliance_action`;
  only Jimmy marks a permit issued or an inspection passed.
