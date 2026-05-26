# Eversports × GHL Automation — Master Overview

> **Revision note (v2 — 2026-05-24):** Major update reflecting confirmed decisions: bidirectional sync, event-driven cadence, per-location sub-accounts, GHL-native consent model, WhatsApp + Email scope in v1 (social and voice in v2), AI usage metering, and use case 05 switched from admin-task to auto-execute. See `CHANGELOG.md` for the full diff.

## Context

You are building an automation product that sits on top of the **Eversports** booking platform and **GoHighLevel (GHL)** CRM. The system bidirectionally syncs data between Eversports and GHL (read via admin-panel scraping, write via admin-panel browser automation — the Eversports Provider API is NOT used), and runs automated AI-powered use case workflows on top of that data.

The product is sold to Pilates and fitness studios that use Eversports as their booking platform. **Each studio location gets its own GHL sub-account** — a studio operating three locations is provisioned as three independent sub-accounts, each with its own scraper instance, consent state, and AI usage meter.

**Commercial model:** flat fee per location with tiered packages + metered AI usage fee.

---

## System Architecture

```
                      INBOUND messages from GHL
   (WhatsApp DM · Email · Instagram DM + comment · Facebook DM + comment)
                              │
┌─────────────────────────────▼─────────────────────────┐
│  LAYER 6  GATEKEEPER (Claude Haiku classifier)         │
│           Classifies every inbound message:            │
│           ▸ Actionable  → routes to UC04 / UC05 / owner│
│           ▸ Noise       → auto-react or silent ignore  │
│           ▸ Opt-out     → routes to consent gate       │
│           Every decision logged + owner-overridable.   │
└──────────────────┬────────────────────────────────────┘
                   │ routed messages
                   ▼
┌───────────────────────────────────────────────────────┐
│                     USE CASE LAYER                     │
│   AI-powered workflows: trial follow-up, sales         │
│   consultant chatbot, booking assistant, trial→member  │
│   tag. All read from GHL only.                         │
└──────────────────┬────────────────────────▲────────────┘
                   │ enqueue writeback       │ read state
                   ▼                         │
┌───────────────────────────────────────────────────────┐
│                   FOUNDATION LAYER                     │
│  Layer 1  READ ingress                                 │
│           • Eversports admin scraper (CSV exports —    │
│             the SINGLE Eversports data ingress path)   │
│  Layer 2  Datastore (Postgres primary; Sheets mirror   │
│           for ops visibility) + delta engine           │
│  Layer 3  GHL sync (contact upsert, tag engine,        │
│           pipeline engine, AI usage logger)            │
│  Layer 4  WRITE — Eversports writeback executor        │
│           • Create customer · Create booking ·         │
│             Reschedule · Cancel                        │
│  Layer 5  Consent gate — every outbound message is     │
│           gated on per-channel consent state           │
│  Layer 6  Gatekeeper — every inbound message is        │
│           classified and routed (see diagram above)    │
└──────────────────┬────────────────────────────────────┘
                   │
                   ▼
              Eversports admin panel
```

---

## Foundation Layer

The foundation layer is the data + action platform. It is not a use case. It runs continuously, keeps GHL contacts up to date with Eversports data, and performs writeback actions on demand. All use cases read state exclusively from GHL and request writebacks via a queue — they never call Eversports directly. See `07_foundation_layer.md` for the full technical spec.

**Technology stack:**
- Read: Eversports browser scraper (Playwright) reading admin CSV exports — the **only** Eversports ingress path
- Datastore: Postgres (primary, transactional) + Google Sheets (read-only ops mirror)
- CRM: GoHighLevel via API v2 (OAuth, `X-GHL-Signature`)
- Write: Eversports browser writeback executor (Playwright) consuming a Redis/Postgres job queue
- AI: Anthropic Claude (primary) + fallback model; usage metered per studio location
- Consent: GHL-native (custom fields + tags) — see `08_consent_model.md`

**Sync pattern:**
- **Read sync** — delta only; only changed fields pushed to GHL each run
- **Write sync** — request-driven; GHL workflows enqueue writeback actions; foundation executes against Eversports and reports back

