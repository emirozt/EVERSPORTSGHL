# Claude Code Subagents — Eversports × GHL Connector

This directory contains five project-scoped subagent definitions for Claude Code. After unzipping the spec package into your repo, **this directory should live at `.claude/agents/`** (with the leading dot). The Cowork environment that produced these files won't write into a dotted directory, so the staging name is `claude_agents/` — rename it on extraction:

```bash
cd /path/to/your/new/repo
unzip eversports-ghl-connector-v1-spec.zip
mv claude_agents .claude/agents     # only needed if the zip didn't already place them
```

Once in `.claude/agents/`, Claude Code (CLI or IDE extension) will pick them up automatically on startup.

## The five agents

| Agent | When to invoke | Tools | Notes |
|---|---|---|---|
| `consent-gate-auditor` | Before merging any code that touches outbound messaging or consent fields | Read-only | Produces a structured PASS/VIOLATIONS/RISKS report. Cannot edit code. |
| `eversports-scraper-specialist` | M1.5 (CSV bootstrap), M2 (read scraper), M5 (writeback executor), M5b (freshness audit) | Full | Encodes Eversports admin panel quirks, CSV parsing rules from sample fixtures, Playwright resilience, writeback_mode switch. |
| `ghl-workflow-architect` | M3 (GHL read sync) and M8 (use case workflows). Anything API v2 / workflow JSON related. | Full | Encodes API v2 conventions, X-GHL-Signature, the 60s tag race-condition guard, workflow JSON format. |
| `spec-consistency-checker` | After any code change to the data model, tags, pipeline stages, sub-account settings, or AI prompts | Read + Edit (spec docs only) | Audits drift in both directions. Auto-edits spec docs when code is the source of truth. Reports code-side issues for the implementer to fix. |
| `uc-prompt-designer` | When working on prompts under `app/ai/prompts/` — design or iteration | Full | Owns tone guardrails, JSON output format for UC04, WhatsApp template variable-fill discipline, multilingual variants (EN, DE-AT, DE-DE). |

## How to invoke them in Claude Code

Claude Code does NOT auto-route to subagents — you invoke them explicitly. Several ways:

**1. By natural-language reference in a prompt:**

```
Use the consent-gate-auditor to review the diff in app/ghl/workflows/uc01.py
since the last commit.
```

```
Have the eversports-scraper-specialist implement the bookings CSV parser
against the sample fixture, with full test coverage.
```

**2. Via the `/agents` slash command:**

Type `/agents` in Claude Code to browse, edit, or invoke a subagent interactively.

**3. By name in the Task tool:**

If you're using the Claude Agent SDK directly, pass the agent name to the `subagent_type` parameter when spawning a Task.

## Recommended invocation patterns by milestone

Each milestone in `DEV_SPEC.md` § 8 ends with a **"Recommended agent invocations"** block that lists exactly which agents to call and when. That's the authoritative source — read it before starting a milestone. The starter prompt in `HANDOFF.md` already instructs Claude Code to consult those blocks.

This README documents *what each agent does and how to invoke them*. The per-milestone "use X then Y" sequencing lives in DEV_SPEC.md so it stays beside the acceptance criteria it relates to.

## Editing an agent

Each agent's behaviour lives in this file (`<agent-name>.md`):
- YAML frontmatter at the top (`name`, `description`, optionally `tools`, `model`)
- Markdown body below the `---` is the system prompt

To change an agent's behaviour, edit the markdown body. Claude Code re-reads the file on each invocation.

Common changes you might make as the project evolves:
- Add new Eversports CSV quirks observed in production to `eversports-scraper-specialist`
- Tighten the consent checklist in `consent-gate-auditor` after a near-miss
- Add new tone examples (good/bad) to `uc-prompt-designer` as the brand voice clarifies
- Add new code/spec sync rules to `spec-consistency-checker` when new domains appear

## Why these five and not more

We considered but rejected:

- **Generic code-reviewer** — Claude Code's built-in review handles this fine
- **Security-reviewer** — use the built-in `/security-review` skill instead
- **Test-fixture-builder** — the sample CSVs we have are sufficient; building synthetic data is best left inline
- **Architect / planner** — `DEV_SPEC.md` is the plan; an agent on top would be a layer of indirection

If you find yourself re-explaining the same context to Claude Code three times across sessions, that's a sign you might want a sixth agent. Until then, five is the right number.

## Two follow-ups when the project grows

When you start onboarding the second studio, consider adding:
- `onboarding-runbook-agent` — codifies the per-location onboarding flow (CSV bootstrap, consent invitation, freshness audit kickoff)
- `incident-responder` — encodes the playbooks for the most common production failures (scrape login failure, GHL rate limit, AI provider outage, writeback dead-letter)

Both are premature for v1 but become valuable around studio location #3.
