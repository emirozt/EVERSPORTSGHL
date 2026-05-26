# Studio Owner UI — Design v1.14

> **v1.14 — 2026-05-25.** Pre-development consistency pass + new **Insights / Opportunities** surface.
>
> **New Insights page (top-level, second nav item after Dashboard).** Five insight categories: membership upsell · at-risk members · trial drop-off · capacity rebalancing · cohort LTV. Each with concrete contact lists, impact estimates, and per-row actions. Where an automation already handles the recommendation, the action button shows current state instead of duplicating the trigger. **Dashboard teaser card** above the KPI strip surfaces the headline ("5 new revenue opportunities · +€940/mo potential") with a click-through.
>
> **Consistency cleanup:** UC codes replaced with friendly names everywhere user-facing · Settings sub-section renamed to "Automation rules" · Email From + signature relocated to Studio profile · Onboarding Step 5 (CSV import) made optional with auto-backfill primary path · Conversations reply composer hidden on AI-handled threads with "Take over" affordance.

# Studio Owner UI — Design v1.13

> **v1.13 — 2026-05-25.** Two updates.
>
> **Communication templates page built out.** Five tabs: WhatsApp templates (with Meta approval status pills · Approved · Pending Meta · Rejected · Draft), Email templates, AI prompts (the system-wide tone guardrails + UC04 chatbot system + gatekeeper classifier + UC05 intent sub-classifier), Gatekeeper auto-replies (emoji_reaction_react + social_compliment_thanks), Owner notifications (conversion email, handoff alert, cookie expiry, writeback failure). Each card shows the template name in monospace, status pill, used-by context, locale availability (DE-AT / DE-DE / EN with on/off pills), text preview with {{variable}} placeholders highlighted, and a Variables-used row.
>
> **Template editor drawer.** Wide 760px right-slide drawer with: header showing template name + sub + locale toggle (3 buttons), Template body textarea with clickable variable-insert buttons (insert at cursor position), Live preview card with sample values + char count + segment count, Meta-approval warning info card (for WhatsApp templates), Save & submit footer button.
>
> **Dev bar now hides on signed-up screens.** The floating black navigation only shows during the signup/wizard flow (screens 1–6: signup + wizard 1–5). On dashboard/contacts/automations/conversations/settings, it's hidden — the topbar nav already handles those. **Shift+D** toggles it on/off if you need to jump to a signup screen for review.

# Studio Owner UI — Design v1.12

> **v1.12 — 2026-05-25.** Built out **Settings → Billing**.
>
> - **Current bill hero card** showing the live billing period, current plan with yellow "Standard" tier chip, status pill ("Within budget · 44%"), and a large total to the right (€238.80 = €199 net + €39.80 VAT). Inline line-item table breaks down subscription + AI overage + subtotal + VAT + grand total with proper accounting hierarchy (subtotal dashed-rule, grand total solid-rule).
> - **Two-column row:** left = AI usage breakdown by category (cross-referenced with the dashboard panel — Conversations / Follow-ups / Booking / Data capture each with a colored dot, token count, and metered cost equivalent). Right = Payment method (Visa card preview with brand-style gradient) + Billing address (Austrian VAT ID surfaced).
> - **Tier comparison** in 3 cards (Light / Standard / Heavy). Current tier (Standard) highlighted with yellow border, glow shadow, and "Current" badge. Each tier shows price per location, "for whom" subtitle, feature checkmarks, and a CTA button labelled per direction (Downgrade / Current plan / Upgrade). Below the grid: overage rates and multi-location math.
> - **Invoice history table** with six rows showing the realistic timeline (May pending, then 5 paid months back to Dec 2025 with a pro-rated first invoice). Per-row PDF download + a "Preview" link for the current pending invoice. Status pills (Paid green, Pending yellow). Footer offers "Download all as ZIP" and "Email invoices to accounting".

# Studio Owner UI — Design v1.11

