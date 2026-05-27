"""
Scraper-layer exception types.

These are raised by EversportsBaseScraper and consumed by sync_runner / API layer.
"""


class SessionExpiredError(RuntimeError):
    """
    Raised when Eversports admin session cookie has expired or was never imported.

    The operator must re-export cookies from their browser (Cookie-Editor) and run
    ``scripts/import_cookies.py`` to update ``locations.eversports_cookie_cache``.

    Message invariant: always contains the human-readable action the operator must take,
    so it can be surfaced directly in sync_log and alert emails without reformatting.
    """
