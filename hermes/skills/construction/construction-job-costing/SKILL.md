---
name: construction-job-costing
description: Track estimated, committed, actual and forecast cost; separate invoiced, collected and reported money.
version: 1.0.0
author: Construction Hermes
license: proprietary
metadata:
  hermes:
    tags: [Construction, Operations]
---
# Job costing and cash

- "Log this receipt to <job>": store the photo (`store_document kind=receipt`), resolve the job,
  then `log_cost` with the receipt DOC ref. If the result says needs_review, say why.
- `job_report section=costs`: estimated vs committed vs actual vs forecast-to-complete per cost
  code, projected margin and erosion. `section=financials`: contract, approved vs pending change
  orders, invoiced, verified cash, reported-unverified payments, retainage held/receivable.
- Words matter: invoiced is not collected; reported is not verified; pending change orders are
  not revenue. Never relabel one as another.
- Invoices: `billing_action draft_milestone_invoice` (milestones come from the accepted
  proposal); issuing is `request_approval invoice.issue`.
- "Customer says they paid" -> `billing_action report_payment`; it stays unverified until Jimmy
  verifies it against a deposit.
- `list_records kind=exceptions` for "show jobs losing margin".