> **v1.11 — 2026-05-25.** New top-level **Automations** page (replaces the previously-unused "Pipelines" nav).
>
> - **Status strip:** 4 status cards (one per use case) showing current operational state. UC04 has an animated pulsing red dot indicating "needs you" (handoffs waiting). UC02 shows paused-state styling. Click any card jumps to Settings → Automations for config.
> - **Per-automation cards in a 2×2 grid:** each shows category chip, status indicator, 3-metric strip (month-to-date numbers), an "In flight right now" list of 3–4 specific customers with their current state, and a footer with a quick fact + Configure link.
> - **Pipeline state panel:** all three pipelines side-by-side (Lead-to-Sale wider since it has more stages). Each stage shows the contact count + which automation acts on it. Stages labeled with `→ UC04`, `→ UC01`, `→ UC04 outbound`, etc. so you can see at a glance which workflow each segment of the customer journey triggers.
> - **Live event stream:** 12 recent automation events with timestamps, color-coded icons per UC, full descriptions including token cost. Includes UC01/UC04/UC05 events plus gatekeeper classifications (showing how social-channel noise gets filtered).
> - Pipelines (the concept) is preserved as a sub-section of Automations rather than its own top-level page. "Pipelines" terminology continues to exist in the spec; the owner-facing nav is just simpler now.

# Studio Owner UI — Design v1.10

> **v1.10 — 2026-05-25.** New **Conversations inbox** with gatekeeper — the omnichannel surface.
>
> - **Gatekeeper layer.** Every inbound message from GHL (WhatsApp DMs, Email, Instagram DMs, Instagram comments, Facebook DMs, Facebook comments) is classified by Claude Haiku before reaching an automation or the owner's inbox. Categories: Inquiry, Booking, Trial reply, Complaint, Opt-out, Acknowledgment, Noise. Actionable ones route to UC04/UC05/owner; noise gets auto-acknowledged or silently ignored.
> - **Top stats strip:** Inbound today (187), Routed to use cases (42 · 22%), Filtered as noise (143 · 76%), Escalated to you (2). Plus a "How the gatekeeper works" pill that opens the same drawer pattern with a 5-step walkthrough.
> - **3-column inbox layout.** Left: smart folders (Needs you / AI handling / All actionable / Filtered out / Closed) + channels (WhatsApp / Email / Instagram / Facebook with counts) + by-category (Inquiry / Booking / Trial reply / Complaint / Opt-out). Middle: 10 realistic conversations with classification chips. Right: full conversation thread with gatekeeper assessment card at top + AI/customer/system bubbles + composer with channel-aware hints.
> - **Sample conversations** show the gatekeeper's range: Seda's escalated pricing question (owner-escalated, currently waiting), Lisa's auto-executed reschedule (routed to UC05), Laura's beginner inquiry (routed to UC04), Frieda's class-cancellation complaint (escalated to owner), Valeriia's STOPP opt-out (routed to consent gate), Maral's Instagram DM about parking (Inquiry → UC04), Nadine's Facebook membership-change question (Inquiry → UC04).
> - **Gatekeeper assessment card** in the detail view shows the classification, the routing decision, the reasoning, the confidence score, and a "Reclassify…" override button. Full transparency.

# Studio Owner UI — Design v1.9

> **v1.9 — 2026-05-25.** Contacts page feedback round 2.
>
> - **First-row visual bug fixed.** Removed `position:sticky` from the contacts table header (it was the source of the yellow stripe leak from the active filter pill above). Cleaner row borders too — `border-top` on cells instead of `border-bottom`, with first row's top border removed explicitly.
> - **"Active automations" section added to the contact detail.** Top card in the Overview tab lists every automation the contact is currently in (UC01 sequence, UC04 inbound conversation, UC04 outbound queued, etc.) with the automation name, a use-case chip, the current state ("Message 2 of 6 scheduled Sun 10:00"), and a red "Remove from sequence" button per automation. Click triggers a confirmation dialog explaining what removal does (stops the automation, preserves history, allows manual re-add).
> - **CRM-added attribution added.** New "Added to CRM" field in the Identifiers card showing the timestamp and source (`studio-import`, `onboarding-form`, `whatsapp-opt-in`, etc.). Also surfaced as a dedicated event at the bottom of the Activity timeline so the customer's journey starts where they actually entered the system.
> - **Per-contact body content.** The drawer body now swaps per contact, not just the header. Three contacts have full fidelity content (Ricarda · converted member, Josmi · mid-trial-follow-up, Seda · chatbot handoff waiting), plus Laura Reichert (new lead from GHL form, showing `onboarding-form` source). Other rows fall back to a placeholder noting the prototype scope.

