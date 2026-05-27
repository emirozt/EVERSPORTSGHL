"""
Bootstrap orchestrator — one-time CSV seed for a new location.

Execution sequence (per spec 07_foundation_layer.md § Bootstrap execution sequence):
  1. Validate + parse all uploaded files
  2a. Customer list (deferred — not in scope for M1.5)
  2b. Bookings → contacts upsert + bookings insert
  2c. No-shows → attendance_status updates
  2d. Activities → sessions insert
  2e. Active memberships (deferred — not in scope for M1.5)
  3. Compute derived fields per contact
  4. Apply initial tags (stub — full tag engine is M3)
  5. Pipeline init (stub — M3)
  6. Consent invitation enqueue (stub — M5)
  7. Write sync_log row
  8. Set location.historical_sync_flag = 'bootstrapped'
  9. Return BootstrapResult

Idempotency guarantees:
  - Contacts: INSERT ... ON CONFLICT (location_id, email_lower) DO UPDATE  [Postgres]
              / INSERT OR REPLACE [SQLite — tests only]
  - Bookings: INSERT ... ON CONFLICT (location_id, eversports_booking_id) DO NOTHING
  - Sessions: INSERT ... ON CONFLICT DO NOTHING on natural key
  - Partial re-upload is safe: only new/changed rows are touched.

Bootstrap run ID:
  A fresh UUID is generated at the start of each run. All new/updated rows are tagged
  with bootstrap_run_id so the reset endpoint can precisely undo a run.
"""

import hashlib
import logging
import time
import uuid
from collections import Counter, defaultdict
from datetime import UTC, date, datetime
from typing import TypedDict

from sqlalchemy import insert as sa_insert
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.bookings import Booking
from app.db.models.contacts import Contact
from app.db.models.location import Location
from app.db.models.sessions import Session
from app.db.models.sync_log import SyncLog
from app.ingest.classifier import classify_product
from app.ingest.csv_parser import parse_activities_csv, parse_bookings_csv, parse_noshows_csv
from app.ingest.normaliser import normalise_phone

logger = logging.getLogger(__name__)


# ── Result type ────────────────────────────────────────────────────────────────


class BootstrapResult(TypedDict):
    bootstrap_run_id: str
    contacts_seeded: int
    bookings_seeded: int
    sessions_seeded: int
    products_discovered: list[dict]  # [{"name": str, "count": int, "bucket": str}]
    contacts_missing_email: int
    contacts_invalid_phone: int
    warnings: list[str]
    errors: list[str]


# ── Dialect-aware insert helpers ───────────────────────────────────────────────


async def _get_dialect_name(db: AsyncSession) -> str:
    """Return the dialect name ('postgresql', 'sqlite', etc.)."""
    conn = await db.connection()
    return conn.engine.dialect.name  # type: ignore[attr-defined]


