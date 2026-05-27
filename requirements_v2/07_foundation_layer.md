# Foundation Layer — Full Spec (v2)

> **Revision note (v2):** Added Layer 4 writeback, Layer 5 consent gate, AI usage logger, event-driven scheduler, Postgres primary datastore, soft-auth model, multilingual STOP. Provider API was added then removed (admin scraping is the sole Eversports ingress). See `CHANGELOG.md`.

## Overview

The foundation is the data + action platform that all use cases sit on top of. It extracts data from Eversports via admin-panel browser scraping (the sole ingress path), stores it in Postgres, computes deltas, syncs changed fields to GHL contacts via API, evaluates tag rules, moves contacts through pipeline stages, and **performs writeback actions** in Eversports on behalf of use case workflows. Use cases never touch Eversports directly — they read state from GHL and request writebacks via a job queue.

**Technology stack:**
- Read scraper: Playwright (headless Chromium) for admin CSV exports — runs in a containerised worker per location. This is the **only** Eversports ingress path (the Eversports Provider API is explicitly NOT used in this product, see CHANGELOG).
- Write executor: Playwright performing admin actions (create customer, create/reschedule/cancel booking)
- Datastore: Postgres (primary, transactional) — supports deltas, audit log, idempotency keys
- Operations mirror: Google Sheets — read-only daily snapshot for studio owners who want to spot-check
- Queue: Postgres-backed job queue (e.g. PgBoss) or Redis — for writeback jobs and event-driven sync triggers
- CRM: GoHighLevel via REST API v2 (OAuth + `X-GHL-Signature` webhook signing)
- AI: Anthropic Claude (default) with model fallback; usage logged per call
- Sync pattern: delta on read; idempotent jobs on write
- Schedule: event-driven (class-end + 15 min) + hourly catch-up during business hours + overnight reconciliation

---

## Architecture Overview

```
┌──────────────────────────────────────────────────────────────┐
│ LAYER 1 — Eversports data extraction                         │
│   • Browser scraper (admin CSV exports) — single ingress     │
│   • Activity schedule + derived `available_spots` from the   │
│     admin activities export (max_participants − registered)  │
│   • Trigger: event-driven (class-end+15m) + hourly + nightly │
└──────────────────────────────┬───────────────────────────────┘
                               │ raw data
┌──────────────────────────────▼───────────────────────────────┐
│ LAYER 2 — Postgres datastore + delta engine                  │
│   • Normalised tables: contacts, products, bookings,         │
│     no_shows, sessions, ai_usage, writeback_jobs             │
│   • Delta engine: compare current vs previous per field      │
│   • Flag computation for tag engine                          │
│   • Google Sheets read-only mirror updated nightly           │
└──────────────────────────────┬───────────────────────────────┘
                               │ delta change set + flags
┌──────────────────────────────▼───────────────────────────────┐
│ LAYER 3 — GHL read sync                                      │
│   • Contact match → upsert delta fields → tag engine →       │
│     pipeline engine → AI usage log → sync log                │
└──────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────┐
│ LAYER 4 — Eversports writeback executor                      │
│   • Consumes job queue (create_customer, create_booking,     │
│     reschedule_booking, cancel_booking)                      │
│   • Idempotent jobs, retry with backoff, dead-letter queue   │
│   • Reports success/failure back via webhook to GHL          │
└──────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────┐
│ LAYER 5 — Consent gate (outbound)                            │
│   • Every outbound message from a use case routes through    │
│     a shared GHL workflow action that checks the relevant    │
│     per-channel consent boolean. No consent → no send,       │
│     log to consent_audit, exit workflow gracefully.          │
└──────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────┐
│ LAYER 6 — Gatekeeper (inbound classifier)                    │
│   • Every inbound message from GHL (WhatsApp DM, Email,      │
│     Instagram DM/comment, Facebook DM/comment) is classified │
│     by Claude Haiku BEFORE reaching a use case workflow.     │
│   • Actionable → routes to UC04 / UC05 / owner                │
│   • Noise → auto-react or silent ignore                       │
│   • Opt-out → routes to consent gate                          │
│   • Every classification logged to gatekeeper_log;            │
│     owner can override.                                       │
└──────────────────────────────────────────────────────────────┘
```

---

## Layer 1 — Eversports Data Extraction

### Authentication

The scraper uses Playwright to log into Eversports admin with stored credentials. Credentials are kept in a secrets manager (Doppler / AWS Secrets Manager / Vault) per location — never in Postgres or any sheet. Session cookies are persisted in encrypted storage and reused until expiry.

### Read sources

| Source | Endpoint | Used for |
|---|---|---|
| Admin CSV — Active appointments | `?export=active` | Customer roster + today's bookings · drives event-driven schedule |
| Admin CSV — All appointments | `?export=all` | Full booking history (30-day window kept in Postgres, summary in GHL) |
| Admin CSV — Booking list | `?export=booking-list` | Package/session usage per booking |
| Admin endpoint — Active products & memberships | (separate endpoint) | Product detection + categorisation |
| Admin CSV — Activities (all) | Activities admin export | Activity schedule + capacity. Drives both the event-driven trigger time grid AND the UC05 availability check. Columns `Max. Teilnehmer` (capacity) and `Angemeldet` (registered) yield `available_spots = Max. Teilnehmer − Angemeldet` |

**Note:** the Eversports Provider API (GraphQL) is **NOT used by this product.** All Eversports ingress is via admin panel scraping.

### One-time historical sync (runs once per location on setup)

Two ingest modes are supported for onboarding. **Mode A is the default** and runs faster than provisioning scraper credentials; Mode B is the fallback if the studio cannot provide CSV exports.

**Mode A — CSV bootstrap (preferred):** the studio owner manually exports 3–5 reports from the Eversports admin UI on day one of onboarding and uploads them via our admin endpoint. The foundation parses, normalises, and seeds Postgres before the scraper is even connected. This decouples onboarding from scraper provisioning and avoids the chicken-and-egg of "we need history before the first daily sync, but the first daily sync only sees today forward."