# Studio Owner UI — Design v1.8

> **v1.8 — 2026-05-25.** New **Contacts** surface — the daily-use operational view.
>
> - **Master list:** searchable + filterable table with 15 realistic contacts drawn from the sample CSV (Ricarda Burger, Josmi Jose, Seda Döbele, Lisa Baisch, etc.) plus invented but spec-aligned cases (Sarah Klein with active trial, Emma Wagner lapsed, Laura Reichert new lead).
> - **Filter row:** dropdowns for pipeline, package type, last activity, AI activity + a top-level search. Quick pills below for All / Trial / Card / Members / At risk / Lapsed / Opted out with counts.
> - **Columns:** Contact (avatar + name + email/phone), Active package (with sessions remaining), Pipeline stage (color-coded chip), Tags (compact pills), Last activity (relative time + what), Consent (per-channel icons that show on/off/opted-out states).
> - **Row click opens a 720px-wide contact detail drawer** sliding in from the right. Selected row highlights yellow.
> - **Detail tabs:** Overview (current state + lifetime stats + identifiers), Activity timeline (event log with bookings, purchases, AI conversations, handoffs — color-coded), Consent & audit (per-channel toggles + 5-event audit log preview with source attribution).
> - Realistic individual stories — Ricarda's detail shows the trial → chatbot conversation → conversion → first member booking arc with the actual product names from the sample data and the spec's UC02 dedupe logic noted in the timeline.

# Studio Owner UI — Design v1.7

> **v1.7 — 2026-05-25.** Applied Automations feedback round 2.
>
> - **UC04 Card → Membership ready trigger** now reads: *"Reaches out when a card customer booked more than [N] sessions per week during last month."* N is an inline editable input (default 2). Frequency-based — captures heavy users who'd benefit from membership economics.
> - **UC04 Renewal handling is now an either-or radio choice** between *STUDIO sends renewal outreach* (with Active chip) and *Defer to Eversports' renewal reminder*. Dependent options live in one block instead of two independent toggles.
> - **UC01 "Stop on positive reply" renamed** to *"Hand off to chatbot when customer replies"* with a clearer hint explaining what happens.
> - **UC01 Channel mix** now drives conditional fields below it. WhatsApp templates row only shows when WhatsApp is in the mix; new Email "From" name + signature fields show only when Email is in the mix. Switch the dropdown to see them appear/disappear.
> - **UC05 third intent added** — *"Schedule a new booking"*. Customer can ask the assistant to book a brand-new session, not only modify existing ones.
> - **Writeback mode moved into UC05 Intent section** as a radio choice (Admin task / Auto-execute) with "How it works" pills. It now lives where it applies. The Eversports connection page shows an info card pointing to the new location.
> - Field values continue to be pre-populated everywhere (carrying forward from v1.6).

# Studio Owner UI — Design v1.6

> **v1.6 — 2026-05-25.** New **Settings → Automations** page — a use-case manager where the owner controls all four AI workflows from one place.
>
> - Each use case is a card showing: category chip (Conversations / Follow-ups / Booking / Detection), name, 1-line description, master toggle (Active / Paused), 4-card stats strip with this-month metrics, a summary configbar with key settings, and an expandable Configure panel grouped by Trigger / Behavior / Output sections.
> - **UC04 Chatbot** has the deepest config (inbound/outbound modes, channel mix, re-trigger cap, persona, hard-auth timeout, handoff rules, owner notification).
> - **UC01 Follow-up** lets you tune sequence length, message timings, channel mix, late-night guard, and exit rules. Links to Communication templates for the WhatsApp templates.
> - **UC05 Reschedule/Cancel** lets you toggle reschedule vs cancel intents independently, set the safety margin and lead time, and cross-links to Locations settings for the late-cancel window and to Eversports settings for the writeback mode.
> - **UC02 Trial → Member tag** is shown in the **paused** state to demonstrate the disabled visual treatment — toggle on to see the active state.
> - Cards collapse by default; expanding one auto-collapses the others. Toggling a card off dims it visually and updates the status label to "Paused".