**Schedule:**
- **Event-driven read sync** — triggered 15 minutes after each class block ends, computed from the day's active appointments report. This is what honours the "+2 hours after session end" timing promise used by trial follow-up and no-show recovery (final slip ≤ 15 min).
- **Hourly catch-up read sync** — every hour during business hours (07:00–22:00 studio local time) for product/membership/booking changes not pinned to a class end.
- **Overnight reconciliation** — 03:00 daily, full pull of all reports for the prior 24 hours to repair any missed deltas.

### 1. One-Time Historical Sync (runs once on setup)

Two ingest modes are supported. **Mode A — CSV bootstrap** is the default and runs before scraper credentials are even configured: the studio owner exports 3–5 reports from the Eversports admin UI on day one and uploads them via `POST /api/v1/admin/locations/{id}/bootstrap`. The foundation parses, normalises, and seeds Postgres. **Mode B — scraper historical sweep** is the fallback when CSVs aren't provided: the scraper performs a 30-day backfill on its first run plus pulls the next 14 days of activity schedule via the admin activities export.

Both modes share the downstream steps:

- Merges data into GHL contacts as baseline
- Creates contacts in GHL if they don't exist
- Computes derived fields (lifetime sessions attended, last session, products purchased, no-show count, active package)
- Applies initial tags + initial pipeline stages
- Sends consent opt-in invitation to legacy contacts (see `08_consent_model.md`)
- Marks `historical_sync_flag = "complete"`, unblocking the event-driven scheduler

CSV column maps, parsing rules (BOM handling, semicolon-vs-comma, German vs English headers, date formats `DD.MM.YYYY` vs `DD/MM/YYYY HH:MM`, phone normalisation), and the bootstrap validation report are documented in `07_foundation_layer.md` § "One-Time CSV Bootstrap Protocol". Sample exports live in `requirements_v2/sample_exports/`.

### 2. Recurring Read Sync (event-driven + hourly catch-up)

Pulls and merges the following Eversports reports into GHL:

| Report | Source | Notes |
|---|---|---|
| Active appointments & registrations | Admin export `?export=active` | Drives event-driven trigger schedule |
| All appointments & registrations | Admin export `?export=all` | Full booking history |
| Booking list | Admin export `?export=booking-list` | Package/session usage |
| Active products & membership packages | Admin separate endpoint | Drives package detection |
| Activity schedule + availability | Admin activities export (`all activities.csv`-style) | Source of `available_spots` per session, derived as `Max. Teilnehmer − Angemeldet`. Refreshed at each sync cadence; staleness mitigated by UC05's `≥ 2 spots` safety margin |

### 3. Today's Sessions & Bookings (live view, part of each event-driven run)

Captures per booking for today:

- Customer name and GHL contact ID
- Class/session name
- Session start time
- Session end time ← drives event-driven trigger
- Package type used to book (trial / card / membership)
- Sessions used vs. total sessions on package
- Attendance status (attended, no-show, upcoming, late-cancelled)

### 4. Writeback Queue

Use case workflows enqueue writeback actions. The foundation's writeback executor processes the queue, performs the action in Eversports, and writes the result back to GHL.

| Action | Trigger | Payload | Idempotency key |
|---|---|---|---|
| `create_customer` | New GHL contact with no `eversports_customer_id` | name, email, phone, marketing consents | hash(email) |
| `create_booking` | UC04 chatbot confirms booking intent | customer_id, activity_id, session_datetime | hash(customer_id, activity_id, session_datetime) |
| `reschedule_booking` | UC05 customer confirms slot | booking_id, new_session_id, reason | hash(booking_id, new_session_id) |
| `cancel_booking` | UC05 cancel intent confirmed | booking_id, reason | hash(booking_id, "cancel") |

Each job is retryable, idempotent, and emits success/failure events back to GHL via webhook.

### 5. Customer Authentication Sub-Workflow (soft-auth model)

Reusable sub-workflow called by use cases that perform sensitive actions. **Identification is by channel (the inbound WhatsApp number or email address); full verification is required only for purchase confirmation, reschedule/cancel submission, or contact-data changes.**

**Soft-auth steps (for general conversation):**
1. Resolve contact by inbound channel identity (phone for WhatsApp, email for Email)
2. If found, set session variable `auth_verified = true`
3. If multiple contacts match, escalate to verification

**Hard-auth steps (before sensitive action):**
1. Send one-time code or verification link via email
2. Wait for confirmation
3. Set session variable `auth_verified_hard = true` for the action

