# Use Case 04 — Sales Consultant Chatbot

## Overview

**Goal:** An AI-powered sales consultant that operates in two modes — inbound (responding to customer messages) and outbound (proactively reaching out based on pipeline stage triggers). The AI handles the full conversation, recommends the best product based on the customer's profile, and triggers Eversports writeback to create the booking or purchase. It hands off to a human when a complex question is detected.

**v1 scope:** Inbound from WhatsApp, Email, Instagram (DM + comment), Facebook (DM + comment) — all routed via the gatekeeper (`07_foundation_layer.md` Layer 6). Outbound: WhatsApp + Email only. Voice deferred to v2.

**Trigger source (inbound):** Messages routed to UC04 by the gatekeeper. The gatekeeper has already filtered out noise (acknowledgments, emoji reactions, social compliments) and only passes through messages classified as `inquiry_*` or `trial_reply`. UC04 receives the message text + the gatekeeper's classification + confidence so it can adapt its tone (e.g. a `trial_reply` warrants a different opener than a cold `inquiry_pricing`).
**Trigger source (outbound):** Pipeline stage changes — Card: Membership ready · Membership: Renewal due
**Channels:** WhatsApp · Email
**GHL role:** Conversation routing, soft-auth by channel identity, profile injection, tag management, human assignment, notifications, consent gate
**AI role:** Full conversation handling — intent detection, product recommendation, objection handling, handoff detection
**Eversports writeback:** When customer confirms purchase or booking, UC04 enqueues `create_booking` or `create_customer` jobs (see foundation Layer 4)
**Products scope:** Cards · memberships · active studio promotions

> **Revision note (v2):** Social channels deferred to v2; soft authentication by channel identity (no link click for general conversation); explicit `HANDOFF_REQUIRED` stripping; 3-attempt cap on outbound re-trigger; coordination with native Eversports renewal reminders.

---

## Mode A — Inbound

### Trigger

Any inbound message received on a connected channel (WhatsApp, Email, Instagram, Facebook) fires the GHL conversation AI workflow.

**GHL trigger:** Inbound message received on any connected channel

### Step 1 — Soft authentication by channel identity

```
inbound_channel = "whatsapp" | "email"
inbound_identifier = phone_number (whatsapp) | email_address (email)

contact = ghl.find_contact_by(inbound_channel_identifier)

IF len(contact) == 1:
  set session variable auth_verified_soft = true
  → proceed to Step 2

IF len(contact) > 1:
  → ambiguous match → escalate via hard-auth (one-time email code)

IF len(contact) == 0:
  → treat as new prospect → run a short identity capture
    (ask for first name + email if missing) → enqueue create_customer writeback
```

**Hard-auth is required only before sensitive actions:**
- Confirming a purchase (UC04 step "Purchase confirmed")
- Submitting a reschedule or cancel (UC05)
- Changing contact profile data

```
IF action requires hard-auth AND auth_verified_hard != true:
  → AI sends: "Before I confirm this, please tap the verification link I'm sending to your email."
  → send one-time code/link
  → wait for click confirmation
  → set auth_verified_hard = true
```

Both `auth_verified_soft` and `auth_verified_hard` are session-scoped only. Never stored on the contact.

### Step 2 — Load customer profile

Pull the following fields from the GHL contact and inject into the AI system prompt:

```
- first_name
- active_package_type
- active_package_name
- active_package_sessions_remaining
- active_package_expiry_date
- last_session_date
- last_class_name
- total_sessions_attended
- no_show_count
- booking_history (last 30 days)
- products_purchased (all time)
- pipeline_lead_stage
- pipeline_card_stage
- pipeline_membership_stage
- available_products (from GHL custom field: eversports_active_products)
- active_promotions (from GHL custom field: studio_active_promotions)
```

### Step 3 — AI conversation loop

The AI handles the conversation turn by turn. Each incoming message is passed to the AI with the full customer profile and conversation history.

#### AI system prompt

