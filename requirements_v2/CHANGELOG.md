# Requirements CHANGELOG

## v2 — 2026-05-24

Major revision incorporating answers from Emir's clarifying-question round and the BA gap analysis (`REVIEW_FINDINGS.md`).

### High-impact architectural changes

- **Bidirectional sync added.** Foundation gains Layer 4 (Eversports writeback executor) supporting `create_customer`, `create_booking`, `reschedule_booking`, `cancel_booking` jobs via Playwright admin automation.
- **Event-driven sync cadence.** Replaced fixed 3×/day schedule (07:00 · 13:00 · 19:00) with event-driven runs triggered 15 min after each class block ends + hourly catch-up + nightly reconciliation. This honours the "+2h after session end" timing promise within ≤ 15 min slip.
- **Postgres as primary datastore.** Google Sheets retained only as a read-only operations mirror. Postgres holds the transactional state (contacts, bookings, sessions, writeback_jobs, ai_usage, consent_audit, sync_log).
- **One GHL sub-account per studio LOCATION** (was: per studio). A studio with three locations gets three sub-accounts.
- **Channel scope tightened for v1.** WhatsApp + Email only. Instagram + Facebook + Voice AI deferred to v2.
- **GHL-native consent model (new doc `08_consent_model.md`).** Per-channel boolean + source + timestamp; consent gate workflow on every outbound send; multilingual STOP detection; preference centre URL per contact; `consent_audit` Postgres table.
- **AI usage metering.** New `ai_usage` table; per-call token + cost logging; soft cap (80%) and hard cap (100%) of per-location monthly budget; UC03 falls back to fixed template at hard cap.
- **WhatsApp Business policy compliance.** Trial follow-up messages 1, 3, 5 and all UC04 outbound use pre-approved WhatsApp templates with AI-generated variable values only — free-form AI WhatsApp messages can only be sent within the 24h customer service window.

### Per-document changes

**00_master_overview.md**
- Rewritten end-to-end to reflect all of the above
- "Per studio" → "per location" throughout
- New "Eversports Automations Boundary" section listing what stays native vs. what we replace
- New "Consent & Compliance" section pointing to `08_consent_model.md`
- Updated tag glossary with new tags: `new-contact`, `late-cancel`, `reschedule-in-flight`, `cancel-in-flight`, `writeback-failed`, `chatbot-handoff`
- Removed fields: `auth_verified` (session-only), `new_package_name` (use `converted_package_name`), raw JSON `booking_history` + `products_purchased` (replaced by `_summary` text fields; full JSON in Postgres)
- Added fields: consent triple + revoked timestamps per channel, `eversports_location_id`, `missed_session_was_late_cancel`, `last_no_show_email_sent_at`, `conversion_source`, `chatbot_outbound_attempts`, `upcoming_sessions_count`
- New sub-account settings: `coordinate_with_eversports_renewals`, `stop_keywords`, `whatsapp_templates_namespace`, `ai_monthly_budget_usd`, `consent_default_locale`

**07_foundation_layer.md**
- Rewritten end-to-end
- New Layers 4 (writeback) and 5 (consent gate)
- Event-driven scheduler + hourly catch-up + overnight reconciliation
- Postgres datastore schema enumerated
- No-show vs late-cancel distinction (cross-reference cancellation timestamps)
- 48-hour cooldown on UC03 trigger
- Soft-auth vs hard-auth model (replaces "every conversation needs verification link")
- Helper functions updated for is_voucher / is_merch (used by UC02 widening)
- Tag firing race-condition guard (60s delay between apply and remove on same contact)
- Provider API added as the canonical source for activity schedules + open spots (used by UC05)

**01_trial_conversion_followup.md**
- Updated header to revision note
- WhatsApp messages 1/3/5 explicitly use pre-approved templates `trial_followup_msg1/3/5`
- New "WhatsApp Business policy compliance" section
- Consent gate dependency added
- Multilingual STOP detection (replaces single-word STOP)

**02_trial_member_tag.md**
- Widened detection: any historical trial product + any newly added non-trial / non-voucher / non-merch product (was: "exactly 1 prior product")
- New Step 4: dedupe check against `chatbot-converted` tag within 24h — suppress owner notification if UC04 already notified
- Consolidated `new_package_name` → `converted_package_name`
- New field `conversion_source` distinguishes chatbot vs direct vs in-studio