`auth_verified` is session-scoped only — never stored on the contact.

---

## GHL Data Model

### Custom Fields (set on GHL contact by foundation)

| Field Name | Type | Source / purpose |
|---|---|---|
| `eversports_customer_id` | Text | Eversports ID — empty until first writeback creates the customer |
| `eversports_location_id` | Text | Foundation — the location this contact belongs to |
| `active_package_type` | Text | trial / card / membership / voucher / drop-in |
| `active_package_name` | Text | e.g. "3-session trial" |
| `active_package_sessions_total` | Number | Total sessions on package |
| `active_package_sessions_used` | Number | Sessions used so far |
| `active_package_sessions_remaining` | Number | Calculated |
| `active_package_expiry_date` | Date | Package expiry |
| `last_session_date` | Date | Most recent attended session |
| `last_session_end_time` | DateTime | End time of most recent session |
| `last_class_name` | Text | Name of last attended class |
| `total_sessions_attended` | Number | Lifetime count |
| `converted_package_name` | Text | First non-trial package purchased (canonical — UC02 writes here; old `new_package_name` removed) |
| `conversion_date` | Date | Date trial conversion was detected |
| `conversion_source` | Text | "chatbot" / "direct" / "in-studio" — used for UC02 dedupe with UC04 |
| `no_show_count` | Number | Lifetime no-show count — kept for at-risk segmentation in UC04 even though UC03 was removed |
| `eversports_active_products` | Text (JSON-summary, ≤ 3500 chars) | Active products from Eversports. Full data in Postgres if it exceeds limit |
| `last_chatbot_interaction` | DateTime | Timestamp of most recent AI conversation |
| `chatbot_outbound_attempts` | Number | Count of outbound chatbot re-triggers — capped at 3 |
| `booking_history_summary` | Text | Compact summary of last 30 days bookings (full JSON in Postgres) |
| `products_purchased_summary` | Text | Compact summary of lifetime products (full JSON in Postgres) |
| `upcoming_session_name` | Text | Next booked session name |
| `upcoming_session_date` | Date | Next booked session date |
| `upcoming_session_start_time` | DateTime | Next booked session start time |
| `upcoming_session_end_time` | DateTime | Next booked session end time |
| `upcoming_sessions_count` | Number | Total number of upcoming bookings — used by UC05 to decide single vs multi-booking flow |
| `reschedule_reason` | Text | Customer-provided reason when late cancel applies |
| `reschedule_requested_date` | Date | Customer's preferred new date |
| `reschedule_requested_time` | Text | Customer's preferred new time |
| `sessions_attended_this_month` | Number | For membership at-risk detection |
| `sessions_attended_last_month` | Number | For membership at-risk detection (50% drop check) |
| `sessions_per_week_last_month` | Number (decimal) | Calculated by foundation: count of bookings the customer made in the prior 30-day window, divided by 4.33. Drives the Card → Membership ready upsell trigger (UC04). |
| `last_booking_date` | Date | For card low-attendance detection (14 day check) |
| `pipeline_lead_stage` | Text | Current stage in lead-to-sale pipeline |
| `pipeline_card_stage` | Text | Current stage in card pipeline |
| `pipeline_membership_stage` | Text | Current stage in membership pipeline |
| **Consent fields (new — see `08_consent_model.md`)** | | |
| `consent_marketing_email` | Boolean | True iff customer opted in to marketing email |
| `consent_marketing_email_source` | Text | "onboarding-form" / "double-opt-in" / "studio-import" / "preference-centre" |
| `consent_marketing_email_at` | DateTime | When consent was captured |
| `consent_marketing_whatsapp` | Boolean | True iff customer opted in to WhatsApp marketing |
| `consent_marketing_whatsapp_source` | Text | Same source enum |
| `consent_marketing_whatsapp_at` | DateTime | When consent was captured |
| `consent_marketing_voice` | Boolean | Reserved for v2 (Voice AI) — defaults false |
| `consent_marketing_voice_source` | Text | Reserved |
| `consent_marketing_voice_at` | DateTime | Reserved |
| `consent_revoked_email_at` | DateTime | Set on STOP/STOPP/unsubscribe |
| `consent_revoked_whatsapp_at` | DateTime | Set on STOP/STOPP/unsubscribe |