**Mode B — scraper historical sweep:** if no CSVs provided, the scraper does a 30-day backfill on its first run, plus pulls the next 14 days of future schedule via the admin activities export.

Both modes share the same downstream steps (consent invitation, tag/pipeline initialisation, marking `historical_sync_flag = "complete"`).

```python
historical_sync_flag = read from config
if historical_sync_flag == "complete":
  → skip, proceed to event-driven sync
else:
  if csv_bootstrap_uploaded:
    → run CSV bootstrap (see "One-Time CSV Bootstrap Protocol" below)
  else:
    → run scraper historical sweep for past 30 days + future 14 days schedule
  → enqueue legacy consent invitation for each imported contact
  → set historical_sync_flag = "complete"
```

---

### One-Time CSV Bootstrap Protocol

**Purpose:** During onboarding, the studio owner exports CSVs from the Eversports admin UI and uploads them via our admin endpoint. The foundation parses them and seeds the Postgres datastore — establishing the baseline that the scraper then maintains going forward.

**Reference samples:** `requirements_v2/sample_exports/` contains real Eversports exports used to derive the column maps and parsing rules below. The samples reveal locale-specific behaviour (German headers in some reports, English in others), date format inconsistencies between reports, BOM-prefixed UTF-8, and semicolon delimiters.

#### Required exports (studio provides during onboarding)

| Export | Eversports menu | What it seeds | Optional? |
|---|---|---|---|
| **Activities** (`all activities.csv`) | Activities → Export → All | `sessions` table (historical activity schedule, attendance counts, capacity) | Recommended |
| **Bookings** (`bookings.csv`) | Booking list → Export → CSV | `contacts` (one row per distinct customer), `bookings` (one row per booking), implies `products_purchased`, `last_session_date`, `total_sessions_attended` | **Required** |
| **No-shows** (`noshows.csv`) | No-shows → Export → CSV | `bookings.attendance_status = "no_show"`, `contacts.no_show_count`, `contacts.last_no_show_email_sent_at` (null on bootstrap) | Recommended |
| **Customer list** | Customers → Export → CSV | `contacts` rows for customers with no recent bookings (gap-fill beyond what bookings.csv covers) | Recommended |
| **Active memberships** | Customers → Active Memberships → Export | Memberships with validity dates, pauses, termination state | Recommended |

A studio onboarding without bookings.csv falls back to Mode B. All other exports are nice-to-have — their absence reduces baseline completeness but doesn't block go-live.

#### Upload endpoint

```
POST /api/v1/admin/locations/{location_id}/bootstrap
Content-Type: multipart/form-data

Fields:
  activities:        file (optional)
  bookings:          file (required)
  noshows:           file (optional)
  customers:         file (optional)
  memberships:       file (optional)
  default_locale:    string (e.g. "de-AT")        # affects date parsing if header detection is ambiguous
  bookings_window_days: integer (default 90)     # how far back the bookings export was taken
```

Response: `202 Accepted` with `bootstrap_job_id`. Result observable via `GET /api/v1/admin/locations/{id}/bootstrap/{bootstrap_job_id}`.

#### Parsing rules (apply to all uploaded CSVs)

