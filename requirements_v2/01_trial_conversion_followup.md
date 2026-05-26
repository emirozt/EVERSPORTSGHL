# Use Case 01 — Trial Conversion Follow-Up

> **Revision note (v2):** Added WhatsApp Business template requirement for messages outside the 24h service window, multilingual STOP detection, consent-gate dependency, event-driven trigger timing.

## Overview

**Goal:** When a customer attends their last trial session, automatically engage them within 2 hours via WhatsApp and email to encourage them to purchase a card or membership. Continue follow-up for up to 6 messages if they don't respond, and stop immediately if they convert or opt out.

**Trigger source:** Foundation layer — `trial-last-session` tag applied during event-driven sync (~15 min after class block ends)
**Channels:** WhatsApp (odd messages), Email (even messages)
**GHL role:** Automation sequencer, tag manager, reply listener, consent gate
**AI role:** Message generation personalised per customer; for WhatsApp messages outside the 24h service window, AI fills variable placeholders inside pre-approved WhatsApp Business templates
**Pre-requisite:** Customer must have `consent_marketing_whatsapp = true` for WhatsApp messages and `consent_marketing_email = true` for email. See `08_consent_model.md`.

---

## Workflow Diagram Description

```
[Foundation: today's sessions captured]
        │
        ▼
[GHL filter: contacts tagged "trial-last-session" today]
        │
        ▼
[Gate: already has non-trial package tag?]
   Yes ──► Skip contact (no action)
   No  ──► Continue
        │
        ▼
[GHL: add to automation, apply tag "trial-follow-up-active"]
        │
        ▼
[Wait: until session_end_time + 2 hours]
[Guard: if send time > 21:00, reschedule to 09:00 next morning]
        │
        ▼
[Message 1 — WhatsApp — AI-generated]
        │
        ▼
[Gate: positive reply received?]
   Yes ──► Handoff flow
   No  ──► Wait 3 days
        │
        ▼
[Message 2 — Email — AI-generated + STOP option]
        │
        ▼
[Gate: STOP / converted / no reply?]
   STOP      ──► Remove from automation, tag "opted-out"
   Converted ──► Handoff flow
   No reply  ──► Wait 5 days
        │
        ▼
[Messages 3–6: alternating WA/Email every 5 days]
[STOP + conversion check after every message]
        │
        ▼
[Gate: converted / STOP / 6 messages done?]
   Converted ──► Conversion exit
   STOP/done ──► End sequence exit
```

---

## Phase 1 — Detection & Trigger

### How the trigger works

The foundation layer runs its daily sync and captures today's sessions. As part of this sync, for every customer who attended a class today using a trial package where `sessions_used == sessions_total`, it applies the GHL tag `trial-last-session`.

The use case automation is triggered by this tag being applied in GHL.

**GHL trigger:** Contact tag added = `trial-last-session`

### Gate: Already converted?

Before starting the sequence, check whether the contact already has a non-trial active package.

```
IF contact.tags contains any of [card-active, membership-active, trial-converted]
  THEN skip → do not add to automation
  ELSE continue → add to automation
```

**GHL implementation:** Add an IF/ELSE branch at the start of the workflow checking for the above tags.

### GHL actions on entry

```
1. Add tag: "trial-follow-up-active"
2. Add contact to automation: "Trial Conversion Follow-Up Sequence"
3. Set workflow variable: send_time_1 = last_session_end_time + 2 hours
4. IF send_time_1 >= 21:00 THEN send_time_1 = next_day 09:00
   # Classes run 08:00-21:00. Sessions ending before 19:00 send same day.
   # Sessions ending at 19:00 or later roll to next morning at 09:00.
```

---

## Phase 2 — Message Sequence

### Timing table

| Message | Channel | Timing | Condition to send | WhatsApp delivery mode |
|---|---|---|---|---|
| 1 | WhatsApp | `session_end_time` + 2 hours (max 21:00) | Always (entry point) | **Pre-approved template `trial_followup_msg1`** — outside 24h window |
| 2 | Email | + 3 days after msg 1 | No reply to msg 1 | n/a |
| 3 | WhatsApp | + 5 days after msg 2 | No reply to msg 2 | **Pre-approved template `trial_followup_msg3`** |
| 4 | Email | + 5 days after msg 3 | No reply to msg 3 | n/a |
| 5 | WhatsApp | + 5 days after msg 4 | No reply to msg 4 | **Pre-approved template `trial_followup_msg5`** |
| 6 | Email | + 5 days after msg 5 | No reply to msg 5 | n/a |

