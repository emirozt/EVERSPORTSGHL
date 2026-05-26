# Requirements Review — Findings & Open Questions

**Reviewer:** Business Analyst (Eversports × GoHighLevel)
**Documents reviewed:** `00_master_overview.md`, `01_trial_conversion_followup.md`, `02_trial_member_tag.md`, `03_ghl_pipelines.md`, `04_noshow_recovery.md`, `05_sales_consultant_chatbot.md`, `06_reschedule_assistant.md`, `07_foundation_layer.md`
**Date:** 2026-05-24

---

## A. Decisions confirmed by your answers (and what they change in the docs)

| Question | Your answer | Required doc change |
|---|---|---|
| Eversports data ingress | Browser scraping | No change — already specified |
| Sync direction | **Bidirectional** | **Foundation spec is currently one-way only.** Need a new "Layer 4 — GHL → Eversports writeback" section. We must define which actions write back (lead capture → create customer? booking? reschedule execution? attribute updates?) |
| Boundary with Eversports marketing | Some use cases stay in Eversports; most move to GHL | Need an explicit list per use case: which Eversports automations are turned off, which stay (e.g. booking confirmation emails) |
| Multi-tenancy | **One GHL sub-account per studio location** | Master overview currently says "per studio". Needs to say "per location" everywhere. Affects onboarding, billing, and how multi-location studios are modelled |
| AI scope v1 | Conversation AI (inbound + outbound). Voice later | Use case 04 stays text-only in v1; voice noted as v2 roadmap. Use case 05 also stays text-only |
| Consent / GDPR | Build within GHL — concept design needed | **New deliverable** — design a GHL-native consent capture, storage, channel-level granularity (email vs WhatsApp vs voice), and unsubscribe handling. Mapped to DACH legal basis (UWG §7 / DSGVO Art. 6/7) |
| Channels v1 | WhatsApp, Email, Voice (later) | Conflict: use case 04 mentions Instagram + Facebook. Need to decide if social is in or out of v1 |
| Commercial model | Flat tier per location + AI usage fee | Foundation needs **AI usage metering** per studio location (token counts, message counts) — not currently in spec |

---

## B. Architectural gaps I found

### B1. Sync frequency cannot meet "+2 hour" timing promise
The foundation runs at 07:00, 13:00, 19:00 daily. Several use cases promise sends at `session_end_time + 2 hours`. A class ending at 10:30 will not appear in GHL until the 13:00 sync, so message-1 send time effectively becomes 13:00–15:00, not 12:30. Same for use case 03. We should either (a) increase sync cadence to hourly during business hours, (b) trigger a sync immediately after each scheduled class block ends, or (c) re-write the SLA promise to "within X hours of the next sync".

### B2. GHL "tag added" trigger semantics vs. tag lifecycle
Foundation applies and removes tags in the same sync run (e.g. `trial-purchase-detected` is added by foundation, then immediately consumed by UC02 which removes it). GHL "tag added" triggers fire on transition from absent → present and are typically debounced. We need to confirm the GHL workflow engine reliably picks up tags that exist for only seconds. Safer pattern: foundation applies the tag and waits N minutes before removing, OR uses GHL Inbound Webhook trigger instead of tag-based triggers for foundation-driven events.

### B3. Bidirectional sync mechanism is undefined
You've confirmed bidirectional, but the foundation spec is read-only. To write back, options are (a) extend the browser scraper to perform actions in the Eversports admin UI (creating customers, cancelling/rescheduling bookings), (b) use the Eversports Provider API where applicable (note: Provider API is read-only — won't help here), or (c) human-in-the-loop (GHL task → admin clicks in Eversports). The reschedule flow already uses option (c). We need to decide writeback strategy per action.

### B4. Scraping resilience and ToS risk
Browser scraping of Eversports admin is not addressed for: 2FA / captcha, session expiry, HTML/URL changes, IP rate limits, and — critically — whether this is permitted by Eversports' contract. This is a single point of failure for the whole product. Suggest: (a) add a monitoring + alerting layer, (b) build retry/circuit-breaker logic, (c) pursue a partnership/data-sharing agreement with Eversports as a parallel risk-mitigation track.

### B5. Google Sheets as datastore
Sheets is fine for an MVP with a handful of locations but won't scale. 10M-cell limit, no transactional guarantees, no audit trail, slow API. Consider Postgres (Supabase / Neon) from day one — same delta logic, same effort to build, dramatically more robust. Worth deciding now since refactoring later is painful.

### B6. AI usage metering not in foundation
Commercial model requires AI usage billing. Need: per-message token counts, per-studio aggregation, monthly summary, hard caps + soft alerts. Add a `ai_usage_log` table/sheet with columns: timestamp, studio_id, use_case, contact_id, model, prompt_tokens, completion_tokens, cost.