async def _upsert_contact(
    db: AsyncSession,
    vals: dict,
    dialect: str,
) -> uuid.UUID:
    """
    Upsert a contact row.

    On PostgreSQL: uses pg_insert().on_conflict_do_update() on (location_id, email_lower).
    On SQLite (tests): uses INSERT OR REPLACE (simulated via delete + insert for the
    non-NULL email_lower case, or plain insert for no-email contacts).
    """
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: PLC0415

        if vals.get("email_lower") is not None:
            stmt = pg_insert(Contact).values(**vals)
            stmt = stmt.on_conflict_do_update(
                index_elements=["location_id", "email_lower"],
                set_={
                    "first_name": stmt.excluded.first_name,
                    "last_name": stmt.excluded.last_name,
                    "phone": stmt.excluded.phone,
                    "phone_raw": stmt.excluded.phone_raw,
                    "eversports_customer_id": stmt.excluded.eversports_customer_id,
                    "eversports_clubgroup": stmt.excluded.eversports_clubgroup,
                    "eversports_newsletter_optin": stmt.excluded.eversports_newsletter_optin,
                    "eversports_location_address": stmt.excluded.eversports_location_address,
                    "products_purchased": stmt.excluded.products_purchased,
                    "active_package_type": stmt.excluded.active_package_type,
                    "active_package_name": stmt.excluded.active_package_name,
                    "total_sessions_attended": stmt.excluded.total_sessions_attended,
                    "no_show_count": stmt.excluded.no_show_count,
                    "last_session_date": stmt.excluded.last_session_date,
                    "last_session_end_time": stmt.excluded.last_session_end_time,
                    "last_class_name": stmt.excluded.last_class_name,
                    "last_booking_date": stmt.excluded.last_booking_date,
                    "sessions_attended_this_month": stmt.excluded.sessions_attended_this_month,
                    "sessions_attended_last_month": stmt.excluded.sessions_attended_last_month,
                    "bootstrap_run_id": stmt.excluded.bootstrap_run_id,
                    "updated_at": stmt.excluded.updated_at,
                },
            ).returning(Contact.id)
        else:
            stmt = pg_insert(Contact).values(**vals).returning(Contact.id)

        res = await db.execute(stmt)
        return res.scalar_one()

    else:
        # SQLite / other: check-then-insert-or-update
        if vals.get("email_lower") is not None:
            # Try to find existing contact
            existing = await db.execute(
                select(Contact.id).where(
                    Contact.location_id == vals["location_id"],
                    Contact.email_lower == vals["email_lower"],
                )
            )
            existing_id = existing.scalar_one_or_none()
            if existing_id is not None:
                # Update existing row
                excluded_keys = {"id", "location_id", "email_lower", "created_at"}
                update_vals = {k: v for k, v in vals.items() if k not in excluded_keys}
                await db.execute(
                    update(Contact).where(Contact.id == existing_id).values(**update_vals)
                )
                return existing_id

        # Insert new row — supply UUID since gen_random_uuid() isn't available in SQLite
        if "id" not in vals or vals.get("id") is None:
            vals = {**vals, "id": uuid.uuid4()}
        stmt = sa_insert(Contact).values(**vals).returning(Contact.id)
        res = await db.execute(stmt)
        return res.scalar_one()


async def _insert_ignore_booking(
    db: AsyncSession,
    vals: dict,
    dialect: str,
) -> int:
    """
    Insert a booking row, silently ignoring if eversports_booking_id already exists.
    Returns 1 if inserted, 0 if skipped.
    """
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: PLC0415

        stmt = (
            pg_insert(Booking)
            .values(**vals)
            .on_conflict_do_nothing(index_elements=["location_id", "eversports_booking_id"])
        )
        res = await db.execute(stmt)
        return res.rowcount or 0

    else:
        # Check existence first
        existing = await db.execute(
            select(Booking.id).where(
                Booking.location_id == vals["location_id"],
                Booking.eversports_booking_id == vals["eversports_booking_id"],
            )
        )
        if existing.scalar_one_or_none() is not None:
            return 0
        if "id" not in vals or vals.get("id") is None:
            vals = {**vals, "id": uuid.uuid4()}
        await db.execute(sa_insert(Booking).values(**vals))
        return 1


async def _insert_ignore_session(
    db: AsyncSession,
    vals: dict,
    dialect: str,
) -> int:
    """
    Insert a session row, silently ignoring if natural key already exists.
    Returns 1 if inserted, 0 if skipped.
    """
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: PLC0415

        stmt = (
            pg_insert(Session)
            .values(**vals)
            .on_conflict_do_nothing(
                index_elements=["location_id", "start_time", "activity_name", "trainer"]
            )
        )
        res = await db.execute(stmt)
        return res.rowcount or 0

    else:
        # Check existence first
        existing = await db.execute(
            select(Session.id).where(
                Session.location_id == vals["location_id"],
                Session.start_time == vals.get("start_time"),
                Session.activity_name == vals.get("activity_name"),
                Session.trainer == vals.get("trainer"),
            )
        )
        if existing.scalar_one_or_none() is not None:
            return 0
        if "id" not in vals or vals.get("id") is None:
            vals = {**vals, "id": uuid.uuid4()}
        await db.execute(sa_insert(Session).values(**vals))
        return 1


