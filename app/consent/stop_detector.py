"""
Multilingual STOP keyword detector (M6).

Matches inbound messages from any channel against the configured opt-out
keyword list.  The default pattern covers DACH opt-out vocabulary and the
universal English variants.  Per-location overrides can be stored in
`locations.stop_keywords` as a regex string.

Matching rules:
  - Full-message match only (anchored ^ … $) — avoids false positives on
    "I want to stop worrying" etc.
  - Case-insensitive.
  - Leading/trailing whitespace stripped before comparison.
  - Normalises ü → ue etc. so "aufhoeren" matches "aufhören".

References:
  - requirements_v2/08_consent_model.md § "Opt-out detection (universal, multilingual)"
  - requirements_v2/00_master_overview.md (stop_keywords setting)
"""

from __future__ import annotations

import re
import unicodedata

# ── Default pattern ───────────────────────────────────────────────────────────

# Covers: STOP, STOPP, AUFHÖREN / AUFHOEREN, ABMELDEN, KEINE WERBUNG,
#         UNSUBSCRIBE, OPT OUT, OPT-OUT.
# The pattern is anchored (^ … $) — must match the ENTIRE message.
DEFAULT_STOP_REGEX = re.compile(
    r"^(stop|stopp|aufh(?:ö|oe)ren|abmelden|keine\s+werbung"
    r"|unsubscribe|opt[\s\-]out)$",
    re.IGNORECASE,
)


def _normalise(text: str) -> str:
    """
    Strip leading/trailing whitespace before matching.

    Umlaut normalisation (ö → oe etc.) is handled separately by `_ascii_fold`,
    which is called on the stripped result.  We match against BOTH the original
    and the folded form so that "aufhören" and "aufhoeren" are both recognised.
    """
    return text.strip()


def _ascii_fold(text: str) -> str:
    """Replace German umlauts with two-letter equivalents before ASCII fold."""
    replacements = {
        "ä": "ae", "ö": "oe", "ü": "ue",
        "Ä": "AE", "Ö": "OE", "Ü": "UE",
        "ß": "ss",
    }
    for char, rep in replacements.items():
        text = text.replace(char, rep)
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def is_stop_keyword(text: str, *, custom_pattern: str | None = None) -> bool:
    """
    Return True if `text` is a recognised opt-out keyword.

    The custom_pattern is ADDITIVE — it extends the default keyword set rather
    than replacing it.  Per DSGVO Art. 7(3) the right to withdraw consent must
    be as easy as granting it; narrowing the recognised stop-words via a custom
    override would violate this requirement.

    Args:
        text:            The full inbound message body.
        custom_pattern:  Optional per-location regex string (from
                         `locations.stop_keywords`).  If provided, the message
                         is checked against BOTH this pattern AND the default
                         pattern — a match on either returns True.  An invalid
                         regex is silently ignored (default pattern still runs).

    Returns:
        True if the message is a stop keyword, False otherwise.

    Examples:
        >>> is_stop_keyword("STOP")
        True
        >>> is_stop_keyword("stopp")
        True
        >>> is_stop_keyword("aufhören")
        True
        >>> is_stop_keyword("aufhoeren")
        True
        >>> is_stop_keyword("I want to stop going to the gym")
        False
        >>> is_stop_keyword("ABMELDEN")
        True
        >>> is_stop_keyword("STOP", custom_pattern=r"^nein$")  # STOP still matches
        True
    """
    normalised = _normalise(text)
    if not normalised:
        return False

    folded = _ascii_fold(normalised)

    def _matches(pattern: re.Pattern[str]) -> bool:
        # Try original (handles ö, ü natively) then ASCII-folded form
        return bool(pattern.match(normalised)) or bool(pattern.match(folded))

    # Always check the default pattern first
    if _matches(DEFAULT_STOP_REGEX):
        return True

    # If a custom (additive) pattern is also configured, check it too
    if custom_pattern:
        try:
            extra = re.compile(custom_pattern, re.IGNORECASE)
            if _matches(extra):
                return True
        except re.error:
            pass  # invalid regex — default already ran above

    return False


# ── Opt-out confirmation messages ─────────────────────────────────────────────

# Keyed by locale (two-letter or full IETF tag).  Used by the consent handler
# to send a confirmation in the customer's configured language.

OPT_OUT_CONFIRMATIONS: dict[str, str] = {
    "en":    "You've been unsubscribed. Have a great day, {first_name}.",
    "de":    "Sie wurden abgemeldet. Einen schönen Tag noch, {first_name}.",
    "de-AT": "Sie wurden abgemeldet. Einen schönen Tag noch, {first_name}.",
    "de-DE": "Sie wurden abgemeldet. Einen schönen Tag noch, {first_name}.",
    "de-CH": "Sie wurden abgemeldet. Einen schönen Tag noch, {first_name}.",
}

DEFAULT_OPT_OUT_LOCALE = "de-AT"


def get_opt_out_confirmation(first_name: str, locale: str) -> str:
    """
    Return the localised opt-out confirmation message for a customer.

    Falls back through: exact locale → two-letter prefix → DEFAULT_OPT_OUT_LOCALE.
    """
    template = (
        OPT_OUT_CONFIRMATIONS.get(locale)
        or OPT_OUT_CONFIRMATIONS.get(locale[:2])
        or OPT_OUT_CONFIRMATIONS[DEFAULT_OPT_OUT_LOCALE]
    )
    return template.format(first_name=first_name or "")