**Fields removed in v2 (use these replacements):**
- `auth_verified` removed from contact — session variable only
- `new_package_name` removed — use `converted_package_name`
- `booking_history` (raw JSON) → `booking_history_summary` (compact text); full JSON in Postgres
- `products_purchased` (raw JSON) → `products_purchased_summary` (compact text); full JSON in Postgres

### GHL Tags (applied/removed by foundation and use cases)

| Tag | Applied when | Removed when |
|---|---|---|
| `new-contact` | Foundation creates contact in GHL | Removed after first sync completes |
| `trial-active` | Active trial package detected | Trial expires or non-trial product purchased |
| `trial-last-session` | Sessions used = total on trial package today | UC02 on conversion · otherwise historical |
| `trial-follow-up-active` | UC01 sequence started | UC01 on any exit |
| `trial-purchase-detected` | Foundation detects new non-trial product on trial-only contact | UC02 after processing |
| `trial-converted` | UC02 on conversion | Never removed |
| `trial-not-converted` | UC01 completed 6 follow-ups without conversion | Never removed |
| `opted-out` | Customer replied STOP/STOPP/AUFHÖREN/ABMELDEN | Manual by studio staff |
| `chatbot-active` | Inbound chatbot conversation started | Any exit |
| `chatbot-sale-initiated` | AI sent purchase link | Purchase confirmed |
| `chatbot-converted` | Sale closed via chatbot | Never removed (used by UC02 to dedupe owner notif) |
| `chatbot-handoff` | UC04 handoff to human triggered | Studio resolves conversation |
| `card-active` | Card package detected | Card expired or upgraded to membership |
| `membership-active` | Membership package detected | Membership expired |
| `low-attendance` | No booking in 14 days while card sessions remain | Booking made or upgraded |
| `membership-ready` | Card customer's `sessions_per_week_last_month` exceeds the per-location threshold (default 2) — i.e. high-frequency card customer who'd benefit from a membership | Membership purchased |
| `at-risk` | Membership attendance dropping (see UC04 spec) | Attendance recovers or churns |
| `renewal-due` | Membership expiry within 14 days | Renewed or churned |
| `renewed` | New membership purchased after expiry | Set to active on new start date |
| `churned` | Package expired with no renewal detected | Re-activated |
| `reschedule-in-flight` | UC05 writeback job enqueued | Writeback succeeds → removed by foundation |
| `cancel-in-flight` | UC05 cancel writeback job enqueued | Writeback succeeds → removed by foundation |
| `writeback-failed` | Any writeback action failed after retries | Owner manually resolves |
| `lapsed` | No bookings in last 30 days | New booking |

---

## Use Case Layer

### Priority 1 — Quick Wins (sell first)

| # | Use Case | Trigger | Channels |
|---|---|---|---|
| 1 | Trial conversion follow-up | `trial-last-session` tag applied by foundation | WhatsApp + Email |
| 2 | Trial → member tag | Foundation detects new non-trial product on trial-historical contact | GHL notification + owner email (dedupes against `chatbot-converted`) |

### Priority 2 — High Value (sell second)

| # | Use Case | Trigger | Channels |
|---|---|---|---|
| 4 | Sales consultant chatbot | Inbound: any conversational message routed by the gatekeeper (Inquiry / Trial reply / Membership question categories) · Outbound: pipeline triggers (max 3 attempts per stage) | Inbound: WhatsApp · Email · Instagram DM + comment · Facebook DM + comment (all via gatekeeper). Outbound: WhatsApp + Email (v1) · Voice (v2) |

> **UC03 removed (2026-05-24, later same day):** the no-show recovery use case was scrapped. The Eversports no-show export does not expose the data we need to reliably distinguish true no-shows from late-cancellations, and Eversports' own native no-show handling is sufficient for v1. Tags, fields, and triggers related to no-show have been removed from all specs. Eversports keeps full responsibility for no-show / late-cancel communications.

### Priority 3 — Operational (sell third)

| # | Use Case | Trigger | Channels |
|---|---|---|---|
| 5 | Booking assistant (schedule / reschedule / cancel) | Inbound message classified by the gatekeeper as `Booking`. UC05 then sub-classifies intent as SCHEDULE, RESCHEDULE, or CANCEL. | Same channel as request · auto-executes via Eversports writeback (or admin task per location config) |

### Future (v2)

