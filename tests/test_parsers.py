"""
Tests for CSV parsers and normaliser helpers.

Uses the real sample exports from requirements_v2/sample_exports/ as fixtures.
These files contain BOM, semicolons, German/English headers — the parsers must
handle all of them correctly without any monkey-patching.
"""

import pathlib
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.ingest.classifier import classify_product, is_card, is_membership, is_trial
from app.ingest.csv_parser import (
    parse_activities_csv,
    parse_bookings_csv,
    parse_noshows_csv,
)
from app.ingest.normaliser import normalise_phone, timezone_to_region

# ── Fixtures ───────────────────────────────────────────────────────────────────

SAMPLE_DIR = pathlib.Path(__file__).parent.parent / "requirements_v2" / "sample_exports"

TZ = "Europe/Berlin"


@pytest.fixture(scope="module")
def bookings_bytes() -> bytes:
    return (SAMPLE_DIR / "bookings.csv").read_bytes()


@pytest.fixture(scope="module")
def activities_bytes() -> bytes:
    return (SAMPLE_DIR / "all activities.csv").read_bytes()


@pytest.fixture(scope="module")
def noshows_bytes() -> bytes:
    return (SAMPLE_DIR / "noshows.csv").read_bytes()


# ── Parser tests ───────────────────────────────────────────────────────────────


class TestParseBookingsCsv:
    def test_row_count(self, bookings_bytes: bytes) -> None:
        rows = parse_bookings_csv(bookings_bytes, TZ)
        # 29 data rows in the sample file (28 distinct emails, 1 duplicate)
        assert len(rows) == 29

    def test_bom_stripped(self, bookings_bytes: bytes) -> None:
        # BOM must not appear in header keys — if it does, 'Start' wouldn't match
        rows = parse_bookings_csv(bookings_bytes, TZ)
        assert rows[0]["start"] is not None, "BOM in header broke 'Start' column parsing"

    def test_known_email(self, bookings_bytes: bytes) -> None:
        rows = parse_bookings_csv(bookings_bytes, TZ)
        emails = [r["email_lower"] for r in rows]
        assert "test+1@example.com" in emails

    def test_datetime_parsing(self, bookings_bytes: bytes) -> None:
        rows = parse_bookings_csv(bookings_bytes, TZ)
        first = rows[0]
        assert isinstance(first["start"], datetime)
        # 01/05/2026 10:00 in Europe/Berlin
        tz = ZoneInfo(TZ)
        expected = datetime(2026, 5, 1, 10, 0, tzinfo=tz)
        assert first["start"] == expected

    def test_end_datetime_parsing(self, bookings_bytes: bytes) -> None:
        rows = parse_bookings_csv(bookings_bytes, TZ)
        first = rows[0]
        assert isinstance(first["end"], datetime)
        tz = ZoneInfo(TZ)
        expected = datetime(2026, 5, 1, 10, 55, tzinfo=tz)
        assert first["end"] == expected

    def test_price_parsing(self, bookings_bytes: bytes) -> None:
        rows = parse_bookings_csv(bookings_bytes, TZ)
        first = rows[0]
        # "252.00 €" → Decimal("252.00")
        assert first["price"] == Decimal("252.00")

    def test_attended_yes_maps_to_attended(self, bookings_bytes: bytes) -> None:
        rows = parse_bookings_csv(bookings_bytes, TZ)
        # All rows in the sample have Attended=yes
        statuses = {r["attendance_status"] for r in rows}
        assert "attended" in statuses
        assert "unknown" not in statuses

    def test_newsletter_is_bool(self, bookings_bytes: bytes) -> None:
        rows = parse_bookings_csv(bookings_bytes, TZ)
        newsletter_vals = [r["newsletter"] for r in rows if r["newsletter"] is not None]
        assert all(isinstance(v, bool) for v in newsletter_vals)
        # Sample has both yes and no values
        assert True in newsletter_vals
        assert False in newsletter_vals

    def test_phone_raw_preserved(self, bookings_bytes: bytes) -> None:
        rows = parse_bookings_csv(bookings_bytes, TZ)
        phones = {r["phone_raw"] for r in rows if r["phone_raw"]}
        # Synthetic fixture: row 0 uses 015x format (no country prefix),
        # remaining rows use +49176xxxxxxxx format.
        assert "015200000001" in phones
        assert "+4917600000002" in phones

    def test_distinct_emails(self, bookings_bytes: bytes) -> None:
        rows = parse_bookings_csv(bookings_bytes, TZ)
        emails = {r["email_lower"] for r in rows if r["email_lower"]}
        # 28 distinct emails in the 29-row sample
        assert len(emails) == 28

    def test_product_names_present(self, bookings_bytes: bytes) -> None:
        rows = parse_bookings_csv(bookings_bytes, TZ)
        products = {r["product_name"] for r in rows if r["product_name"]}
        assert "10er Karte-Gruppe" in products
        assert "3 Trial Cards-Introduction to Pilates Reformer" in products
        assert "Gruppenmitgliedschaft-1 x Woche" in products

    def test_customer_number_not_relied_on(self, bookings_bytes: bytes) -> None:
        rows = parse_bookings_csv(bookings_bytes, TZ)
        # Per spec, customer_number is consistently empty — we still parse it but never rely on it
        customer_nums = [r["customer_number"] for r in rows if r["customer_number"]]
        assert len(customer_nums) == 0, (
            "customer_number unexpectedly populated — spec says it's blank"
        )

    def test_empty_bytes_returns_empty_list(self) -> None:
        assert parse_bookings_csv(b"", TZ) == []


