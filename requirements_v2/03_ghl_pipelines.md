# GHL Pipelines Spec (v2)

> **Revision note (v2):** Clarified "New lead" definition; pipelines are per-location (one set per GHL sub-account); added dedupe rules; corrected card-converted-then-churned behaviour.

## Overview

Three GHL opportunity pipelines run in parallel. A contact can exist in the lead-to-sale pipeline AND one product pipeline simultaneously. Pipelines are moved automatically by GHL automations triggered by foundation sync data and use case workflows.

---

## Pipeline 1 — Lead to Sale

**Purpose:** Tracks every contact from first detection through to their first product conversion. Every contact created by the foundation enters this pipeline. It never closes — Lost is the only terminal stage for non-converters.

**Entry point:** Contact created in GHL by foundation (any sync)

### Stages

| Stage | Description | Trigger |
|---|---|---|
| New lead | Contact created in GHL, has not yet purchased any product (e.g. captured via GHL form / WhatsApp first contact, or imported from Eversports as a customer with no products) | Foundation creates contact AND `products_purchased` is empty |
| Trial sold | Trial package detected on contact | Foundation detects product name matching trial keywords |
| Trial booked | Customer has made at least one booking using their trial | Foundation detects first booking with trial package |
| Converted (card) | Customer purchased a card package | `trial-converted` tag applied + `converted_package_name` is a card (not membership) |
| Converted (membership) | Customer purchased a membership | `trial-converted` tag applied + `converted_package_name` is a membership |
| Lost | Trial ended, no conversion after full follow-up sequence | `trial-not-converted` tag applied by use case 01 |

### Stage transition logic (pseudocode)

```
ON contact created by foundation:
  → move to "New lead"

ON foundation sync detects trial/probe product on contact:
  → move to "Trial sold"

ON foundation sync detects first booking with trial package:
  → move to "Trial booked"

ON tag "trial-converted" applied:
  IF converted_package_name does NOT contain "trial", "probe", "membership":
    → move to "Converted (card)"
  ELSE IF converted_package_name contains "membership":
    → move to "Converted (membership)"

ON tag "trial-not-converted" applied:
  → move to "Lost"
```

### Notes

- Converted (card) and Converted (membership) are both terminal stages in this pipeline. The contact remains here and simultaneously enters the relevant product pipeline.
- A contact who buys a membership directly (skipping card) enters Converted (membership) and the membership pipeline.

---

## Pipeline 2 — Card Package

**Purpose:** Tracks card package customers through attendance health and upsell to membership. Focused on reducing churn and maximising the chance of upgrading to membership.

**Entry point:** Card package detected by foundation sync, OR `trial-converted` tag applied where `converted_package_name` is a card (not membership, not trial).

### Stages

| Stage | Description | Trigger |
|---|---|---|
| Standard card | Active card package, healthy attendance | Card package detected, sessions being used |
| Low attendance warning | Card sessions remain but customer has gone quiet | No booking in last 14 days while `sessions_remaining > 0` |
| Membership ready | High-frequency card customer — prime upsell moment | `sessions_per_week_last_month > card_upsell_min_sessions_per_week` (per-location threshold, default 2) |
| Converted | Customer purchased a membership | Membership product detected in foundation sync |
| Churned | Card expired, no renewal or membership purchase | `active_package_expiry_date` passed + no new package detected |

### Stage transition logic (pseudocode)

```
ON card package detected in foundation sync (non-trial, non-membership):
  → enter pipeline at "Standard card"

ON daily sync check:
  IF sessions_remaining > 0 AND last_booking_date < today - 14 days:
    → move to "Low attendance warning"
    → trigger: use case (no-show / re-engagement automation)

  IF sessions_per_week_last_month > location.card_upsell_min_sessions_per_week:
    → move to "Membership ready"
    → trigger: sales consultant chatbot upsell sequence (UC04)
    (frequency-based as of v3 — was previously sessions_remaining < 3)

  IF new membership product detected:
    → move to "Converted"
    → exit card pipeline
    → enter membership pipeline at "Active"

  IF active_package_expiry_date < today AND no new package:
    → move to "Churned"
    → trigger: win-back automation (future use case)
```

### Notes