# Studio Owner UI — Design v1.5

> **v1.5 — 2026-05-25 (later, yet again).** Built out **Settings → AI budget & usage** as the first full settings sub-page. Five sections in order:
>
> - **Date range selector** in the header (same chips as Dashboard)
> - **Budget card** with current usage, stacked bar (same colour map as Dashboard), and an inline budget adjuster
> - **Alert thresholds card** with three rows (50% / 80% / 100%) and toggles. 100% is locked-on by design.
> - **Daily tokens by category chart** — full-width stacked bar chart over the month with an on-budget pace dashed line. Past days at full opacity, today partial, future days projected at 18% opacity.
> - **Category breakdown table** — tokens, % of used, activity counts, avg tokens per activity, vs prior month delta
> - **Recent AI calls audit log** — last 24h calls with category filters, search, per-call cost. "Show all 187 calls →" expands to full log.
>
> Also fixed: the duplicate Writeback mode block in Eversports connection page has been removed.

# Studio Owner UI — Design v1.4

> **v1.4 — 2026-05-25 (later again, again).** Eversports connection page updates from Feedback round 2:
>
> - **Sync schedule is now editable** — each cadence has a toggle + parameters. Event-driven sync offset (default 15 min) is a number input, hourly catch-up has frequency dropdown + start/end time pickers, overnight reconciliation has a time picker. Save / Reset buttons in the card footer.
> - **Writeback mode recommendation flipped.** Admin task (manual) is now marked "Recommended to start" — rationale: let the AI learn how the studio designed their booking system before automating it. Auto-execute remains available but is positioned as the upgrade path. Updated copy explains the progression.
> - **"How it works" drawer.** Each writeback option has a blue "ⓘ How it works" pill. Clicking opens a right-side slide-in drawer with a numbered 6–7 step walkthrough showing the customer journey end-to-end, with colour-coded actor chips (Customer / STUDIO / You). Different content per mode. Closes on backdrop click or ESC.

# Studio Owner UI — Design v1.3

> **v1.3 — 2026-05-25 (later, again).** Added **Settings shell + Eversports connection page** as screen 8.
>
> - Settings sidebar with 8 sub-sections (Studio profile, Locations, Eversports connection, Communication templates, AI budget & usage, Team, Compliance & DPA, Billing). Active section highlighted; "expires 22h" badge on Eversports sidebar item.
> - **Eversports connection (the main design):** big status hero card with cookie expiry timeline + colour-graded progress bar; sync schedule readout; writeback mode radio (Auto-execute vs Admin task with full descriptions); danger zone for disconnect / delete.
> - **Inline cookie refresh editor** expands directly inside the hero card when "Refresh authenticator cookie" is clicked. Same paste/upload tab pattern as onboarding step 3 — minimum friction for a recurring task.
> - "Refresh cookie" link in the dashboard's Needs-Your-Attention panel jumps directly into the open refresh editor.
> - **Studio profile** sub-page is functional (re-uses onboarding step 1 fields with edit-in-place pattern + Save/Cancel footer); **Locations** lists current location and previews multi-location upsell; other 5 sub-sections are friendly placeholders with what's coming.

# Studio Owner UI — Design v1.2