| Concern | Rule |
|---|---|
| **Encoding** | UTF-8; strip BOM (`﻿`) if present at file start |
| **Delimiter** | Detect: `,` vs `;`. Default to `;` for German exports |
| **Quote char** | `"` if present; otherwise unquoted |
| **Header locale** | Auto-detect by matching the first row against known German + English column-name dictionaries (see column maps below). Fall back to `default_locale` from the request |
| **Date formats** | Activities use `DD.MM.YYYY` + `HH:MM`. Bookings use `DD/MM/YYYY HH:MM`. Parsers must accept both per file type |
| **Phone normalisation** | Use libphonenumber with `default_region` from location's country (DE/AT/CH). Output E.164. Reject obvious invalids (length < 7 after normalization) but keep raw value in `contacts.phone_raw` |
| **Email normalisation** | Lowercase + trim. If empty → contact still created but flagged `email_missing` (won't receive email-channel comms) |
| **Customer matching key** | Primary: `email` (lowercase). Secondary: `phone` (E.164). Tertiary: `(first_name, last_name)`. Reject ambiguous matches and surface to admin |
| **Idempotency** | Re-uploading the same files is safe: the protocol uses `(location_id, email)` as the upsert key for contacts and `(location_id, eversports_booking_id)` for bookings. If no booking ID is exported (the samples don't include one), the foundation synthesises a deterministic ID from `sha256(location_id + customer_email + session_datetime + activity_name)` |

#### Column maps

##### `bookings.csv` (semicolon-delimited, quoted, English headers — seen in sample)

| CSV column | Type | Maps to | Notes |
|---|---|---|---|
| `Start` | `DD/MM/YYYY HH:MM` | `bookings.session_datetime` | Parse with location timezone |
| `End` | `DD/MM/YYYY HH:MM` | `bookings.session_end_datetime` | |
| `Activity name` | text | `bookings.activity_name` | |
| `Location` | text (postal address) | `contacts.eversports_location_address` (informational) | Same value for all rows when location is single-site |
| `Trainer nickname` | text | `bookings.trainer` | |
| `Customer number` | text | `contacts.eversports_customer_id` | **Sample shows this column is consistently empty** — must not rely on it as match key; email is the actual primary |
| `First name` | text | `contacts.first_name` | |
| `Last name` | text | `contacts.last_name` | |
| `E-Mail` | text | `contacts.email` (lowercase + trim) | **Primary match key** |
| `Clubgroup name` | text | `contacts.eversports_clubgroup` (informational) | Observed values: `Extern`, `Corporate Benefits` |
| `Newsletter` | `yes`/`no` | `contacts.eversports_newsletter_optin` | **Informational only — does NOT auto-grant `consent_marketing_email`**. Used as a soft signal in the consent invitation wording (see Implications below) |
| `Product name` | text | appended to `contacts.products_purchased` AND set as `bookings.package_used` | Used by classifier to derive `active_package_type` |
| `Price` | text (e.g. `252.00 €`) | `bookings.price` (parsed to numeric, currency stripped) | |
| `Attended` | `yes`/`no` | `bookings.attendance_status` (`attended` / `no_show` if `Attended=no`) | Bookings export only includes past bookings; future bookings come from the scraper |
| `Phone number` | text | `contacts.phone` (E.164 normalised) | Sample shows mixed formats: `015258067348`, `+491759472221`, `17645699133` |

##### `all activities.csv` (semicolon-delimited, unquoted, German headers — seen in sample)

| CSV column | Type | Maps to | Notes |
|---|---|---|---|
| `Typ` | text | `sessions.session_type` | Sample: `Klasse` |
| `Datum` | `DD.MM.YYYY` | `sessions.start_time` (date portion) | Combine with `Startzeit` |
| `Startzeit` | `HH:MM` | `sessions.start_time` (time portion) | |
| `Endzeit` | `HH:MM` | `sessions.end_time` | |
| `Name` | text | `sessions.activity_name` | |
| `Angemeldet` | int | `sessions.registered_count` | |
| `Anwesend` | int | `sessions.attended_count` | |
| `Max. Teilnehmer` | int | `sessions.total_spots` | |
| `Warteliste` | int | `sessions.waitlist_count` | |
| `Trainer` | text | `sessions.trainer` | |
| `Ort` | text | `sessions.location_label` (often blank) | |
| `Status` | text | `sessions.status` | Sample: `buchbar` (=bookable) |
| `Sport` | text | `sessions.sport` | Sample: `Reformer Pilates` |
| `Aktivitätsgruppe` | text | `sessions.activity_group` | Sample values: `All levels pilates equipment`, `Lower Body Focus`, `Equipment Private-Duett`, etc. |
| `Kommentar zur Einheit` | text | `sessions.comment` | |
| `Veröffentlicht` | text/bool | `sessions.published` | |

Note: `available_spots` for future sessions is derived from this same export as `Max. Teilnehmer − Angemeldet`. The bootstrap seeds both historical (past 30 days) and future (next 14 days) sessions. Subsequent recurring syncs refresh the future window.

##### `noshows.csv` (sample is EMPTY — schema assumed)

Sample file is 0 bytes — confirming this studio simply has no no-shows in the export window rather than a zero-row file with headers. The expected schema (to be verified against a non-empty sample during integration) is the **same columns as bookings.csv filtered to attendance = no-show**, possibly with an additional `Cancellation timestamp` column needed to distinguish true no-shows from late cancellations.

| CSV column (expected) | Maps to | Notes |
|---|---|---|
| Same as `bookings.csv` | `bookings.attendance_status = "no_show"` or `"late_cancel"` based on cancellation timestamp | If cancellation timestamp is present, apply the late-cancel logic from the main spec |
| `Cancellation timestamp` (expected) | drives `no_show` vs `late_cancel` flagging | **OPEN ITEM** — confirm with studio whose CSV is non-empty |

Bootstrap behaviour with empty noshows.csv: treat as "no no-shows in the export window" — initialise `contacts.no_show_count = 0` for all contacts. Future scraper runs override.

##### Customer list export (column map deferred)

Schema not seen in this sample upload. The Eversports help centre describes it as containing at minimum: email, first_name, last_name, member_since, phone. To be added when a sample is provided.

##### Active memberships export (column map deferred)

Schema not seen in this sample upload. Per Eversports docs the columns are: Customer, Membership Name, Validity (start/end), Termination Received, Automatic Extension, Payment Terms, Payment Type, Last Pause + comment. To be added when a sample is provided.

#### Bootstrap execution sequence

```
1. Validate uploaded files (encoding, header detection, basic schema)
2. PARSE in order:
   a. Customer list (if uploaded)     → contacts upsert (no products yet)
   b. Bookings                         → contacts upsert (fill missing) + bookings insert
   c. No-shows                         → bookings.attendance_status updates
   d. Activities                       → sessions insert (historical reference)
   e. Active memberships               → contacts.active_package_* fields

3. COMPUTE derived per-contact:
   total_sessions_attended  = count of bookings with attendance_status = "attended"
   no_show_count            = count of bookings with attendance_status in ("no_show", "late_cancel")
   last_session_date        = max(session_datetime) over attended bookings
   last_session_end_time    = corresponding end time
   last_class_name          = activity_name of latest attended session
   last_booking_date        = max(session_datetime) over all bookings
   products_purchased       = distinct set of product names across all bookings
   active_package_type      = classify(latest non-trial product if not expired; else trial product if active)
   active_package_name      = the chosen product's name
   sessions_attended_this_month / last_month = rolling 30-day windows from now

4. APPLY initial tags (idempotent — only adds if missing):
   - trial-active / card-active / membership-active per active_package_type
   - lapsed if last_booking_date < today - 30
   - new-contact for any contact created in this run

5. INITIALISE pipeline stages per `03_ghl_pipelines.md`

6. ENQUEUE legacy consent invitation (see step "Consent invitation wording" below)

7. WRITE bootstrap_result with counts, errors, warnings → Postgres

8. MARK historical_sync_flag = "complete"

9. UNBLOCK event-driven scheduler for this location
```

The whole sequence runs in a single database transaction per logical step (contacts upsert, bookings insert, derived computation), with each step idempotent so a partial failure can be safely retried from the last successful step.

#### Consent invitation wording (uses Newsletter flag as soft signal)

The bookings CSV exposes Eversports' own newsletter opt-in per booking row. This is NOT our consent — but it's useful signal for personalising the invitation copy:

```
For each newly-created contact, determine eversports_newsletter_optin:
  - Take the MOST RECENT booking row for this contact
  - If that row's Newsletter == "yes" → invitation_variant = "warm"
  - Else                              → invitation_variant = "cold"

Warm variant (DE):
  "Wir haben gesehen, dass Sie Updates von [studio_name] erhalten. Möchten Sie auch
   weiterhin über Klassen, Workshops und Angebote informiert werden? [opt-in link]"

Cold variant (DE):
  "[studio_name] arbeitet jetzt mit einem neuen System für Ankündigungen und
   Updates. Wenn Sie informiert bleiben möchten, klicken Sie hier zum Anmelden. [opt-in link]"
```

The legal effect is identical (both require explicit click to set `consent_marketing_email = true`) — only the framing differs. This improves the warm-list opt-in rate without overstating consent.

#### Implications & validations the bootstrap surfaces

Real data tends to reveal classifier gaps. The bootstrap MUST output a validation report:

```
Bootstrap result includes:
  - Count of distinct contacts seeded
  - Count of products discovered (sorted by frequency)
  - For each product: which classifier bucket the foundation assigned (trial / card / membership / voucher / merch)
  - Count of contacts with empty email (cannot receive email comms)
  - Count of contacts with invalid phone (cannot receive WhatsApp)
  - List of ambiguous customer matches (same name, different emails — surfaced for admin)
```

Studio owner reviews the classifier mapping before go-live and corrects any miscategorisations via `locations.product_keyword_map`. Example from the sample: `"3 Trial Cards-Introduction to Pilates Reformer"` is correctly classified as trial (matches `trial`); `"Gruppenmitgliedschaft-1 x Woche"` as membership (matches `mitgliedschaft`); `"10er Karte-Gruppe"` as card (residual — matches no trial/membership/voucher/merch keyword).

**Strengthening recommendation:** add "Karte" / "pack" / "credits" as explicit positive keywords for `is_card()` rather than relying on residual classification — see updated helpers below.

#### Updated helper: explicit-positive `is_card`

```python
def is_card(name_or_type):
  s = str(name_or_type).lower()
  # Explicit positive keywords (more robust than residual)
  if any(k in s for k in ("karte", "card", "pack", "credits", "punktekarte")):
    # Even with positive match, trial card patterns ("trial cards") must defer to is_trial
    if is_trial(name_or_type):
      return False
    return True
  # Fallback to residual (preserves backwards compatibility)
  return (
    not is_trial(name_or_type)
    and not is_membership(name_or_type)
    and not is_voucher(name_or_type)
    and not is_merch(name_or_type)
  )
```

#### Bootstrap re-runs

Re-uploading is supported. The protocol is fully idempotent. Use cases:

- Studio re-exports after fixing a data issue in Eversports → re-upload → only new/changed rows touched
- Studio uploads a wider date range later → bookings outside the original window are added; contacts gain additional history
- Studio uploads an active memberships export that was missed initially → memberships seeded into existing contacts

To force a complete re-seed (e.g. mistaken initial upload): `POST /api/v1/admin/locations/{id}/bootstrap/reset` followed by a fresh upload. This deletes the bootstrap-sourced rows tagged `bootstrap_run_id = <prior>` from the previous run.

#### Hand-off to daily sync

Once `historical_sync_flag = "complete"`, the event-driven scheduler activates and the daily sync takes over. The bootstrap row in `sync_log` is preserved as the audit anchor for "this is the historical baseline." All subsequent sync runs only push deltas vs. the state established by the bootstrap.

---

### Event-driven schedule

At 06:00 local time daily, the scheduler reads the day's active appointments report and computes a sorted list of unique `session_end_time` values. For each such time T, it schedules a sync run at **T + 15 min**.

```python
sessions = read("active_appointments_today")
class_block_ends = sorted(set(s.session_end_time for s in sessions))
for t in class_block_ends:
  scheduler.enqueue_sync_run(at=t + 15min)
```

Plus a baseline hourly catch-up 07:00–22:00 local for product/membership/billing changes that aren't pinned to a class.

Plus 03:00 overnight full reconciliation.

---

## Layer 2 — Postgres Datastore

### Tables

| Table | Purpose |
|---|---|
| `locations` | One row per studio location · sub-account ID, timezone, secrets refs, ai_monthly_budget |
| `contacts` | One row per Eversports customer per location · current + previous values for delta · `ghl_contact_id` after first sync |
| `products` | Eversports active products & memberships per location · refreshed each run |
| `bookings` | One row per Eversports booking (last 90 days) · session_id, customer_id, attendance_status |
| `sessions` | Activity schedule from admin activities export · session_id, activity, datetime, total_spots, registered_count, derived available_spots |
| `writeback_jobs` | Pending / in-flight / completed writeback actions · idempotency_key, status, retries, error |
| `ai_usage` | One row per AI call · location_id, use_case, contact_id, model, prompt_tokens, completion_tokens, cost_usd, ts |
| `consent_audit` | Append-only log of consent changes · contact_id, channel, value, source, ts, ip, message_shown |
| `gatekeeper_log` | Append-only log of every inbound-message classification + routing decision · contact_id, channel, raw_text, classification, confidence, route_to, action_taken, owner_override, ts |
| `sync_log` | One row per sync run · run_type, contacts_processed, contacts_updated, tags_applied, pipeline_moves, errors, duration |

### Delta engine

```python
for customer in contacts_in_scope:
  change_set = {}
  for field in syncable_fields:
    if customer.current[field] != customer.previous[field]:
      change_set[field] = customer.current[field]

  flags = compute_flags(customer)
  if change_set or flags:
    enqueue_ghl_sync(customer, change_set, flags)
```

### Flag computation

```python
def compute_flags(customer):
  flags = {}

  # Trial conversion detection (UC02) — historical-trial detection (not "exactly 1 prior product")
  prior = set(customer.previous.products_purchased)
  current = set(customer.current.products_purchased)
  had_any_trial_historically = any(is_trial(p) for p in prior)
  newly_added = [
    p for p in (current - prior)
    if not is_trial(p) and not is_voucher(p) and not is_merch(p)
  ]
  if had_any_trial_historically and newly_added:
    flags["trial_purchase_detected"] = True
    flags["new_package_name"] = newly_added[0]

  # Last trial session (UC01)
  if (
    is_trial(customer.active_package_type)
    and customer.active_package_sessions_remaining == 0
    and customer.last_session_date == today
  ):
    flags["trial_last_session"] = True

  # Card upsell — high-frequency customer (UC04 Membership ready trigger)
  # Counts bookings made in the last 30 days, divides by 4.33 to normalise to per-week
  bookings_last_30 = count(b for b in customer.bookings if b.session_datetime >= today - 30 and b.session_datetime <= today)
  customer.sessions_per_week_last_month = round(bookings_last_30 / 4.33, 2)

  if is_card(customer.active_package_type) and customer.sessions_per_week_last_month > location.card_upsell_min_sessions_per_week:
    flags["card_upsell_ready"] = True

  # Attendance drop (UC04 membership at-risk)
  if customer.sessions_attended_last_month > 0 and customer.sessions_attended_this_month < (customer.sessions_attended_last_month * 0.5):
    flags["attendance_drop_50"] = True

  # Card 14-day inactivity
  if customer.last_booking_date and customer.last_booking_date < today - 14 and is_card(customer.active_package_type):
    flags["no_booking_14_days"] = True

  # Membership renewal due
  if customer.active_package_expiry_date and customer.active_package_expiry_date <= today + 14:
    flags["renewal_due"] = True

  return flags
```

---

## Layer 3 — GHL Read Sync

### Step 1 — Contact match & upsert

```python
ghl_contact = ghl_api.search_contacts(email=customer.email)
if not ghl_contact and customer.phone:
  ghl_contact = ghl_api.search_contacts(phone=customer.phone)
if not ghl_contact:
  ghl_contact = ghl_api.create_contact({
    "email": customer.email,
    "phone": customer.phone,
    "firstName": customer.first_name,
    "lastName": customer.last_name,
    "tags": ["new-contact"],
    "customFields": {
      "eversports_customer_id": customer.eversports_customer_id,
      "eversports_location_id": customer.location_id,
      "consent_marketing_email": False,
      "consent_marketing_whatsapp": False,
      "consent_marketing_voice": False,
    },
  })
  enqueue_consent_invitation(ghl_contact)
contacts_row.ghl_contact_id = ghl_contact.id
```

Standard GHL fields (`firstName`, `lastName`, `email`, `phone`) are written to GHL standard fields — never to custom fields.

### Step 2 — Push delta fields

Only delta fields are pushed. JSON-summary fields (e.g. `booking_history_summary`) are computed at sync time as compact text ≤ 3500 chars; full JSON lives in Postgres.

### Step 3 — Tag engine

```python
tag_rules = [
  # Trial
  {"if": flags.trial_last_session,        "apply": ["trial-last-session"]},
  {"if": is_trial(pkg) and sessions > 0,  "apply": ["trial-active"]},
  {"if": flags.trial_purchase_detected,   "apply": ["trial-purchase-detected"], "remove": ["trial-active"]},

  # Card
  {"if": is_card(pkg),                                       "apply": ["card-active"], "remove": ["trial-active"]},
  {"if": flags.no_booking_14_days and is_card(pkg),          "apply": ["low-attendance"]},
  {"if": is_card(pkg) and customer.sessions_per_week_last_month > location.card_upsell_min_sessions_per_week,
                                                              "apply": ["membership-ready"], "remove": ["low-attendance"]},

  # Membership
  {"if": is_membership(pkg),                                 "apply": ["membership-active"], "remove": ["card-active", "trial-active"]},
  {"if": is_membership(pkg) and (last_session_date < today - 14 or flags.attendance_drop_50),
                                                              "apply": ["at-risk"]},
  {"if": flags.renewal_due and is_membership(pkg),           "apply": ["renewal-due"]},

  # Renewed
  {"if": is_membership(pkg) and new_membership_detected and prev_expiry_within_14d,
                                                              "apply": ["renewed", "membership-active"],
                                                              "remove": ["renewal-due", "at-risk", "churned"]},
  # Lapsed
  {"if": last_booking_date < today - 30,                     "apply": ["lapsed"]},

  # Churned
  {"if": active_package_expiry_date < today and not has_active_package, "apply": ["churned"],
                                                              "remove": ["card-active", "membership-active", "renewal-due"]},
]
```

**Tag firing semantics:** the foundation applies tags first, then waits 60 seconds before processing any tag removals on the same contact in the same sync. This prevents GHL "tag added" triggers from racing with foundation removals on short-lived signal tags (e.g. `trial-purchase-detected`).

### Step 4 — Pipeline engine

Pipeline transitions are driven by tag state. See `03_ghl_pipelines.md`.

---

## Layer 4 — Eversports Writeback Executor

### Job queue model

```
                  ┌──────────────────────────────┐
GHL workflow ────▶│ writeback_jobs (Postgres)     │
                  │ status: queued / running /    │
                  │ succeeded / failed / dead     │
                  └──────────────┬───────────────┘
                                 │
                                 ▼
                       Playwright writeback worker
                                 │
                                 ▼
                       Eversports admin panel
                                 │
                                 ▼
                       GHL webhook (result)
```

### Supported job types

| Job type | Payload | Idempotency key | Action in Eversports |
|---|---|---|---|
| `create_customer` | first_name, last_name, email, phone, marketing_consents | sha256(location_id + email) | Customers → New customer form |
| `create_booking` | customer_id, activity_id, session_datetime, package_id | sha256(customer_id + session_id) | Activity calendar → Add participant |
| `reschedule_booking` | booking_id, new_session_id, reason | sha256(booking_id + new_session_id) | Booking detail → Move to new session |
| `cancel_booking` | booking_id, reason | sha256(booking_id + "cancel") | Booking detail → Cancel |

### Retry policy

- Up to 3 attempts with exponential backoff (30s, 2min, 10min)
- On failure after retries: status = `dead`, apply `writeback-failed` tag, notify owner with failure payload (3-action standard)
- Idempotency key prevents duplicate actions on retry

### Result reporting

- Success → emit GHL inbound webhook → fires `writeback-success` workflow → removes in-flight tag, sends customer confirmation message, updates opportunity stage
- Failure → emit failure webhook → fires `writeback-failed` workflow → notifies owner with the full failure context so they can fall back to manual action in Eversports

---

## Layer 5 — Consent Gate

Every outbound message from any use case workflow routes through a shared **Consent Gate** sub-workflow before delivery.

```
[Use case wants to send message]
        │
        ▼
[Consent Gate]
   - channel ∈ {email, whatsapp, voice (v2)}
   - read consent_marketing_<channel> from contact
   - IF false OR contact has opted-out tag:
       → log to consent_audit
       → exit workflow gracefully (no error notification)
   - IF true:
       → continue to send
```

See `08_consent_model.md` for full consent capture and opt-out flow.

### Multilingual STOP detection

A shared workflow listens for inbound messages matching the per-location `stop_keywords` regex. Default:

```
^(stop|stopp|aufhören|aufhoeren|abmelden|keine werbung|unsubscribe|opt out|opt-out)$
```

Match → flip the relevant channel consent boolean false, stamp `consent_revoked_<channel>_at`, apply `opted-out` tag, send confirmation in the customer's language, exit all active automations for that contact.

---

## Layer 6 — Gatekeeper (inbound classifier)

The gatekeeper sits between GHL's inbound webhook and every use case workflow. It exists for one reason: when we ingest from social channels (Instagram DMs + comments, Facebook DMs + comments), most of the volume is noise — emoji reactions, compliments, off-topic chatter. Without filtering, that noise burns Sonnet tokens in UC04 / UC05 and pages owners for nothing. The gatekeeper triages with the cheap, fast classifier (Claude Haiku) and only escalates what matters.

### Inputs

- The inbound message text (raw, in whatever language the customer used)
- The contact's GHL profile snippet, when contact is matched: active package, pipeline stages, recent activity, consent state, `opted-out` tag
- The channel and surface (e.g. `instagram_comment_on_post_xyz` vs `instagram_dm`)
- Optional context: prior conversation history in the last 24h

### Classification categories

| Category | Routing | Notes |
|---|---|---|
| `inquiry_pricing` | UC04 | Sales chatbot |
| `inquiry_class_info` | UC04 | Class details, schedules, beginner suitability |
| `inquiry_membership` | UC04 | Renewal, change plan, freeze |
| `booking` | UC05 | Schedule / reschedule / cancel — UC05 sub-classifies the intent |
| `trial_reply` | UC04 (handed off from UC01) | Customer replied to a UC01 follow-up message |
| `complaint` | Owner (escalation, 3-action notification) | Pages immediately |
| `injury_medical` | Owner | Sensitive, never AI-handled |
| `billing_dispute` | Owner | Sensitive |
| `opt_out` | Consent gate | STOP / STOPP / AUFHÖREN / ABMELDEN / etc. handled by the existing opt-out workflow |
| `acknowledgment` | Noise — silent ignore by default; `auto_reply_template` per studio config | "thanks!" "ok" "got it" |
| `emoji_reaction` | Noise — `react_emoji` by default (responds with 🙏 or matching emoji) | Standalone emoji messages |
| `social_compliment` | Noise — `react_emoji` or `auto_reply_template` ("Thanks for the love! 💛") | "Amazing space!" "Love this class" on Instagram/Facebook comments |
| `off_topic` | Noise — `silent_ignore` | Unrelated to the studio |
| `spam` | Noise — `silent_ignore` + flagged | Marketing pitches, scams |
| `low_confidence` | Owner | Confidence below `gatekeeper_confidence_threshold` for ANY category — escalate rather than guess |

### Algorithm

```python
def gatekeeper(message, contact, channel, location):
    if not location.gatekeeper_enabled:
        return route_legacy_direct_to_uc04(message, contact)

    # Multilingual STOP detection runs FIRST and bypasses the AI classifier
    if matches_stop_regex(message.text, location.stop_keywords):
        return route_to_consent_gate(message, contact, channel)

    # Classify
    result = claude_haiku.classify(
        message.text,
        contact_snippet=build_contact_snippet(contact),
        channel=channel,
        location=location,
    )
    log_to_gatekeeper_log(message, result)

    # Confidence floor
    if result.confidence < location.gatekeeper_confidence_threshold:
        return escalate_to_owner(message, contact, reason="low_confidence")

    # Category-driven routing
    if result.category in ("inquiry_pricing", "inquiry_class_info", "inquiry_membership", "trial_reply"):
        return route_to_uc04(message, contact, gatekeeper_classification=result)
    elif result.category == "booking":
        return route_to_uc05(message, contact, gatekeeper_classification=result)
    elif result.category in location.gatekeeper_owner_alert_categories:
        return escalate_to_owner(message, contact, reason=result.category)
    elif result.category.startswith("noise_") or result.category in ("acknowledgment", "emoji_reaction", "social_compliment", "off_topic", "spam"):
        policy = location.gatekeeper_noise_action.get(result.category, "silent_ignore")
        return execute_noise_policy(message, contact, channel, policy)
    else:
        return escalate_to_owner(message, contact, reason="unknown_category")
```

### Noise policies

Per noise category, the studio configures one of:

- `silent_ignore` — do nothing. Logged, not surfaced.
- `react_emoji` — auto-emoji reaction (Instagram/Facebook native reactions) or a single-emoji WhatsApp reply.
- `auto_reply_template` — short pre-approved text reply, varies by channel and locale ("Thanks for the love! 🙏" / "Vielen Dank! 🙏"). Falls back to silent ignore if no template is configured for the channel.

Auto-reactions and auto-replies bypass the consent gate because they're acknowledgments to customer-initiated contact, not marketing communications.

### Owner overrides

Every gatekeeper decision is auditable in the Conversations inbox's "Filtered out" folder. The owner can:

- **Reclassify** a single message — the new classification routes the message normally
- **Mark a sender as VIP** — future messages from this contact skip the noise filter for N days (per-location setting)
- **Add a rule** — content patterns that should always route to a specific category (per-location, max 20 rules)

Overrides write to `gatekeeper_log.owner_override` and (in v2) become labelled training data for an improved per-location classifier.

### Per-location configuration

| Setting | Description | Default |
|---|---|---|
| `gatekeeper_enabled` | Master switch | `true` |
| `gatekeeper_confidence_threshold` | Min confidence before auto-action | `0.7` |
| `gatekeeper_noise_action` | JSON map: category → policy | see Noise policies above |
| `gatekeeper_owner_alert_categories` | Comma list of categories that page the owner | `complaint,injury_medical,billing_dispute,low_confidence` |

### AI usage

Every gatekeeper classification call writes an `ai_usage` row with `use_case = "gatekeeper"` and `step = "classification"`. Model is `claude-haiku-4-5` for cost efficiency. Typical cost ~€0.001 per call. At 200 inbound messages/day, monthly gatekeeper spend per location is ~€6.

### Inbound channel scope (v1)

With the gatekeeper in place, v1 ingests all of:

- WhatsApp DMs
- Email
- Instagram DMs
- Instagram comments on the studio's own posts
- Facebook DMs
- Facebook comments on the studio's own posts

(Outbound channel scope is unchanged — UC01 + UC04 outbound still target WhatsApp + Email only. Voice deferred to v2.)

---

## AI Usage Logger

Every AI call writes a row to `ai_usage`:

| Column | Notes |
|---|---|
| `id` | uuid |
| `location_id` | FK |
| `contact_id` | GHL contact ID |
| `use_case` | UC01 / UC02 / UC03 / UC04 / UC05 |
| `step` | "intent_detection" / "message_generation" / "reply_handling" / "summary" |
| `model` | e.g. "claude-sonnet-4-6" |
| `prompt_tokens` | int |
| `completion_tokens` | int |
| `cost_usd` | computed from model price card |
| `ts` | datetime |

Billing roll-up: monthly per location → invoice line item.

Soft cap: when `ai_monthly_spend > 0.8 × ai_monthly_budget`, owner warning email.
Hard cap: when `ai_monthly_spend ≥ ai_monthly_budget`, suspend non-essential AI calls (UC03 falls back to a fixed-template email; UC04 outbound suppressed; UC04 inbound stays live to avoid breaking customer support).

---

## Sync Log & Observability

After each sync run, append a row to `sync_log` and emit metrics to monitoring:

| Column | Value |
|---|---|
| `run_timestamp` | Start time |
| `run_type` | event-driven / hourly-catchup / overnight / historical |
| `contacts_processed` | Total Eversports customers in reports |
| `contacts_updated_ghl` | Contacts where GHL fields changed |
| `contacts_created_ghl` | New GHL contacts |
| `tags_applied` | Apply ops |
| `tags_removed` | Remove ops |
| `pipeline_moves` | Stage changes |
| `errors` | Count |
| `error_details` | JSON list |
| `run_duration_seconds` | Total time |
| `writeback_jobs_processed` | Completed in window |
| `writeback_jobs_failed` | Failed in window |

### Alerting

- Scrape failure 2× consecutive → owner email + ops Slack
- GHL API quota 80% → ops alert
- Writeback worker stalled (no job processed in 30 min while queue > 0) → ops alert
- AI monthly spend 80% → owner email
- Any dead-letter writeback job → owner email + GHL task

---

## Error Handling

### GHL API failure on a single contact

```python
MAX_RETRIES = 3
BACKOFF = [2, 5, 10]
for attempt in range(MAX_RETRIES):
  try:
    ghl_api.update_contact(...)
    break
  except GHLAPIError as e:
    if attempt < MAX_RETRIES - 1:
      sleep(BACKOFF[attempt])
    else:
      log_error(contact.email, str(e))
      contacts_row.ghl_sync_status = "error"
      continue
```

### Scrape failure (Eversports unreachable)

```python
try:
  download_reports()
except ScrapeError:
  log_sync_error("Eversports scrape failed at " + now())
  if consecutive_failures >= 2:
    send_alert_email(studio_owner_email, "Foundation sync failed")
  abort_run()  # never push stale data
```

### Partial report failure

Update only fields sourced from successfully downloaded reports. Log which report failed. Do not clear existing GHL field values for the failed report's fields.

### Writeback failure

After 3 retries with backoff, mark `dead`, apply `writeback-failed` tag, fire owner notification with the failed action payload so they can fall back to manual action in Eversports.

---

## Helper Functions

```python
def is_trial(name_or_type):
  s = str(name_or_type).lower()
  return any(k in s for k in ("trial", "probe", "probestunde", "proba", "introductory"))

def is_membership(name_or_type):
  s = str(name_or_type).lower()
  return any(k in s for k in ("membership", "mitgliedschaft", "abo", "subscription"))

def is_voucher(name_or_type):
  s = str(name_or_type).lower()
  return any(k in s for k in ("voucher", "gutschein", "gift"))

def is_merch(name_or_type):
  s = str(name_or_type).lower()
  return any(k in s for k in ("merch", "shirt", "bottle", "towel", "article"))

def is_card(name_or_type):
  if is_trial(name_or_type): return False
  if is_membership(name_or_type): return False
  if is_voucher(name_or_type): return False
  if is_merch(name_or_type): return False
  return True
```

Per-location overrides in `locations.product_keyword_map` JSON.

---

## GHL API Calls Used

| Operation | Endpoint |
|---|---|
| Search contact by email | `GET /contacts/?email=` |
| Search contact by phone | `GET /contacts/?phone=` |
| Create contact | `POST /contacts/` |
| Update contact fields | `PUT /contacts/{id}` |
| Apply tags | `POST /contacts/{id}/tags` |
| Remove tags | `DELETE /contacts/{id}/tags` |
| Get opportunities by contact | `GET /opportunities/?contactId=` |
| Update opportunity stage | `PUT /opportunities/{id}` |
| Create opportunity | `POST /opportunities/` |
| Trigger inbound webhook (writeback result) | `POST /hooks/{webhook_id}` |
| Send conversation message | `POST /conversations/messages` |
| Add task | `POST /tasks/` |

---

## Configuration (per location, stored in `locations` table)

| Key | Description | Example |
|---|---|---|
| `location_id` | Internal UUID (PK — `id` column in the `locations` table) | uuid |
| `eversports_studio_id` | Eversports studio ID in export URLs | `Yneu3U` |
| `eversports_location_id` | Optional sub-location identifier within a multi-site studio. Nullable. | `loc_abc` |
| `ghl_subaccount_id` | GHL sub-account ID | `abc123` |
| `ghl_oauth_token_ref` | Secret manager reference | `secret://ghl/abc123` |
| `eversports_credentials_ref` | Scraper login | `secret://eversports/login/abc123` |
| `timezone` | IANA timezone | `Europe/Vienna` |
| `country` | ISO 3166-1 alpha-2 country code. Used as `default_region` for phone normalisation (libphonenumber). DACH only: `DE`, `AT`, `CH`. Default `DE`. | `AT` |
| `historical_sync_flag` | Whether 30-day historical sync has run | `complete` / `pending` |
| `late_cancel_window_hours` | Studio policy | `24` |
| `studio_owner_email` | Notifications | `owner@studio.com` |
| `studio_name` | AI prompts | `Flow Pilates` |
| `location_name` | Display | `Flow Pilates — Mariahilf` |
| `stop_keywords` | Opt-out regex | see Layer 5 default |
| `ai_monthly_budget_usd` | Hard cap | `200` |
| `renewal_handling_mode` | Either-or choice for renewal touches. `studio_outreach` (UC04 sends our own renewal nudge) or `defer_to_eversports` (UC04 stays silent on renewals; Eversports' native reminder is the only one). | `studio_outreach` |
| `card_upsell_min_sessions_per_week` | Threshold for Card → Membership ready upsell trigger. The `membership-ready` tag fires when a card customer's `sessions_per_week_last_month` exceeds this. | `2` |
| `product_keyword_map` | JSON override for is_trial/is_membership/etc | `{}` |
| `whatsapp_templates` | Approved WhatsApp Business templates list | see UC01 |
| `consent_default_locale` | BCP-47 locale used as the fallback when auto-detecting date/phone formats during CSV bootstrap and when generating consent invitation copy. | `de-AT` |
| `writeback_mode` | Per-location: how UC04/UC05 perform Eversports actions | `auto_execute` (default) or `admin_task` |
| `uc05_slot_min_lead_time_minutes` | UC05 won't propose slots starting within N minutes of "now". Default 60 min — absorbs sync staleness up to one hourly catch-up cycle. | `60` |
| `uc05_safety_margin_spots` | Minimum free-spots a slot must show before UC05 will propose it. Default 2. | `2` |

---

## UC05 availability freshness (no separate audit needed)

Because the Provider API is not used, UC05 availability is derived from the admin activities export, refreshed at the standard sync cadence:

- Event-driven sync (~15 min after each class block ends) — picks up bookings/cancellations from completed slots
- Hourly catch-up sync (07:00–22:00 local) — picks up booking/cancellation activity that isn't pinned to a class end
- Overnight reconciliation (03:00) — full pull

This means UC05's `available_spots` view of a future session is at worst ~60 minutes stale (between hourly catch-up runs). To handle this, UC05 enforces three protections:

1. **`available_spots >= 2` safety margin** — UC05 never proposes a slot with only one apparent spot free; this absorbs minor lag.
2. **Slot-minimum lead time** — UC05 never proposes a slot starting within `locations.uc05_slot_min_lead_time_minutes` (default 60 min) of "now"; this avoids proposing a slot that may have changed since the last sync.
3. **Writeback re-validation** — the writeback executor performs the booking action in Eversports admin in real time; if the slot has filled since the AI proposed it, the writeback action fails and the failure webhook fires UC05's "team will follow up" fallback message.

The combination produces effectively zero customer-visible double-bookings without needing a separate availability source.

---

## Open Items / To Confirm

- [x] **RESOLVED (2026-05-24):** UC05 availability is derived from admin activities export with `available_spots = max_participants − registered`. Safety margin `≥ 2 spots` + 60-min slot-minimum lead time + writeback re-validation produce zero double-bookings in practice.
- [x] **RESOLVED (2026-05-24):** Eversports admin browser automation legality handled via studio-attestation in the DPA (each studio attests they authorize us as their delegate; see `08_consent_model.md` § DPA). If Eversports formally declines despite this, fall back to admin-task mode per location (see UC05 § "Admin-task fallback mode" and `locations.writeback_mode` setting).
- [ ] Define per-location scraper concurrency limit (avoid IP blocks across many locations on shared infrastructure).
- [ ] Confirm GHL webhook signing secret rotation process for the writeback result callbacks.
