# Consent & GDPR Model (v2 — new doc)

> **Status:** This is a new document added in v2. It describes the GHL-native consent capture, gating, and revocation model for marketing communications. Designed for DACH compliance (UWG §7, DSGVO Art. 6 + 7) and extensible to EU more broadly.

## Goals

1. Capture explicit, channel-specific marketing consent from every contact before any use case sends an outbound message.
2. Store consent state + provenance (source, timestamp) on the GHL contact so any workflow can gate on it consistently.
3. Honour universal multilingual opt-out instantly (STOP, STOPP, AUFHÖREN, ABMELDEN, UNSUBSCRIBE, KEINE WERBUNG).
4. Provide each contact a self-service preference centre URL.
5. Maintain a tamper-evident `consent_audit` log in Postgres for regulator-facing accountability.

---

## Per-channel consent fields on the GHL contact

| Field | Type | Notes |
|---|---|---|
| `consent_marketing_email` | Boolean | False until opt-in captured |
| `consent_marketing_email_source` | Text | One of: `onboarding-form`, `double-opt-in`, `studio-import`, `preference-centre`, `whatsapp-opt-in` |
| `consent_marketing_email_at` | DateTime | When set |
| `consent_marketing_whatsapp` | Boolean | False until opt-in |
| `consent_marketing_whatsapp_source` | Text | Same enum |
| `consent_marketing_whatsapp_at` | DateTime | When set |
| `consent_marketing_voice` | Boolean | Reserved for v2 (Voice AI) |
| `consent_marketing_voice_source` | Text | Reserved |
| `consent_marketing_voice_at` | DateTime | Reserved |
| `consent_revoked_email_at` | DateTime | Set on opt-out — set together with flipping the boolean false |
| `consent_revoked_whatsapp_at` | DateTime | Same |
| `consent_locale` | Text | e.g. `de-AT`, `de-DE`, `en` — drives language of consent confirmations |

The fields above are managed by a small set of workflows; no use case writes them directly except through these workflows.

---

## Capture sources

### A. Studio onboarding form (legacy contacts at studio go-live)

When a studio location goes live with our product, the foundation runs a one-time sweep:

```
For each Eversports customer imported with no existing consent record:
  - Create GHL contact (already happens)
  - Enqueue a "legacy consent invitation" workflow
  - This sends ONE compliant transactional email + ONE WhatsApp template (if customer's number is known) asking them to opt in via a hosted preference centre URL
  - No marketing copy in this message — it's a regulatory transactional notification
  - source = "studio-import"
```

