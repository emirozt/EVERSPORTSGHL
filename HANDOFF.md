# HANDOFF — Eversports × GoHighLevel Connector

This package contains the complete v1 specification for an AI-powered CRM connector built on Eversports (booking) and GoHighLevel (CRM), targeted at Pilates and fitness studios. The product is ready to be built. This document is the front door.

---

## TL;DR — getting Claude Code started

1. Unzip this package into a new empty directory (will become your repo).
2. Open Claude Code in that directory: `claude` in the terminal, or open the folder in your IDE with the Claude Code extension.
3. Paste this starter prompt into Claude Code:

```text
You are implementing v1 of the Eversports × GoHighLevel CRM connector.

Read in this order:
  1. HANDOFF.md (this file — high-level orientation)
  2. requirements_v2/00_master_overview.md (product surface area)
  3. DEV_SPEC.md (build contract: repo layout, schema, milestones, acceptance)
  4. requirements_v2/07_foundation_layer.md (the data + writeback platform)
  5. Each use case doc under requirements_v2/ (01, 02, 05, 06) — in numerical order
  6. requirements_v2/08_consent_model.md (consent + DPA)
  7. requirements_v2/CHANGELOG.md (decisions and their rationale)
  8. REVIEW_FINDINGS.md (background — the gap analysis that produced v2)

Then start at milestone M1 in DEV_SPEC.md (repo scaffold). Before
starting any milestone:
  1. Read that milestone's "Recommended agent invocations" block in
     DEV_SPEC.md § 8 — it tells you which subagents to use during the
     milestone and which must sign off before merge.
  2. List the assumptions you're making and ask me for clarification on
     any open item in § 11 of DEV_SPEC.md or in the "Open Questions"
     section of the relevant requirement doc that affects that milestone.

Treat the following as load-bearing — do not skip or shortcut them:
  - The consent gate (every outbound send routes through it)
  - Multilingual STOP detection (DACH market — STOP, STOPP, AUFHÖREN, ABMELDEN, UNSUBSCRIBE)
  - Writeback idempotency (sha256 keys + safe retries)
  - The per-location writeback_mode switch (auto_execute vs admin_task)
  - The ≥ 2 spots safety margin + 60-min slot-minimum lead time + writeback re-validation that protect UC05 against stale availability data
  - AI usage logging on every AI call (used for billing)
  - The "Recommended agent invocations" blocks per milestone — agents are
    not auto-routed; invoke them explicitly when the block says to.

Commit after each milestone with the milestone tag in the message.
Pause for review at end of each milestone.
```

---

## What this product is

A SaaS CRM and AI-driven outreach layer that sits on top of Eversports (the booking system that pilates studios use) and GoHighLevel (the CRM where contact state and conversations live). It bidirectionally syncs data between the two systems and runs four AI-powered use cases on the resulting unified profile:

- **UC01 — Trial conversion follow-up.** When a trial customer attends their last trial session, run a 6-message WhatsApp + email sequence to convert them to a paid package.
- **UC02 — Trial → member tag.** When any historical trial customer purchases a non-trial product (chatbot, in-studio, direct), apply conversion tags and notify the owner (deduped against UC04).
- **UC04 — Sales consultant chatbot.** Inbound + outbound AI conversations on WhatsApp and Email, with the customer's full profile injected. Handles purchase, upsell, renewal, and handoff to humans for complex questions. Can write bookings back into Eversports.
- **UC05 — Reschedule / cancel assistant.** Customer asks to reschedule or cancel; AI confirms slot and policy reason; foundation auto-executes the change in Eversports via browser automation. Per-location fallback to admin-task model.

UC03 (no-show recovery) was scoped in v1 of the spec, then removed in v2 because the Eversports no-show export doesn't expose the data needed to reliably distinguish true no-shows from late cancellations. Eversports' own native no-show comms remain in effect for v1.

---

## Architecture in one paragraph

