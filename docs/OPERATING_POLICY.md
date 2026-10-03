# Operating policy

## Modes
- **BUILD** — development and synthetic data. Outbound actions are recorded as `simulated`.
- **SHADOW** (current commissioning target) — real reads and internal drafts; approvals work;
  outbound actions are still `simulated`. Nothing reaches customers, vendors or crews.
- **LIVE** — approved actions are delivered through *connected* integrations only. Requires the
  owner to type `LIVE`. Demo and restore-test databases can never be LIVE.

## What runs without asking
Classification, extraction, calculations, drafts (leads, estimates, proposals as drafts,
invoices as drafts, change orders, POs, daily logs), record lookups, private owner summaries,
official-source research fetches (allowlisted, read-only).

## What needs an approval record
Sending any message to a customer/vendor/crew (`message.send`), issuing a proposal
(`proposal.issue`), invoice (`invoice.issue`), change order (`change_order.issue`) or purchase
order (`purchase_order.issue`), and an agent-reported customer acceptance
(`proposal.record_acceptance`). Payments, refunds, signatures and permit submissions are not
automated at all; they are recorded by the owner with evidence.

Approval record: action id, exact payload + SHA-256, destination, amount, target revision,
requester + channel, decider + channel, expiry (default 72 h), decision, execution result.
Editing content, destination, amount or revision voids it. A bare "yes" in chat maps to the one
pending request in that conversation, or the tool asks which one.

## Standing policies
Optional, narrowly scoped, expiring pre-approvals (`standing_policies`). They never cover
purchase orders or agent-reported acceptances. None are configured.

## Evidence rules
- A payment is cash only when the owner/office verifies it against a deposit reference.
- An inspection is passed only on an owner entry with a source; agent/field reports stay
  `unverified`.
- A permit is `issued` / `not_required_verified` only with a source and owner/office entry.
- An appointment is `confirmed` only with recorded customer-confirmation evidence.
- Change orders count as revenue only after evidenced customer approval.

## Kill switch
Anyone with access (including Hermes) can engage it; only the owner can release it. It blocks
new external dispatch immediately and is rechecked right before every send. Jobs already
accepted by a provider cannot be recalled; jobs whose state is ambiguous are shown as UNKNOWN
for the owner to resolve (`delivered` / `resend once` / `abandon`).

## Learning
Productivity and omission suggestions are stored with evidence and sample size
(`learning_proposals`); accepting one records the decision only. Rates, templates, permissions,
tax rules and code are never changed automatically. Hermes skill writes are staged for review.