**Total sequence duration if no reply:** ~23 days
**Channel pattern:** WhatsApp → Email → WhatsApp → Email → WhatsApp → Email

### WhatsApp Business policy compliance

Every WhatsApp send in this sequence is outside the 24h customer service window. Free-form messages will not deliver. Mitigation:

- Maintain 3 pre-approved WhatsApp Business Message Templates per location: `trial_followup_msg1`, `trial_followup_msg3`, `trial_followup_msg5`
- Each has variable placeholders for `{first_name}`, `{last_class_name}`, `{studio_name}`, `{package_options_short}`
- AI generates ONLY the variable values — never the body. A validator checks length + allowed characters before send.
- Once the customer replies, the 24h window opens; subsequent messages can be free-form AI generation.

### Consent gate

Every message in this sequence routes through the shared Consent Gate (`07_foundation_layer.md` Layer 5) before send. If the relevant channel consent is `false`, the message is skipped and the sequence either falls back to the other channel (if consented) or exits.

---

## Message 1 — WhatsApp

**Timing:** session_end_time + 2 hours (guard: max 21:00)  
**Channel:** WhatsApp  
**Generated by:** AI (Claude or GPT-4 via GHL webhook)

### AI prompt template

```
You are a warm, friendly assistant for [studio_name], a Pilates studio.

Write a short WhatsApp message to [first_name] who just finished their last trial session
in [last_class_name] today.

The message should:
- Feel personal and warm, not salesy
- Acknowledge they just completed their trial
- Gently mention that this was their last trial session
- Express that you'd love to have them continue their practice
- Invite them to explore membership or class card options
- End with a soft, open question (e.g. "How did you find today's class?")

Keep it under 80 words. Do not offer a discount. Do not use emojis excessively.
Write in the tone of a boutique Pilates studio — calm, encouraging, professional.

Customer first name: [first_name]
Class attended: [last_class_name]
Trial package name: [active_package_name]
Studio name: [studio_name]
```

### GHL data fields used

- `contact.first_name`
- `contact.last_class_name`
- `contact.active_package_name`
- `contact.last_session_end_time`
- `account.studio_name` (GHL sub-account level)

---

## Message 2 — Email

**Timing:** +3 days after message 1 (if no reply)  
**Channel:** Email  
**Generated by:** AI

### AI prompt template

```
You are a warm, friendly assistant for [studio_name], a Pilates studio.

Write a short follow-up email to [first_name] who completed their trial a few days ago.
They haven't responded to our first message.

The email should:
- Have a warm, personal subject line
- Acknowledge their trial is complete
- Focus on the value of continuing their practice (consistency, progress, community)
- Mention available options (class cards, memberships) without being pushy
- Include this exact line at the end: "If you'd prefer not to hear from us, simply reply with STOP."

Keep the email under 120 words. No discounts. Professional boutique studio tone.

Customer first name: [first_name]
Class attended: [last_class_name]
Trial package: [active_package_name]
Studio name: [studio_name]
```

### Output format expected from AI

```json
{
  "subject": "...",
  "body": "..."
}
```

---

## Messages 3–6 — Alternating WhatsApp / Email

**Timing:** Every 5 days  
**Channel pattern:** WA (msg 3) → Email (msg 4) → WA (msg 5) → Email (msg 6)  
**Generated by:** AI, with message number passed so tone can evolve slightly

### AI prompt template (shared, message number injected)

```
You are a warm assistant for [studio_name], a Pilates studio.

Write follow-up message number [message_number] of 6 to [first_name],
who completed their trial [days_since_trial] days ago and hasn't responded yet.

Channel: [whatsapp / email]

Guidelines:
- Do NOT repeat the same angle as previous messages
- Use a different value-focused angle each time (e.g. msg 3: consistency,
  msg 4: community, msg 5: progress tracking, msg 6: seasonal/timing)
- Keep tone warm and soft — never pushy or urgent
- No discounts or special offers
- For email: include subject line. For WhatsApp: no subject.
- From message 2 onwards, include: "Reply STOP anytime to unsubscribe."
- Keep WhatsApp under 80 words. Keep email under 130 words.

Customer first name: [first_name]
Message number: [message_number]
Days since trial ended: [days_since_trial]
Last class attended: [last_class_name]
Studio name: [studio_name]
```

---

## Reply Handling

### Positive reply detection