```
You are a warm, knowledgeable sales consultant for [studio_name], a Pilates studio.
You are chatting with [first_name] via [channel].

Your role is to:
- Understand what the customer needs or is asking
- Recommend the most suitable product based on their history and current package
- Answer questions about classes, packages, and studio offerings
- Guide them toward a purchase naturally — never pushy
- Send the purchase link when they are ready to buy

Customer profile:
- Current package: [active_package_name] ([active_package_sessions_remaining] sessions remaining)
- Package expires: [active_package_expiry_date]
- Last class attended: [last_class_name] on [last_session_date]
- Total sessions attended: [total_sessions_attended]
- Past products: [products_purchased]
- Booking history (last 30 days): [booking_history]

Available products:
[available_products]

Active promotions:
[active_promotions]

Tone guidelines:
- Warm, personal, boutique studio feel
- Never use pressure tactics or urgency language
- No discounts unless a promotion is in active_promotions
- Match the channel tone: WhatsApp = casual, Email = slightly more formal, Social = friendly

Handoff rule:
- If the customer asks something you cannot confidently answer (pricing disputes,
  injury-specific advice, class scheduling changes, billing issues, complaints),
  respond warmly and say a team member will follow up shortly.
- Output JSON: {"customer_message": "...", "handoff_required": true|false}
- The workflow extracts customer_message for delivery; handoff_required triggers the handoff branch.
```

**Implementation note:** the AI returns structured JSON. The workflow:
1. Parses the response
2. Sends `customer_message` to the channel
3. If `handoff_required == true`, fires the handoff branch
4. The literal token `HANDOFF_REQUIRED` is never sent to the customer (the prior approach of "invisible sentinel at end of message" was unreliable — JSON output is the v2 pattern)

#### Intent classification (AI evaluates each message)

```
PURCHASE_INTENT    → customer signals readiness to buy
QUESTION           → customer asks about products, classes, schedule
OBJECTION          → customer expresses hesitation or concern
COMPLEX_QUESTION   → pricing dispute, injury, billing, complaint
GREETING           → casual opening, no clear intent yet
OTHER              → anything else
```

#### AI response logic per intent

```
PURCHASE_INTENT:
  → confirm which product fits best
  → send purchase link from available_products
  → tag: "chatbot-sale-initiated"

QUESTION:
  → answer using profile + available_products data
  → guide naturally toward recommendation

OBJECTION:
  → acknowledge concern empathetically
  → reframe with value, not pressure
  → do not offer discounts unless active_promotions applies

COMPLEX_QUESTION or HANDOFF_REQUIRED detected:
  → send warm holding message to customer
  → trigger human handoff (see below)

GREETING:
  → warm opening response
  → ask open question to understand their needs
```

### Step 4 — Human handoff

Triggered when AI response contains `HANDOFF_REQUIRED`.

**Three simultaneous GHL actions:**

```
1. Assign GHL conversation to studio owner user
2. GHL internal notification:
   Title: "Chatbot handoff — [first_name] [last_name]"
   Body: "Customer needs human assistance. View conversation: [GHL conversation link]"

3. Email to studio owner:
   Subject: "Action needed — chatbot handoff for [first_name] [last_name]"
   Body:
     [first_name] [last_name] is in a conversation that needs your attention.
     Package: [active_package_name]
     Pipeline stage: [pipeline_card_stage or pipeline_membership_stage]
     Conversation: [GHL conversation link]
     Contact profile: [GHL contact link]
```

**Customer-facing holding message (AI sends this before handoff):**

```
"Great question — let me get one of our team members to help you with that directly.
Someone will be in touch with you shortly, [first_name]!"
```

### Step 5 — Purchase / booking confirmed

When the customer confirms purchase or booking intent in the conversation:

```
1. Hard-auth gate (if not already hard-auth verified this session)
2. Enqueue Eversports writeback job:
   - If new customer with no eversports_customer_id → create_customer
   - For product purchase → (delivered via Eversports checkout URL in the AI message)
   - For session booking → create_booking
3. Apply tag "chatbot-sale-initiated"
4. AI sends purchase link + confirmation message to customer
5. Wait for foundation sync to detect product/booking added
   (event-driven, so within ~15 min of class-end OR next hourly catch-up)
6. On detection:
   Apply tag: "chatbot-converted"
   Stamp custom field: conversion_source = "chatbot"
   Stamp custom field: chatbot_converted_at = now()
   Remove tag: "chatbot-active"
   Update pipeline stages:
     IF new product is card → Card pipeline: Standard card
     IF new product is membership → Membership pipeline: Active
                                  + Lead pipeline: Converted (membership)
   GHL owner notification (3-action standard):
     "Sale closed by chatbot — [first_name] purchased [product_name]"
```