Non-responders remain `consent = false` and receive only transactional comms (Eversports' native booking confirms, payment receipts, etc., which remain enabled).

### B. Double opt-in via GHL form (new prospects from web)

Studios who use GHL forms (lead magnet, contact form, trial-booking widget):

```
Form submission → contact created
→ confirmation email sent with verification link
→ click → consent_marketing_email = true, source = "double-opt-in"
```

Required wording (per UWG §7 / DSGVO):
- Plain language consent text (no pre-ticked boxes)
- Separate opt-in for each channel
- Reference to the privacy policy

### C. WhatsApp first contact opt-in

If a prospect first contacts the studio via WhatsApp (the WhatsApp Business number), the first AI response includes:

```
"Hi! Before we chat — would you like to receive class updates and offers from
[studio_name] via WhatsApp? Reply YES to opt in (you can stop anytime by
replying STOP)."
```

YES → `consent_marketing_whatsapp = true`, source = `whatsapp-opt-in`.
No reply → soft conversation continues (transactional Q&A allowed under WhatsApp policy), no marketing sent.

### D. Preference centre

Every contact gets a unique preference-centre URL (signed token, 90-day expiry, refreshable). They can view and change their consents at any time. The link appears in every outbound marketing email.

---

## Consent Gate (shared workflow)

Every outbound use case message routes through this gate. Implemented as a GHL workflow sub-action.

```
def consent_gate(contact, channel):
  if contact.tags contains "opted-out":
    log_audit("blocked: opted-out tag", contact, channel)
    return DENY

  consent_field = f"consent_marketing_{channel}"
  if not contact.custom_fields[consent_field]:
    log_audit("blocked: no consent", contact, channel)
    return DENY

  return ALLOW
```

DENY behaviour per use case:

| Use case | If consent missing |
|---|---|
| UC01 trial follow-up | Fall back to other channel if consented; otherwise exit |
| UC03 no-show recovery | Exit (this is email-only; no fallback) |
| UC04 chatbot outbound | Exit (don't initiate) — outbound triggers only when channel is consented |
| UC04 chatbot inbound | Inbound messages are customer-initiated — implied consent for that conversation. Gate ONLY outbound sends within the conversation. |
| UC05 reschedule confirmation | This is transactional, not marketing — gate is bypassed |

---

## Opt-out detection (universal, multilingual)

A shared GHL workflow listens for inbound messages from any contact on any channel matching the configured `stop_keywords` regex. Default:

```
^(stop|stopp|aufhören|aufhoeren|abmelden|keine werbung|unsubscribe|opt out|opt-out)$/i
```

On match:

```
flip consent_marketing_<channel> = false
stamp consent_revoked_<channel>_at = now()
apply tag "opted-out"
remove from all active automation sequences
send confirmation in customer's locale:
  EN: "You've been unsubscribed. Have a great day, [first_name]."
  DE: "Sie wurden abgemeldet. Einen schönen Tag noch, [first_name]."
write row to consent_audit
```

The `opted-out` tag is global (applies to all channels for that contact). To reverse, the customer can use the preference centre URL or contact the studio directly.

### Email-specific opt-out

Every outbound marketing email includes a one-click unsubscribe link (RFC 8058 + visible footer link). Click → same flow as above.

---

## `consent_audit` table

Append-only, write-protected (no UPDATE / DELETE in normal operation). Used for regulator-facing accountability.

| Column | Notes |
|---|---|
| `id` | uuid |
| `contact_id` | GHL contact ID |
| `location_id` | FK |
| `channel` | email / whatsapp / voice |
| `event` | granted / revoked / blocked-send / preference-centre-update |
| `value` | new boolean value (where applicable) |
| `source` | onboarding-form / double-opt-in / studio-import / preference-centre / whatsapp-opt-in / stop-keyword / unsubscribe-link |
| `ts` | datetime |
| `actor` | system / customer / studio-staff |
| `message_shown` | The exact consent copy presented (snapshot, for audit) |
| `ip` | Inbound IP if HTTP-sourced (null otherwise) |

Retention: 6 years (DSGVO Art. 17 carve-out for legal claims & accountability).

---

## Special cases

### Existing newsletter opt-ins in Eversports

Eversports already has its own newsletter opt-in for customers who booked via the Eversports platform. **We do NOT inherit this consent automatically** — Eversports' consent covers Eversports' newsletter, not the studio's GHL communications. Studios must re-capture consent through one of the four sources above. The studio-import flow above (Source A) is the cleanest path for legacy contacts.

### Studio's own existing list (e.g. Mailchimp)

If a studio has an existing newsletter list with documented opt-ins, the studio can attest (in writing during onboarding) that consents are valid, and we import with `source = "studio-import"`. This is the studio's legal responsibility — they signed a DPA with us that makes them the controller and us the processor.

### Phone-number-only contacts (no email)

For contacts with no email, the preference centre URL can be delivered via WhatsApp template (`preference_centre_link` template).

### Minors

Marketing consent for under-16s requires guardian consent (DSGVO Art. 8). Studios must capture date of birth on intake; under-16s default to `consent_marketing_* = false` regardless of any other flow until guardian consent recorded. Implementation deferred to v2 unless a partner studio explicitly serves minors — flag during onboarding.

---

## Data Processing Agreement (template)

Each studio (controller) signs a DPA with us (processor) covering:

- Categories of data processed (contacts, bookings, payments-metadata, communications content)
- Sub-processors (GHL, Anthropic, AWS/Postgres host, scraper hosting, Google Sheets if used as mirror)
- Security measures (encryption at rest + transit, access controls, audit logging)
- Sub-processor change notice (30 days)
- Audit rights
- Breach notification (within 24h to controller)
- Data subject rights handling (access, rectification, erasure, portability)
- Data return / deletion at end of engagement

Template document maintained separately; legal review at v1 launch.

### Studio-attestation clause (Eversports admin automation)

Because our foundation performs scraping reads and writeback actions against the Eversports admin panel on the studio's behalf, the DPA contains an explicit **studio-attestation** clause:

> The Studio (Controller) represents and warrants that:
> (a) it has lawful authority to provide the Processor with Eversports admin login credentials and authorise the Processor to act as its delegate within the Eversports admin interface;
> (b) such authorisation is not prohibited by the Studio's own contractual arrangements with Eversports (the Studio acknowledges responsibility for confirming this with Eversports);
> (c) the Studio shall promptly inform the Processor in writing if Eversports objects to or restricts the Studio's grant of delegated access;
> (d) the Studio indemnifies the Processor against any third-party claim arising from the Studio's grant of delegated access exceeding the Studio's authority under (a).

The legal effect is to place the contractual relationship between the **Studio and Eversports**, not between us and Eversports. We act as the studio's delegate (a standard SaaS pattern). If Eversports later restricts the studio's grant of delegated access, the studio is contractually obligated to inform us and we fall back to admin-task mode for that location (see UC05 § "Admin-task fallback mode" and `locations.writeback_mode` setting).

This clause must be present and accepted as part of the onboarding flow — no location is provisioned for auto-execute writeback without it.

---

## Open Items

- [ ] Final consent capture copy per language (DE-AT, DE-DE, EN, FR if needed) — drafted by legal counsel
- [ ] Preference centre URL hosting: GHL-hosted page vs. our own (recommended: GHL-hosted using custom values + funnels)
- [ ] DPA template — engage legal counsel for DACH-grade DPA
- [ ] Confirm Anthropic + GHL sub-processor terms align with EU/DACH requirements (Anthropic offers a DPA; GHL has EU data residency options to verify)
- [ ] Determine whether transactional booking confirmations sent by Eversports satisfy the "transactional channel" the customer would expect, so we don't accidentally duplicate transactional messages from GHL
