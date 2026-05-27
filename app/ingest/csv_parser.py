"""
CSV parsers for Eversports admin exports.

Quirks documented from sample_exports/:
  - All three files use UTF-8 with BOM (\\xef\\xbb\\xbf) — strip before parsing.
  - Delimiter is semicolon in all known exports, but auto-detected for safety.
  - bookings.csv: quoted fields, English headers, dates as DD/MM/YYYY HH:MM.
  - all activities.csv: unquoted, German headers, Datum=DD.MM.YYYY + Startzeit/Endzeit=HH:MM.
  - noshows.csv: may be 0 bytes (no no-shows in window) — treat as empty result.
  - Customer number column in bookings is consistently blank — not used as match key.
  - Price format: "252.00 €" — strip currency symbol and whitespace.
  - Attended column: "yes"/"no" → "attended"/"no_show".
  - available_spots = max(0, Max. Teilnehmer − Angemeldet) — clamped to 0 if negative.
"""

import csv
import io
import logging
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

_PRICE_STRIP_RE = re.compile(r"[^\d.,]")
# Matches "252.00 €", "100,00 €" — keep digits and decimal separators only.

_BOOL_TRUE = frozenset(("yes", "ja", "true", "1", "wahr"))
_BOOL_FALSE = frozenset(("no", "nein", "false", "0", "falsch"))


# ── Internal helpers ──────────────────────────────────────────────────────────


def _decode_bom(file_bytes: bytes) -> str:
    """Decode bytes to str, stripping UTF-8 BOM if present."""
    return file_bytes.decode("utf-8-sig")


def _detect_delimiter(sample: str) -> str:
    """
    Sniff the delimiter from the first line of the CSV.
    Falls back to ';' if sniffing fails — all Eversports exports use semicolons.
    """
    try:
        dialect = csv.Sniffer().sniff(sample[:2048], delimiters=",;\t|")
        return dialect.delimiter
    except csv.Error:
        logger.debug("Delimiter detection failed — defaulting to ';'")
        return ";"


def _safe_int(val: str, field: str) -> int | None:
    """Parse an integer, returning None (and logging) on failure."""
    stripped = val.strip()
    if not stripped:
        return None
    try:
        return int(stripped)
    except ValueError:
        logger.warning("Could not parse int for field %r: %r", field, val)
        return None


def _parse_price(val: str) -> Decimal | None:
    """Strip currency symbols and whitespace, return Decimal or None."""
    if not val.strip():
        return None
    # Remove everything except digits, dot, comma
    cleaned = _PRICE_STRIP_RE.sub("", val)
    # Normalise German comma-decimal to dot-decimal
    cleaned = cleaned.replace(",", ".")
    if not cleaned:
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        logger.warning("Could not parse price: %r → %r", val, cleaned)
        return None


def _parse_bool_field(val: str) -> bool | None:
    """Map yes/no/ja/nein to bool; return None for blank."""
    v = val.strip().lower()
    if v in _BOOL_TRUE:
        return True
    if v in _BOOL_FALSE:
        return False
    if not v:
        return None
    logger.debug("Unrecognised boolean value: %r", val)
    return None


