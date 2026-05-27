"""
Scraper-layer exception types.

These are raised by EversportsBaseScraper and consumed by sync_runner / API layer.
"""


class SessionExpiredError(RuntimeError):
    """
    Raised when an Eversports admin session cookie has expired mid-run.

    Distinct from ``SessionNotConfiguredError`` — this means the location *was*
    previously onboarded (cookies imported) but the session has since expired.
    The operator must re-export cookies via Cookie-Editor and run
    ``scripts/import_cookies.py`` to refresh ``locations.eversports_cookie_cache``.

    Message invariant: always contains the human-readable action the operator must take,
    so it can be surfaced directly in sync_log and alert emails without reformatting.
    """


class SessionNotConfiguredError(RuntimeError):
    """
    Raised when a location has never been onboarded (cookie_state == 'unset').

    This is *not* an error — it is the expected initial state for a newly created
    location row.  The scheduled sweep should catch this and skip the location
    silently; ``sync_runner.run_sync`` handles it before the browser is launched
    (returns ``{"skipped": True}`` rather than raising).

    ``EversportsBaseScraper.__aenter__`` raises this defensively if called directly
    with an un-onboarded location, so callers get a clear message rather than the
    misleading "session expired" wording.
    """