- Voice AI for inbound + outbound calls (requires per-contact voice consent)
- Lapsed customer win-back automation
- Card pipeline `Churned` → win-back
- Gatekeeper override-training loop (overrides become labelled training data for an improved classifier per location)
- Trial-bought-never-booked nudge sequence (surfaced as an Insight in v1; promoted to automation in v2 once policy is validated)
- Churn re-engagement automation (currently surfaced as Insight + handled via owner personal outreach)

### Insights / Opportunities (v1 product surface — new)

A top-level surface that turns raw foundation + GHL data into actionable revenue recommendations, refreshed nightly at 06:00 local time. Five insight categories ship in v1:

| Insight | Computed from | Action |
|---|---|---|
| **Membership upsell candidates** | Card customers with `sessions_per_week_last_month > card_upsell_min_sessions_per_week` | Queue chatbot outbound · or show that chatbot is already handling |
| **At-risk member roster** | Members with `at-risk` tag (no booking 14d or attendance drop ≥ 50%) sorted by membership value | Personal owner message (re-engagement automation is v2) |
| **Trial drop-off** | Contacts who bought a trial product in last 14d but `total_sessions_attended == 0` | Send manual booking nudge · candidate for v2 automation |
| **Capacity rebalancing** | Activity schedule analysis · find full-with-waitlist classes + under-40%-capacity classes | Recommend slot to add or move · owner makes the call in Eversports admin |
| **Cohort LTV** | Trial cohort analysis · group by month, track conversion rate + LTV by acquisition source | Visibility only — informs future budget allocation |

The Insights page is the highest-leverage UI surface — it embodies the product's stated purpose ("harvest data → generate revenue"). A teaser card on the Dashboard surfaces the top headline ("5 new opportunities this week · +€940/mo potential") with a link through to the full page. Impact estimates use each studio's historical conversion rate, not generic benchmarks.

---

## GHL Opportunity Pipelines

Three pipelines run in parallel. A contact can be in the lead pipeline AND one product pipeline simultaneously. See `03_ghl_pipelines.md` for full stage logic.

| Pipeline | Entry point | Purpose |
|---|---|---|
| Lead to sale | Contact created by foundation, no product yet | Tracks every contact from first detection to first conversion |
| Card package | Card product detected | Tracks card customers — attendance health, upsell to membership |
| Membership | Membership product detected | Tracks members — retention, at-risk detection, renewal |

### Pipeline stage summary

- **Lead to sale:** New lead (no product) → Trial sold → Trial booked → Converted (card) / Converted (membership) → Lost
- **Card package:** Standard card → Low attendance warning → Membership ready → Converted → Churned
- **Membership:** Active → At risk → Renewal due → Renewed → Churned

---

## GHL Sub-Account Settings

Set once per location on onboarding. Stored as GHL custom values at sub-account level.

