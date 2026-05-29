# Eversports × GoHighLevel Connector — Development Specification

**Audience:** Claude Code (and any human reviewer of the implementation plan)
**Source of truth:** `requirements_v2/` (revised requirements docs)
**Status:** Build spec for v1
**Last updated:** 2026-05-24

This document is the build-ready translation of the v2 requirements. Treat it as the contract: file layout, module boundaries, data schemas, API contracts, environment variables, build order, milestones, acceptance criteria. Open product/legal questions are intentionally NOT in this document — they live in the requirements docs' "Open Questions" sections and must be resolved before each affected component reaches "Done".

---

## 1. Scope of v1

**In scope:**
- Foundation layer (Layers 1–5: read scraper, Postgres store, GHL read sync, Eversports writeback executor, consent gate)
- Use cases UC01, UC02, UC04, UC05 (text-only — WhatsApp + Email channels). UC03 (no-show recovery) removed in v2 — Eversports' own no-show comms remain in effect.
- Consent capture + multilingual opt-out + preference centre URL
- AI usage logging + monthly billing roll-up
- Observability (sync log + alerting on critical failures)
- Multi-tenant: one GHL sub-account per studio location

**Out of scope (v2 / later):**
- Voice AI (UC04 voice channel + inbound voice routing)
- Instagram DM + Facebook Messenger conversation routing
- Card pipeline `Churned` → win-back automation
- Membership pipeline `At risk` re-engagement automation (beyond pipeline movement)
- Lapsed customer win-back
- Chronic no-show escalation workflow
- Studio-facing dashboard UI (use GHL native dashboards + Google Sheets mirror for v1)

---

## 2. High-level architecture

```
┌─────────────────────────────────────────────────────────┐
│ studio-owner-facing surface                              │
│   GHL sub-account UI (provided by GHL)                   │
│   Google Sheets read-only ops mirror                     │
└────────────────────────┬────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────┐
│ GHL workflows (use case layer — UC01..UC05)              │
│   - GHL workflow JSON exported under /ghl-workflows/     │
│   - Workflows call our foundation API via               │
│     inbound/outbound webhooks                            │
└────────────────────────┬────────────────────────────────┘
                         │  webhooks
┌────────────────────────▼────────────────────────────────┐
│ FOUNDATION SERVICE (this codebase)                       │
│                                                          │
│   ┌─────────────────────────┐  ┌─────────────────┐    │
│   │ Scraper workers          │  │ Writeback exec  │    │
│   │ (Playwright — sole       │  │ (Playwright)    │    │
│   │  Eversports ingress)     │  │                 │    │
│   └──────┬──────────────────┘  └────────┬────────┘    │
│          │               │                 │            │
│          ▼               ▼                 │            │
│   ┌─────────────────────────────┐          │            │
│   │ Delta engine + flag computer│          │            │
│   └──────┬──────────────────────┘          │            │
│          │                                  │            │
│          ▼                                  ▼            │
│   ┌─────────────────────────────────────────────────┐  │
│   │ Postgres (primary store)                          │  │
│   │ + Redis (job queue, ephemeral state)              │  │
│   └─────────────────────────────────────────────────┘  │
│          │                                              │
│          ▼                                              │
│   ┌─────────────────────────────┐                       │
│   │ GHL sync (REST API v2)      │                       │
│   └─────────────────────────────┘                       │
│                                                          │
│   ┌─────────────────────────────┐                       │
│   │ AI client (Anthropic Claude)│                       │
│   │ + usage logger               │                       │
│   └─────────────────────────────┘                       │
│                                                          │
│   ┌─────────────────────────────┐                       │
│   │ Foundation HTTP API          │                       │
│   │  (consumed by GHL workflows) │                       │
│   └─────────────────────────────┘                       │
└─────────────────────────────────────────────────────────┘
```

---

## 3. Tech stack

- **Language:** Python 3.12 (chosen for Playwright maturity, AI ecosystem, ops familiarity)
- **Framework:** FastAPI (the foundation HTTP API exposed to GHL workflows)
- **Browser automation:** Playwright (Chromium)
- **Datastore:** Postgres 16
- **Queue:** PgBoss (Postgres-backed) — avoids Redis dependency for v1; switch to Redis if throughput requires
- **Migration:** Alembic
- **Secrets:** Doppler (or AWS Secrets Manager — pick one during ops setup)
- **AI:** Anthropic Claude (`claude-sonnet-4-6` default, `claude-haiku-4-5` for cheap classification calls)
- **Observability:** Sentry (errors) + Prometheus (metrics) + Grafana Cloud (dashboards) + Slack webhook (alerts)
- **Hosting (suggested):** Fly.io or Railway — supports per-region deployment for DACH data residency
- **CI/CD:** GitHub Actions (lint + tests + deploy on tag)

---

## 4. Repository layout

