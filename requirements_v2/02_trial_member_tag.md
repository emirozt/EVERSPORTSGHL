# Use Case 02 — Trial → Member Tag

> **Revision note (v2):** Widened detection (no longer requires "exactly 1 prior product"); added dedupe with UC04 chatbot conversions; consolidated to `converted_package_name` field.

## Overview

**Goal:** Detect when a customer with **any historical trial product** purchases a new non-trial, non-voucher, non-merch package. Apply the `trial-converted` tag immediately, clean up active sequences, and notify the studio owner — unless UC04 already notified them about a chatbot-driven sale.

**Trigger source:** Foundation layer — daily sync (active products & membership packages)
**Channels:** GHL internal notification + email to studio owner
**GHL role:** Tag manager, sequence controller, notification sender
**AI role:** None — this is pure logic, no AI message generation

---

## Detection Logic

### What counts as a trial product

A product is considered a trial if its name contains any of the following (case-insensitive):

```
"trial" OR "probe"
```

Examples that match: "3-session Trial", "Probe Kurs", "Trial Pack", "Probestunde"

### What triggers the conversion

During each sync, the foundation updates `products_purchased`. This use case fires when:

```
IF contact had at least one trial product historically (any time, any quantity of other products is fine)
  AND a NEW product appears in today's sync that is:
       - not a trial (name does not match trial-keywords)
       - not a voucher / gift card
       - not merchandise / single drop-in
       - not previously in products_purchased
  AND contact does NOT already have tag `trial-converted`
THEN → conversion detected
```

This catches cases the old "exactly 1 prior product" rule missed (e.g. customer had Trial + Gift Voucher, then bought a card).

### Important edge cases

```
IF contact already has tag "trial-converted"       → skip, already processed
IF new product name matches trial keywords         → skip, not a real conversion
IF contact has no prior trial product on record    → skip, not a trial customer
IF contact has tag "chatbot-converted" set within  → skip OWNER NOTIFICATION ONLY
   the last 24 hours (UC04 already notified)         (still apply tags + update field)
```

---

## Workflow Diagram Description

```
[Foundation daily sync runs]
        │
        ▼
[Check each updated contact's products_purchased]
        │
        ▼
[Gate: exactly 1 prior product AND name contains "trial"/"probe"?]
   No  ──► Skip contact
   Yes ──► Continue
        │
        ▼
[Gate: new non-trial product detected in today's sync?]
   No  ──► Skip contact
   Yes ──► Continue
        │
        ▼
[Gate: contact has tag "trial-follow-up-active"?]
   Yes ──► Remove from automation + remove tag "trial-follow-up-active"
   No  ──► Continue
        │
        ▼
[Apply tag: "trial-converted"]
[Remove tag: "trial-active" (if present)]
[Remove tag: "trial-last-session" (if present)]
        │
        ▼
[Update GHL custom field: converted_package_name = new product name]
        │
        ▼
[Send GHL internal notification to studio owner]
[Send email to studio owner]
        │
        ▼
[Done]
```

---

## Phase 1 — Detection (runs as part of foundation daily sync)

### GHL trigger

**Trigger:** Contact updated (by foundation sync) where `products_purchased` field changes.

The foundation script compares the previous value of `products_purchased` against the new value after each sync. If a new non-trial product is added and the contact had only a trial product before, it applies the GHL tag `trial-purchase-detected` to the contact.

This tag fires the GHL automation for use case 02.

```
Foundation script logic (pseudocode):

FOR each contact updated in today's sync:
  prior_products = contact.products_purchased (before sync)
  new_products   = contact.products_purchased (after sync)

  prior_trial_only = (
    len(prior_products) == 1 AND
    ("trial" in prior_products[0].name.lower() OR
     "probe" in prior_products[0].name.lower())
  )

  new_non_trial = [
    p for p in new_products
    if "trial" not in p.name.lower()
    and "probe" not in p.name.lower()
    and p not in prior_products
  ]

  IF prior_trial_only AND len(new_non_trial) > 0:
    contact.converted_package_name = new_non_trial[0].name
    apply GHL tag: "trial-purchase-detected"
```

