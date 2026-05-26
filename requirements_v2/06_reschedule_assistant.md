# Use Case 05 — Booking Assistant (v3)

> **Revision note (v3):** Renamed from "Reschedule / Cancel Assistant" to **Booking Assistant** to reflect the third intent. Added **SCHEDULE** intent — customer can ask the assistant to book a brand-new session (not only modify existing ones). Writeback mode (auto_execute vs admin_task) is now configured on this use case's settings page rather than the Eversports connection page — it logically belongs where it's applied.
>
> **Revision note (v2):** Switched from admin-task model to **auto-execute via Eversports writeback** (Layer 4). Added cancel intent handling (no longer punted to "future use case"). Added multi-booking selection flow. Availability check reads from the foundation's `sessions` table (populated by the admin activities scrape; `available_spots` derived as `max_participants − registered`). Provider API is NOT used. Hard-auth required before submission. Customer summary explicitly says "request, not confirmed yet" until writeback succeeds.

## Overview

**Goal:** When a customer sends a schedule, reschedule, or cancel request on WhatsApp or Email, the AI detects the intent, soft-authenticates by channel identity, loads context (the booking to change, or — for SCHEDULE — the customer's active package), checks policy where relevant, collects/confirms the slot, verifies availability against the foundation's `sessions` table (populated by the admin activities scrape), hard-authenticates the submission, enqueues a writeback job, sends a "request received" confirmation, and (on writeback success) sends a "confirmed" follow-up. If writeback fails, the owner is notified to act manually.

**Trigger source:** Any inbound message on WhatsApp or Email with reschedule or cancel intent detected
**Channels:** WhatsApp · Email (same channel as request)
**GHL role:** Intent routing, soft/hard authentication, consent gate, writeback job enqueue, notifications
**AI role:** Intent detection, slot collection conversation, summary, confirmation generation
**Foundation role:** Writeback execution against Eversports admin

---

## Workflow Diagram

```
[Inbound message — WhatsApp / Email]
        │
        ▼
[Phase 1: AI intent classification]
   RESCHEDULE | CANCEL | PURCHASE | QUESTION | OTHER
        │
        ▼ (RESCHEDULE or CANCEL)
[Phase 2: Soft-auth by channel identity]
   ambiguous match → escalate to hard-auth
   no match → identity capture flow
        │
        ▼
[Phase 3: Load booking(s)]
   IF upcoming_sessions_count == 1: use the only one
   IF > 1: AI asks "which booking?" (list next 14 days)
   IF == 0: AI replies "no upcoming booking found"
        │
        ▼
[Phase 4: Late-cancel policy check]
   late? → AI informs + collects reason → stored as reschedule_reason
   not late? → reschedule_reason = "N/A"
        │
        ▼ (RESCHEDULE path)            ▼ (CANCEL path)
[Phase 5a: Preferred slot + avail.]    [Phase 5b: Confirm cancel intent]
   AI asks for new date/time           AI confirms: "cancel [session] — sure?"
   Foundation lookup in sessions tab
   (from admin activities scrape)
   Not avail → ask again (max 3)
   Avail → continue
        │                                       │
        ▼                                       ▼
[Phase 6: Hard-auth gate]
   AI sends one-time email verification link
   Wait for click → set auth_verified_hard
        │
        ▼
[Phase 7: Summarise + enqueue writeback]
   AI summarises with "REQUEST — pending confirmation"
   Enqueue writeback job (reschedule_booking | cancel_booking)
   Apply "reschedule-in-flight" or "cancel-in-flight" tag
   GHL owner notification (3-action standard) — informational
        │
        ▼
[Wait for writeback result webhook]
   SUCCESS → AI sends "confirmed" message · remove in-flight tag · update fields
   FAILURE → AI sends "we're working on it — team will follow up" ·
             writeback-failed tag · owner notification (action required)
```

---

> **Pre-condition: messages arrive via the gatekeeper.** UC05 no longer reads directly from GHL inbound webhooks — the gatekeeper (`07_foundation_layer.md` Layer 6) classifies every inbound message first and routes only those it categorises as `booking` here. UC05's Phase 1 intent classifier still runs (to sub-classify SCHEDULE / RESCHEDULE / CANCEL), but it's running on messages that are already known to be booking-related, which improves accuracy and avoids spending Sonnet tokens on obvious-noise messages.

## Phase 1 — Intent Detection

### AI prompt

```
Classify the intent of the following customer message. Reply with ONE word only:
SCHEDULE, RESCHEDULE, CANCEL, PURCHASE, QUESTION, or OTHER.

Customer message: [inbound_message_text]
```

Routing:
- `SCHEDULE` → continue this workflow (schedule new booking branch — added in v3)
- `RESCHEDULE` → continue this workflow (reschedule branch)
- `CANCEL` → continue this workflow (cancel branch)
- `PURCHASE` / `QUESTION` → route to UC04 chatbot
- `OTHER` → AI asks a clarifying question

**Disambiguating SCHEDULE vs PURCHASE.** SCHEDULE means the customer already has an active card/membership and wants to book a specific session ("Can I book Wednesday's Reformer 10am class?"). PURCHASE means the customer is deciding which product to buy ("Do you have any beginner packages?"). When in doubt, the AI asks a clarifying question rather than guessing.

---

## Phase 2 — Soft Authentication

Same model as UC04: identify by inbound channel.

```
contact = ghl.find_contact_by(channel_identifier)

IF found uniquely:    set auth_verified_soft = true
IF ambiguous:          escalate to hard-auth (one-time email code)
IF not found:          identity capture (name + email) → enqueue create_customer
```

---

## Phase 3 — Branch on intent

After soft-auth, the workflow forks on the classified intent:

- **SCHEDULE** → Phase 3-SCHEDULE (new in v3): no existing booking to load. Verify the customer has an active package with sessions/visits available; collect target activity + datetime; jump to Phase 5a (availability check) then Phase 6+.
- **RESCHEDULE** → Phase 3-MODIFY: load existing booking, then Phase 4 (policy check), then Phase 5a.
- **CANCEL** → Phase 3-MODIFY: load existing booking, then Phase 4 (policy check), then Phase 5b.

## Phase 3-SCHEDULE — Verify package &amp; capture target (SCHEDULE intent only)

```
1. Read contact.active_package_type and contact.active_package_sessions_remaining.
   IF none active OR sessions_remaining == 0:
     AI replies: "It looks like you don't have an active package with sessions
                  available. Would you like to see what's on offer?"
     → route to UC04 PURCHASE intent. Exit UC05.

2. AI asks (if not already in the customer's message):
   - What kind of class? (defaults to recent activity_type from booking history)
   - What date/time?

3. Continue to Phase 5a — Preferred slot + availability check.
```

The SCHEDULE branch reuses Phase 5a (availability lookup) and the writeback enqueue from Phase 7, with `create_booking` as the writeback action (same job type UC04 uses for purchase-flow bookings).

## Phase 3-MODIFY — Load existing booking(s)

Foundation maintains:
- `upcoming_sessions_count` (number of bookings in the next 14 days)
- `upcoming_session_*` fields (the single next one)
- Full upcoming-bookings list in Postgres (queryable via GHL workflow → foundation internal API)

### Single-booking case (`upcoming_sessions_count == 1`)

```
AI:
"Hi [first_name]! I see you have [upcoming_session_name] booked for
[upcoming_session_date] at [upcoming_session_start_time].
Is this the booking you'd like to [reschedule|cancel]?"
```

### Multi-booking case (`upcoming_sessions_count > 1`)

```
AI lists the next up to 5 upcoming bookings, numbered:
"You have a few sessions coming up — which one?
 1. [Reformer 10:00 — Mon 3 Jun]
 2. [Mat 18:30 — Wed 5 Jun]
 3. [Reformer 12:00 — Fri 7 Jun]
Reply with the number or describe the session."
```

If customer's choice is ambiguous after 2 attempts → escalate to a GHL task with the conversation transcript.

### No-booking case

```
AI:
"It looks like you don't have any upcoming bookings right now.
Did you mean a different session, or would you like to book a new class?"
```

---

## Phase 4 — Late Cancellation Policy

```
late_cancel_threshold = upcoming_session_start_time − late_cancel_window_hours

IF now > late_cancel_threshold:
  AI:
  "Just so you know, [first_name] — our policy considers this a late [reschedule|cancellation]
   as your session starts in less than [late_cancel_window_hours] hours.
   We'll still process your request, but we need to note the reason.
   Could you let us know why?"
  Store customer's reply → reschedule_reason
ELSE:
  reschedule_reason = "N/A"
```

---

## Phase 5a — Reschedule: Preferred Slot + Availability

```
AI:
"What date and time would work best for you, [first_name]?
I'll check availability."

→ store preferred_date, preferred_time

Foundation lookup:
  candidate_sessions = postgres.sessions.where(
    activity_type ≈ original.activity_type,         -- prefer same class type
    datetime ≈ preferred_datetime ± 90 min,
    available_spots >= 2,                            -- SAFETY MARGIN (v2 decision)
  )
  # Safety margin: we only offer slots with ≥ 2 spots free because
  # the available_spots field is derived from the admin activities
  # scrape and refreshed at sync cadence (event-driven + hourly
  # catch-up), so it can be up to ~60 minutes stale. Operating at the ≥ 2
  # threshold makes double-bookings vanishingly rare at the cost of
  # occasionally telling a customer "no availability" when there is
  # technically one spot left. Studio owner can resolve those manually.

IF candidate_sessions:
  AI:
  "I found a few options around then:
   1. [Reformer Wed 5 Jun 10:00 — 3 spots open]
   2. [Reformer Wed 5 Jun 11:30 — 1 spot open]
   3. [Reformer Thu 6 Jun 10:00 — 5 spots open]
   Which works?"
  → store new_session_id

ELSE (no candidates within 90 min OR all full):
  AI:
  "We don't have availability close to that time. What's another option?"
  → loop back (max 3 attempts)
  → after 3 attempts: escalate with GHL task, exit workflow
```

The foundation's `sessions` table is the canonical availability source. It is refreshed at the standard sync cadence (event-driven + hourly catch-up) from the admin activities export. Two additional protections keep this safe:

- The writeback executor performs the booking in real time against Eversports admin. If the slot has filled since the AI proposed it, the writeback fails and UC05's failure path notifies the customer + owner.
- `locations.uc05_slot_min_lead_time_minutes` (default 60) prevents UC05 from proposing slots starting within that lead time — i.e. never proposes a slot that might have changed since the last sync.

---

## Phase 5b — Cancel: Confirmation

```
AI:
"Just to confirm — you'd like to cancel [upcoming_session_name] on [upcoming_session_date]
at [upcoming_session_start_time]. Is that right?"

IF customer confirms → proceed to Phase 6
IF customer changes mind → exit politely
```

---

## Phase 6 — Hard Authentication Gate (before submission)

Reschedule and cancel are sensitive actions — hard-auth is required.

```
IF auth_verified_hard != true:
  AI:
  "Before I lock in this change, please tap the verification link I'm sending to your email."

  Send one-time link (24h validity) to contact.email
  Wait up to 30 minutes for click
  IF clicked: set auth_verified_hard = true → continue
  IF timeout: AI:
    "I didn't get the verification click. I've passed your request to our team
     and they'll follow up shortly."
  → escalate to owner task, exit
```

---

## Phase 7 — Summarise + Enqueue Writeback

### Customer-facing summary (clear it's a request, NOT confirmed)

```
AI:
"Here's your reschedule request, [first_name]:

  CURRENT: [upcoming_session_name] on [upcoming_session_date] at [upcoming_session_start_time]
  REQUESTED: [new_session_name] on [new_session_date] at [new_session_time]
  [if late cancel] Late cancellation noted — reason: [reschedule_reason]

This is a request — I'm processing it now. You'll get a confirmation message
within a few minutes once it's locked in."
```

For cancel, similar phrasing with "cancelling this booking" instead.

### Enqueue writeback job

```
job = {
  type: "reschedule_booking" | "cancel_booking",
  payload: {
    location_id, booking_id, [new_session_id], reason
  },
  idempotency_key: sha256(...),
  callback_ghl_webhook: <reschedule_result_webhook_url>,
}
foundation.writeback_queue.enqueue(job)

Apply tag: "reschedule-in-flight" | "cancel-in-flight"
Set fields:
  reschedule_requested_date, reschedule_requested_time, reschedule_reason
```

### GHL owner notification (informational — 3-action standard)

```
GHL internal notification:
  Title: "Reschedule request in flight — [first_name] [last_name]"
  Body:  "Auto-executing via Eversports writeback. Will notify on success/failure."

GHL task (auto-resolved on success):
  Subject: "Reschedule pending — [first_name] [last_name]"
  Due: today

Email to owner (informational, summary-only)
```

---

## Phase 8 — Wait for Writeback Result

Foundation writeback executor performs the action in Eversports, then calls back via webhook.

### Success path

```
Webhook fires → workflow:
  Remove tag: "reschedule-in-flight" or "cancel-in-flight"
  Update upcoming_session_* fields from the new booking (or null on cancel)
  AI sends customer confirmation on the same channel:
    "[first_name], your [reschedule|cancellation] is confirmed.
     [reschedule] You're now booked for [new_session_name] on [new_session_date] at [new_session_time].
     [cancel] Your [session_name] on [date] is cancelled. Hope to see you again soon."
  Auto-resolve the owner's GHL task with note: "Auto-executed successfully"
```

### Failure path (after foundation retries)

```
Webhook fires (failure) → workflow:
  Apply tag: "writeback-failed"
  AI sends customer holding message:
    "[first_name], I'm working on locking this in but ran into a snag.
     Our team has been notified and will reach out shortly to confirm."
  GHL owner notification (3-action standard) with FULL failure context:
    - The job payload
    - The Eversports error message
    - A link to the conversation
  Owner manually executes the action in Eversports, then marks task complete.
```

---

## GHL Sub-Account Settings Used

| Setting | Usage |
|---|---|
| `late_cancel_window_hours` | Phase 4 policy check |
| `studio_owner_email` | Owner notifications |
| `studio_owner_name` | Salutations |

---

## Timing Table

| Step | Timing |
|---|---|
| Intent detection | Immediate on inbound |
| Soft-auth | Immediate |
| Booking load | Immediate (reads from GHL custom fields + foundation internal API) |
| Policy check | Immediate |
| Slot collection + availability check | Conversational, typical 1–3 turns |
| Hard-auth | Customer-side: up to 30 min for verification click |
| Writeback enqueue | Immediate after hard-auth |
| Writeback execution | Foundation worker — typical 30–90 seconds |
| Customer "confirmed" message | Immediately after writeback success webhook |

---

## Exit Conditions

| Exit | Condition | Actions |
|---|---|---|
| Normal success | Writeback succeeds | Confirmation sent, tag removed, fields updated, owner task auto-resolved |
| Writeback failure | Failed after retries | `writeback-failed` tag, owner notified with full context, customer told team will follow up |
| Hard-auth timeout | Customer didn't click link in 30 min | Owner task with full context, customer told team will follow up |
| Not reschedule/cancel intent | Classified as other | Routed appropriately |
| No availability after 3 attempts | Customer can't find a suitable slot (reschedule only) | Owner task: "No slot found — manual assistance" |
| Customer changes mind during cancel confirmation | Customer aborts | Polite exit, no tag changes |

---

## GHL Implementation Notes

### Tags used by this use case

| Tag | Applied by | Removed by |
|---|---|---|
| `reschedule-in-flight` | This workflow on writeback enqueue | This workflow on writeback success |
| `cancel-in-flight` | This workflow on writeback enqueue | This workflow on writeback success |
| `writeback-failed` | This workflow on writeback failure | Studio owner manually after resolving |

### Custom fields read

| Field | Used for |
|---|---|
| `upcoming_session_name`, `_date`, `_start_time`, `_end_time` | Identify booking (single case) |
| `upcoming_sessions_count` | Decide single vs multi-booking flow |
| `active_package_type`, `active_package_name` | Context |
| `email`, `phone` | Hard-auth delivery + channel matching |

### Custom fields written

| Field | Value |
|---|---|
| `reschedule_reason` | Customer-provided reason (late cancel) |
| `reschedule_requested_date` | Customer's preferred new date |
| `reschedule_requested_time` | Customer's preferred new time |

### Foundation API endpoints used

| Endpoint | Purpose |
|---|---|
| `GET /api/upcoming-bookings?contact_id=` | Multi-booking selection list |
| `GET /api/availability?activity_type=&datetime=&window=90` | Phase 5a slot lookup |
| `POST /api/writeback/jobs` | Enqueue reschedule_booking or cancel_booking |

---

## Open Questions / To Confirm

- [x] **RESOLVED (2026-05-24, later same day):** Provider API removed from the product entirely. UC05 availability is derived from the admin activities scrape (`available_spots = max_participants − registered`) and protected by the `≥ 2 spots` safety margin + 60-min slot-minimum lead time + writeback re-validation. See `07_foundation_layer.md` § "UC05 availability freshness".
- [ ] Verify Eversports admin UI flow for "move booking to new session" is automatable via Playwright (test during foundation build). **Fallback if not automatable / not contractually permitted:** see "Admin-task fallback mode" below.
- [ ] Hard-auth link timeout — 30 min reasonable? Or longer (e.g. 2h)? *(Recommended: 30 min for security; raise if customers complain)*
- [ ] Cancellation consequences — does Eversports auto-credit the session back to the card on cancellation (within or outside policy)? Need to mirror in `active_package_sessions_remaining` after writeback success
- [ ] If hard-auth succeeds but customer never confirms the AI summary explicitly, do we still enqueue? *(Recommended: yes, hard-auth + explicit reschedule confirmation request = consent to proceed)*

---

## Admin-task fallback mode (used if Eversports declines automated writeback)

If Eversports formally declines automated admin writeback (despite the studio-attestation DPA clause), UC05 falls back to the v1-original admin-task model without any AI / data-flow changes — only Phase 7 onwards changes:

```
Phase 7 (fallback):
  Apply tag: "reschedule-pending" (was: "reschedule-in-flight")
  Do NOT enqueue writeback job
  Create GHL task assigned to studio_owner with:
    - Original booking
    - Requested new slot
    - Late cancellation flag + reason
    - Customer contact link
  Send 3-action owner notification (notification + task + email)
  AI sends customer: "Got your reschedule request — our team
    will lock it in shortly. You'll hear back on this channel
    within a few hours."
  Workflow ends.

Owner manually executes in Eversports, then marks the GHL task complete.
Marking the task complete fires a "reschedule-confirmed" sub-workflow
that sends the customer the confirmation message.
```

This mode is enabled per location via `locations.writeback_mode = "admin_task"` (default: `"auto_execute"`). The decision is independent per location, allowing a hybrid rollout where some studios run auto-execute and others run admin-task during the ToS resolution period.

> **Note on UI placement (v3):** The owner-facing control for `writeback_mode` lives in **Settings → Automations → Booking assistant → Intents** rather than on the Eversports connection page. It logically belongs alongside the intents it governs. The underlying data model (`locations.writeback_mode`) is unchanged. The setting still applies to all writeback actions across the system — including `create_customer` and `create_booking` triggered by UC04 — not only UC05's reschedule/cancel.