```
eversports-ghl-connector/
├── README.md
├── DEV_SPEC.md                       ← this file
├── requirements_v2/                  ← business requirements (source of truth)
├── pyproject.toml
├── alembic/                          ← DB migrations
│   ├── env.py
│   └── versions/
├── app/
│   ├── main.py                       ← FastAPI app factory
│   ├── config.py                     ← settings (Pydantic Settings)
│   ├── db/
│   │   ├── __init__.py
│   │   ├── session.py                ← Async SQLAlchemy session
│   │   └── models/                   ← SQLAlchemy models
│   │       ├── base.py               ← declarative Base
│   │       ├── location.py           ← M1 — multi-tenant anchor
│   │       ├── contacts.py           ← M1.5 — one row per customer per location
│   │       ├── bookings.py           ← M1.5 — booking history
│   │       ├── sessions.py           ← M1.5 — activity schedule (from activities CSV)
│   │       ├── sync_log.py           ← M1.5 — one row per sync run
│   │       ├── writeback_job.py      ← M5 (deferred)
│   │       ├── ai_usage.py           ← M6 (deferred)
│   │       └── consent_audit.py      ← M7 (deferred)
│   ├── ingest/
│   │   ├── __init__.py
│   │   ├── csv_parser.py             ← locale-aware CSV parsing (BOM, delimiter, date formats)
│   │   ├── normalisers.py            ← phone (libphonenumber), email, dates
│   │   ├── column_maps.py            ← bookings / activities / noshows / customers / memberships column maps
│   │   └── bootstrap.py              ← one-time CSV bootstrap orchestrator
│   ├── scrapers/
│   │   ├── __init__.py
│   │   ├── base.py                   ← shared Playwright session/login logic
│   │   ├── admin_csv.py              ← downloads active/all/booking-list/activities CSVs (then hands off to ingest.csv_parser)
│   │   ├── products.py               ← active products & memberships
│   │   └── activities.py             ← downloads + parses the activities export (drives the sessions table + UC05 availability)
│   ├── delta/
│   │   ├── __init__.py
│   │   ├── engine.py                 ← compare current vs previous, produce change_set
│   │   ├── flags.py                  ← compute_flags() — UC trigger flags
│   │   └── classifiers.py            ← is_trial / is_card / is_membership / is_voucher / is_merch
│   ├── ghl/
│   │   ├── __init__.py
│   │   ├── client.py                 ← REST API v2 wrapper (OAuth, X-GHL-Signature)
│   │   ├── sync.py                   ← contact upsert + delta push
│   │   ├── tags.py                   ← apply/remove tag rules
│   │   └── pipelines.py              ← stage transition logic
│   ├── writeback/
│   │   ├── __init__.py
│   │   ├── queue.py                  ← PgBoss wrapper
│   │   ├── executor.py               ← consumes queue, dispatches to handler
│   │   └── handlers/
│   │       ├── create_customer.py
│   │       ├── create_booking.py
│   │       ├── reschedule_booking.py
│   │       └── cancel_booking.py
│   ├── ai/
│   │   ├── __init__.py
│   │   ├── client.py                 ← Anthropic wrapper + usage logger
│   │   ├── prompts/
│   │   │   ├── uc01_*.txt
│   │   │   ├── uc03_*.txt
│   │   │   ├── uc04_intent.txt
│   │   │   ├── uc04_chatbot_system.txt
│   │   │   └── uc05_*.txt
│   │   └── templates/                ← WhatsApp Business templates (variable-only fill)
│   │       ├── trial_followup_msg1.de_at.json
│   │       └── ...
│   ├── consent/
│   │   ├── __init__.py
│   │   ├── gate.py                   ← consent_gate(contact, channel) — used by workflows
│   │   ├── opt_out.py                ← multilingual STOP detection
│   │   ├── invitation.py             ← legacy contact opt-in sweep
│   │   └── audit.py                  ← append-only consent_audit writes
│   ├── gatekeeper/
│   │   ├── __init__.py
│   │   ├── classifier.py             ← Claude Haiku classifier (Layer 6)
│   │   ├── router.py                 ← routes message based on classification
│   │   ├── noise_policy.py           ← silent_ignore / react_emoji / auto_reply_template handlers
│   │   ├── owner_override.py         ← reclassify, VIP rules, content-pattern rules
│   │   └── audit.py                  ← append-only gatekeeper_log writes
│   ├── api/                          ← FastAPI routers (foundation HTTP API)
│   │   ├── __init__.py
│   │   ├── webhooks_ghl.py           ← inbound from GHL workflows (writeback enqueue, consent gate, etc.)
│   │   ├── webhooks_writeback.py     ← outbound result webhooks to GHL
│   │   ├── availability.py           ← GET /api/availability (UC05)
│   │   ├── upcoming.py               ← GET /api/upcoming-bookings (UC05)
│   │   ├── billing.py                ← GET /api/billing/usage (monthly AI usage)
│   │   └── health.py
│   ├── scheduler/
│   │   ├── __init__.py
│   │   ├── orchestrator.py           ← computes class-end times + enqueues sync runs
│   │   └── jobs.py                   ← hourly catchup + nightly reconciliation
│   ├── observability/
│   │   ├── metrics.py
│   │   └── alerts.py
│   └── utils/
│       ├── retry.py                  ← exponential backoff helper
│       ├── idempotency.py            ← sha256 key generation
│       └── time.py                   ← timezone-aware datetime helpers
├── tests/
│   ├── unit/
│   ├── integration/                  ← spins up Postgres + GHL sandbox + mock Eversports
│   └── e2e/                          ← Playwright-driven against test Eversports account
├── ghl-workflows/                    ← exported GHL workflow JSON (source-controlled)
│   ├── uc01_trial_followup.json
│   ├── uc02_trial_member_tag.json
│   ├── uc03_noshow_recovery.json
│   ├── uc04_chatbot.json
│   ├── uc05_reschedule.json
│   ├── consent_gate.json
│   ├── opt_out.json
│   ├── writeback_success.json
│   └── writeback_failed.json
├── ops/
│   ├── docker-compose.yml            ← local dev
│   ├── Dockerfile.app
│   ├── Dockerfile.scraper
│   ├── prometheus.yml
│   └── grafana-dashboards/
├── scripts/
│   ├── onboard_location.py           ← provision a new studio location
│   ├── run_historical_sync.py
│   ├── seed_test_data.py
│   └── export_ghl_workflows.py
└── .github/workflows/
    ├── ci.yml
    └── deploy.yml
```

---

## 5. Database schema (Alembic migrations)