**03_ghl_pipelines.md**
- Clarified "New lead" stage: contact created with empty `products_purchased`
- Reverted Card → Lost → Card stays at "Converted (card)" even after later churn (it's a permanent achievement)
- Renewed → Active transition clarified (transitional stage)
- 50% attendance drop guard against zero-zero false positives

**04_noshow_recovery.md**
- Now distinguishes `no-show` vs `late-cancel` with separate AI prompts
- 48-hour cooldown enforced by foundation via `last_no_show_email_sent_at`
- `chronic-no-show` tag added for 3+ in rolling 30-day window (future use case route)

**05_sales_consultant_chatbot.md**
- v1 channels reduced to WhatsApp + Email (social → v2, voice → v2)
- Soft-auth by channel identity for general conversation
- Hard-auth (one-time email link) required only before sensitive actions (purchase, reschedule, cancel)
- `HANDOFF_REQUIRED` sentinel replaced with structured JSON output (`{customer_message, handoff_required}`) — eliminates the leak risk
- Outbound re-trigger capped at 3 attempts (was: indefinite every 7 days)
- New coordination flag `coordinate_with_eversports_renewals` — suppress UC04 renewal when Eversports native is active
- Outbound WhatsApp uses pre-approved templates
- Purchase/booking now triggers an Eversports writeback job (create_customer + create_booking)
- Sets `conversion_source = "chatbot"` and `chatbot_converted_at = now()` — used by UC02 dedupe

**06_reschedule_assistant.md (most significant restructure)**
- Switched from admin-task model to **auto-execute via Eversports writeback**
- Added CANCEL intent path (was: punted to "future use case")
- Multi-booking selection flow (was: defaulted to next-upcoming)
- Availability check sourced from Provider API real `available_spots` (was: derived from scraping, no actual source)
- Hard-auth gate before submission
- Customer summary explicitly says "request, pending confirmation"
- Two-stage customer communication: "request received" → wait for writeback → "confirmed" or "team will follow up"
- Tag `reschedule-pending` replaced with `reschedule-in-flight` and `cancel-in-flight` (auto-removed on success)
- `writeback-failed` tag + owner notification on retry-exhausted failure

### New documents

- `08_consent_model.md` — GHL-native consent capture, gating, opt-out flow, audit log, DPA outline
- `CHANGELOG.md` — this file
- `REVIEW_FINDINGS.md` (in repo root) — the BA gap analysis that produced this revision

### Added 2026-05-24 (later same day)

- **One-Time CSV Bootstrap Protocol** (`07_foundation_layer.md` § new section). The studio owner can manually export 3–5 CSVs from Eversports admin (bookings, activities, no-shows, customer list, active memberships) and upload them via a dedicated admin endpoint to seed the baseline before the scraper is even connected. Mode A = CSV bootstrap (default); Mode B = scraper historical sweep (fallback).
- Column maps documented for bookings (English headers, `DD/MM/YYYY HH:MM` dates, `;`-delimited quoted) and activities (German headers, `DD.MM.YYYY` + `HH:MM`, `;`-delimited unquoted). No-shows column map flagged for confirmation since sample was empty.
- Sample exports stored in `requirements_v2/sample_exports/` for use as test fixtures.
- Idempotency rules: `(location_id, email_lower)` for contacts; `(location_id, eversports_booking_id)` for bookings — with sha256 fallback when booking ID column is absent.
- BOM-stripping, delimiter auto-detection, locale auto-detection, German+English date parsers, phone E.164 normalisation.
- Validation report: products discovered + classifier bucket per product (lets studio owner fix misclassifications via `product_keyword_map` before go-live).
- "Newsletter" column from bookings.csv used as a SOFT signal for consent invitation copy (warm vs cold variant) — does NOT auto-grant `consent_marketing_email`.
- Helper `is_card()` strengthened with explicit positive keywords (`karte`, `card`, `pack`, `credits`, `punktekarte`) — was previously residual-only.
- Master overview and DEV_SPEC updated; new M1.5 milestone (CSV bootstrap uploader) added to dev spec with acceptance criteria tied to the sample fixtures.

### Removed 2026-05-24 (later same day) — UC03

- **UC03 (No-Show Recovery) removed.** Reason: the Eversports no-show CSV export does not expose the data needed to reliably distinguish true no-shows from late-cancellations, and Eversports' own native no-show communications already cover this customer touchpoint sufficiently for v1.
- Deleted: `04_noshow_recovery.md`
- Removed from `00_master_overview.md`:
  - UC03 row from Priority 2 use case table
  - Custom fields: `missed_session_date`, `missed_session_name`, `missed_session_end_time`, `missed_session_was_late_cancel`, `last_no_show_email_sent_at`
  - Tags: `no-show`, `late-cancel`, `chronic-no-show`
  - Added "No-show / late-cancel comms" to the "Eversports stays" list
  - Retained: `no_show_count` (still useful for UC04 at-risk segmentation)
- Removed from `07_foundation_layer.md`:
  - "Admin CSV — No-shows" row in read sources
  - "No-show vs late-cancel distinction" section
  - No-show flag computation in `compute_flags()`
  - 48h cooldown logic
  - `no_shows` table from the Postgres schema
  - Related tag rules
- Removed from `03_ghl_pipelines.md`: UC03 row in the Pipeline × Use Case matrix
- Updated `DEV_SPEC.md`:
  - Removed `no_show.py` SQLAlchemy model from the layout
  - Removed `no_show_count` `last_no_show_email_sent_at` from the contacts schema (kept `no_show_count` per master overview decision)
  - Removed M8c milestone
  - Removed UC03 from "Open items that block parts of the build"
  - Note added to scope section pointing to the changelog

### Decisions made 2026-05-24 (later same day) — Provider API & writeback ToS

- **Provider API freshness:** resolved by empirical measurement, not contractual guarantee. New onboarding procedure runs a 14-day audit polling Provider API every 60s alongside the scraper, computes per-location p50/p90/p99 lag, and fills `locations.provider_api_freshness_p99_seconds` + `locations.uc05_slot_min_lead_time_minutes`. Documented in `07_foundation_layer.md` § "Provider API freshness validation".
- **UC05 stale-data safety margin:** UC05 only proposes session candidates with `available_spots >= 2`. Always-on; cheap insurance against minor freshness lag. Documented in `06_reschedule_assistant.md` Phase 5a.
- **Eversports admin browser automation legality:** resolved via studio-attestation in the DPA. New clause added to `08_consent_model.md` placing the contractual relationship between studio and Eversports rather than us and Eversports. Provisioning gate: a location cannot be set to `writeback_mode = auto_execute` until the DPA attestation flag is set.
- **Writeback fallback path:** new `locations.writeback_mode` setting — `auto_execute` (default) or `admin_task`. The same UC04 / UC05 workflows route through this switch. If Eversports declines for a specific location, that location flips to `admin_task` without re-deploy. UC05's admin-task fallback flow is fully specified in `06_reschedule_assistant.md` § "Admin-task fallback mode".
- **DEV_SPEC.md updates:** added M5b (freshness audit) milestone, expanded M5 acceptance criteria with the writeback_mode switch, removed M5 ToS blocker from § 11.

### Removed 2026-05-24 (later same day, again) — Eversports Provider API

- **Provider API removed from the product entirely.** No GraphQL client, no per-location Provider API token, no €50/mo/location dependency, no freshness audit procedure.
- **UC05 availability moves to the admin activities scrape.** `available_spots` is now derived at parse time as `Max. Teilnehmer − Angemeldet` from the activities CSV (German column names confirmed in `requirements_v2/sample_exports/all activities.csv`). The `sessions` table is populated from this same export.
- **New protection layer for UC05 staleness:** in addition to the existing `≥ 2 spots` safety margin (now configurable via `locations.uc05_safety_margin_spots`, default 2), UC05 also enforces a 60-min slot-minimum lead time via `locations.uc05_slot_min_lead_time_minutes`, and relies on real-time writeback re-validation as the final safety net.
- **Removed from docs:** Provider API rows in foundation read sources, the entire "Provider API freshness validation" section, `provider_api_token_ref` from per-location config, `provider_api_freshness_p99_seconds` setting, `EVERSPORTS_PROVIDER_API_BASE` env var, the `app/scrapers/provider_api.py` file from the repo layout (replaced by `app/scrapers/activities.py`), `provider_api_freshness_audit` Postgres table, milestone M5b (Provider API freshness audit).
- **Updated DEV_SPEC M2** to mention the activities export explicitly as the source for `sessions` table + UC05 availability.
- **Updated `eversports-scraper-specialist` agent** to remove Provider API knowledge and add explicit "do not implement Provider API" guardrail.

### v3 — 2026-05-25 — UI v1.7 alignment

Three behaviour changes surfaced through UI design iterations now ratified in the spec.

**UC04 — Card → Membership ready trigger reworked as frequency-based**
- Was: `sessions_remaining < 3` (catches customers running out of card sessions)
- Now: `sessions_per_week_last_month > location.card_upsell_min_sessions_per_week` (catches high-frequency customers who'd materially benefit from membership economics)
- New foundation-computed field `sessions_per_week_last_month` (NUMERIC; bookings_last_30 / 4.33)
- New per-location setting `card_upsell_min_sessions_per_week` (default 2; editable per location)
- The `membership-ready` tag semantic shifts accordingly; pipeline transition in `03_ghl_pipelines.md` updated
- Rationale: targets the engaged-and-paying-more-than-necessary segment, not the about-to-lapse segment. Higher conversion intent.

**UC04 — Renewal handling consolidated into an either-or**
- Removed: independent `coordinate_with_eversports_renewals` boolean
- Added: `renewal_handling_mode` enum — `studio_outreach` (default) or `defer_to_eversports`
- `studio_outreach` mode: UC04 sends our renewal nudge when `renewal-due` tag is applied
- `defer_to_eversports` mode: UC04 suppresses all renewal outreach; Eversports' native reminder is the only touch
- Eliminates the partial-overlap configuration confusion (two settings that could conflict). One choice now.

**UC05 — Renamed to "Booking Assistant" + new SCHEDULE intent**
- File `06_reschedule_assistant.md` heading updated to "Booking Assistant"
- Intent classifier now recognises SCHEDULE alongside RESCHEDULE and CANCEL
- New Phase 3-SCHEDULE: verify active package + capture target activity/datetime, then proceed to Phase 5a availability check, then writeback `create_booking` (reuses the same writeback action UC04 uses for purchase-flow bookings)
- AI prompt for intent disambiguation explicitly distinguishes SCHEDULE (customer wants a specific session) from PURCHASE (customer is choosing a product)

**Writeback mode owner-control surface moved**
- `locations.writeback_mode` data model unchanged
- The owner-facing radio control moved from Settings → Eversports connection to Settings → Automations → Booking assistant → Intents, where it logically sits next to the intents it governs
- Still applies globally to all writeback actions (UC04 create_customer/create_booking, UC05 reschedule/cancel/schedule)

**Affected files**
- `00_master_overview.md` — sub-account settings table, tag glossary entry for `membership-ready`, new custom field `sessions_per_week_last_month`, Eversports-stays note updated
- `03_ghl_pipelines.md` — Card pipeline Membership ready transition logic + glossary
- `05_sales_consultant_chatbot.md` — Trigger 1 + Trigger 2 reworded; no-reply re-trigger logic respects `renewal_handling_mode`
- `06_reschedule_assistant.md` — renamed; intent set widened; SCHEDULE branch documented; writeback-mode UI placement noted
- `07_foundation_layer.md` — flag computation gains the frequency calculation; config table updated
- `DEV_SPEC.md` — Postgres schema for `locations` (dropped `coordinate_with_eversports_renewals`; added `renewal_handling_mode` + `card_upsell_min_sessions_per_week`) and `contacts` (added `sessions_per_week_last_month`)

### v4 — 2026-05-25 — Gatekeeper layer added

New foundational layer for inbound message classification, unlocking omnichannel ingestion (Instagram + Facebook now in v1) without overwhelming use cases and owners with social-channel noise.

**What the gatekeeper does**
- Sits between GHL's inbound webhook and every use case workflow (`07_foundation_layer.md` § Layer 6)
- Classifies every inbound message with Claude Haiku (cheap, fast)
- Categories: `inquiry_pricing`, `inquiry_class_info`, `inquiry_membership`, `booking`, `trial_reply`, `complaint`, `injury_medical`, `billing_dispute`, `opt_out`, `acknowledgment`, `emoji_reaction`, `social_compliment`, `off_topic`, `spam`, `low_confidence`
- Actionable categories route to UC04, UC05, the consent gate, or escalate to the owner
- Noise categories silently ignore, react with an emoji, or send a pre-approved auto-reply per per-location policy
- Every decision logged to `gatekeeper_log` (append-only); owner can reclassify any message
- Multilingual STOP detection runs FIRST and bypasses the classifier (cheaper + faster + consistent with the consent gate)

**Inbound channel scope expanded for v1**
- Was: WhatsApp + Email only (with Instagram/Facebook deferred to v2 because of noise)
- Now: WhatsApp + Email + Instagram DMs + Instagram comments + Facebook DMs + Facebook comments
- (Outbound channel scope unchanged: WhatsApp + Email only; Voice still v2)

**Affected files**
- `00_master_overview.md` — architecture diagram now shows Layer 6, channel scope updated for UC04/UC05, new sub-account settings (`gatekeeper_*`)
- `07_foundation_layer.md` — new Layer 6 section with full algorithm + categories + per-location config + AI usage notes + new `gatekeeper_log` table in Layer 2 schema
- `05_sales_consultant_chatbot.md` — note that messages arrive via the gatekeeper, not direct GHL webhook; UC04 reads gatekeeper classification + confidence as workflow variables
- `06_reschedule_assistant.md` — same pre-condition note; UC05's Phase 1 intent classifier now sub-classifies booking-only messages (SCHEDULE / RESCHEDULE / CANCEL) rather than triaging from raw inbound
- `DEV_SPEC.md` — Postgres schema additions (`gatekeeper_log`, 4 new `locations` columns), repo layout (`app/gatekeeper/` module), new milestone M6b
- `.claude/agents/ghl-workflow-architect.md` — knows about gatekeeper routing pattern; do not trigger UC04/UC05 directly from GHL inbound webhooks anymore

**Cost estimate (per location)**
- ~200 inbound messages/day × ~€0.001/classification × 30 days = **€6/month/location** for the gatekeeper
- Net SAVES UC04/UC05 token spend by filtering noise upstream (would have burned ~10× more on Sonnet for messages that didn't need it)

**Future (v2)**
- Owner-override training loop: reclassifications become labelled training data for an improved per-location classifier
- VIP rules (sender allowlist) and content-pattern rules (regex/keyword bypass) — already designed but skipping the formal implementation in v1

### v5 — 2026-05-25 — Pre-development consistency pass + Insights surface

Final cleanup before development handoff. Three categories of change.

**New product surface: Insights / Opportunities (v1)**
- Top-level page surfacing 5 insight categories: membership upsell, at-risk members, trial drop-off, capacity rebalancing, cohort LTV
- Nightly generation at 06:00 local time; impact estimates use per-studio historical rates
- Dashboard teaser card linking to the full page (one of the first things the owner sees)
- Documented in `00_master_overview.md` § "Insights / Opportunities"
- Most aligned with the stated product purpose: "harvest data → generate revenue, delivered passively to the owner"

**UI consistency fixes**
- UC codes (UC01/UC02/UC04/UC05) replaced with friendly names throughout user-facing UI. Codes remain only in audit logs, the spec, and internal docs.
- Settings → Automations sub-section renamed to **Automation rules** to disambiguate from the top-level Automations operational view.
- Email "From" name + signature relocated from UC01 settings to Studio profile (they were studio-wide, miscategorised).
- Onboarding Step 5 (CSV import) now optional. Primary CTA: "Skip · auto-backfill starts now". The scraper performs a 30-day backfill automatically once the cookie validates. CSV upload remains as an opt-in shortcut for users who want to skip the ~10 min wait.

**Conversations inbox behaviour**
- Reply composer hidden by default. Visible only when the conversation is in a handoff state (chatbot escalated to owner, complaint, etc.).
- AI-handled conversations show a "The AI is handling this. You don't need to do anything — but you can step in if you want." banner with a "Take over →" button instead of the composer.
- Aligns with the "trust AI by default" product philosophy; manual reply becomes the exception, not the default affordance.

**Affected files**
- `00_master_overview.md` — Insights surface documented; v2 list refreshed
- UI prototype `design/studio-owner-ui.html` — all v1.14 updates

### v6 — 2026-05-26 — M1 spec consistency pass (locations table audit)

Post-M1 audit comparing `app/db/models/location.py` and `alembic/versions/d3f8b2a4c1e9_initial_locations.py` against `07_foundation_layer.md` and `DEV_SPEC.md § 5`.

**Spec updated to match code (code was correct; specs were stale):**

- `DEV_SPEC.md § 5` DDL was missing `writeback_mode`, `uc05_slot_min_lead_time_minutes`, and `uc05_safety_margin_spots`. All three are documented in `07_foundation_layer.md` and were intentionally implemented by M1. Added all three to the `CREATE TABLE locations` block in DEV_SPEC with inline comments pointing to the foundation-layer spec and their defaults (`'auto_execute'`, `60`, `2`).
- `07_foundation_layer.md` config table was missing `eversports_location_id` (nullable; added in v2 for multi-site studios) and `consent_default_locale` (TEXT NOT NULL DEFAULT `'de-AT'`; locale fallback for CSV bootstrap and consent invitation copy). Both are present in the model and migration. Both are now documented in the config table.

**No code changes required.** Model and migration are internally consistent and fully implement the `07_foundation_layer.md` config table.

### Still-open items

Each updated doc lists location-specific open items in its "Open Questions / To Confirm" section. The highest-impact open items rolled up:

- WhatsApp Business template texts per locale need legal + studio approval before v1 launch
- Eversports admin ToS — verify browser automation is contractually permitted; pursue B2B data agreement in parallel as risk mitigation
- DPA template — engage legal counsel for DACH-grade DPA
- No-show vs late-cancel distinction — confirm Eversports' export exposes cancellation timestamps reliably; otherwise foundation needs a manual classification UI per location

### v7 — 2026-05-27 — M2 auth model redesign: cookie-export replaces automated login

**Trigger:** Pre-M2 dependency review revealed that Eversports admin login uses TOTP 2FA via an authenticator app. Automated email/password login cannot complete the 2FA step, and storing/deriving the TOTP seed is explicitly prohibited.

**Decision:** M2 uses the cookie-export pattern from the reference PoC (`reference/eversports_scraping_poc/`). The operator logs in manually, exports session cookies via Cookie-Editor, and hands the JSON to `scripts/import_cookies.py`, which writes it into `locations.eversports_cookie_cache`. The scraper injects those cookies into Playwright's browser context on every run. On session expiry (login redirect detected), the scraper sets `eversports_cookie_state = 'expired'` and alerts the operator rather than crashing.

**Affected spec sections:**
- `07_foundation_layer.md` § Authentication — fully rewritten; old "stored credentials + secrets manager login" text replaced with cookie-export model, state machine, and reference to PoC
- `07_foundation_layer.md` config table — added `eversports_cookie_cache` (JSONB nullable) and `eversports_cookie_state` (TEXT DEFAULT 'unset'); updated description of `eversports_credentials_ref` (now informational only in v1)
- `DEV_SPEC.md` § M2 — updated bullet points and acceptance criterion: "using exported session cookies (not automated login)"
- `DEV_SPEC.md` § 5 DDL — added the two new columns

**Secrets provider:** `env` mode for v1 (existing stub in `app/config.py`). `EVERSPORTS_EMAIL` / `EVERSPORTS_PASSWORD` stored in `.env` for documentation; not used by the scraper for login in v1. Doppler migration deferred to multi-environment phase.

**Cookie storage:** `locations.eversports_cookie_cache` JSONB column on the `locations` table (encrypted at rest by Postgres). No Redis, no filesystem, no secrets-manager writeback.

**No impact on M1, M1.5, M3–M8.** The cookie-export model is fully transparent to the delta engine, GHL sync, and all use-case layers.