A Python/FastAPI foundation service does five things: (1) reads from Eversports via Playwright scraping of the admin panel CSV exports — this is the sole Eversports ingress path (the Eversports Provider API is intentionally NOT used); (2) stores normalised state in Postgres with Google Sheets as a read-only ops mirror; (3) syncs delta changes into GoHighLevel via REST API v2, with a tag engine and a pipeline engine; (4) executes writeback actions (create customer, create booking, reschedule, cancel) back to Eversports via Playwright on demand from GHL workflows, with idempotent retry; (5) gates every outbound message through a consent check and logs AI usage per call for billing. GHL workflows hold the use case logic. Each studio location runs as an independent GHL sub-account with its own scraper instance, consent state, and AI budget.

---

## Directory layout

```
.
├── HANDOFF.md                              ← you are here
├── DEV_SPEC.md                             ← build contract for Claude Code
├── REVIEW_FINDINGS.md                      ← BA gap analysis (background)
├── .claude/
│   └── agents/                             ← project-scoped Claude Code subagents
│       ├── README.md                       ← when to use each agent + invocation guide
│       ├── consent-gate-auditor.md
│       ├── eversports-scraper-specialist.md
│       ├── ghl-workflow-architect.md
│       ├── spec-consistency-checker.md
│       └── uc-prompt-designer.md
└── requirements_v2/                        ← the source-of-truth requirements
    ├── 00_master_overview.md               ← product surface, data model, tag glossary, principles
    ├── 01_trial_conversion_followup.md     ← UC01
    ├── 02_trial_member_tag.md              ← UC02
    ├── 03_ghl_pipelines.md                 ← Lead / Card / Membership pipelines
    ├── 05_sales_consultant_chatbot.md      ← UC04
    ├── 06_reschedule_assistant.md          ← UC05
    ├── 07_foundation_layer.md              ← the data + writeback platform (the bulk of the build)
    ├── 08_consent_model.md                 ← consent capture + DPA + studio-attestation clause
    ├── CHANGELOG.md                        ← every decision in this spec and why
    └── sample_exports/                     ← real Eversports CSV exports — use as test fixtures
        ├── all activities.csv
        ├── bookings.csv
        └── noshows.csv                     ← empty in this sample (test studio has no no-shows)
```

## Subagents shipped with this spec

Five project-scoped Claude Code subagents live under `.claude/agents/`. Each encodes domain knowledge or a quality gate that recurs across the build. Claude Code does not auto-invoke them — reference them explicitly by name. See `.claude/agents/README.md` for usage, but the short version:

| Agent | What it's for |
|---|---|
| `consent-gate-auditor` | Audits outbound-message code paths against the consent model. Run before any merge touching messaging. Read-only. |
| `eversports-scraper-specialist` | Encodes Eversports admin panel quirks + Playwright resilience + CSV parsing rules. Use for M1.5, M2, M5, M5b. |
| `ghl-workflow-architect` | Encodes API v2 conventions, webhook signing, the 60s tag race-condition guard, workflow JSON format. Use for M3 and M8. |
| `spec-consistency-checker` | Audits drift between code and spec docs. Can edit spec docs to match code; reports code-side issues for the implementer. |
| `uc-prompt-designer` | Tone guardrails, JSON output for UC04, WhatsApp template variable-fill, multilingual variants. Use during M8 prompt iteration. |

Example invocations:

```
Use the eversports-scraper-specialist to implement the bookings CSV parser
against the sample fixture with full test coverage.

After that, have the spec-consistency-checker verify the column map in
07_foundation_layer.md matches the parser exactly.

Before merging M8a, run the consent-gate-auditor on the UC01 send paths.
```

**File 04 is intentionally absent** — UC03 (no-show recovery) was removed in v2. The numbering gap is the signal.

---

## Reading order (for human reviewers)

If you (a human) want to understand the product before reading code, take 30 minutes:

1. `requirements_v2/00_master_overview.md` — what the product is, what data it manages, what the tags and pipelines mean.
2. `requirements_v2/07_foundation_layer.md` (skim) — how data moves between Eversports, Postgres, GHL.
3. `requirements_v2/05_sales_consultant_chatbot.md` — the most complex use case; understanding it makes the others easy.
4. `requirements_v2/CHANGELOG.md` — the rationale for every non-obvious choice.