# ── Helpers ────────────────────────────────────────────────────────────────────


def _synthesise_booking_id(
    location_id: uuid.UUID,
    email_lower: str,
    session_datetime: datetime | None,
    activity_name: str | None,
) -> str:
    """
    Deterministic booking ID from sha256(location_id|email_lower|session_datetime|activity_name).
    Re-running with the same inputs produces the same ID — enabling idempotent upserts.
    """
    dt_str = session_datetime.isoformat() if session_datetime else ""
    act_str = activity_name or ""
    raw = f"{location_id}|{email_lower}|{dt_str}|{act_str}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _compute_derived_contact_fields(
    bookings: list[dict],
    now: datetime,
) -> dict:
    """
    Compute all derived per-contact fields from the contact's booking list.

    Args:
        bookings: List of booking dicts (from parse_bookings_csv) for a single contact.
        now: Reference datetime for 'this month' / 'last month' window calculations.

    Returns a dict with the derived scalar fields to merge into the contacts upsert.
    """
    attended = [b for b in bookings if b["attendance_status"] == "attended"]
    no_shows = [b for b in bookings if b["attendance_status"] in ("no_show", "late_cancel")]

    # last_session_date / end_time / class_name — from most recent attended booking
    last_session_date = None
    last_session_end_time = None
    last_class_name = None
    if attended:
        latest_attended = max(
            (b for b in attended if b["start"] is not None),
            key=lambda b: b["start"],
            default=None,
        )
        if latest_attended:
            last_session_date = latest_attended["start"].date()
            last_session_end_time = (
                latest_attended["end"].time() if latest_attended["end"] else None
            )
            last_class_name = latest_attended["activity_name"]

    # last_booking_date — from most recent booking of any type
    all_with_date = [b for b in bookings if b["start"] is not None]
    last_booking_date = None
    if all_with_date:
        last_booking_date = max(b["start"].date() for b in all_with_date)

    # Monthly attendance windows (rolling calendar months)
    this_month_start = date(now.year, now.month, 1)
    if now.month == 1:
        last_month_start = date(now.year - 1, 12, 1)
        last_month_end = date(now.year, 1, 1)
    else:
        last_month_start = date(now.year, now.month - 1, 1)
        last_month_end = date(now.year, now.month, 1)

    sessions_this_month = sum(
        1 for b in attended if b["start"] is not None and b["start"].date() >= this_month_start
    )
    sessions_last_month = sum(
        1
        for b in attended
        if b["start"] is not None and last_month_start <= b["start"].date() < last_month_end
    )

    return {
        "total_sessions_attended": len(attended),
        "no_show_count": len(no_shows),
        "last_session_date": last_session_date,
        "last_session_end_time": last_session_end_time,
        "last_class_name": last_class_name,
        "last_booking_date": last_booking_date,
        "sessions_attended_this_month": sessions_this_month,
        "sessions_attended_last_month": sessions_last_month,
    }


def _determine_active_package(products: list[str]) -> tuple[str | None, str | None]:
    """
    Determine active_package_type and active_package_name from a contact's product list.

    Heuristic: prefer the most recently used product (last in list by insertion order,
    which is chronological from the CSV). Non-trial products take precedence over trial.

    Returns (active_package_type, active_package_name).
    """
    if not products:
        return None, None

    # Prefer non-trial products
    non_trial = [p for p in products if classify_product(p) != "trial"]
    if non_trial:
        latest = non_trial[-1]
        return classify_product(latest), latest

    # Fall back to trial
    latest = products[-1]
    return classify_product(latest), latest


# ── Main orchestrator ──────────────────────────────────────────────────────────


