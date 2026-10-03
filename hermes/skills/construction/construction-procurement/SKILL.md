---
name: construction-procurement
description: Takeoffs, like-for-like supplier quote comparison, purchase order drafts and subcontractor document tracking.
version: 1.0.0
author: Construction Hermes
license: proprietary
metadata:
  hermes:
    tags: [Construction, Operations]
---
# Procurement

- `job_report section=takeoff` gives material quantities from the accepted estimate.
- Record quotes with `procurement_action record_quote`; each line needs an exact `item_key`,
  `unit`, `pack_size`, and `unit_price` per pack. Compare with `compare_quotes`; it converts
  packs, adds delivery, flags expired quotes, missing tax info and unstated availability.
- Never substitute a structural item because it is cheaper; compare identical item_keys only.
- No claim of current inventory without a current quote/availability statement.
- `draft_po` lines need a job cost code. Sending a PO is `request_approval purchase_order.issue`.
- Subcontractor COI/license: record as supplied with expiry; `list_records vendor_compliance`
  shows missing/expiring/unverified. This system records status; it does not certify anything.