The `chatbot-converted` tag + 24h timestamp signals UC02 to suppress its own owner notification (dedupe).

---

## Mode B — Outbound

### Trigger 1 — Card pipeline: Membership ready (frequency-based)

**Fires when:** Contact's `sessions_per_week_last_month` exceeds the per-location `card_upsell_min_sessions_per_week` threshold (default 2) AND `card-active` tag present. Surfaces as the Card pipeline "Membership ready" stage.
**Channel:** WhatsApp
**Rationale:** Customers using the studio more than ~2 sessions/week get materially better economics on a membership. We target them while they're engaged — not when they're about to run out.
**One message only per attempt — re-trigger cap applies (max 3 attempts every 7 days; see "No-reply re-trigger logic")**

#### AI outbound prompt — card upsell

```
You are a warm sales consultant for [studio_name], a Pilates studio.

Write a short WhatsApp message to [first_name] who has been a card customer
and is running low on sessions ([sessions_remaining] sessions left on their [active_package_name]).

The message should:
- Feel personal and timely — they are close to finishing their card
- Highlight the value of switching to a membership (better value per session,
  no need to keep buying cards, uninterrupted practice)
- Include a direct link to view membership options: [membership_product_link]
- Be warm, not pushy. No discounts unless active_promotions is not empty.
- Under 90 words. No subject line needed (WhatsApp).

Customer first name: [first_name]
Sessions remaining: [sessions_remaining]
Current package: [active_package_name]
Sessions attended total: [total_sessions_attended]
Available memberships: [available_products filtered to memberships]
Active promotions: [active_promotions]
Studio name: [studio_name]
```

---

### Trigger 2 — Membership pipeline: Renewal due

**Fires when:** Contact moves to "Renewal due" stage in membership pipeline (expiry within 14 days) AND `location.renewal_handling_mode == "studio_outreach"`. If the location's renewal handling mode is `defer_to_eversports`, this trigger is suppressed entirely and Eversports' native renewal reminder is the only touch the customer receives.
**Channel:** Email
**One message only per attempt — re-trigger cap applies**

#### AI outbound prompt — membership renewal

```
You are a warm sales consultant for [studio_name], a Pilates studio.

Write a short renewal reminder email to [first_name] whose membership expires
on [active_package_expiry_date] (in [days_until_expiry] days).

The email should:
- Be warm and appreciate their loyalty as a member
- Gently remind them their membership is expiring soon
- Encourage them to renew to keep their practice uninterrupted
- Include a direct renewal link: [membership_renewal_link]
- Mention any active promotions if available
- Under 120 words. Include a subject line.

Customer first name: [first_name]
Membership: [active_package_name]
Expiry date: [active_package_expiry_date]
Days until expiry: [days_until_expiry]
Total sessions attended: [total_sessions_attended]
Active promotions: [active_promotions]
Studio name: [studio_name]
```

---

### Outbound reply handling

If the customer replies to an outbound message, the conversation transitions to inbound mode — the AI picks up the reply and continues the conversation loop (Mode A, Step 3 onwards). Authentication is skipped since the contact is already known from the pipeline trigger.

```
ON inbound reply to outbound message:
  SET session variable auth_verified = true (contact is known — pipeline trigger confirmed identity)
  → enter Mode A conversation loop at Step 3
```

### No-reply re-trigger logic — capped at 3 attempts

```
ON daily sync:
  IF pipeline_card_stage == "Membership ready"
     AND chatbot_outbound_attempts < 3
     AND (last_chatbot_interaction is null OR last_chatbot_interaction < today - 7 days):
    → re-send outbound card upsell message (WhatsApp template)
    → chatbot_outbound_attempts += 1
    → last_chatbot_interaction = now()

  IF pipeline_membership_stage == "Renewal due"
     AND chatbot_outbound_attempts < 3
     AND (last_chatbot_interaction is null OR last_chatbot_interaction < today - 7 days)
     AND location.renewal_handling_mode == "studio_outreach":
    → send outbound renewal message (Email)
    → chatbot_outbound_attempts += 1
    → last_chatbot_interaction = now()

  IF location.renewal_handling_mode == "defer_to_eversports":
    → skip all UC04 renewal outreach for this location entirely
    → Eversports' own renewal reminder is the sole touchpoint
```