For Claude Code, the starter prompt above already specifies the order.

---

## Build order

10 milestones in `DEV_SPEC.md` § 8. Quick map:

| # | What | Estimate |
|---|---|---|
| M1 | Repo skeleton + Postgres + Sentry + health endpoint | 1 week |
| M1.5 | CSV bootstrap uploader (the one-time onboarding path) | 1 week |
| M2 | Read scraper (Playwright — sole Eversports ingress) | 2 weeks |
| M3 | Delta engine + GHL read sync | 2 weeks |
| M4 | Event-driven scheduler | 1 week |
| M5 | Writeback executor (with `writeback_mode` switch + admin-task fallback) | 2 weeks |
| M6 | Consent layer + multilingual opt-out | 1 week |
| M7 | AI client + usage logger | 1 week |
| M8 | Use case workflows (UC01, UC02, UC04, UC05) | 3 weeks |
| M9 | Observability + alerting | 1 week |
| M10 | Hardening + first studio onboarding | 2 weeks |

Single engineer: ~15 weeks. With parallelism across foundation + use cases: 8–10 weeks. Each milestone has explicit acceptance criteria — see DEV_SPEC.md.

---

## Open dependencies that gate parts of the build

Two remaining blockers, both at the end of the path:

- **M8a (UC01) is blocked** on WhatsApp Business template text approvals per locale (DE-AT, DE-DE, EN). Template strings need legal + studio approval before Meta can approve them. Get this started early — Meta approval can take a week.
- **M10 (production launch) is blocked** on DPA template legal review (including the studio-attestation clause in `08_consent_model.md`) and confirming Anthropic + GHL sub-processor terms are EU/DACH-compliant.

Two non-blockers that have already been resolved by design:

- Eversports admin browser automation legality → studio-attestation clause in the DPA + `writeback_mode = admin_task` fallback per location.
- UC05 availability → derived from the admin activities scrape (`available_spots = max_participants − registered`); protected by ≥ 2 spots safety margin + 60-min slot-minimum lead time + writeback re-validation. Provider API not used.

Everything else listed as "Open Questions" inside individual docs is a refinement, not a blocker.

---

## How Claude Code should ask questions back

Several requirement docs end with an "Open Questions / To Confirm" section. Some are answered, some aren't. Claude Code should:

- Before starting any milestone, scan that milestone's relevant docs for open items
- Group them and ask in one message rather than drip-feeding
- Default to the "Recommended" option where one is marked
- Pause for human review at the end of each milestone before proceeding

Claude Code should treat the spec as authoritative but is allowed to push back if it spots a logical conflict between docs. The CHANGELOG explains the reasoning behind non-obvious decisions — read it before deciding to deviate.

---

## How to interpret the spec when reality drifts

This is a real-world integration with two third-party platforms whose UIs and APIs will change. When Claude Code (or any engineer) hits a divergence between the spec and Eversports / GHL's actual behaviour:

- The spec describes intent. The platforms describe constraint. The platforms win.
- Open a discussion before silently working around it. Many drifts have implications across multiple use cases.
- Update the CHANGELOG with what you observed and what you changed. The CHANGELOG is the running record of why the build looks the way it does.

---

## Stack reminder

- Python 3.12 / FastAPI / SQLAlchemy / Alembic / Playwright (Chromium)
- Postgres 16 / PgBoss queue (Redis only if throughput requires)
- Anthropic Claude (`claude-sonnet-4-6` default, `claude-haiku-4-5` for classification)
- GoHighLevel REST API v2 (OAuth + `X-GHL-Signature`)
- Hosting: Fly.io or Railway recommended (EU region for DACH data residency)
- CI: GitHub Actions

---

## What success looks like

End of v1:

- One real Pilates studio location is live on the system in production
- It has been running for 7+ consecutive days with no P1 incidents
- UC01, UC02, UC04, UC05 are exercised on real customers
- Consent flows have been audited; opt-outs propagate within 30 seconds
- Monthly AI usage report can be generated for billing
- A second studio can be onboarded by the studio onboarding script in under 30 minutes

That's the bar. Go.