```sql
-- locations: one row per studio location (= one GHL sub-account)
CREATE TABLE locations (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  eversports_studio_id TEXT NOT NULL,
  eversports_location_id TEXT,
  ghl_subaccount_id TEXT NOT NULL UNIQUE,
  ghl_oauth_token_ref TEXT NOT NULL,
  eversports_credentials_ref TEXT NOT NULL,         -- secrets-manager ref; informational in v1 (TOTP 2FA blocks automated login)
  eversports_cookie_cache JSONB,                    -- Cookie-Editor export; injected by scripts/import_cookies.py; NULL = not yet imported
  eversports_cookie_state TEXT NOT NULL DEFAULT 'unset',  -- 'unset' | 'ok' | 'expired'
  timezone TEXT NOT NULL,
  country TEXT NOT NULL DEFAULT 'DE',                                    -- ISO 3166-1 alpha-2; used as libphonenumber default_region
  late_cancel_window_hours INT NOT NULL DEFAULT 24,
  studio_owner_email TEXT NOT NULL,
  studio_name TEXT NOT NULL,
  location_name TEXT NOT NULL,
  stop_keywords TEXT NOT NULL,
  ai_monthly_budget_usd NUMERIC NOT NULL DEFAULT 200,
  renewal_handling_mode TEXT NOT NULL DEFAULT 'studio_outreach',  -- 'studio_outreach' | 'defer_to_eversports'
  card_upsell_min_sessions_per_week NUMERIC NOT NULL DEFAULT 2,
  gatekeeper_enabled BOOLEAN NOT NULL DEFAULT true,
  gatekeeper_confidence_threshold NUMERIC NOT NULL DEFAULT 0.7,
  gatekeeper_noise_action JSONB NOT NULL DEFAULT '{"acknowledgment":"silent_ignore","emoji_reaction":"react_emoji","social_compliment":"react_emoji","off_topic":"silent_ignore","spam":"silent_ignore"}',
  gatekeeper_owner_alert_categories TEXT NOT NULL DEFAULT 'complaint,injury_medical,billing_dispute,low_confidence',
  product_keyword_map JSONB NOT NULL DEFAULT '{}',
  whatsapp_templates JSONB NOT NULL DEFAULT '{}',
  consent_default_locale TEXT NOT NULL DEFAULT 'de-AT',
  historical_sync_flag TEXT NOT NULL DEFAULT 'pending',
  writeback_mode TEXT NOT NULL DEFAULT 'auto_execute',           -- 'auto_execute' | 'admin_task' (07_foundation_layer.md § config table)
  uc05_slot_min_lead_time_minutes INT NOT NULL DEFAULT 60,       -- UC05 slot lead-time guard; see 07_foundation_layer.md § UC05 availability freshness
  uc05_safety_margin_spots INT NOT NULL DEFAULT 2,               -- UC05 min free-spots; see 07_foundation_layer.md § UC05 availability freshness
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- contacts: one row per Eversports customer per location
-- NOTE: This is the M1.5 baseline schema (app/db/models/contacts.py +
-- alembic/versions/a1b2c3d4e5f6). Fields deferred to M3+ are noted inline.
CREATE TABLE contacts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  location_id UUID NOT NULL,

  -- Identity
  email TEXT,
  email_lower TEXT,                               -- normalised for upsert key
  first_name TEXT,
  last_name TEXT,
  phone TEXT,                                     -- E.164 normalised
  phone_raw TEXT,                                 -- original value before normalisation

  -- Eversports-sourced
  eversports_customer_id TEXT,
  eversports_clubgroup TEXT,
  eversports_newsletter_optin BOOLEAN,            -- Eversports' own opt-in; NOT our consent
  eversports_location_address TEXT,

  -- Package / product
  products_purchased JSONB NOT NULL DEFAULT '[]',
  active_package_type TEXT,
  active_package_name TEXT,
  active_package_expiry_date DATE,
  active_package_sessions_remaining INT,

  -- Attendance / engagement (derived at bootstrap/sync time)
  total_sessions_attended INT NOT NULL DEFAULT 0,
  no_show_count INT NOT NULL DEFAULT 0,
  last_session_date DATE,
  last_session_end_time TIME,
  last_class_name TEXT,
  last_booking_date DATE,
  last_no_show_email_sent_at TIMESTAMPTZ,
  sessions_attended_this_month INT NOT NULL DEFAULT 0,
  sessions_attended_last_month INT NOT NULL DEFAULT 0,
  sessions_per_week_last_month NUMERIC,

  -- GHL sync (populated after first GHL sync — M3)
  ghl_contact_id TEXT,

  -- Bootstrap tracking
  bootstrap_run_id UUID,

  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

  UNIQUE (location_id, email_lower)               -- upsert key per spec
);
-- Deferred to M3+: prev_state JSONB, ghl_sync_status, last_sync_timestamp, ghl_last_updated,
--   converted_package_name, conversion_date, conversion_source, chatbot_outbound_attempts,
--   last_chatbot_interaction, booking_history, upcoming_sessions_count,
--   active_package_sessions_total, active_package_sessions_used, upcoming_session_*

CREATE INDEX idx_contacts_location ON contacts(location_id);
CREATE INDEX idx_contacts_email ON contacts(email);
CREATE INDEX idx_contacts_phone ON contacts(phone);
CREATE INDEX idx_contacts_ghl ON contacts(ghl_contact_id);

-- products: Eversports active products & memberships per location
CREATE TABLE products (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  location_id UUID NOT NULL REFERENCES locations(id),
  eversports_product_id TEXT NOT NULL,
  name TEXT NOT NULL,
  type TEXT NOT NULL,
  price NUMERIC,
  checkout_url TEXT,
  is_active BOOLEAN NOT NULL DEFAULT true,
  fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (location_id, eversports_product_id)
);

-- bookings: last 90 days
-- NOTE: This is the M1.5 baseline schema (app/db/models/bookings.py +
-- alembic/versions/a1b2c3d4e5f6).
CREATE TABLE bookings (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  location_id UUID NOT NULL,
  contact_id UUID NOT NULL,
  eversports_booking_id TEXT NOT NULL,            -- sha256 synthetic when CSV has no explicit ID
  activity_name TEXT,
  session_datetime TIMESTAMPTZ,
  session_end_datetime TIMESTAMPTZ,
  trainer TEXT,
  package_used TEXT,                              -- product name used for this booking
  price NUMERIC,
  attendance_status TEXT NOT NULL DEFAULT 'unknown',  -- attended | no_show | late_cancel | unknown
  bootstrap_run_id UUID,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (location_id, eversports_booking_id)
);
-- Deferred: cancellation_timestamp, eversports_customer_id, fetched_at

CREATE INDEX idx_bookings_contact ON bookings(contact_id);
CREATE INDEX idx_bookings_session_dt ON bookings(session_datetime);

-- sessions: activity schedule from the admin activities scrape (NOT Provider API)
-- NOTE: This is the M1.5 baseline schema (app/db/models/sessions.py +
-- alembic/versions/a1b2c3d4e5f6). Unique key is the natural composite, not a
-- scraper-assigned ID (Eversports CSVs do not expose a session ID column).
CREATE TABLE sessions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  location_id UUID NOT NULL,
  session_type TEXT,
  start_time TIMESTAMPTZ,
  end_time TIMESTAMPTZ,
  activity_name TEXT,
  activity_group TEXT,
  sport TEXT,
  trainer TEXT,
  location_label TEXT,
  total_spots INT,
  registered_count INT,
  attended_count INT,
  waitlist_count INT,
  available_spots INT,                            -- derived: max(0, total_spots - registered_count)
  status TEXT,
  comment TEXT,
  published BOOLEAN,
  bootstrap_run_id UUID,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (location_id, start_time, activity_name, trainer)  -- natural key
);
-- Deferred: eversports_session_id, checkout_link, fetched_at

CREATE INDEX idx_sessions_lookup ON sessions(location_id, start_time);

-- writeback_jobs: queue + audit
CREATE TABLE writeback_jobs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  location_id UUID NOT NULL REFERENCES locations(id),
  contact_id UUID REFERENCES contacts(id),
  job_type TEXT NOT NULL,                         -- create_customer | create_booking | reschedule_booking | cancel_booking
  payload JSONB NOT NULL,
  idempotency_key TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued',          -- queued | running | succeeded | failed | dead
  attempts INT NOT NULL DEFAULT 0,
  last_error TEXT,
  result JSONB,
  enqueued_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at TIMESTAMPTZ,
  completed_at TIMESTAMPTZ,
  callback_url TEXT,
  UNIQUE (location_id, idempotency_key)
);

CREATE INDEX idx_writeback_status ON writeback_jobs(status, location_id);

-- ai_usage: every AI call
-- NOTE: M6b baseline (app/db/models/ai_usage.py + alembic/versions/j5k6l7m8n9o0).
CREATE TABLE ai_usage (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  location_id UUID NOT NULL REFERENCES locations(id),
  ghl_contact_id TEXT,                            -- nullable; GHL contact ID string (no FK — may not be synced yet)
  use_case TEXT NOT NULL,                         -- gatekeeper | UC01 | UC02 | UC03 | UC04 | UC05
  step TEXT NOT NULL,                             -- classification | intent_detection | message_generation | reply_handling | summary
  model TEXT NOT NULL,
  prompt_tokens INT NOT NULL,
  completion_tokens INT NOT NULL,
  cost_usd NUMERIC(12,6) NOT NULL,
  ts TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_ai_usage_location_ts ON ai_usage(location_id, ts);
CREATE INDEX idx_ai_usage_use_case_ts ON ai_usage(use_case, ts);

-- gatekeeper_log: append-only log of inbound classification + routing
-- NOTE: M6b baseline (app/db/models/gatekeeper_log.py + alembic/versions/j5k6l7m8n9o0).
CREATE TABLE gatekeeper_log (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  location_id UUID NOT NULL REFERENCES locations(id),
  ghl_contact_id TEXT,                                      -- GHL contact ID string; nullable (contact may not yet be matched)
  contact_id UUID,                                          -- nullable FK to contacts.id; no FK constraint (contact may not yet exist)
  inbound_channel TEXT NOT NULL,                            -- whatsapp | email | instagram_dm | instagram_comment | facebook_dm | facebook_comment
  inbound_surface TEXT,                                     -- e.g. instagram post ID for comments
  ghl_message_id TEXT,                                      -- nullable; not always provided by GHL payload
  raw_text TEXT NOT NULL,
  classification TEXT NOT NULL,                             -- inquiry_pricing | inquiry_class_info | inquiry_membership | booking | trial_reply | complaint | injury_medical | billing_dispute | opt_out | acknowledgment | emoji_reaction | social_compliment | off_topic | spam | low_confidence
  confidence NUMERIC(4,3) NOT NULL,                         -- 0.000–1.000
  route_to TEXT NOT NULL,                                   -- uc04 | uc05 | owner | noise | consent_gate | legacy
  action_taken TEXT NOT NULL,                               -- dynamic; examples: silent_ignore | react_emoji | auto_reply_template | routed_<category> | escalated_<category> | consent_gate_opt_out | legacy_uc04 (no DB CHECK constraint — values set by router.py)
  owner_override TEXT,                                      -- if reclassified by owner
  override_ts TIMESTAMPTZ,
  ts TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_gatekeeper_log_location_ts ON gatekeeper_log(location_id, ts);
CREATE INDEX idx_gatekeeper_log_contact_id ON gatekeeper_log(contact_id);
CREATE INDEX idx_gatekeeper_log_classification ON gatekeeper_log(classification, ts);

-- consent_audit: append-only
CREATE TABLE consent_audit (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  contact_id UUID REFERENCES contacts(id),
  location_id UUID NOT NULL REFERENCES locations(id),
  channel TEXT NOT NULL,                          -- email | whatsapp | voice
  event TEXT NOT NULL,                            -- granted | revoked | blocked-send | preference-centre-update
  value BOOLEAN,
  source TEXT NOT NULL,
  ts TIMESTAMPTZ NOT NULL DEFAULT now(),
  actor TEXT NOT NULL,
  message_shown TEXT,
  ip TEXT
);

CREATE INDEX idx_consent_audit_contact ON consent_audit(contact_id);
-- prevent UPDATE/DELETE in normal flow — enforced at application layer

-- sync_log: one row per sync run
-- NOTE: This is the M1.5 baseline schema (app/db/models/sync_log.py +
-- alembic/versions/a1b2c3d4e5f6). Richer counters deferred to M3+ when GHL
-- sync is wired up (contacts_updated_ghl, contacts_created_ghl, tags_removed,
-- writeback_jobs_processed, writeback_jobs_failed).
CREATE TABLE sync_log (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  location_id UUID NOT NULL,
  run_type TEXT NOT NULL,                         -- 'bootstrap' | 'incremental' | 'historical_backfill' | 'scrape_error'
  contacts_processed INT NOT NULL DEFAULT 0,
  contacts_updated INT NOT NULL DEFAULT 0,
  tags_applied INT NOT NULL DEFAULT 0,
  pipeline_moves INT NOT NULL DEFAULT 0,
  errors JSONB NOT NULL DEFAULT '[]',             -- list of error message strings
  bootstrap_run_id UUID,
  duration_seconds NUMERIC,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Deferred to M3+: contacts_updated_ghl, contacts_created_ghl, tags_removed,
--   writeback_jobs_processed, writeback_jobs_failed, run_timestamp (use created_at)

CREATE INDEX idx_sync_log_recent ON sync_log(location_id, created_at DESC);
```