---

## Phase 2 — GHL Automation

**GHL trigger:** Contact tag added = `trial-purchase-detected`

### Step 1 — Check and clean up active sequences

```
IF contact.tags contains "trial-follow-up-active":
  Remove contact from automation: "Trial Conversion Follow-Up Sequence"
  Remove tag: "trial-follow-up-active"
```

### Step 2 — Apply conversion tags

```
Apply tag:  "trial-converted"
Remove tag: "trial-active"        (if present)
Remove tag: "trial-last-session"  (if present)
Remove tag: "trial-purchase-detected"  (cleanup trigger tag)
```

### Step 3 — Update custom field

```
Set custom field: converted_package_name = contact.converted_package_name
Set custom field: conversion_date = today
```

### Step 4 — Dedupe check (NEW in v2)

```
IF contact.tags contains "chatbot-converted"
   AND chatbot_converted_at within last 24 hours:
  → SKIP owner notification (UC04 already sent one)
  → set conversion_source = "chatbot"
  → end workflow
ELSE:
  → set conversion_source = "direct"
  → continue to owner notification
```

### Step 5 — Notify studio owner

#### GHL internal notification

```
Type: Task or Alert
Assigned to: studio owner (GHL user)
Title: "New trial conversion — [first_name] [last_name]"
Body:
  "[first_name] [last_name] has converted from their trial.
   New package purchased: [converted_package_name]
   Date: [conversion_date]"
```

#### Email to studio owner

```
To: [studio_owner_email]  (GHL sub-account setting)
Subject: "Trial converted — [first_name] [last_name]"
Body:
  Hi [owner_name],

  Great news! [first_name] [last_name] has just purchased a new package
  after completing their trial.

  Package purchased: [converted_package_name]
  Conversion date: [conversion_date]

  You can view their contact here: [GHL contact link]

  — Your automation system
```

---

## Exit Conditions

This use case has a single linear flow with no branches beyond the detection gates. Once all steps complete, the workflow ends.

| Exit | Condition | GHL actions |
|---|---|---|
| Conversion processed | New non-trial product detected on trial-only contact | Tags updated, sequences stopped, owner notified |
| Skipped — not trial only | Contact had more than 1 prior product | No action |
| Skipped — new product is also trial | New product name contains "trial" or "probe" | No action |
| Skipped — no prior trial | Contact had no trial product on record | No action |

---

## GHL Implementation Notes

### Tags used by this use case

| Tag | Applied by | Removed by |
|---|---|---|
| `trial-purchase-detected` | Foundation sync script | This workflow (cleanup after trigger) |
| `trial-converted` | This workflow | Never removed |
| `trial-active` | Foundation layer | This workflow on conversion |
| `trial-last-session` | Foundation layer | This workflow on conversion |
| `trial-follow-up-active` | Use case 01 | This workflow if sequence was active |

### Custom fields written by this use case

| Field | Value set |
|---|---|
| `converted_package_name` | Name of the newly purchased non-trial product |
| `conversion_date` | Date the conversion was detected |

### Custom fields read by this use case

| Field | Used for |
|---|---|
| `products_purchased` | Core detection logic |
| `active_package_name` | Cross-reference during detection |
| `first_name`, `last_name` | Owner notification content |

### Relationship to use case 01

This use case acts as a safety net for use case 01. If a customer buys a new package through any channel (direct on Eversports website, in-studio, etc.) without going through the follow-up chatbot, this use case catches it via the foundation sync and ensures:

- The follow-up sequence is stopped cleanly
- The correct conversion tags are applied
- The owner is notified regardless of how the conversion happened

---

## Open Questions / To Confirm

- [ ] If a customer buys two new packages on the same day (e.g. card + merch), record both in `products_purchased_summary`; `converted_package_name` takes the highest-tier non-voucher, non-merch product
- [ ] `studio_owner_email` is a GHL sub-account-level setting (confirmed in master overview)