> **v1.2 — 2026-05-25 (later again).** Added **AI activity & budget panel** to the dashboard: stacked horizontal bar showing per-category token consumption (Conversations, Follow-ups, Booking assistant, Data capture) + tokens remaining + month-end projection. Sits between the pipeline funnel and the conversations chart.
>
> **v1.1 — 2026-05-25 (later).** Applied feedback from `Feedback 1.pptx`:
> - Step 1: removed studio tagline; single "Deutsch" language option
> - Step 2: split into "Location details" + new "Business settings" group; AI budget now in tokens (not euros)
> - Step 3: removed studio ID; location ID now mandatory; replaced email+password with mandatory **Authenticator Cookie** (paste text OR upload file)
> - Added Step 4: Data Processing Agreement (with full DPA template + studio-attestation clause)
> - Added Step 5: Import history (CSV bootstrap upload — bookings, activities, products)
> - Strict validation: every step's Continue button stays disabled until all required fields are filled
> - Dashboard rebuilt: new KPI set (Active members, Active Card customers, Avg revenue/client, Avg time Trial → Sale), date-range chips, Lead-to-Sale pipeline funnel, monthly conversations chart (AI vs handed-off), refreshed "Needs attention" panel, locations list, recent activity feed
>
> The 7-screen flow is now: Sign up → Studio brand → Location & business → Eversports → DPA → Import → Dashboard.

# Studio Owner UI — Design v1 (historical)

Clickable HTML prototype of the studio owner experience. Use it to validate flow, copy, and visual direction before any code gets written.

## Files

- `tokens.css` — design tokens copied from the AI Social Media Generator brand. Single source of truth for colors, typography, spacing, radii, shadows.
- `studio-owner-ui.html` — single-file clickable prototype containing all v1 owner-facing screens. Open in any browser.

## How to use

Open `studio-owner-ui.html` in your browser (double-click, or right-click → Open With → your browser of choice).

You'll see a **floating dev bar at the bottom** that lets you jump between the 5 screens. Form submits also advance through the flow. The dev bar is for review only — remove the `<nav class="devbar">` block when this code is handed to the implementer.

## Screens in this prototype

| # | Screen | Purpose |
|---|---|---|
| 1 | **Sign up** | First impression. Studio owner creates an account. Email + password + studio name. Google option. |
| 2 | **Onboarding step 1 — Studio brand** | Studio name, owner first name, tagline, default language (DE-AT / DE-DE / EN). |
| 3 | **Onboarding step 2 — First location** | Address, country, timezone, operating hours, late-cancel window, AI monthly budget. |
| 4 | **Onboarding step 3 — Connect Eversports** | Eversports admin credentials + studio ID. Shows the test-connection success state. |
| 5 | **Dashboard** | Post-onboarding home. Trial conversions, active members, renewals due, AI budget, recent activity, "needs your attention" panel, locations list. |

Three more wizard screens are still TBD (DPA acceptance, historical-data import, "all set" confirmation) — design them in the next iteration when the flow above is approved.

## Branding cues applied

- **Yellow `#f6c026`** as primary CTA + active state, exactly as in `tokens.css`
- **Multi-color logo cycle** (blue · red · yellow · blue · green · red) — used the 6-letter placeholder "STUDIO". Find-and-replace `STUDIO` and the `<span>` letters in `studio-owner-ui.html` to your final brand name.
- **Soft warm neutrals**, generous radii (8–20px), subtle shadows
- **Segoe UI / system-ui** body font
- **Friendly section tags** (uppercase, wide letter-spacing) for context above headlines
- **No gradient text, no glassmorphism, no side-stripe borders, no hero-metric template** — adhering to the design-system bans

## What's intentionally NOT here

- A login screen — implied by the "Already have an account? Sign in" link; design in next pass
- DPA acceptance screen (wizard step 4) — needs the actual DPA text from legal review first
- CSV bootstrap upload screen (wizard step 5) — depends on the `POST /api/v1/admin/locations/{id}/bootstrap` endpoint spec
- All settings sub-pages (locations, team, billing, AI usage, communication templates, consent management) — designed in v2
- Customer-facing preference centre — separate prototype (the consent management URL the customer hits)
- Mobile responsive layout — desktop-first for v1; tablet/mobile in a later pass

## Feedback loop

After review, common iteration paths:

- **Tone shift**: copy is currently confident-warm. Can dial to playful, formal, or pragmatic.
- **Visual register**: this is mid-density boutique-SaaS. Can compress to denser admin (think Linear), or open up to airier marketing-style (think Notion onboarding).
- **Flow changes**: wizard step order can change (e.g. DPA before Eversports), steps can split/merge.
- **Branding swap**: rename STUDIO → your brand name; swap the 6-letter logo cycle to match.

Tell me what to change and I'll iterate.