---

## 6. Foundation HTTP API (consumed by GHL workflows)

All endpoints require `X-Foundation-Signature` HMAC header (shared secret per location).

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/v1/webhooks/ghl/writeback` | GHL workflow enqueues a writeback job |
| `GET`  | `/api/v1/contacts/{ghl_contact_id}/upcoming-bookings` | UC05 multi-booking selection list |
| `GET`  | `/api/v1/locations/{loc_id}/availability?activity_type=&datetime=&window=90` | UC05 slot lookup |
| `POST` | `/api/v1/consent/check` | Consent gate: `{contact_id, channel}` → `{allowed: bool, reason?: string}` |
| `POST` | `/api/v1/consent/revoke` | Opt-out detected: `{contact_id, channel, source, message_shown}` |
| `POST` | `/api/v1/ai/generate` | AI message generation: `{template_id, contact_id, variables}` → `{output, usage}` |
| `POST` | `/api/v1/ai/classify-intent` | UC04/UC05 intent classification: `{message_text}` → `{intent}` |
| `GET`  | `/api/v1/billing/usage?location_id=&month=` | AI usage roll-up for billing |
| `POST` | `/api/v1/admin/locations` | Internal — provision new location |
| `POST` | `/api/v1/admin/locations/{id}/historical-sync` | Internal — trigger one-time scraper-based sync (Mode B) |
| `POST` | `/api/v1/admin/locations/{id}/bootstrap` | Onboarding — upload Eversports CSV exports (Mode A) |
| `GET`  | `/api/v1/admin/locations/{id}/bootstrap/{job_id}` | Bootstrap result / validation report |
| `POST` | `/api/v1/admin/locations/{id}/bootstrap/reset` | Wipe prior bootstrap rows and unlock re-upload |
| `GET`  | `/api/v1/health` | Liveness |
| `GET`  | `/api/v1/health/sync?location_id=` | Sync health per location |

### Outbound webhooks (foundation → GHL)

| Event | GHL webhook URL (per sub-account) | Payload |
|---|---|---|
| `writeback.succeeded` | configured per sub-account | `{job_id, job_type, contact_id, result}` |
| `writeback.failed` | configured per sub-account | `{job_id, job_type, contact_id, error}` |
| `consent.revoked` | configured per sub-account | `{contact_id, channel, source}` |
| `sync.completed` | configured per sub-account (optional) | `{run_id, summary}` |

---

## 7. Environment variables

```
# Database
DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/ghlconnector