### B7. Observability and alerting
Sync log table exists but no alerting on: failed scrapes, GHL API quota exhaustion, AI errors, message delivery failures. For a production SaaS we need owner-facing health monitoring per location.

### B8. Idempotency and replay
If a sync fails mid-run, no described recovery behaviour. We need: per-contact sync state (last successful field), retry on next run only for failed contacts, and explicit deduplication for owner notifications (so a retry doesn't email the owner twice).

---

## C. Use-case-level gaps I found

### C1. WhatsApp Business policy (delivery blocker)
WhatsApp Business API only allows free-form business-initiated messages within 24h of a customer-initiated message. Messages 3–6 of trial follow-up (sent every 5 days) are outside the window. Without pre-approved WhatsApp template messages, those messages **will not deliver**. Same risk for use case 04 outbound card upsell and the 7-day re-trigger loop. We need a WhatsApp template strategy: a small set of pre-approved templates with variable placeholders, with AI personalization scoped to where it's allowed.

### C2. Authentication friction in use case 04
Sending a verification link every time a known customer messages will hurt response rate. Industry practice: identify by inbound channel (the WhatsApp number itself, the email address). Suggest making authentication soft-required only for sensitive actions (purchase confirmation, reschedule submission). Channel identity is enough for general conversation.

### C3. `HANDOFF_REQUIRED` sentinel will leak to customer
UC04 asks AI to append "HANDOFF_REQUIRED" at end of response and calls it "invisible to the customer". It's not — unless the workflow strips it before send. Add an explicit strip step in the GHL workflow.

### C4. Multilingual STOP keyword
DACH customers will type STOPP, AUFHÖREN, ABMELDEN, KEINE WERBUNG, etc. STOP-only detection will fail many opt-out attempts (regulatory issue under DSGVO). Need a per-language opt-out keyword list, ideally regex.

### C5. Use case 02 over-narrow trial detection
Logic `len(prior_products) == 1` misses customers who had a trial + a voucher / merch / single drop-in. Better logic: "had at least one trial product historically AND now has a new non-trial, non-trial-class-pass product".

### C6. No-show vs late-cancellation collision (UC03)
Open question already flagged in the doc. Critical for UX — empathetic "we missed you" email to someone who actively late-cancelled is awkward. Recommend: distinguish in the no-show export, branch the AI prompt accordingly, OR send only for true no-shows.

### C7. UC05 availability check has no data source
The reschedule flow checks availability against `active_sessions_data`. Browser scraping of the admin panel does not naturally produce open-slot counts per class. The Eversports **Provider API** does (it's read-only but exposes activity schedules + total/available spots). Suggest layering the Provider API alongside scraping just for schedule + availability — €50/month/location is plausible inside the tier pricing.

### C8. UC05 customer summary message implies confirmation
Wording "Our team will confirm your new booking shortly" is good, but the AI's preceding summary "Requested new slot: …" could be misread as confirmed. Strengthen language: "**This is a request — not confirmed yet.** Our team will book it shortly."

### C9. UC05 multiple upcoming bookings unaddressed
Active members often have 5–10 upcoming bookings. Spec defaults to "next upcoming" and "flag for admin if multiple". For active members this means almost all reschedule requests hit manual triage. Recommend: AI asks which booking to reschedule when multiple exist, scoped to next 14 days.

### C10. UC05 cancellation intent dead-ends
"CANCEL → future use case". In the meantime, the customer is told nothing or the AI fumbles. Even a v1 should at least: detect cancel intent, send "Got it — I'll pass this to the team to confirm", create the same kind of admin task.

### C11. UC04 7-day outbound re-trigger has no cap
Spec says re-send every 7 days until conversion or stage change. Could harass customers indefinitely. Cap at 3 attempts, then move to a "stale lead" stage.

### C12. UC04 vs UC02 owner notification duplication
If a customer converts via chatbot (UC04 sends owner notification "sale closed"), foundation sync later applies `trial-purchase-detected` and UC02 also notifies owner. Owner gets two emails for one conversion. UC02 should check for `chatbot-converted` tag and skip notification if present.

### C13. UC01 message-1 timing dependency on sync timing (see B1)

### C14. UC02 `new_package_name` field missing from data model
UC02 spec references `contact.new_package_name` but it's not in the master custom fields table. Add it, or use `converted_package_name` consistently.

### C15. `auth_verified` contradiction
Foundation says session-scoped, never on contact. UC04 says "custom fields written by this use case: `auth_verified`". Reconcile — session-scoped only.

### C16. Tag mutual exclusivity assumes single-product state
Rules like `card-active` removing `trial-active` and `membership-active` removing `card-active` assume customers hold one product at a time. Some studios let members hold an active membership + an open card pack. Need to decide: enforce single-product or allow co-existence.

### C17. `is_card()` fallback is unsafe
Defined as "not trial AND not membership". Misclassifies vouchers, articles, drop-ins, workshops. Use the Eversports product category field if available; otherwise maintain a per-studio explicit product-type allow-list.

### C18. Pipeline "New lead" stage is unclear
Every Eversports customer is already past lead — they've booked. What is "New lead" tracking? Suggest: customers with no product purchased yet (just created an account, not yet booked).

### C19. JSON fields may exceed GHL custom-field length limits
`booking_history` (30 days), `products_purchased` (lifetime), `active_sessions_data` (all upcoming sessions) stored as JSON in GHL text fields. GHL text custom fields cap around 4000 chars. Active members may exceed. Recommend storing summaries in GHL and full JSON in Google Sheets / Postgres, joined at query time.

### C20. Time zone: per location, not per studio
With one sub-account per location and multi-location studios, time zone is a per-location setting (matches the answer about per-location sub-accounts). Currently a per-studio config field.

---

## D. Cross-cutting / compliance gaps

### D1. GDPR consent capture model (new design needed)
You asked me to design this in GHL. High-level concept (full spec in dev doc):

- A `consent_marketing_email`, `consent_marketing_whatsapp`, `consent_marketing_voice` triple-boolean per contact (custom fields), each with a paired `_source` and `_timestamp` field.
- Initial source: (a) studio onboarding form for legacy customers (one-time sweep), (b) first booking confirmation flow asks customer to opt in (double opt-in for email per DACH UWG §7), (c) WhatsApp template offering opt-in on first contact.
- All outbound messages gated on the matching channel consent.
- Universal STOP / STOPP / unsubscribe handling flips the relevant boolean to false and writes an `_unsubscribed_at` timestamp.
- An audit log preserved (Postgres / Sheets) showing source, timestamp, message text shown to the customer.
- Per-contact preference centre URL the customer can use to view + change consent.

### D2. DPA / data flow documentation
We will become a data processor for each studio. Need a Data Processing Agreement (DPA) template, sub-processor disclosures (GHL, our AI provider, Google for Sheets, any other), and a documented data-flow diagram for each customer-facing record.

### D3. Voice channel (later) compliance preview
Voice / SMS in DACH is heavily restricted. When voice comes in, you'll need explicit voice-channel consent + DACH carrier-specific rules. Not a v1 issue, but worth noting now so the consent model is built with voice as a first-class channel.

---

## E. Smaller cleanups I will apply directly when I update the docs (no decision needed)

These are inconsistencies and editorial fixes I'll silently fix while updating:

- Standardize `eversports_active_products` vs `available_products` naming throughout
- Add `new-contact` tag to the tag glossary (currently used by foundation but not listed)
- Add `days_until_expiry` as a derived field, not stored
- Move `first_name` / `last_name` references from custom fields to GHL standard fields
- Replace "per studio" with "per location" everywhere (master overview, foundation config)
- Reconcile `auth_verified` to be session-scoped only
- Add the `new_package_name`/`converted_package_name` consolidation
- Add an explicit "strip HANDOFF_REQUIRED before send" step in UC04
- Add UC02 dedupe-with-UC04 logic (skip if `chatbot-converted` tag present)
- Add cap of 3 attempts on UC04 7-day outbound re-trigger
- Add multilingual STOP keyword list
- Strengthen UC05 customer-summary wording to make "request, not confirmed" unambiguous
- Add UC05 v1 cancel-intent stub (admin task + customer ack)
- Add `ai_usage_log` schema for billing
- Tag glossary cleanup: tags applied only by foundation vs only by use cases vs both
- Add Eversports Provider API as a parallel read source for activity schedules + availability

---

## F. Top blocking questions I still need answered before updating the doc

1. **Bidirectional writeback scope** — which actions should write back from GHL to Eversports? (Lead → create customer? Reschedule task execution? Cancel? Booking? Attribute updates?)
2. **Eversports marketing automations that stay live** — which Eversports automations should remain on (transactional booking confirmations, payment receipts) versus turned off (renewal reminders, trial follow-ups, newsletter)?
3. **Social channels in v1** — keep Instagram + Facebook as v1 channels alongside WhatsApp + Email, or push them to v2?
4. **Sync cadence** — increase to hourly during business hours (more reliable +2h timing), keep at 3×/day (faster to build, but with documented SLA slip), or trigger sync after each class block ends?

I've drafted these as multiple-choice options below.