After 3 attempts with no reply, the contact moves to a "Stale lead" sub-stage (visible to the owner) and no further outbound is sent for this pipeline stage. Resets when the pipeline stage changes (e.g. they make a booking, churn, or renew).

**WhatsApp outbound template:** All outbound WhatsApp messages use the pre-approved template `chatbot_upsell_<msg_n>` with AI-generated variable values only.

---

## Product & Promotions Data

### Available products (foundation-synced)

Stored in GHL custom field: `eversports_active_products`
Format: JSON array

```json
[
  {
    "name": "10-session card",
    "type": "card",
    "price": 150,
    "link": "https://eversports.com/checkout/..."
  },
  {
    "name": "Monthly membership",
    "type": "membership",
    "price": 89,
    "link": "https://eversports.com/checkout/..."
  }
]
```

Updated by foundation daily sync from Eversports active products endpoint.

### Active promotions

Stored in GHL custom field: `studio_active_promotions`
Format: Text (plain language, studio owner updates manually in GHL)

Example: `"January offer: 10% off monthly membership until Jan 31"`

If empty: AI does not mention any promotions.

---

## Timing Table

| Mode | Trigger | Channel | Timing |
|---|---|---|---|
| Inbound | Customer sends message | WhatsApp / Email / Social | Immediate response |
| Outbound — card upsell | sessions_remaining < 3 | WhatsApp | Same day pipeline stage changes |
| Outbound — renewal | Expiry within 14 days | Email | Same day pipeline stage changes |

---

## Exit Conditions

| Exit | Condition | GHL actions |
|---|---|---|
| Sale closed | Purchase confirmed via chatbot | Tag `chatbot-converted`, update pipeline stages, notify owner |
| Human handoff | Complex question detected | Assign conversation, notify owner via GHL + email, pause AI |
| No reply (outbound) | Customer does not reply to outbound message | No action — conversation stays open, no follow-up sent |
| Auth failed | Customer cannot be verified | Send error message, end workflow |

---

## GHL Implementation Notes

### Tags used by this use case

| Tag | Applied by | Removed by |
|---|---|---|
| `chatbot-active` | This workflow on inbound start | This workflow on any exit |
| `chatbot-sale-initiated` | AI when purchase link sent | After purchase confirmed |
| `chatbot-converted` | This workflow on sale close | Never removed |
| `membership-ready` | Card pipeline (foundation) | This workflow on conversion |
| `renewal-due` | Membership pipeline (foundation) | This workflow on renewal |

### Custom fields read by this use case

| Field | Used for |
|---|---|
| `active_package_type` | Product recommendation logic |
| `active_package_name` | AI prompt context |
| `active_package_sessions_remaining` | Outbound card upsell trigger |
| `active_package_expiry_date` | Outbound renewal trigger + prompt |
| `booking_history` | AI context for personalisation |
| `products_purchased` | AI context — avoid recommending what they already had |
| `eversports_active_products` | Product list for AI recommendations |
| `studio_active_promotions` | Promotions for AI to mention |
| `pipeline_card_stage` | Outbound trigger condition |
| `pipeline_membership_stage` | Outbound trigger condition |

### Custom fields written by this use case

| Field | Value |
|---|---|
| `auth_verified` | Boolean — set true after successful authentication |
| `last_chatbot_interaction` | DateTime — timestamp of most recent AI conversation |

---

## Open Questions / To Confirm

- [ ] Should the AI have a name (e.g. "Sara from [studio_name]") or remain anonymous as the studio itself? *(Recommended: anonymous; safer for handoff continuity)*
- [ ] Purchase link: direct Eversports checkout URL vs GHL-tracked link. *(Recommended: GHL-tracked wrapper that 302-redirects to Eversports — gives us attribution AND the customer sees the Eversports checkout)*
- [ ] Outbound messages: pipeline stage is sufficient identity proof for outbound — soft-auth not required before outbound send (we already know who they are)
- [ ] `days_until_expiry` computed at send time as `active_package_expiry_date - today`. Foundation does not store this — workflow computes inline.
- [ ] WhatsApp template texts for `chatbot_upsell_card_msg1/2/3` and `chatbot_renewal_email_msg1/2/3` per locale — approval needed