# Queue (PgBoss uses DATABASE_URL)

# Secrets manager
SECRETS_PROVIDER=doppler   # or aws-secrets-manager
DOPPLER_TOKEN=...

# GHL
GHL_OAUTH_CLIENT_ID=...
GHL_OAUTH_CLIENT_SECRET=...
GHL_WEBHOOK_SIGNING_SECRET=...
GHL_REDIRECT_URI=https://...

# AI
ANTHROPIC_API_KEY=...
AI_DEFAULT_MODEL=claude-sonnet-4-6
AI_CLASSIFIER_MODEL=claude-haiku-4-5

# Observability
SENTRY_DSN=...
PROMETHEUS_PUSHGATEWAY=...
SLACK_ALERT_WEBHOOK=...

# Ops
ENV=production
LOG_LEVEL=INFO
FOUNDATION_API_SIGNING_SECRET=...

# Hosting
PORT=8080
PYTHONUNBUFFERED=1
```

---

## 8. Build order — milestones & acceptance

### M1 — Skeleton (1 week)
- Repo scaffold, FastAPI app, Postgres + Alembic, Sentry, basic health endpoint
- `locations` table + `scripts/onboard_location.py`
- CI green
**Acceptance:** can provision a test location, health endpoint returns 200, CI green on first commit.

**Recommended agent invocations:**
- Build with Claude Code defaults — no specialist agents needed for scaffolding
- Before milestone close: `spec-consistency-checker` — verify the `locations` SQLAlchemy model matches the spec's "Configuration (per location)" table in `requirements_v2/07_foundation_layer.md`

### M1.5 — CSV bootstrap uploader (1 week, can run in parallel with M2)
- `POST /api/v1/admin/locations/{id}/bootstrap` multipart endpoint
- Parsers per report type with locale-aware header detection (DE/EN), BOM stripping, delimiter detection, German vs English date format support
- Phone normaliser using `libphonenumber` (default region from location country)
- Idempotent upsert: contacts by `(location_id, email_lower)`, bookings by `(location_id, eversports_booking_id)` or synthesised sha256 ID if missing
- Derived-field computation step (total_sessions_attended, last_session_date, products_purchased, etc.)
- Initial tag + pipeline initialisation
- Bootstrap validation report: products discovered + classifier bucket assignments, ambiguous matches, contacts missing email/phone
- `POST /api/v1/admin/locations/{id}/bootstrap/reset` to wipe and re-seed
- Reference samples in `requirements_v2/sample_exports/` used as fixtures in tests
**Acceptance:** uploading the three sample CSVs against a test location seeds Postgres with 28 distinct contacts, 29 bookings, 42 sessions; products classified correctly per validation report; re-uploading the same files results in zero new rows (idempotency).

**Recommended agent invocations:**
- Step 1: `eversports-scraper-specialist` — implement the parsers + normalisers + bootstrap orchestrator. Reference `requirements_v2/sample_exports/` as test fixtures throughout.
- Step 2: `spec-consistency-checker` — verify the column maps in code match the maps documented in `07_foundation_layer.md` § "Column maps" exactly

### M2 — Read scraper (2 weeks)
- Playwright base class with **cookie-export auth** (NOT automated login — Eversports uses TOTP 2FA; see `07_foundation_layer.md` § Authentication for the full model)
- `scripts/import_cookies.py` — CLI tool to write Cookie-Editor JSON exports into `locations.eversports_cookie_cache`
- `locations.eversports_cookie_state` — `unset` / `ok` / `expired`; scraper surfaces human-readable alert on expiry
- Admin CSV downloaders for all report types — including the **activities export** which seeds the `sessions` table and produces `available_spots = max_participants − registered` for UC05
- Persist raw data into Postgres tables (contacts, products, bookings, sessions)
- Reuses the same parsers + normalisers from M1.5 (the scraper just provides files instead of HTTP upload)
- `sync_log` writes
**Acceptance:** for one test location, scraper runs end-to-end against a real Eversports test account **using exported session cookies** (not automated login); all reports land in Postgres; `sync_log` has a row; `sessions` table is populated for the next 14 days with derived `available_spots`; on an intentionally-expired cookie the scraper sets `cookie_state = expired` and logs a clear error rather than crashing.

**Recommended agent invocations:**
- Primary: `eversports-scraper-specialist` — owns the entire build. Login resilience, cookie persistence, retry/backoff, partial-failure handling.
- Before milestone close: `spec-consistency-checker` — verify the Postgres tables in `app/db/models/` match `07_foundation_layer.md` § "Layer 2 — Postgres Datastore" exactly

### M3 — Delta engine + GHL read sync (2 weeks)
- `delta/engine.py` + `delta/flags.py`
- GHL client v2 with OAuth
- Contact upsert + custom field push (only delta)
- Tag engine + pipeline engine
- 60s race-condition guard on apply-then-remove tags
**Acceptance:** changing a customer's package in Eversports test account propagates to the GHL test sub-account within one event-driven cycle; correct tags applied; pipeline stage updated.

**Recommended agent invocations:**
- For `delta/`: build with Claude Code defaults — the delta engine is local computation, no specialist context needed
- For GHL client + tag/pipeline engines: `ghl-workflow-architect` — encodes API v2 calls, OAuth refresh, `X-GHL-Signature`, and the 60s tag race-condition guard
- Before milestone close: `spec-consistency-checker` — verify every tag in `00_master_overview.md` glossary is referenced in code, and every pipeline rule matches `03_ghl_pipelines.md`

### M4 — Event-driven scheduler (1 week)
- `scheduler/orchestrator.py` computes class-end times daily
- PgBoss queue for sync runs
- Hourly catch-up + nightly reconciliation jobs
**Acceptance:** scheduler enqueues sync runs at +15min after each class-end on the test schedule; jobs execute in order.

**Recommended agent invocations:**
- Build with Claude Code defaults — orchestration is straightforward Python/PgBoss work, no specialist context needed

### M5 — Writeback executor (2 weeks)
- Playwright handlers for create_customer, create_booking, reschedule_booking, cancel_booking
- Retry with exponential backoff
- Idempotency key enforcement
- Success/failure callback webhook to GHL
- **`locations.writeback_mode` switch**: `auto_execute` (Playwright path) vs `admin_task` (creates a GHL task assigned to studio owner instead of executing). The same UC04 / UC05 workflows route through this switch per location.
- **Studio-attestation gate on provisioning**: a new location cannot be set to `writeback_mode = auto_execute` until the DPA acceptance flag is set (see `08_consent_model.md` § Studio-attestation clause).
**Acceptance:** each writeback type executes against the test Eversports account end-to-end in auto-execute mode; replays with same idempotency key are no-ops; failure path fires GHL webhook with error context; toggling a location to `admin_task` mode reroutes the next write to a GHL task without touching Eversports.

**Recommended agent invocations:**
- Primary: `eversports-scraper-specialist` — owns the four Playwright writeback handlers, the retry/backoff, and the `writeback_mode` branching
- For the GHL result webhooks (success + failure): `ghl-workflow-architect` — encodes the signature verification and the post-success / post-failure workflows
- Before milestone close: `consent-gate-auditor` — even though writeback success messages are transactional, verify the bypass is explicit and that any owner-notification path respects the standard consent rules
- Plus: `spec-consistency-checker` — verify writeback handlers cover every job type documented in `07_foundation_layer.md` § "Supported job types"

> **M5b removed.** The Provider API freshness audit no longer exists — the Provider API isn't used. UC05 availability is derived from the admin activities scrape and protected by the safety margin + slot-minimum lead time + writeback re-validation (see `07_foundation_layer.md` § "UC05 availability freshness").

### M6 — Consent layer + opt-out (1 week)
- `consent_audit` table, append-only enforcement
- Consent gate endpoint
- Multilingual STOP listener workflow (GHL JSON)
- Legacy contact invitation sweep
- Preference centre URL (signed token, hosted in GHL funnels)
**Acceptance:** sending a marketing message to a no-consent contact is blocked at gate + logged in audit; reply "STOPP" flips boolean false in <30s and removes from sequences.

**Recommended agent invocations:**
- For the GHL workflows (STOP listener, consent gate sub-workflow, opt-in invitation): `ghl-workflow-architect`
- Before milestone close: `consent-gate-auditor` — full audit pass. This is the milestone the auditor was built for; it MUST sign off before merge.
- Plus: `spec-consistency-checker` — verify the consent fields in code match `08_consent_model.md` § "Per-channel consent fields"

### M6b — Gatekeeper (1 week, runs alongside M7) ✓ COMPLETE (2026-05-29)
- New Postgres table `gatekeeper_log` + 3 indexes (migration `j5k6l7m8n9o0`)
- New Postgres table `ai_usage` + 2 indexes (same migration — first writer is the gatekeeper; M7 adds more writers)
- New `app/gatekeeper/` module: `classifier.py` (Haiku, 15-category), `router.py`, `noise_policy.py`, `gate.py`, `audit.py`
- `app/api/v1/admin/gatekeeper.py`: `PATCH /log/{log_id}/override` + `GET /log` endpoints
- `app/api/v1/webhooks/ghl_inbound.py` updated: STOP detection first, then gatekeeper; `consent_gate` route_to re-uses `_handle_stop()` path
- `app/main.py` updated: `gatekeeper_admin_router` wired
- Per-location config respected: `gatekeeper_enabled`, `gatekeeper_confidence_threshold`, `gatekeeper_noise_action`, `gatekeeper_owner_alert_categories`
- Multilingual STOP detection runs BEFORE the classifier (consistency with consent gate)
- Channel scope expansion: Instagram DMs + comments + Facebook DMs + comments now in v1 inbound
- Owner-override API: reclassify a message (`owner_override` + `override_ts` columns); VIP rules and content-pattern rules deferred to v2
**Acceptance:** in the test location, 30 sample messages across all 6 channels classify correctly, route to the right destination, and write `gatekeeper_log` rows. Noise messages don't reach UC04/UC05. Owner override changes the routing for the affected message.

**Recommended agent invocations:**
- Primary: `ghl-workflow-architect` — encodes the gatekeeper-then-use-case routing topology + the inbound webhook signature/auth
- Step 2: `uc-prompt-designer` — writes the classifier prompt + per-locale auto-reply templates
- Pre-merge: `consent-gate-auditor` — verify gatekeeper bypass cases (auto-react, auto-reply) are correctly scoped as acknowledgments to customer-initiated contact, NOT marketing

### M7 — AI client + usage logger (1 week)
- Anthropic wrapper with model fallback
- AI usage logger
- Prompt templates under `app/ai/prompts/`
- WhatsApp template validator (variable-only fill)
- Soft cap + hard cap enforcement
**Acceptance:** AI calls write to `ai_usage` correctly; reaching 80% of monthly budget fires owner email; reaching 100% suppresses non-essential AI calls.

**Recommended agent invocations:**
- Anthropic wrapper + usage logger: Claude Code defaults
- For the initial prompt template stubs: `uc-prompt-designer` — establish the file naming convention (`<use_case>_<step>.<locale>.txt`), the JSON output format for UC04, and the WhatsApp template variable-fill rules so M8 can build on a solid base

### M8 — Use cases (3 weeks, in parallel after M6+M7)
Build the GHL workflows for each use case using the foundation API.

- M8a UC01 trial follow-up (consent-gated, WhatsApp template + email, multilingual STOP)
- M8b UC02 trial → member tag (dedupe vs UC04)
- M8c UC04 sales consultant chatbot (soft-auth, hard-auth for sensitive, JSON output, 3-attempt cap, writeback for booking)
- M8d UC05 reschedule / cancel assistant (multi-booking flow, availability from scraped sessions table, hard-auth, writeback)

> UC03 (no-show recovery) was removed in v2. See `requirements_v2/CHANGELOG.md`.

**Recommended agent invocations — per sub-milestone:**

**M8a (UC01):**
- Step 1: `uc-prompt-designer` — write the 6 message prompts (3 WhatsApp templates + 3 emails) per locale (EN, DE-AT, DE-DE). Test against sample contact profiles.
- Step 2: `ghl-workflow-architect` — build the sequencer workflow, the reply listener, the conversion exit, the multilingual STOP integration
- Pre-merge: `consent-gate-auditor` — verify every send routes through the consent gate AND respects the WhatsApp template-vs-free-form rules
- Pre-merge: `spec-consistency-checker` — verify tag transitions match `01_trial_conversion_followup.md` § "Tags used by this use case"

**M8b (UC02):**
- Primary: `ghl-workflow-architect` — the detection logic + tag application + dedupe-against-`chatbot-converted`
- Pre-merge: `spec-consistency-checker` — verify dedupe behaviour and the new `conversion_source` field semantics

**M8c (UC04 chatbot):**
- Step 1: `uc-prompt-designer` — write the chatbot system prompt + the intent classifier + handoff trigger criteria. Enforce JSON output. Test all five intent categories against realistic profiles.
- Step 2: `ghl-workflow-architect` — inbound conversation routing, outbound pipeline triggers, the 3-attempt cap, the soft-auth + hard-auth sub-workflows
- Step 3: `eversports-scraper-specialist` — wire the `create_customer` + `create_booking` writeback handoffs from the chatbot
- Pre-merge: `consent-gate-auditor` — full audit. Inbound implied-consent rules + outbound consent checks + the coordinate-with-Eversports-renewals flag.
- Pre-merge: `spec-consistency-checker` — verify chatbot tag state machine matches `05_sales_consultant_chatbot.md`

**M8d (UC05 reschedule/cancel):**
- Step 1: `uc-prompt-designer` — intent classifier for RESCHEDULE/CANCEL/PURCHASE/QUESTION/OTHER routing, the slot-collection conversation, the customer-facing "request received vs confirmed" wording
- Step 2: `ghl-workflow-architect` — multi-booking selection branch, late-cancel policy check, hard-auth gate, the `writeback_mode` branch (auto_execute vs admin_task)
- Step 3: `eversports-scraper-specialist` — the reschedule_booking + cancel_booking writeback executors + the success/failure result webhooks
- Pre-merge: `consent-gate-auditor` — verify the transactional bypass is explicit AND that any sales-handoff path back to UC04 re-checks consent
- Pre-merge: `spec-consistency-checker` — verify availability lookup honours the ≥ 2 spots safety margin AND the per-location `uc05_slot_min_lead_time_minutes`

**Acceptance per use case:** end-to-end happy path test passes in the test sub-account; consent gate blocks no-consent contacts; STOP keyword opts out within 30s.

### M9 — Observability + alerting (1 week)
- Prometheus metrics + Grafana dashboards
- Slack alerting on: scrape failure 2× consecutive, GHL API quota 80%, writeback worker stalled, AI spend 80%, dead-letter writeback job
- Per-location health dashboard
**Acceptance:** induced failure scenarios fire the right alerts within SLA (1 min for critical, 5 min for warning).

**Recommended agent invocations:**
- Build with Claude Code defaults
- Before milestone close: `spec-consistency-checker` — verify the alert thresholds match the spec (e.g. AI spend 80% soft cap, 100% hard cap)

### M10 — Hardening + first studio onboarding (2 weeks)
- Load test: 1000 contacts, 200 bookings/day, simulate sync runs every 30 min
- DPA template + sub-processor disclosure
- Onboarding runbook
- First production studio location goes live
**Acceptance:** first studio runs in production for 7 consecutive days with no P1 incidents.

**Recommended agent invocations:**
- Before go-live: `consent-gate-auditor` — final full-codebase sweep. Verify every outbound path is gated. Sign-off blocks production launch.
- Before go-live: `spec-consistency-checker` — final drift audit. Sign-off ensures the spec docs reflect the system that's about to ship.
- During onboarding: `eversports-scraper-specialist` — handles the bootstrap CSV ingestion + the first sync runs of the activities scrape
- Throughout: capture any production quirk that required a workaround in `CHANGELOG.md` so future studios benefit

**Total build estimate:** ~15 weeks for a single engineer; 8–10 weeks with parallelism across foundation + use cases.

---

## 9. Testing strategy

- **Unit tests:** delta engine, classifiers, idempotency key generation, consent gate, AI prompt template fill, retry logic — fast, no external dependencies
- **Integration tests:** spin up Postgres + a mock Eversports endpoint server + a mock GHL endpoint server; exercise foundation HTTP API
- **E2E tests:** Playwright-driven against a dedicated test Eversports account + test GHL sub-account; one happy-path scenario per use case
- **Load tests:** k6 or Locust against the foundation API; targets 50 sync runs/min, 200 writeback jobs/hour per location

CI runs unit + integration on every PR; E2E nightly + on release tags.

---

## 10. Operational runbooks (high-level — full versions in `ops/`)

- **Scraper login failure** — rotate credentials, retry, escalate to studio if password changed
- **Eversports schema change** — scraper diagnostics flag report, engineer reviews HTML/CSV diff, patches scraper
- **GHL API rate limit** — backoff + queue drain; if persistent, add per-sub-account rate limiter
- **Writeback dead-letter queue** — owner notified with full job context; owner performs action manually in Eversports, then marks task complete
- **AI provider outage** — fall back to secondary model; if both down, suppress non-essential AI calls (UC03 fixed template, UC04 outbound paused), continue inbound use cases with degraded mode

---

## 11. Open items that block parts of the build

The following are unresolved and the relevant milestone cannot complete until answered:

- **M5 (writeback) no longer blocked** by Eversports admin ToS. Resolved via studio-attestation in the DPA (each studio authorises us as their delegate). If Eversports later objects per-location, the `writeback_mode = admin_task` fallback ships in the same milestone and switches affected locations without re-deploy.
- **M8a (UC01) is blocked** by WhatsApp Business template text approvals per locale (DE-AT, DE-DE, EN).
- **M10 (production launch) is blocked** by DPA template (now including the studio-attestation clause from `08_consent_model.md`) + Anthropic + GHL sub-processor terms review by legal counsel.

These items are also enumerated in the relevant `requirements_v2/` docs' "Open Questions" sections; resolving them updates both this dev spec and the requirement docs.

---

## 12. Definition of Done (per use case)

A use case is "Done" when:

1. End-to-end happy path tested in a real test environment (test Eversports + test GHL sub-account)
2. Negative paths exercised:
   - No-consent contact → gate blocks send → audit row written
   - Opt-out keyword → consent flipped → sequence exits within 30s
   - Eversports writeback failure → owner notified with full context
3. AI prompts reviewed by a native speaker for each supported locale
4. WhatsApp Business templates pre-approved by Meta where applicable
5. Acceptance criteria from this doc verified by the engineer + product reviewer
6. Runbook entry exists in `ops/` for the most likely failure modes

---

## 13. Hand-off

Once `requirements_v2/` and this `DEV_SPEC.md` are accepted by Emir, this directory can be shared with Claude Code (or any implementing engineer). The recommended starting prompt for Claude Code:

> "Implement v1 of the Eversports × GoHighLevel connector per `DEV_SPEC.md` and the requirements in `requirements_v2/`. Start at milestone M1 (skeleton) and proceed sequentially. Before starting any milestone, list the assumptions you're making and ask for clarification on any open item in section 11 that affects that milestone. Treat the consent model, multilingual STOP detection, and writeback idempotency as load-bearing — do not skip or shortcut them."