def _make_tz(location_timezone: str) -> ZoneInfo:
    """Return a ZoneInfo for the given IANA timezone string."""
    try:
        return ZoneInfo(location_timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(
            f"Unknown IANA timezone: {location_timezone!r}. "
            "Pass a valid value such as 'Europe/Berlin'."
        ) from exc


def _parse_datetime_bookings(date_str: str, tz: ZoneInfo) -> datetime | None:
    """
    Parse 'DD/MM/YYYY HH:MM' as used in bookings.csv.
    Returns an aware datetime in the given timezone.
    """
    val = date_str.strip()
    if not val:
        return None
    try:
        dt = datetime.strptime(val, "%d/%m/%Y %H:%M")
        return dt.replace(tzinfo=tz)
    except ValueError:
        logger.warning("Could not parse booking datetime: %r", date_str)
        return None


def _parse_datetime_activities(datum: str, zeit: str, tz: ZoneInfo) -> datetime | None:
    """
    Combine 'DD.MM.YYYY' + 'HH:MM' from activities.csv into an aware datetime.
    """
    d = datum.strip()
    t = zeit.strip()
    if not d or not t:
        return None
    try:
        dt = datetime.strptime(f"{d} {t}", "%d.%m.%Y %H:%M")
        return dt.replace(tzinfo=tz)
    except ValueError:
        logger.warning("Could not parse activity datetime: datum=%r zeit=%r", datum, zeit)
        return None


# ── Public parsers ─────────────────────────────────────────────────────────────


def parse_bookings_csv(file_bytes: bytes, location_timezone: str) -> list[dict]:
    """
    Parse a bookings.csv export from the Eversports admin panel.

    Returns a list of normalised dicts with keys:
      start, end, activity_name, location_address, trainer, customer_number,
      first_name, last_name, email, email_lower, clubgroup, newsletter,
      product_name, price, attendance_status, phone_raw

    Date fields are tz-aware datetimes in location_timezone.
    Price is Decimal or None.
    newsletter is bool or None.
    attendance_status is 'attended' | 'no_show' (based on 'Attended' column).
    """
    if not file_bytes:
        logger.info("parse_bookings_csv: received empty bytes — returning []")
        return []

    text = _decode_bom(file_bytes)
    delimiter = _detect_delimiter(text)
    tz = _make_tz(location_timezone)

    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    rows: list[dict] = []
    for line_num, raw_row in enumerate(reader, start=2):  # 1 = header
        try:
            email_raw = raw_row.get("E-Mail", "").strip()
            email_lower = email_raw.lower()

            start_str = raw_row.get("Start", "").strip()
            end_str = raw_row.get("End", "").strip()

            attended_raw = raw_row.get("Attended", "").strip().lower()
            if attended_raw == "yes":
                attendance_status = "attended"
            elif attended_raw == "no":
                attendance_status = "no_show"
            else:
                attendance_status = "unknown"

            rows.append(
                {
                    "start": _parse_datetime_bookings(start_str, tz),
                    "end": _parse_datetime_bookings(end_str, tz),
                    "activity_name": raw_row.get("Activity name", "").strip() or None,
                    "location_address": raw_row.get("Location", "").strip() or None,
                    "trainer": raw_row.get("Trainer nickname", "").strip() or None,
                    "customer_number": raw_row.get("Customer number", "").strip() or None,
                    "first_name": raw_row.get("First name", "").strip() or None,
                    "last_name": raw_row.get("Last name", "").strip() or None,
                    "email": email_raw or None,
                    "email_lower": email_lower or None,
                    "clubgroup": raw_row.get("Clubgroup name", "").strip() or None,
                    "newsletter": _parse_bool_field(raw_row.get("Newsletter", "")),
                    "product_name": raw_row.get("Product name", "").strip() or None,
                    "price": _parse_price(raw_row.get("Price", "")),
                    "attendance_status": attendance_status,
                    "phone_raw": raw_row.get("Phone number", "").strip() or None,
                }
            )
        except Exception:  # noqa: BLE001
            logger.exception("parse_bookings_csv: error on line %d, row=%r", line_num, raw_row)
            # Partial failure: skip this row, continue processing
            continue

    logger.info("parse_bookings_csv: parsed %d rows", len(rows))
    return rows


def parse_activities_csv(file_bytes: bytes, location_timezone: str) -> list[dict]:
    """
    Parse an 'all activities.csv' export from the Eversports admin panel.

    German headers expected:
      Typ, Datum, Startzeit, Endzeit, Name, Angemeldet, Anwesend, Max. Teilnehmer,
      Warteliste, Trainer, Ort, Status, Sport, Aktivitätsgruppe,
      Kommentar zur Einheit, Veröffentlicht

    Returns a list of normalised dicts with keys:
      session_type, start_time, end_time, activity_name, registered_count,
      attended_count, total_spots, waitlist_count, available_spots,
      trainer, location_label, status, sport, activity_group, comment, published

    available_spots = max(0, total_spots - registered_count).
    """
    if not file_bytes:
        logger.info("parse_activities_csv: received empty bytes — returning []")
        return []

    text = _decode_bom(file_bytes)
    delimiter = _detect_delimiter(text)
    tz = _make_tz(location_timezone)

    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    rows: list[dict] = []
    for line_num, raw_row in enumerate(reader, start=2):
        try:
            total_spots = _safe_int(raw_row.get("Max. Teilnehmer", ""), "Max. Teilnehmer")
            registered_count = _safe_int(raw_row.get("Angemeldet", ""), "Angemeldet")

            if total_spots is not None and registered_count is not None:
                available_spots = max(0, total_spots - registered_count)
            else:
                available_spots = None

            datum = raw_row.get("Datum", "").strip()
            start_zeit = raw_row.get("Startzeit", "").strip()
            end_zeit = raw_row.get("Endzeit", "").strip()

            published_raw = raw_row.get("Veröffentlicht", "").strip()
            published = _parse_bool_field(published_raw) if published_raw else None

            rows.append(
                {
                    "session_type": raw_row.get("Typ", "").strip() or None,
                    "start_time": _parse_datetime_activities(datum, start_zeit, tz),
                    "end_time": _parse_datetime_activities(datum, end_zeit, tz),
                    "activity_name": raw_row.get("Name", "").strip() or None,
                    "registered_count": registered_count,
                    "attended_count": _safe_int(raw_row.get("Anwesend", ""), "Anwesend"),
                    "total_spots": total_spots,
                    "waitlist_count": _safe_int(raw_row.get("Warteliste", ""), "Warteliste"),
                    "available_spots": available_spots,
                    "trainer": raw_row.get("Trainer", "").strip() or None,
                    "location_label": raw_row.get("Ort", "").strip() or None,
                    "status": raw_row.get("Status", "").strip() or None,
                    "sport": raw_row.get("Sport", "").strip() or None,
                    "activity_group": raw_row.get("Aktivitätsgruppe", "").strip() or None,
                    "comment": raw_row.get("Kommentar zur Einheit", "").strip() or None,
                    "published": published,
                }
            )
        except Exception:  # noqa: BLE001
            logger.exception("parse_activities_csv: error on line %d, row=%r", line_num, raw_row)
            continue

    logger.info("parse_activities_csv: parsed %d rows", len(rows))
    return rows


def parse_noshows_csv(file_bytes: bytes, location_timezone: str) -> list[dict]:
    """
    Parse a noshows.csv export from the Eversports admin panel.

    The sample file is 0 bytes (no no-shows in the export window) — this is
    treated as "no no-shows", not as a parse error.

    Expected schema: same columns as bookings.csv, filtered to Attended=no,
    with an optional 'Cancellation timestamp' column for late-cancel detection.

    Returns a list of dicts with the same shape as parse_bookings_csv, plus:
      attendance_status = 'late_cancel' | 'no_show'

    If 'Cancellation timestamp' is absent, all rows default to 'no_show'.
    """
    if not file_bytes:
        logger.info("parse_noshows_csv: empty file — no no-shows in export window")
        return []

    text = _decode_bom(file_bytes)
    if not text.strip():
        return []

    delimiter = _detect_delimiter(text)
    tz = _make_tz(location_timezone)

    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    if reader.fieldnames is None:
        logger.warning("parse_noshows_csv: could not read headers — returning []")
        return []

    has_cancellation_ts = "Cancellation timestamp" in (reader.fieldnames or [])

    rows: list[dict] = []
    for line_num, raw_row in enumerate(reader, start=2):
        try:
            email_raw = raw_row.get("E-Mail", "").strip()
            email_lower = email_raw.lower()

            # Determine attendance_status
            if has_cancellation_ts:
                cancel_ts_raw = raw_row.get("Cancellation timestamp", "").strip()
                # OPEN ITEM: late-cancel window comparison requires session_datetime.
                # For now, presence of a non-empty cancellation timestamp → late_cancel;
                # absent → no_show. The full late-cancel logic (comparing against
                # location.late_cancel_window_hours) is deferred to M2 where the
                # session datetime is available for comparison.
                attendance_status = "late_cancel" if cancel_ts_raw else "no_show"
            else:
                attendance_status = "no_show"

            start_str = raw_row.get("Start", "").strip()
            end_str = raw_row.get("End", "").strip()

            rows.append(
                {
                    "start": _parse_datetime_bookings(start_str, tz),
                    "end": _parse_datetime_bookings(end_str, tz),
                    "activity_name": raw_row.get("Activity name", "").strip() or None,
                    "location_address": raw_row.get("Location", "").strip() or None,
                    "trainer": raw_row.get("Trainer nickname", "").strip() or None,
                    "customer_number": raw_row.get("Customer number", "").strip() or None,
                    "first_name": raw_row.get("First name", "").strip() or None,
                    "last_name": raw_row.get("Last name", "").strip() or None,
                    "email": email_raw or None,
                    "email_lower": email_lower or None,
                    "clubgroup": raw_row.get("Clubgroup name", "").strip() or None,
                    "newsletter": _parse_bool_field(raw_row.get("Newsletter", "")),
                    "product_name": raw_row.get("Product name", "").strip() or None,
                    "price": _parse_price(raw_row.get("Price", "")),
                    "attendance_status": attendance_status,
                    "phone_raw": raw_row.get("Phone number", "").strip() or None,
                }
            )
        except Exception:  # noqa: BLE001
            logger.exception("parse_noshows_csv: error on line %d, row=%r", line_num, raw_row)
            continue

    logger.info("parse_noshows_csv: parsed %d rows", len(rows))
    return rows