- A contact can move from Low attendance warning to Membership ready only if their frequency picks back up (Low attendance warning fires on 14-day inactivity; Membership ready fires on high recent frequency — they're mutually exclusive in practice unless a sudden burst follows a quiet stretch).
- Membership ready is the key trigger point for the sales consultant chatbot (use case 04) to initiate an upsell outreach.
- Converted is a positive terminal stage. Churned is a negative terminal stage.

---

## Pipeline 3 — Membership

**Purpose:** Tracks membership customers through their lifecycle — identifying at-risk members early, managing renewals, and reducing churn.

**Entry point:** Membership package detected by foundation sync (product name does not contain "trial" or "probe" and is categorised as membership by Eversports).

### Stages

| Stage | Description | Trigger |
|---|---|---|
| Active | Membership is live, attendance healthy | Membership package detected |
| At risk | Attendance dropping — churn signal | No attendance in 14 days OR frequency dropped 50%+ vs prior month |
| Renewal due | Membership approaching expiry | `active_package_expiry_date` within 14 days |
| Renewed | Customer renewed their membership | New membership product detected after previous one nears expiry |
| Churned | Membership expired, no renewal | `active_package_expiry_date` passed + no new membership detected within 7 days |

### Stage transition logic (pseudocode)

```
ON membership package detected in foundation sync:
  → enter pipeline at "Active"

ON daily sync check:
  // At risk detection — whichever comes first
  IF last_session_date < today - 14 days:
    → move to "At risk"
    → trigger: re-engagement automation

  IF attendance_this_month < (attendance_last_month * 0.5):
    → move to "At risk"
    → trigger: re-engagement automation

  // Renewal detection
  IF active_package_expiry_date <= today + 14 days:
    → move to "Renewal due"
    → trigger: renewal reminder automation

  // Renewed detection
  IF new membership product detected AND active_package_expiry_date approaching:
    → move to "Renewed"
    → reset to "Active" on new membership start date

  // Churn detection
  IF active_package_expiry_date < today AND no new membership within 7 days:
    → move to "Churned"
    → trigger: win-back automation (future use case)
```

### Notes

- At risk can be triggered from either Active or Renewal due stage — a member can be both at risk and near expiry simultaneously.
- Renewed resets the member back to Active once the new membership starts. The Renewed stage is transitional (tracks the window between purchase and new start date).
- The 50% attendance frequency drop is calculated by the foundation sync comparing `sessions_attended_this_month` vs `sessions_attended_last_month`. Both fields must be maintained as custom fields on the GHL contact.

---

## GHL Custom Fields Added for Pipeline Logic

| Field | Type | Used by |
|---|---|---|
| `sessions_attended_this_month` | Number | Membership pipeline — at risk detection |
| `sessions_attended_last_month` | Number | Membership pipeline — at risk detection |
| `last_booking_date` | Date | Card pipeline — low attendance detection |
| `pipeline_lead_stage` | Text | Tracking current stage in lead pipeline |
| `pipeline_card_stage` | Text | Tracking current stage in card pipeline |
| `pipeline_membership_stage` | Text | Tracking current stage in membership pipeline |

---

## GHL Tags Added for Pipeline Logic

| Tag | Applied when | Used by |
|---|---|---|
| `card-active` | Card package detected in foundation | Card pipeline entry |
| `membership-active` | Membership package detected in foundation | Membership pipeline entry |
| `low-attendance` | No booking in 14 days (card) | Card pipeline — low attendance stage |
| `membership-ready` | `sessions_per_week_last_month > location.card_upsell_min_sessions_per_week` (default threshold: 2/week) — high-frequency card customer | Card pipeline — membership ready stage trigger (UC04 upsell) |
| `at-risk` | Attendance drop detected (membership) | Membership pipeline — at risk stage |
| `renewal-due` | Expiry within 14 days | Membership pipeline — renewal due stage |
| `renewed` | New membership purchased | Membership pipeline — renewed stage |
| `churned` | Package expired, no renewal | Both product pipelines — churn stage |

---

## Pipeline × Use Case Matrix

This table shows which pipeline stages and tags are the trigger points for each use case automation:

| Trigger | Use case |
|---|---|
| Tag `trial-last-session` applied (foundation) | Use case 01 — trial conversion follow-up |
| Tag `trial-purchase-detected` applied (foundation) | Use case 02 — trial member tag |
| Card pipeline stage = Membership ready | Use case 04 — sales consultant chatbot (card upsell) |
| Membership pipeline stage = Renewal due | Use case 04 — sales consultant chatbot (renewal) |
| Any inbound message (WhatsApp/Email/Social) | Use case 04 — sales consultant chatbot (inbound) |
| Any inbound message with reschedule intent | Use case 05 — booking reschedule assistant |
| Card pipeline stage = Low attendance warning | Future: re-engagement automation |
| Card pipeline stage = Churned | Future: win-back automation |
| Membership pipeline stage = At risk | Future: re-engagement automation |
| Membership pipeline stage = Churned | Future: win-back automation |

---

## Open Questions / To Confirm

- [ ] Confirm product naming conventions per location (per-location `product_keyword_map` override available in foundation config)
- [ ] 50% attendance drop — exclude months where `sessions_attended_last_month == 0` to avoid false positives on summer breaks (Recommended: yes, treat 0 → 0 as "stable")
- [ ] A contact who moves Card → Churned remains in Lead pipeline at "Converted (card)" (that's a permanent achievement) — Lost is only for trial customers who never converted
- [ ] Renewed member's new membership: pipeline transitions to "Active" on the new start date. Treat the Renewed stage as transitional, not a separate opportunity row.