class TestParseActivitiesCsv:
    def test_row_count(self, activities_bytes: bytes) -> None:
        rows = parse_activities_csv(activities_bytes, TZ)
        # 43 rows in the sample activities CSV (spec said 42, actual is 43)
        assert len(rows) == 43

    def test_bom_stripped(self, activities_bytes: bytes) -> None:
        rows = parse_activities_csv(activities_bytes, TZ)
        assert rows[0]["start_time"] is not None, "BOM in header broke 'Datum' column parsing"

    def test_datetime_parsing(self, activities_bytes: bytes) -> None:
        rows = parse_activities_csv(activities_bytes, TZ)
        first = rows[0]
        tz = ZoneInfo(TZ)
        # Datum=01.05.2026 Startzeit=10:00
        expected = datetime(2026, 5, 1, 10, 0, tzinfo=tz)
        assert first["start_time"] == expected

    def test_end_time_parsing(self, activities_bytes: bytes) -> None:
        rows = parse_activities_csv(activities_bytes, TZ)
        first = rows[0]
        tz = ZoneInfo(TZ)
        expected = datetime(2026, 5, 1, 10, 55, tzinfo=tz)
        assert first["end_time"] == expected

    def test_available_spots_derivation(self, activities_bytes: bytes) -> None:
        rows = parse_activities_csv(activities_bytes, TZ)
        # First row: Max. Teilnehmer=10, Angemeldet=10 → available_spots=0
        assert rows[0]["total_spots"] == 10
        assert rows[0]["registered_count"] == 10
        assert rows[0]["available_spots"] == 0

    def test_available_spots_positive(self, activities_bytes: bytes) -> None:
        rows = parse_activities_csv(activities_bytes, TZ)
        # Third row: Max. Teilnehmer=10, Angemeldet=9 → available_spots=1
        assert rows[2]["available_spots"] == 1

    def test_available_spots_never_negative(self, activities_bytes: bytes) -> None:
        rows = parse_activities_csv(activities_bytes, TZ)
        for row in rows:
            if row["available_spots"] is not None:
                assert row["available_spots"] >= 0, (
                    f"available_spots went negative for {row['activity_name']}"
                )

    def test_waitlist_count_parsed(self, activities_bytes: bytes) -> None:
        rows = parse_activities_csv(activities_bytes, TZ)
        # First row has waitlist=12
        assert rows[0]["waitlist_count"] == 12

    def test_german_fields_present(self, activities_bytes: bytes) -> None:
        rows = parse_activities_csv(activities_bytes, TZ)
        first = rows[0]
        assert first["session_type"] == "Klasse"
        assert first["sport"] == "Reformer Pilates"
        assert first["activity_group"] == "All levels pilates equipment"
        assert first["status"] == "buchbar"

    def test_empty_bytes_returns_empty_list(self) -> None:
        assert parse_activities_csv(b"", TZ) == []


