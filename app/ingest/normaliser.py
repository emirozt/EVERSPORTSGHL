"""
Phone normaliser and product classifier helpers.

Phone normalisation uses the `phonenumbers` library (libphonenumber Python binding).
E.164 is the canonical output format — e.g. '+491234567890'.

Product classification is keyword-based, matching against known German/English terms
seen in real Eversports exports. Per-location overrides are handled upstream via
locations.product_keyword_map (not implemented here — classifiers operate on name only).

Observed product names in sample exports (requirements_v2/sample_exports/bookings.csv):
  - '10er Karte-Gruppe'                               → card
  - '20er Karte-Gruppe'                               → card
  - '5er Karte-Gruppe'                                → card
  - '3 Trial Cards-Introduction to Pilates Reformer'  → trial (trial + card: trial wins)
  - 'Gruppenmitgliedschaft-1 x Woche'                 → membership
  - 'Gruppenmitgliedschaft-2 x Woche'                 → membership
  - 'Gruppe-Equipment Mitgliedschaft-1'               → membership
  - 'Limitless-Gruppenmitgliedschaft'                 → membership
"""

import logging
import re

import phonenumbers
from phonenumbers import NumberParseException, PhoneNumberFormat

logger = logging.getLogger(__name__)

# ── Phone normalisation ────────────────────────────────────────────────────────

# Minimum significant-digit length for a valid phone number (after E.164 formatting).
_MIN_PHONE_DIGITS = 7

# Whitespace / formatting chars that are valid in raw phone input
_PHONE_CLEAN_RE = re.compile(r"[\s\-\(\)\.\/]")


def normalise_phone(raw: str, default_region: str = "DE") -> tuple[str | None, str]:
    """
    Parse and normalise a phone number to E.164.

    Args:
        raw: Raw phone string as extracted from the CSV (e.g. '015258067348',
             '+491759472221', '17645699133', '01783011577', '+49 152 01581624',
             '15 229 852 941 ').
        default_region: ISO-3166-1 alpha-2 region code used when the number has no
                        country prefix. Derived from location.timezone heuristic:
                        Europe/Berlin → 'DE', Europe/Vienna → 'AT',
                        Europe/Zurich → 'CH'. Default: 'DE'.

    Returns:
        (e164_or_None, raw_original)

        e164_or_None is None when:
          - raw is blank / None
          - the number fails phonenumbers parsing
          - the parsed number is invalid
          - the significant digit count after E.164 formatting is < _MIN_PHONE_DIGITS
    """
    raw_str = (raw or "").strip()
    if not raw_str:
        return None, raw_str

    try:
        parsed = phonenumbers.parse(raw_str, default_region)
    except NumberParseException as exc:
        logger.debug("Phone parse failed (%s): %r", exc.error_type.name, raw_str)
        return None, raw_str

    if not phonenumbers.is_valid_number(parsed):
        logger.debug("Phone invalid after parse: %r → %r", raw_str, parsed)
        return None, raw_str

    e164 = phonenumbers.format_number(parsed, PhoneNumberFormat.E164)

    # Sanity-check: count digits only
    digit_count = sum(c.isdigit() for c in e164)
    if digit_count < _MIN_PHONE_DIGITS:
        logger.debug("Phone too short (%d digits): %r", digit_count, e164)
        return None, raw_str

    return e164, raw_str


def timezone_to_region(iana_timezone: str) -> str:
    """
    Derive an ISO-3166 region code from an IANA timezone string.
    Used to set the default_region for phone normalisation when the location
    doesn't have an explicit country field.

    Handles the three expected studio countries:
      Europe/Berlin, Europe/Hamburg, etc. → 'DE'
      Europe/Vienna                        → 'AT'
      Europe/Zurich                        → 'CH'

    Falls back to 'DE' for anything unrecognised.
    """
    tz = iana_timezone.lower()
    if "vienna" in tz or "wien" in tz:
        return "AT"
    if "zurich" in tz or "zürich" in tz:
        return "CH"
    # Default: Germany (most common customer geography)
    return "DE"