| Setting | Description | Example |
|---|---|---|
| `location_name` | Studio location display name | "Flow Pilates — Mariahilf" |
| `location_timezone` | IANA timezone | `Europe/Vienna` |
| `late_cancel_window_hours` | Hours before session that triggers late cancellation policy | 24 |
| `studio_owner_email` | Email for admin notifications across all use cases | owner@studio.com |
| `studio_owner_name` | Owner first name for email salutations | Anna |
| `studio_name` | Studio brand name injected into AI prompts | "Flow Pilates" |
| `studio_active_promotions` | Current promotions — updated manually by owner · injected into AI prompts · NOT a contact-level field | "Jan offer: 10% off membership" |
| `renewal_handling_mode` | Either-or choice — `studio_outreach` (default; STUDIO sends renewal nudges via UC04) or `defer_to_eversports` (STUDIO stays silent on renewals; Eversports' native reminder is the only one customers receive) | `studio_outreach` |
| `card_upsell_min_sessions_per_week` | Threshold for the Card → Membership ready upsell trigger. Fires when a card customer's `sessions_per_week_last_month` exceeds this value. | `2` |
| `gatekeeper_enabled` | Master switch. If false, all inbound messages route through the legacy direct-to-use-case path. | `true` |
| `gatekeeper_confidence_threshold` | Minimum classifier confidence (0.0–1.0) before auto-action. Below this, the message is escalated to the owner regardless of category. | `0.7` |
| `gatekeeper_noise_action` | Per-noise-category JSON policy. Each noise category (`acknowledgment`, `emoji_reaction`, `social_compliment`, `off_topic`, `spam`) maps to either `silent_ignore`, `react_emoji`, or `auto_reply_template`. | see `08_consent_model.md` defaults |
| `gatekeeper_owner_alert_categories` | Comma list of categories that auto-page the owner. Default: `complaint, injury_medical, billing_dispute, low_confidence`. | `complaint,injury_medical,billing_dispute,low_confidence` |
| `stop_keywords` | Per-locale opt-out keyword regex | `^(stop|stopp|aufhören|abmelden|keine werbung)$/i` |
| `whatsapp_templates_namespace` | WhatsApp Business template namespace for this location | `wa_template_ns_xxx` |
| `ai_monthly_budget_usd` | Hard cap on AI spend per month (owner sees warning at 80%) | 200 |
| `consent_default_locale` | Default language for consent capture forms | `de-AT` |

---

## Eversports Automations Boundary (which native flows stay live)

The following Eversports native automations remain enabled — our system does **not** replicate them:

| Eversports automation | Reason kept |
|---|---|
| Transactional booking confirmations & receipts | Tightly coupled to invoicing/financial flow |
| Class reminders before sessions | Reliable transactional, no AI value-add |
| Membership / SEPA payment failure dunning | Coupled to billing system |
| Waitlist promotion notifications | Spot-opening detection requires real-time access we don't have |
| No-show / late-cancel comms | UC03 removed in v2; Eversports' own no-show messages stay on |

The following Eversports native automations should be **turned off** when our product activates (verified during onboarding):

- Trial follow-up emails (replaced by UC01)
- Renewal reminder emails (replaced by UC04 when `renewal_handling_mode = studio_outreach`; preserved as the sole renewal touch when `renewal_handling_mode = defer_to_eversports`)
- Generic marketing newsletters from Eversports (the studio's own newsletter ops move to GHL)

---

## Consent & Compliance (v2 addition — see `08_consent_model.md`)

We operate in DACH and elsewhere in the EU. Marketing communications via WhatsApp and email require explicit opt-in. Our consent model lives in GHL:

- Per-channel boolean (`consent_marketing_email`, `consent_marketing_whatsapp`, `consent_marketing_voice` for v2)
- Each paired with `_source` (onboarding form / double-opt-in / studio-import / preference-centre) and `_at` (timestamp)
- All outbound use case messages gated through a single **consent gate** action before send
- Universal opt-out: any STOP / STOPP / AUFHÖREN / ABMELDEN / UNSUBSCRIBE message flips the relevant boolean to false and stamps `consent_revoked_<channel>_at`
- A customer-facing preference centre URL (one per location) lets contacts view/change their consents

Legacy contacts (existing Eversports customers at onboarding) receive a one-time opt-in invitation; non-responders default to `false` and receive only transactional communications.

---

## General Design Principles

- **Foundation owns all data and all Eversports interactions.** Use cases never read or write Eversports directly.
- **GHL is the source of truth** for contact state, tags, pipelines, and workflow control.
- **AI messages are personalised per customer** from GHL custom fields — but **WhatsApp business-initiated messages outside the 24h customer service window must be pre-approved templates** with variable placeholders. AI personalisation applies inside the placeholder values. See UC01 and UC04 for template strategy.
- **Universal opt-out detection is multilingual** — at minimum STOP, STOPP, AUFHÖREN, ABMELDEN, UNSUBSCRIBE, KEINE WERBUNG.
- **Consent gate is mandatory** on every outbound message from message 1 onwards. Even the first message must verify the channel-level consent boolean.
- **Conversion exit is always active** — if a contact converts during a sequence, the sequence ends immediately and they are re-tagged.
- **Timing guard for time-based sends** — classes run 08:00–21:00. Any calculated send time at or after 21:00 is held and sent at 09:00 the following morning.
- **Soft-auth by channel identity** — general conversation does not require verification; hard-auth (one-time email link) gates only sensitive actions (purchase, reschedule, cancel, profile change).
- **Owner notifications follow the 3-action standard with dedupe** — GHL internal notification + GHL task + email. UC02 suppresses its notification if `chatbot-converted` tag is present (UC04 already notified).
- **Writeback actions are idempotent** — re-running the same job (same idempotency key) is safe.
- **AI usage is metered** per location per use case per call (token counts, cost) for billing transparency.
