---
name: construction-intake
description: Capture and qualify construction leads from texts, emails, voice notes, photos and web forms without inventing details.
version: 1.0.0
author: Construction Hermes
license: proprietary
metadata:
  hermes:
    tags: [Construction, Operations]
---
# Lead intake and qualification

Use when a new inquiry arrives or Jimmy forwards one ("new lead", a pasted text, a voice note, a photo).

1. Extract only what is stated: name, phone/email, address, job type, scope, timing, budget, source.
   For each field you inferred rather than read, add it to `extraction` with a confidence below 0.8.
2. If the message came from a channel with a message id, pass `provider` and `provider_event_id`
   so a repeated delivery does not create a second lead.
3. Call `capture_lead`. If the result says `needs_review` or lists `review_reasons`
   (possible duplicate contact, uncertain fields), tell Jimmy exactly what to check. Never merge
   customers yourself.
4. Call `lead_followup_questions` and draft at most 3-5 short, job-specific questions.
   Sending them is `request_approval` with `message.send` - never claim it was sent.
5. Site visits: `site_visit` books TENTATIVE. Only confirm with evidence of the customer's yes.

Pipeline: inquiry -> qualified -> site_visit -> estimating -> proposal -> follow_up -> won/lost.
Lost needs a reason. Do not score leads or quote conversion rates; there is no data for that.