GHL listens for any inbound reply on the active conversation. If a reply is received that is NOT "STOP", the workflow branches to the handoff flow.

**GHL implementation:** Use a "Customer replied" trigger or conversation AI intent detection to classify reply as positive/neutral vs. STOP.

### STOP detection (multilingual)

Handled by the shared opt-out workflow in `07_foundation_layer.md` Layer 5. Detection regex (default):

```
^(stop|stopp|aufhören|aufhoeren|abmelden|keine werbung|unsubscribe|opt out|opt-out)$/i
```

Per-location override via `stop_keywords` config.

On match:
```
Flip consent_marketing_<channel> = false
Stamp consent_revoked_<channel>_at = now()
Remove tag: "trial-follow-up-active"
Add tag: "opted-out"
Remove from this automation
Send acknowledgement in customer's language:
  EN: "You've been unsubscribed. Have a great day, [first_name]."
  DE: "Sie wurden abgemeldet. Einen schönen Tag noch, [first_name]."
```

---

## Handoff Flow (triggered on any positive reply)

When a positive reply is detected at any point in the sequence:

```
1. Pause sequence (no more scheduled messages)
2. AI reads the customer reply and generates a contextual response
3. AI response sent on same channel as the reply came in
4. AI includes a direct product/checkout link from Eversports:
   - If customer seems ready to buy: send checkout link directly
   - If customer has questions: answer then send link
5. GHL: apply tag "trial-converted" once purchase is confirmed
6. GHL: remove tag "trial-follow-up-active"
7. GHL: remove contact from automation
8. GHL: notify studio owner — three simultaneous actions:
   a. GHL internal notification: "Trial customer [first_name] converted — new package purchased"
   b. GHL task assigned to studio owner with customer name + package + contact link
   c. Email to studio owner: subject "Trial converted — [first_name] [last_name]", body includes package purchased + GHL contact link
```

### AI prompt for handoff response

```
You are a helpful assistant for [studio_name].

A trial customer just replied to your follow-up message. Read their reply and respond naturally.

If they seem interested in buying:
- Confirm which product fits them best based on their usage
- Share the direct purchase link: [eversports_product_link]
- Keep it warm and brief

If they have a question:
- Answer it helpfully
- Then guide them toward the purchase link

Customer name: [first_name]
Their reply: [customer_reply]
Last class: [last_class_name]
Available products: [active_products_list]
Purchase link: [eversports_checkout_url]
```

---

## Exit Conditions

| Exit | Trigger | GHL actions |
|---|---|---|
| Converted | Purchase detected or positive reply confirmed | Tag `trial-converted`, remove `trial-follow-up-active`, remove from automation, notify owner |
| Opted out | Reply = STOP | Tag `opted-out`, remove `trial-follow-up-active`, remove from automation |
| Sequence complete | 6 messages sent, no response | Tag `trial-not-converted`, remove `trial-follow-up-active`, remove from automation |
| Already converted at entry | Non-trial package tag found at gate | Skip — no action taken |

---

## GHL Implementation Notes

### Tags used by this use case

| Tag | Applied by | Removed by |
|---|---|---|
| `trial-last-session` | Foundation layer | Removed by use case 02 on conversion · otherwise left as historical record |
| `trial-follow-up-active` | This workflow on entry | This workflow on any exit |
| `trial-converted` | This workflow on conversion | Never removed |
| `trial-not-converted` | This workflow on sequence end | Never removed |
| `opted-out` | This workflow on STOP | Manual by studio staff only |

### Custom fields read by this use case

- `last_session_end_time` — used to calculate message 1 send time
- `last_class_name` — used in AI prompts
- `active_package_name` — used in AI prompts
- `active_package_sessions_used` — used in detection gate
- `active_package_sessions_total` — used in detection gate

### Late-night guard

```
IF calculated_send_time.hour >= 21
  THEN reschedule to next_day at 09:00
```

Apply this guard to message 1 only. Messages 2–6 are day-level delays, not time-specific.

---

## Open Questions / To Confirm

- [ ] Does Eversports export include session end time in the booking reports, or does it need to be calculated from start time + class duration?
- [ ] What is the Eversports product/checkout URL format for sending direct links?
- [ ] Approve final WhatsApp Business template texts for `trial_followup_msg1/3/5` per locale (EN, DE-AT, DE-DE)
- [ ] If `consent_marketing_whatsapp = false` but `consent_marketing_email = true`, skip msg 1 (WhatsApp) and start at msg 2 (Email)? *(Recommended: yes)*