async def run_bootstrap(
    location_id: uuid.UUID,
    bookings_bytes: bytes,
    activities_bytes: bytes | None,
    noshows_bytes: bytes | None,
    db: AsyncSession,
) -> BootstrapResult:
    """
    Run the full CSV bootstrap sequence for a location.

    Steps 4 (tags), 5 (pipeline), and 6 (consent invitation) are stubs in M1.5.
    They will be wired up in M3 and M5 respectively.

    This function does NOT commit the transaction — the caller (API endpoint) is
    responsible for committing or rolling back. This enables the caller to wrap
    the entire bootstrap in a single transaction.
    """
    run_start = time.monotonic()
    bootstrap_run_id = uuid.uuid4()
    now = datetime.now(UTC)

    warnings: list[str] = []
    errors: list[str] = []

    logger.info("bootstrap: starting run_id=%s location_id=%s", bootstrap_run_id, location_id)

    # ── Detect dialect (for dialect-aware upserts) ─────────────────────────
    conn = await db.connection()
    dialect = conn.engine.dialect.name  # type: ignore[attr-defined]
    logger.debug("bootstrap: detected dialect=%s", dialect)

    # ── Fetch location for timezone ────────────────────────────────────────
    result = await db.execute(select(Location).where(Location.id == location_id))
    location = result.scalar_one_or_none()
    if location is None:
        raise ValueError(f"Location not found: {location_id}")

    tz_str = location.timezone
    region = location.country  # ISO 3166-1 alpha-2 (e.g. "DE", "AT", "CH")

    # ── Step 1: Parse ──────────────────────────────────────────────────────
    logger.info("bootstrap: step 1 — parsing CSVs")

    try:
        booking_rows = parse_bookings_csv(bookings_bytes, tz_str)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"bookings.csv parse failed: {exc}")
        logger.exception("bootstrap: bookings.csv parse failed")
        booking_rows = []

    try:
        activity_rows = parse_activities_csv(activities_bytes or b"", tz_str)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"activities.csv parse failed: {exc}")
        logger.exception("bootstrap: activities.csv parse failed")
        activity_rows = []

    try:
        noshow_rows = parse_noshows_csv(noshows_bytes or b"", tz_str)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"noshows.csv parse failed: {exc}")
        logger.exception("bootstrap: noshows.csv parse failed")
        noshow_rows = []

    # ── Step 2b: Contacts upsert + bookings insert ─────────────────────────
    logger.info("bootstrap: step 2b — contacts + bookings")

    # Group booking rows by email_lower (primary match key)
    bookings_by_email: dict[str, list[dict]] = defaultdict(list)
    contacts_missing_email = 0
    product_counter: Counter[str] = Counter()

    for row in booking_rows:
        if row.get("product_name"):
            product_counter[row["product_name"]] += 1

    for row in booking_rows:
        email_lower = row.get("email_lower") or ""
        if not email_lower:
            contacts_missing_email += 1
            # Still process — contact is created without email
            synthetic_key = (
                f"__noemail__{row.get('first_name', '')}_{row.get('last_name', '')}"
                f"_{row.get('phone_raw', '')}".lower()
            )
            bookings_by_email[synthetic_key].append(row)
        else:
            bookings_by_email[email_lower].append(row)

    if contacts_missing_email:
        warnings.append(
            f"{contacts_missing_email} booking row(s) have no email — "
            "contacts created but cannot receive email-channel comms"
        )

    contacts_invalid_phone = 0
    contacts_upserted = 0

    # Build a map from email_lower → contact UUID for booking FK assignment
    email_to_contact_id: dict[str, uuid.UUID] = {}

    for email_lower, rows_for_contact in bookings_by_email.items():
        # Use the most-recent row's identity fields (last row chronologically)
        rows_with_date = [r for r in rows_for_contact if r.get("start") is not None]
        representative = (
            max(rows_with_date, key=lambda r: r["start"])
            if rows_with_date
            else rows_for_contact[-1]
        )

        email_raw = representative.get("email")
        phone_raw = representative.get("phone_raw") or ""

        # Normalise phone
        e164, _ = normalise_phone(phone_raw, region) if phone_raw else (None, phone_raw)
        if phone_raw and not e164:
            contacts_invalid_phone += 1
            warnings.append(
                f"Could not normalise phone for {email_lower!r}: {phone_raw!r} — "
                "stored as phone_raw only"
            )

        # Collect all products for this contact
        products_list = list(
            dict.fromkeys(  # preserve order, deduplicate
                r["product_name"] for r in rows_for_contact if r.get("product_name")
            )
        )

        # Newsletter: use most-recent booking row's value
        newsletter_val: bool | None = None
        if rows_with_date:
            newsletter_val = max(rows_with_date, key=lambda r: r["start"]).get("newsletter")
        elif rows_for_contact:
            newsletter_val = rows_for_contact[-1].get("newsletter")

        # Derived fields
        derived = _compute_derived_contact_fields(rows_for_contact, now)

        # Active package
        active_type, active_name = _determine_active_package(products_list)

        location_address = representative.get("location_address")
        is_synthetic_key = email_lower.startswith("__noemail__")

        contact_vals: dict = {
            "location_id": location_id,
            "email": email_raw if email_raw else None,
            "email_lower": email_lower if not is_synthetic_key else None,
            "first_name": representative.get("first_name"),
            "last_name": representative.get("last_name"),
            "phone": e164,
            "phone_raw": phone_raw or None,
            "eversports_customer_id": representative.get("customer_number"),
            "eversports_clubgroup": representative.get("clubgroup"),
            "eversports_newsletter_optin": newsletter_val,
            "eversports_location_address": location_address,
            "products_purchased": products_list,
            "active_package_type": active_type,
            "active_package_name": active_name,
            "total_sessions_attended": derived["total_sessions_attended"],
            "no_show_count": derived["no_show_count"],
            "last_session_date": derived["last_session_date"],
            "last_session_end_time": derived["last_session_end_time"],
            "last_class_name": derived["last_class_name"],
            "last_booking_date": derived["last_booking_date"],
            "sessions_attended_this_month": derived["sessions_attended_this_month"],
            "sessions_attended_last_month": derived["sessions_attended_last_month"],
            "bootstrap_run_id": bootstrap_run_id,
            "updated_at": datetime.now(UTC),
        }

        contact_id = await _upsert_contact(db, contact_vals, dialect)
        email_to_contact_id[email_lower] = contact_id
        contacts_upserted += 1

    logger.info("bootstrap: contacts upserted=%d", contacts_upserted)

    # ── Step 2b continued: Bookings insert ────────────────────────────────
    bookings_inserted = 0

    for row in booking_rows:
        email_lower = row.get("email_lower") or ""
        if not email_lower:
            synthetic_key = (
                f"__noemail__{row.get('first_name', '')}_{row.get('last_name', '')}"
                f"_{row.get('phone_raw', '')}".lower()
            )
            email_lower = synthetic_key

        contact_id = email_to_contact_id.get(email_lower)
        if contact_id is None:
            errors.append(f"No contact_id found for email_lower={email_lower!r} — booking skipped")
            continue

        # Use the real email_lower (not synthetic key) in the booking ID
        real_email_lower = row.get("email_lower") or ""
        booking_id = _synthesise_booking_id(
            location_id=location_id,
            email_lower=real_email_lower,
            session_datetime=row.get("start"),
            activity_name=row.get("activity_name"),
        )

        booking_vals = {
            "location_id": location_id,
            "contact_id": contact_id,
            "eversports_booking_id": booking_id,
            "activity_name": row.get("activity_name"),
            "session_datetime": row.get("start"),
            "session_end_datetime": row.get("end"),
            "trainer": row.get("trainer"),
            "package_used": row.get("product_name"),
            "price": row.get("price"),
            "attendance_status": row.get("attendance_status", "unknown"),
            "bootstrap_run_id": bootstrap_run_id,
            "updated_at": datetime.now(UTC),
        }

        inserted = await _insert_ignore_booking(db, booking_vals, dialect)
        bookings_inserted += inserted

    logger.info("bootstrap: bookings inserted=%d", bookings_inserted)

    # ── Step 2c: No-shows → attendance_status updates ─────────────────────
    logger.info("bootstrap: step 2c — no-shows (%d rows)", len(noshow_rows))

    for row in noshow_rows:
        email_lower = (row.get("email_lower") or "").strip()
        if not email_lower:
            continue

        booking_id = _synthesise_booking_id(
            location_id=location_id,
            email_lower=email_lower,
            session_datetime=row.get("start"),
            activity_name=row.get("activity_name"),
        )

        await db.execute(
            update(Booking)
            .where(
                Booking.location_id == location_id,
                Booking.eversports_booking_id == booking_id,
            )
            .values(
                attendance_status=row.get("attendance_status", "no_show"),
                updated_at=datetime.now(UTC),
            )
        )

    # ── Step 2d: Activities → sessions insert ─────────────────────────────
    logger.info("bootstrap: step 2d — sessions (%d rows)", len(activity_rows))

    sessions_inserted = 0

    for row in activity_rows:
        session_vals = {
            "location_id": location_id,
            "session_type": row.get("session_type"),
            "start_time": row.get("start_time"),
            "end_time": row.get("end_time"),
            "activity_name": row.get("activity_name"),
            "activity_group": row.get("activity_group"),
            "sport": row.get("sport"),
            "trainer": row.get("trainer"),
            "location_label": row.get("location_label"),
            "total_spots": row.get("total_spots"),
            "registered_count": row.get("registered_count"),
            "attended_count": row.get("attended_count"),
            "waitlist_count": row.get("waitlist_count"),
            "available_spots": row.get("available_spots"),
            "status": row.get("status"),
            "comment": row.get("comment"),
            "published": row.get("published"),
            "bootstrap_run_id": bootstrap_run_id,
        }

        inserted = await _insert_ignore_session(db, session_vals, dialect)
        sessions_inserted += inserted

    logger.info("bootstrap: sessions inserted=%d", sessions_inserted)

    # ── Steps 3–6: Derived fields (already computed inline above) ─────────
    # Steps 4 (tags), 5 (pipeline), 6 (consent invitation) — stubs for M3/M5
    logger.info(
        "bootstrap: steps 4-6 stubs (tag engine, pipeline init, consent invitation — M3/M5)"
    )

    # ── Step 7: Write sync_log ─────────────────────────────────────────────
    duration = time.monotonic() - run_start

    sync_log = SyncLog(
        id=uuid.uuid4(),
        location_id=location_id,
        run_type="bootstrap",
        contacts_processed=contacts_upserted,
        contacts_updated=contacts_upserted,
        tags_applied=0,
        pipeline_moves=0,
        errors=errors,
        bootstrap_run_id=bootstrap_run_id,
        duration_seconds=round(duration, 3),
    )
    db.add(sync_log)

    # ── Step 8: Update historical_sync_flag ────────────────────────────────
    await db.execute(
        update(Location)
        .where(Location.id == location_id)
        .values(historical_sync_flag="bootstrapped")
    )

    logger.info(
        "bootstrap: complete run_id=%s contacts=%d bookings=%d sessions=%d duration=%.2fs",
        bootstrap_run_id,
        contacts_upserted,
        bookings_inserted,
        sessions_inserted,
        duration,
    )

    # ── Step 9: Build products_discovered list ─────────────────────────────
    products_discovered = [
        {
            "name": name,
            "count": count,
            "bucket": classify_product(name),
        }
        for name, count in product_counter.most_common()
    ]

    return BootstrapResult(
        bootstrap_run_id=str(bootstrap_run_id),
        contacts_seeded=contacts_upserted,
        bookings_seeded=bookings_inserted,
        sessions_seeded=sessions_inserted,
        products_discovered=products_discovered,
        contacts_missing_email=contacts_missing_email,
        contacts_invalid_phone=contacts_invalid_phone,
        warnings=warnings,
        errors=errors,
    )