class TestParseNoshowsCsv:
    def test_empty_bytes_returns_empty_list(self, noshows_bytes: bytes) -> None:
        # The sample noshows.csv is 0 bytes — confirmed in requirements
        assert len(noshows_bytes) == 0
        result = parse_noshows_csv(noshows_bytes, TZ)
        assert result == []

    def test_completely_empty_bytes(self) -> None:
        assert parse_noshows_csv(b"", TZ) == []

    def test_noshow_without_cancellation_ts_column(self) -> None:
        """No 'Cancellation timestamp' column → attendance_status = 'no_show' for all rows."""
        sample = (
            b'\xef\xbb\xbf"Start";"End";"Activity name";"Location";"Trainer nickname";'
            b'"Customer number";"First name";"Last name";"E-Mail";"Clubgroup name";'
            b'"Newsletter";"Product name";"Price";"Attended";"Phone number"\n'
            b'"01/05/2026 10:00";"01/05/2026 10:55";"Yoga";"";"Anna";"";'
            b'"Jane";"Doe";"jane@example.com";"Extern";"no";'
            b'"10er Karte";"100.00 \xe2\x82\xac";"no";"01234567890"\n'
        )
        rows = parse_noshows_csv(sample, TZ)
        assert len(rows) == 1
        assert rows[0]["attendance_status"] == "no_show"

    def test_noshow_with_cancellation_ts_column(self) -> None:
        """Presence of 'Cancellation timestamp' with a value → 'late_cancel'."""
        sample = (
            b'\xef\xbb\xbf"Start";"End";"Activity name";"Location";"Trainer nickname";'
            b'"Customer number";"First name";"Last name";"E-Mail";"Clubgroup name";'
            b'"Newsletter";"Product name";"Price";"Attended";"Phone number";'
            b'"Cancellation timestamp"\n'
            b'"01/05/2026 10:00";"01/05/2026 10:55";"Yoga";"";"Anna";"";'
            b'"Jane";"Doe";"jane@example.com";"Extern";"no";'
            b'"10er Karte";"100.00 \xe2\x82\xac";"no";"01234567890";"30/04/2026 22:00"\n'
        )
        rows = parse_noshows_csv(sample, TZ)
        assert len(rows) == 1
        assert rows[0]["attendance_status"] == "late_cancel"


# ── Normaliser tests ───────────────────────────────────────────────────────────


class TestNormalisePhone:
    """
    Test all phone formats the normaliser must handle.
    Uses synthetic numbers that match the format classes present in
    Eversports CSV exports (German 015x / 017x mobiles, +49 prefix,
    with/without spaces, US numbers).  Default region = 'DE'.
    """

    def test_german_mobile_without_prefix(self) -> None:
        # 015x format — no country code, 12 digits total
        e164, raw = normalise_phone("015200000001", "DE")
        assert e164 == "+4915200000001"
        assert raw == "015200000001"

    def test_german_mobile_with_plus(self) -> None:
        # +49 17x format — full E.164 already
        e164, raw = normalise_phone("+491760000001", "DE")
        assert e164 == "+491760000001"
        assert raw == "+491760000001"

    def test_german_mobile_short_without_prefix(self) -> None:
        # 17x format — no leading 0, no country code
        e164, raw = normalise_phone("17600000002", "DE")
        assert e164 == "+4917600000002"

    def test_german_mobile_017x_prefix(self) -> None:
        # 017x prefix with leading 0
        e164, raw = normalise_phone("01780000003", "DE")
        assert e164 is not None
        assert e164.startswith("+49")

    def test_international_with_spaces(self) -> None:
        # +49 with spaces between segments — normaliser must strip spaces
        e164, raw = normalise_phone("+49 152 00000004", "DE")
        assert e164 == "+4915200000004"

    def test_with_trailing_space(self) -> None:
        # Trailing space — normaliser must strip before parsing; use +1 prefix for clarity
        e164, raw = normalise_phone("+1 415 520 0001 ", "US")
        assert e164 is not None  # parses as a US number

    def test_empty_string_returns_none(self) -> None:
        e164, raw = normalise_phone("", "DE")
        assert e164 is None
        assert raw == ""

    def test_none_equivalent_returns_none(self) -> None:
        e164, raw = normalise_phone("  ", "DE")
        assert e164 is None

    def test_invalid_number_returns_none(self) -> None:
        e164, raw = normalise_phone("123", "DE")
        # Too short to be valid
        assert e164 is None
        assert raw == "123"

    def test_raw_always_returned(self) -> None:
        _, raw = normalise_phone("+491760000001", "DE")
        assert raw == "+491760000001"


