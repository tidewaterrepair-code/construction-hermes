---
name: construction-estimating
description: Build labor-only or turnkey estimates with sourced quantities and rates; produce proposals only when the numbers are supported.
version: 1.0.0
author: Construction Hermes
license: proprietary
metadata:
  hermes:
    tags: [Construction, Operations]
---
# Estimating

1. `create_estimate` (labor_only or turnkey). For decks/framing/sunrooms/repairs use
   `list_records kind=assemblies` and `estimate_apply_assembly` with each parameter's
   `source`: field_measured | plans | customer_supplied | photo_estimate | assumed.
   A photo is never field_measured. Omit unknown values; the lines stay visibly missing.
2. Add other lines with `estimate_add_line` using library `rate_code`s. If you only have a quote,
   pass `unit_cost` + `cost_source`; it is provisional until Jimmy confirms it.
3. Read the `totals` block from the tool: direct cost, contingency, overhead, cost basis, price,
   gross margin, `firm_blockers`. Report those numbers; never compute your own.
4. Firm quote only when `firm_quote_ready` is true. Otherwise offer a rough range, clearly
   labeled, listing its assumptions, or list the blockers and who must resolve each.
5. Markup vs margin: the owner's policy decides; never mix them. $10,000 at 30% margin is
   $14,285.71; at 30% markup it is $13,000.00 (the tool does this).
6. `create_proposal` locks the revision. Changes after that need `estimate_new_revision` and a
   new approval. Issuing to the customer is `request_approval proposal.issue`.
Templates are editable examples, not local pricing or engineered designs. Spans, footings,
connections and electrical need verified sources and qualified review.
