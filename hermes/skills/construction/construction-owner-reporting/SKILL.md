---
name: construction-owner-reporting
description: Owner digests and approvals: what needs Jimmy today, approval prompts, and the kill switch.
version: 1.0.0
author: Construction Hermes
license: proprietary
metadata:
  hermes:
    tags: [Construction, Operations]
---
# Owner reporting and approvals

- "What needs me today?" -> `whats_next`; reply with its `text` (one phone screen) and offer detail.
  Priority: collect earned money, protect active jobs, respond to leads, prevent missed
  commitments, then system issues. If nothing is urgent, say so in one line.
- Approvals: after `request_approval`, show the summary, amount, destination and APR ref, plus the
  `owner_link`. When Jimmy says "yes"/"approve", call `owner_decision` (with the APR ref if more
  than one is pending). The approval prompt asks him directly; your message is not the approval.
  If chat approvals are disabled, give him the dashboard link.
- Never say something was sent, paid, approved, permitted or scheduled unless a tool result
  shows it. Outbound items in SHADOW mode are recorded as simulated, not sent.
- If anything looks like a bad or runaway send, `engage_kill_switch` and tell Jimmy. Only he can
  release it (dashboard or CLI).