class TestTimezoneToRegion:
    def test_berlin_returns_de(self) -> None:
        assert timezone_to_region("Europe/Berlin") == "DE"

    def test_vienna_returns_at(self) -> None:
        assert timezone_to_region("Europe/Vienna") == "AT"

    def test_zurich_returns_ch(self) -> None:
        assert timezone_to_region("Europe/Zurich") == "CH"

    def test_unknown_returns_de(self) -> None:
        assert timezone_to_region("America/New_York") == "DE"


# ── Classifier tests ───────────────────────────────────────────────────────────


class TestClassifier:
    """Test product names seen in the sample bookings.csv."""

    # Cards
    def test_10er_karte_is_card(self) -> None:
        assert classify_product("10er Karte-Gruppe") == "card"

    def test_20er_karte_is_card(self) -> None:
        assert classify_product("20er Karte-Gruppe") == "card"

    def test_5er_karte_is_card(self) -> None:
        assert classify_product("5er Karte-Gruppe") == "card"

    # Trial — note: also contains "Card"/"Karte" but trial wins
    def test_trial_cards_is_trial_not_card(self) -> None:
        assert classify_product("3 Trial Cards-Introduction to Pilates Reformer") == "trial"

    def test_is_trial_true_for_trial_name(self) -> None:
        assert is_trial("3 Trial Cards-Introduction to Pilates Reformer") is True

    def test_is_card_false_when_trial(self) -> None:
        # is_card must return False when is_trial returns True (spec requirement)
        assert is_card("3 Trial Cards-Introduction to Pilates Reformer") is False

    # Memberships
    def test_gruppenmitgliedschaft_1_is_membership(self) -> None:
        assert classify_product("Gruppenmitgliedschaft-1 x Woche") == "membership"

    def test_gruppenmitgliedschaft_2_is_membership(self) -> None:
        assert classify_product("Gruppenmitgliedschaft-2 x Woche") == "membership"

    def test_equipment_mitgliedschaft_is_membership(self) -> None:
        assert classify_product("Gruppe-Equipment Mitgliedschaft-1") == "membership"

    def test_limitless_mitgliedschaft_is_membership(self) -> None:
        assert classify_product("Limitless-Gruppenmitgliedschaft") == "membership"

    def test_is_membership_true(self) -> None:
        assert is_membership("Gruppenmitgliedschaft-1 x Woche") is True

    # Edge cases
    def test_empty_string_is_card_residual(self) -> None:
        # Empty string doesn't match any named category → residual → card
        assert classify_product("") == "card"

    def test_unknown_product_is_card_residual(self) -> None:
        assert classify_product("Einzelstunde Privatunterricht") == "card"

    def test_voucher_classification(self) -> None:
        assert classify_product("Gutschein 50€") == "voucher"

    def test_merch_classification(self) -> None:
        assert classify_product("Pilates Mat Premium") == "merch"
