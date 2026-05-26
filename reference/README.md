# Reference implementations

## eversports_scraping_poc/

Proof-of-concept Eversports admin login + CSV capture. Validated end-to-end
on a real test studio account before the production build started.

When implementing milestone M2 (read scraper), use this as a reference for:
- The Playwright login flow and cookie handling that work in practice
- Which CSV endpoints respond reliably and what their actual response shapes are
- Any quirks of the Eversports admin UI we've already discovered

Do NOT copy this code verbatim into app/scrapers/. It was written for
exploration, not production — it lacks idempotency, retry, error handling,
multi-tenant scoping by location_id, and the secrets-manager integration
the spec requires. Extract the patterns that work, then implement them
properly per DEV_SPEC.

Delete this folder once M2 is complete and validated.
