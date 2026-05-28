"""
Tests for the M5 writeback executor stack.

All tests run with WRITEBACK_DRY_RUN=true and WRITEBACK_SAFETY_MODE=dev.
No tests contact the live Eversports account.  Dry-run mode is verified
by asserting that handlers return a "dry_run" status response and that
the NotImplementedError (live path) is never raised.

IMPORTANT: Only the following targets are used in test fixtures:
  Customer email: emiroztrk@gmail.com
  Class:          "Reformer Booty Burn Group Class"
  Start datetime: 2026-11-30T19:00:00+00:00

Do NOT add faker/random payloads — the safety guard will reject them and
the tests will fail with SafetyGuardError, which is the correct behaviour.

Covers:
  - app/writeback/safety.py    — SafetyGuard whitelist enforcement
  - app/writeback/audit.py     — audit log + notification stub
  - app/writeback/handlers/    — all four handlers (dry-run path)
  - app/writeback/executor.py  — execute_writeback_job, retry policy
  - app/db/models/writeback_job.py — WritebackJob model
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models.base import Base
from app.db.models.location import Location
from app.db.models.writeback_job import WritebackJob
from app.writeback.executor import (
    MAX_ATTEMPTS,
    RETRY_DELAYS,
    _claim_next_job,
    _mark_failed_or_dead,
    _mark_succeeded,
    execute_writeback_job,
)
from app.writeback.safety import (
    WHITELISTED_CLASS_NAME,
    WHITELISTED_EMAIL,
    WHITELISTED_START_DT,
    SafetyGuard,
    SafetyGuardError,
)

# ── Whitelisted test fixtures ────────────────────────────────────────────────

TEST_EMAIL = WHITELISTED_EMAIL
TEST_CLASS = WHITELISTED_CLASS_NAME
TEST_START_DT = WHITELISTED_START_DT
TEST_START_ISO = TEST_START_DT.isoformat()

CUSTOMER_PAYLOAD = {
    "first_name": "Emir",
    "last_name": "Test",
    "email": TEST_EMAIL,
    "phone": "+43123456789",
    "marketing_consents": False,
}

BOOKING_PAYLOAD = {
    "customer_id": "cust-001",
    "activity_id": "act-001",
    "session_id": "sess-001",
    "session_datetime": TEST_START_ISO,
    "class_name": TEST_CLASS,
    "package_id": None,
}

RESCHEDULE_PAYLOAD = {
    "booking_id": "book-001",
    "new_session_id": "sess-002",
    "new_class_name": TEST_CLASS,
    "new_session_datetime": TEST_START_ISO,
    "customer_email": TEST_EMAIL,
    "reason": "Customer request",
}

CANCEL_PAYLOAD = {
    "booking_id": "book-001",
    "class_name": TEST_CLASS,
    "session_datetime": TEST_START_ISO,
    "customer_email": TEST_EMAIL,
    "reason": "Test teardown",
}


# ── DB fixtures ───────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def db(engine) -> AsyncGenerator[AsyncSession, None]:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session


@pytest_asyncio.fixture
async def factory(engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def location(db: AsyncSession) -> Location:
    loc = Location(
        id=uuid.uuid4(),
        eversports_studio_id="test-studio",
        ghl_subaccount_id=f"ghl-{uuid.uuid4().hex[:8]}",
        ghl_oauth_token_ref="secret://test/ghl",
        eversports_credentials_ref="secret://test/eversports",
        timezone="Europe/Vienna",
        studio_owner_email="owner@test.com",
        studio_name="Test Studio",
        location_name="Test Studio — Main",
        eversports_cookie_state="ok",
        historical_sync_flag="pending",
    )
    db.add(loc)
    await db.flush()
    return loc


def _make_job(
    location_id: uuid.UUID,
    job_type: str,
    payload: dict,
    *,
    status: str = "queued",
    attempt_count: int = 0,
    next_retry_at: datetime | None = None,
) -> WritebackJob:
    from app.writeback.safety import SafetyGuard  # noqa: PLC0415
    guard = SafetyGuard(mode="prod")
    if job_type == "create_customer":
        idem_key = guard.make_create_customer_key(str(location_id), payload.get("email", ""))
    elif job_type == "create_booking":
        idem_key = guard.make_create_booking_key(payload["customer_id"], payload["session_id"])
    elif job_type == "reschedule_booking":
        idem_key = guard.make_reschedule_booking_key(payload["booking_id"], payload["new_session_id"])
    else:
        idem_key = guard.make_cancel_booking_key(payload["booking_id"])

    return WritebackJob(
        id=uuid.uuid4(),
        location_id=location_id,
        job_type=job_type,
        payload=payload,
        idempotency_key=idem_key,
        status=status,
        attempt_count=attempt_count,
        next_retry_at=next_retry_at,
    )


# ─────────────────────────────────────────────────────────────────────────────
# TestSafetyGuard
# ─────────────────────────────────────────────────────────────────────────────


class TestSafetyGuard:
    def test_dev_mode_allows_whitelisted_email(self):
        guard = SafetyGuard(mode="dev")
        guard.check_create_customer(TEST_EMAIL)  # should not raise

    def test_dev_mode_rejects_non_whitelisted_email(self):
        guard = SafetyGuard(mode="dev")
        with pytest.raises(SafetyGuardError, match="non-whitelisted email"):
            guard.check_create_customer("random@example.com")

    def test_dev_mode_rejects_empty_email(self):
        guard = SafetyGuard(mode="dev")
        with pytest.raises(SafetyGuardError):
            guard.check_create_customer("")

    def test_prod_mode_allows_any_email(self):
        guard = SafetyGuard(mode="prod")
        guard.check_create_customer("random@example.com")  # no raise in prod

    def test_dev_mode_allows_whitelisted_class(self):
        guard = SafetyGuard(mode="dev")
        guard.check_booking_target(TEST_CLASS, TEST_START_DT)  # should not raise

    def test_dev_mode_rejects_wrong_class_name(self):
        guard = SafetyGuard(mode="dev")
        with pytest.raises(SafetyGuardError, match="non-whitelisted class"):
            guard.check_booking_target("Yoga Flow", TEST_START_DT)

    def test_dev_mode_rejects_wrong_start_dt(self):
        guard = SafetyGuard(mode="dev")
        wrong_dt = datetime(2026, 11, 30, 20, 0, 0, tzinfo=timezone.utc)  # wrong hour
        with pytest.raises(SafetyGuardError, match="non-whitelisted start_dt"):
            guard.check_booking_target(TEST_CLASS, wrong_dt)

    def test_dev_mode_accepts_naive_dt_as_utc(self):
        guard = SafetyGuard(mode="dev")
        naive_dt = TEST_START_DT.replace(tzinfo=None)
        guard.check_booking_target(TEST_CLASS, naive_dt)  # naive → treated as UTC

    def test_prod_mode_allows_any_class(self):
        guard = SafetyGuard(mode="prod")
        guard.check_booking_target("Yoga Flow", datetime(2025, 1, 1, tzinfo=timezone.utc))

    def test_invalid_mode_raises(self):
        with pytest.raises(ValueError, match="must be 'dev' or 'prod'"):
            SafetyGuard(mode="staging")

    def test_idempotency_key_create_customer(self):
        key1 = SafetyGuard.make_create_customer_key("loc-1", "a@b.com")
        key2 = SafetyGuard.make_create_customer_key("loc-1", "a@b.com")
        key3 = SafetyGuard.make_create_customer_key("loc-1", "c@d.com")
        assert key1 == key2  # deterministic
        assert key1 != key3  # different email → different key

    def test_idempotency_key_cancel_booking(self):
        key1 = SafetyGuard.make_cancel_booking_key("booking-123")
        key2 = SafetyGuard.make_cancel_booking_key("booking-123")
        key3 = SafetyGuard.make_cancel_booking_key("booking-456")
        assert key1 == key2
        assert key1 != key3


# ─────────────────────────────────────────────────────────────────────────────
# TestHandlersDryRun
# ─────────────────────────────────────────────────────────────────────────────


class TestHandlersDryRun:
    """All four handlers return 'dry_run' status when dry_run=True."""

    @pytest.mark.asyncio
    async def test_create_customer_dry_run(self):
        from app.writeback.handlers.create_customer import handle_create_customer

        result = await handle_create_customer(
            CUSTOMER_PAYLOAD, "loc-1", dry_run=True, safety_mode="dev"
        )
        assert result["status"] == "dry_run"
        assert "DRY_RUN" in result["customer_id"]

    @pytest.mark.asyncio
    async def test_create_booking_dry_run(self):
        from app.writeback.handlers.create_booking import handle_create_booking

        result = await handle_create_booking(
            BOOKING_PAYLOAD, "loc-1", dry_run=True, safety_mode="dev"
        )
        assert result["status"] == "dry_run"
        assert "DRY_RUN" in result["booking_id"]

    @pytest.mark.asyncio
    async def test_reschedule_booking_dry_run(self):
        from app.writeback.handlers.reschedule_booking import handle_reschedule_booking

        result = await handle_reschedule_booking(
            RESCHEDULE_PAYLOAD, "loc-1", dry_run=True, safety_mode="dev"
        )
        assert result["status"] == "dry_run"
        assert result["booking_id"] == "book-001"

    @pytest.mark.asyncio
    async def test_cancel_booking_dry_run(self):
        from app.writeback.handlers.cancel_booking import handle_cancel_booking

        result = await handle_cancel_booking(
            CANCEL_PAYLOAD, "loc-1", dry_run=True, safety_mode="dev"
        )
        assert result["status"] == "dry_run"
        assert result["booking_id"] == "book-001"

    @pytest.mark.asyncio
    async def test_create_customer_safety_guard_fires(self):
        """Safety guard raises SafetyGuardError before dry-run logic."""
        from app.writeback.handlers.create_customer import handle_create_customer

        bad_payload = {**CUSTOMER_PAYLOAD, "email": "hacker@evil.com"}
        with pytest.raises(SafetyGuardError, match="non-whitelisted email"):
            await handle_create_customer(
                bad_payload, "loc-1", dry_run=True, safety_mode="dev"
            )

    @pytest.mark.asyncio
    async def test_create_booking_safety_guard_fires(self):
        from app.writeback.handlers.create_booking import handle_create_booking

        bad_payload = {**BOOKING_PAYLOAD, "class_name": "Wrong Class"}
        with pytest.raises(SafetyGuardError, match="non-whitelisted class"):
            await handle_create_booking(
                bad_payload, "loc-1", dry_run=True, safety_mode="dev"
            )

    @pytest.mark.asyncio
    async def test_live_path_raises_not_implemented(self):
        """Live path (dry_run=False) raises NotImplementedError until wired."""
        from app.writeback.handlers.create_customer import handle_create_customer

        with pytest.raises(NotImplementedError):
            await handle_create_customer(
                CUSTOMER_PAYLOAD, "loc-1", dry_run=False, safety_mode="dev"
            )

    @pytest.mark.asyncio
    async def test_create_booking_invalid_datetime(self):
        from app.writeback.handlers.create_booking import handle_create_booking

        bad_payload = {**BOOKING_PAYLOAD, "session_datetime": "not-a-date"}
        with pytest.raises(ValueError, match="invalid session_datetime"):
            await handle_create_booking(
                bad_payload, "loc-1", dry_run=True, safety_mode="dev"
            )


# ─────────────────────────────────────────────────────────────────────────────
# TestWritebackJobModel
# ─────────────────────────────────────────────────────────────────────────────


class TestWritebackJobModel:
    @pytest.mark.asyncio
    async def test_insert_and_read(self, db: AsyncSession, location: Location):
        job = _make_job(location.id, "create_customer", CUSTOMER_PAYLOAD)
        db.add(job)
        await db.commit()

        row = (
            await db.execute(select(WritebackJob).where(WritebackJob.id == job.id))
        ).scalar_one()
        assert row.status == "queued"
        assert row.job_type == "create_customer"
        assert row.attempt_count == 0
        assert row.next_retry_at is None
        assert row.payload["email"] == TEST_EMAIL

    @pytest.mark.asyncio
    async def test_idempotency_key_unique_constraint(self, db: AsyncSession, location: Location):
        """Inserting two jobs with the same idempotency key raises IntegrityError."""
        from sqlalchemy.exc import IntegrityError

        job1 = _make_job(location.id, "create_customer", CUSTOMER_PAYLOAD)
        job2 = _make_job(location.id, "create_customer", CUSTOMER_PAYLOAD)
        # They have the same idempotency key
        assert job1.idempotency_key == job2.idempotency_key

        db.add(job1)
        await db.commit()

        db.add(job2)
        with pytest.raises(IntegrityError):
            await db.flush()

    @pytest.mark.asyncio
    async def test_different_jobs_different_keys(self, db: AsyncSession, location: Location):
        job1 = _make_job(location.id, "create_customer", CUSTOMER_PAYLOAD)
        job2 = _make_job(location.id, "create_booking", BOOKING_PAYLOAD)
        assert job1.idempotency_key != job2.idempotency_key


# ─────────────────────────────────────────────────────────────────────────────
# TestExecutorDryRun
# ─────────────────────────────────────────────────────────────────────────────


class TestExecutorDryRun:
    """execute_writeback_job with dry_run=True marks the job succeeded."""

    @pytest.mark.asyncio
    async def test_executes_create_customer_and_marks_succeeded(
        self, db: AsyncSession, factory: async_sessionmaker[AsyncSession], location: Location
    ):
        job = _make_job(location.id, "create_customer", CUSTOMER_PAYLOAD)
        db.add(job)
        await db.commit()

        await execute_writeback_job(
            job.id,
            location.id,
            "create_customer",
            CUSTOMER_PAYLOAD,
            job.idempotency_key,
            0,
            factory,
            dry_run=True,
            safety_mode="dev",
        )

        async with factory() as fresh_db:
            row = (
                await fresh_db.execute(select(WritebackJob).where(WritebackJob.id == job.id))
            ).scalar_one()
        assert row.status == "succeeded"
        assert row.error is None

    @pytest.mark.asyncio
    async def test_executes_create_booking_dry_run(
        self, db: AsyncSession, factory: async_sessionmaker[AsyncSession], location: Location
    ):
        job = _make_job(location.id, "create_booking", BOOKING_PAYLOAD)
        db.add(job)
        await db.commit()

        await execute_writeback_job(
            job.id, location.id, "create_booking", BOOKING_PAYLOAD,
            job.idempotency_key, 0, factory, dry_run=True, safety_mode="dev",
        )

        async with factory() as fresh_db:
            row = (await fresh_db.execute(select(WritebackJob).where(WritebackJob.id == job.id))).scalar_one()
        assert row.status == "succeeded"

    @pytest.mark.asyncio
    async def test_executes_reschedule_booking_dry_run(
        self, db: AsyncSession, factory: async_sessionmaker[AsyncSession], location: Location
    ):
        job = _make_job(location.id, "reschedule_booking", RESCHEDULE_PAYLOAD)
        db.add(job)
        await db.commit()

        await execute_writeback_job(
            job.id, location.id, "reschedule_booking", RESCHEDULE_PAYLOAD,
            job.idempotency_key, 0, factory, dry_run=True, safety_mode="dev",
        )

        async with factory() as fresh_db:
            row = (await fresh_db.execute(select(WritebackJob).where(WritebackJob.id == job.id))).scalar_one()
        assert row.status == "succeeded"

    @pytest.mark.asyncio
    async def test_executes_cancel_booking_dry_run(
        self, db: AsyncSession, factory: async_sessionmaker[AsyncSession], location: Location
    ):
        job = _make_job(location.id, "cancel_booking", CANCEL_PAYLOAD)
        db.add(job)
        await db.commit()

        await execute_writeback_job(
            job.id, location.id, "cancel_booking", CANCEL_PAYLOAD,
            job.idempotency_key, 0, factory, dry_run=True, safety_mode="dev",
        )

        async with factory() as fresh_db:
            row = (await fresh_db.execute(select(WritebackJob).where(WritebackJob.id == job.id))).scalar_one()
        assert row.status == "succeeded"

    @pytest.mark.asyncio
    async def test_safety_guard_violation_marks_dead_immediately(
        self, db: AsyncSession, factory: async_sessionmaker[AsyncSession], location: Location
    ):
        """SafetyGuardError → job goes straight to dead with no retry."""
        bad_payload = {**CUSTOMER_PAYLOAD, "email": "bad@actor.com"}
        guard = SafetyGuard(mode="prod")  # use prod for key derivation
        idem_key = guard.make_create_customer_key(str(location.id), "bad@actor.com")
        job = WritebackJob(
            id=uuid.uuid4(),
            location_id=location.id,
            job_type="create_customer",
            payload=bad_payload,
            idempotency_key=idem_key,
        )
        db.add(job)
        await db.commit()

        await execute_writeback_job(
            job.id, location.id, "create_customer", bad_payload,
            idem_key, 0, factory, dry_run=True, safety_mode="dev",
        )

        async with factory() as fresh_db:
            row = (await fresh_db.execute(select(WritebackJob).where(WritebackJob.id == job.id))).scalar_one()
        # Safety violation → dead immediately, no retry
        assert row.status == "dead"
        assert "SafetyGuard" in (row.error or "")


# ─────────────────────────────────────────────────────────────────────────────
# TestRetryPolicy
# ─────────────────────────────────────────────────────────────────────────────


class TestRetryPolicy:
    @pytest.mark.asyncio
    async def test_first_failure_marks_failed_with_retry(
        self, db: AsyncSession, factory: async_sessionmaker[AsyncSession], location: Location
    ):
        """Attempt 1 failure → status='failed', next_retry_at set."""
        job = _make_job(location.id, "create_customer", CUSTOMER_PAYLOAD)
        db.add(job)
        await db.commit()

        new_status = await _mark_failed_or_dead(factory, job.id, 0, "timeout")

        assert new_status == "failed"
        async with factory() as fresh_db:
            row = (await fresh_db.execute(select(WritebackJob).where(WritebackJob.id == job.id))).scalar_one()
        assert row.status == "failed"
        assert row.attempt_count == 1
        assert row.next_retry_at is not None
        # next_retry_at should be approximately 30 seconds from now.
        # SQLite returns timezone-naive datetimes; normalise before comparing.
        retry_at = row.next_retry_at
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        delay = (retry_at - datetime.now(timezone.utc)).total_seconds()
        assert 20 <= delay <= 40, f"Expected ~30s delay, got {delay:.1f}s"

    @pytest.mark.asyncio
    async def test_second_failure_marks_failed_with_longer_retry(
        self, db: AsyncSession, factory: async_sessionmaker[AsyncSession], location: Location
    ):
        """Attempt 2 failure → next_retry_at ~2 minutes."""
        job = _make_job(location.id, "create_customer", CUSTOMER_PAYLOAD, attempt_count=1)
        db.add(job)
        await db.commit()

        await _mark_failed_or_dead(factory, job.id, 1, "timeout")

        async with factory() as fresh_db:
            row = (await fresh_db.execute(select(WritebackJob).where(WritebackJob.id == job.id))).scalar_one()
        assert row.status == "failed"
        assert row.attempt_count == 2
        # ~2 minutes (120s). SQLite returns timezone-naive; normalise.
        retry_at = row.next_retry_at
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        delay = (retry_at - datetime.now(timezone.utc)).total_seconds()
        assert 100 <= delay <= 140, f"Expected ~120s delay, got {delay:.1f}s"

    @pytest.mark.asyncio
    async def test_third_failure_marks_dead(
        self, db: AsyncSession, factory: async_sessionmaker[AsyncSession], location: Location
    ):
        """Attempt 3 failure → status='dead', no next_retry_at."""
        job = _make_job(location.id, "create_customer", CUSTOMER_PAYLOAD, attempt_count=2)
        db.add(job)
        await db.commit()

        new_status = await _mark_failed_or_dead(factory, job.id, 2, "final failure")

        assert new_status == "dead"
        async with factory() as fresh_db:
            row = (await fresh_db.execute(select(WritebackJob).where(WritebackJob.id == job.id))).scalar_one()
        assert row.status == "dead"
        assert row.attempt_count == MAX_ATTEMPTS
        assert row.next_retry_at is None
        assert row.error == "final failure"

    @pytest.mark.asyncio
    async def test_max_attempts_constant(self):
        assert MAX_ATTEMPTS == 3

    @pytest.mark.asyncio
    async def test_retry_delays_sequence(self):
        """Verify retry delay constants match spec: 30s, 2min, 10min."""
        assert RETRY_DELAYS[0] == 30
        assert RETRY_DELAYS[1] == 120
        assert RETRY_DELAYS[2] == 600

    @pytest.mark.asyncio
    async def test_mark_succeeded(
        self, db: AsyncSession, factory: async_sessionmaker[AsyncSession], location: Location
    ):
        job = _make_job(location.id, "cancel_booking", CANCEL_PAYLOAD)
        db.add(job)
        await db.commit()

        await _mark_succeeded(factory, job.id)

        async with factory() as fresh_db:
            row = (await fresh_db.execute(select(WritebackJob).where(WritebackJob.id == job.id))).scalar_one()
        assert row.status == "succeeded"
        assert row.completed_at is not None
        assert row.error is None


# ─────────────────────────────────────────────────────────────────────────────
# TestJobClaiming
# ─────────────────────────────────────────────────────────────────────────────


class TestJobClaiming:
    @pytest.mark.asyncio
    async def test_claim_queued_job(
        self, db: AsyncSession, factory: async_sessionmaker[AsyncSession], location: Location
    ):
        job = _make_job(location.id, "create_customer", CUSTOMER_PAYLOAD)
        db.add(job)
        await db.commit()

        claimed = await _claim_next_job(factory)
        assert claimed is not None
        job_id, loc_id, job_type, payload, idem_key, attempt = claimed
        assert job_id == job.id
        assert job_type == "create_customer"

    @pytest.mark.asyncio
    async def test_does_not_claim_future_retry(
        self, db: AsyncSession, factory: async_sessionmaker[AsyncSession], location: Location
    ):
        """A failed job with next_retry_at in the future is not claimed."""
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        job = _make_job(
            location.id, "create_customer", CUSTOMER_PAYLOAD,
            status="failed", next_retry_at=future,
        )
        db.add(job)
        await db.commit()

        claimed = await _claim_next_job(factory)
        assert claimed is None

    @pytest.mark.asyncio
    async def test_claims_past_due_retry(
        self, db: AsyncSession, factory: async_sessionmaker[AsyncSession], location: Location
    ):
        """A failed job with next_retry_at in the past is claimable."""
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        job = _make_job(
            location.id, "create_customer", CUSTOMER_PAYLOAD,
            status="failed", next_retry_at=past,
        )
        db.add(job)
        await db.commit()

        claimed = await _claim_next_job(factory)
        assert claimed is not None
        assert claimed[0] == job.id

    @pytest.mark.asyncio
    async def test_does_not_claim_succeeded_or_dead(
        self, db: AsyncSession, factory: async_sessionmaker[AsyncSession], location: Location
    ):
        for status in ("succeeded", "dead", "running"):
            guard = SafetyGuard(mode="prod")
            idem = guard.make_create_customer_key(str(location.id), f"test+{status}@test.com")
            job = WritebackJob(
                id=uuid.uuid4(),
                location_id=location.id,
                job_type="create_customer",
                payload={**CUSTOMER_PAYLOAD, "email": f"test+{status}@test.com"},
                idempotency_key=idem,
                status=status,
            )
            db.add(job)
        await db.commit()

        claimed = await _claim_next_job(factory)
        assert claimed is None

    @pytest.mark.asyncio
    async def test_empty_queue_returns_none(
        self, factory: async_sessionmaker[AsyncSession]
    ):
        claimed = await _claim_next_job(factory)
        assert claimed is None


# ─────────────────────────────────────────────────────────────────────────────
# TestAuditLog
# ─────────────────────────────────────────────────────────────────────────────


class TestAuditLog:
    @pytest.mark.asyncio
    async def test_audit_log_written_on_live_success(self, tmp_path: Path):
        """record_writeback writes a JSON line to the audit log."""
        from app.writeback.audit import record_writeback  # noqa: PLC0415
        import app.writeback.audit as audit_module  # noqa: PLC0415

        audit_log = tmp_path / "test_audit.log"
        orig_path = audit_module.AUDIT_LOG_PATH
        audit_module.AUDIT_LOG_PATH = audit_log

        try:
            with patch(
                "app.config.get_settings",
                return_value=_mock_settings(
                    notification_owner_email=None,  # skip email
                    notification_smtp_host=None,
                ),
            ):
                await record_writeback(
                    action="create_booking",
                    customer_email=TEST_EMAIL,
                    class_name=TEST_CLASS,
                    start_dt=TEST_START_DT,
                    idempotency_key="test-key-123",
                    eversports_response={"booking_id": "B001"},
                    ghl_webhook_fired="writeback-success",
                )
        finally:
            audit_module.AUDIT_LOG_PATH = orig_path

        assert audit_log.exists()
        import json
        lines = audit_log.read_text().strip().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["action"] == "create_booking"
        assert entry["customer_email"] == TEST_EMAIL
        assert entry["class_name"] == TEST_CLASS
        assert entry["idempotency_key"] == "test-key-123"

    @pytest.mark.asyncio
    async def test_audit_log_failure_raises_audit_error(self, tmp_path: Path):
        """If audit log cannot be written, AuditError is raised."""
        from app.writeback.audit import AuditError, record_writeback  # noqa: PLC0415
        import app.writeback.audit as audit_module  # noqa: PLC0415

        # Point to an unwritable path (directory instead of file)
        audit_module_path_orig = audit_module.AUDIT_LOG_PATH
        audit_module.AUDIT_LOG_PATH = tmp_path / "subdir_that_is_actually_a_dir" / "audit.log"
        # Make the parent a directory where the log name would conflict
        bad_dir = tmp_path / "subdir_that_is_actually_a_dir"
        bad_dir.mkdir()
        # Make a file where AUDIT_LOG_PATH points (so open() fails as it's a dir)
        (bad_dir / "audit.log").mkdir()  # directory, not file — open() will fail

        try:
            with patch(
                "app.config.get_settings",
                return_value=_mock_settings(
                    notification_owner_email=None,
                    notification_smtp_host=None,
                ),
            ):
                with pytest.raises(AuditError, match="Failed to write audit log"):
                    await record_writeback(
                        action="create_booking",
                        customer_email=TEST_EMAIL,
                        class_name=TEST_CLASS,
                        start_dt=TEST_START_DT,
                        idempotency_key="key-fail",
                        eversports_response={},
                        ghl_webhook_fired=None,
                    )
        finally:
            audit_module.AUDIT_LOG_PATH = audit_module_path_orig

    @pytest.mark.asyncio
    async def test_teardown_record_written(self, tmp_path: Path):
        """record_teardown appends a teardown entry to the audit log."""
        from app.writeback.audit import record_teardown  # noqa: PLC0415
        import app.writeback.audit as audit_module  # noqa: PLC0415
        import json

        audit_log = tmp_path / "teardown_audit.log"
        orig_path = audit_module.AUDIT_LOG_PATH
        audit_module.AUDIT_LOG_PATH = audit_log

        try:
            await record_teardown(
                action="cancel_booking",
                booking_id="BOOK-001",
                customer_email=TEST_EMAIL,
            )
        finally:
            audit_module.AUDIT_LOG_PATH = orig_path

        lines = audit_log.read_text().strip().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["action"] == "teardown:cancel_booking"
        assert entry["booking_id"] == "BOOK-001"
        assert entry["customer_email"] == TEST_EMAIL


# ─────────────────────────────────────────────────────────────────────────────
# TestExecutorWithRetryIntegration
# ─────────────────────────────────────────────────────────────────────────────


class TestExecutorWithRetryIntegration:
    """Integration tests: executor runs through failure → retry → dead lifecycle."""

    @pytest.mark.asyncio
    async def test_handler_error_marks_failed_first_attempt(
        self, db: AsyncSession, factory: async_sessionmaker[AsyncSession], location: Location
    ):
        """If handler raises a generic exception, job is marked 'failed' (not dead)."""
        job = _make_job(location.id, "create_customer", CUSTOMER_PAYLOAD)
        db.add(job)
        await db.commit()

        with patch(
            "app.writeback.executor._load_handler",
            return_value=AsyncMock(side_effect=RuntimeError("Playwright crash")),
        ):
            await execute_writeback_job(
                job.id, location.id, "create_customer", CUSTOMER_PAYLOAD,
                job.idempotency_key, 0, factory, dry_run=True, safety_mode="dev",
            )

        async with factory() as fresh_db:
            row = (await fresh_db.execute(select(WritebackJob).where(WritebackJob.id == job.id))).scalar_one()
        assert row.status == "failed"
        assert row.attempt_count == 1
        assert "RuntimeError" in (row.error or "")

    @pytest.mark.asyncio
    async def test_handler_error_after_max_attempts_marks_dead(
        self, db: AsyncSession, factory: async_sessionmaker[AsyncSession], location: Location
    ):
        """After MAX_ATTEMPTS failures, job is marked 'dead'."""
        job = _make_job(
            location.id, "create_customer", CUSTOMER_PAYLOAD,
            attempt_count=MAX_ATTEMPTS - 1,  # already at limit
        )
        db.add(job)
        await db.commit()

        with patch(
            "app.writeback.executor._load_handler",
            return_value=AsyncMock(side_effect=RuntimeError("Final crash")),
        ):
            await execute_writeback_job(
                job.id, location.id, "create_customer", CUSTOMER_PAYLOAD,
                job.idempotency_key, MAX_ATTEMPTS - 1, factory,
                dry_run=True, safety_mode="dev",
            )

        async with factory() as fresh_db:
            row = (await fresh_db.execute(select(WritebackJob).where(WritebackJob.id == job.id))).scalar_one()
        assert row.status == "dead"
        assert row.attempt_count == MAX_ATTEMPTS

    @pytest.mark.asyncio
    async def test_ghl_success_webhook_fired_on_live_success(
        self, db: AsyncSession, factory: async_sessionmaker[AsyncSession], location: Location
    ):
        """GHL success webhook is fired after a live (non-dry-run) success."""
        job = _make_job(location.id, "create_customer", CUSTOMER_PAYLOAD)
        db.add(job)
        await db.commit()

        webhook_calls: list[str] = []

        async def fake_fire_webhook(url, payload, label):
            if url:
                webhook_calls.append(label)
            return label if url else None

        import app.writeback.audit as audit_module
        import app.writeback.executor as executor_module

        orig_fire = executor_module._fire_ghl_webhook
        orig_record = audit_module.record_writeback

        async def fake_record(*args, **kwargs):
            pass  # skip audit log in this test

        executor_module._fire_ghl_webhook = fake_fire_webhook
        audit_module.record_writeback = fake_record

        try:
            with patch(
                "app.writeback.executor._load_handler",
                return_value=AsyncMock(return_value={"customer_id": "C001", "status": "dry_run"}),
            ):
                await execute_writeback_job(
                    job.id, location.id, "create_customer", CUSTOMER_PAYLOAD,
                    job.idempotency_key, 0, factory,
                    dry_run=False,  # Live mode
                    safety_mode="prod",  # prod to bypass safety guard
                    ghl_success_webhook_url="https://hooks.ghl.test/success",
                )
        finally:
            executor_module._fire_ghl_webhook = orig_fire
            audit_module.record_writeback = orig_record

        assert "writeback-success" in webhook_calls

    @pytest.mark.asyncio
    async def test_ghl_failure_webhook_fired_on_dead(
        self, db: AsyncSession, factory: async_sessionmaker[AsyncSession], location: Location
    ):
        """GHL failure webhook is fired when a job goes dead."""
        job = _make_job(location.id, "create_customer", CUSTOMER_PAYLOAD, attempt_count=MAX_ATTEMPTS - 1)
        db.add(job)
        await db.commit()

        webhook_calls: list[str] = []

        import app.writeback.executor as executor_module
        orig_fire = executor_module._fire_ghl_webhook

        async def fake_fire(url, payload, label):
            if url:
                webhook_calls.append(label)
            return label if url else None

        executor_module._fire_ghl_webhook = fake_fire

        try:
            with patch(
                "app.writeback.executor._load_handler",
                return_value=AsyncMock(side_effect=RuntimeError("Final failure")),
            ):
                await execute_writeback_job(
                    job.id, location.id, "create_customer", CUSTOMER_PAYLOAD,
                    job.idempotency_key, MAX_ATTEMPTS - 1, factory,
                    dry_run=True, safety_mode="dev",
                    ghl_failure_webhook_url="https://hooks.ghl.test/failure",
                )
        finally:
            executor_module._fire_ghl_webhook = orig_fire

        assert "writeback-failed" in webhook_calls


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _mock_settings(**overrides):
    """Return a minimal Settings-like object for patching get_settings()."""
    from app.config import Settings

    defaults = {
        "writeback_safety_mode": "dev",
        "writeback_dry_run": True,
        "notification_owner_email": None,
        "notification_smtp_host": None,
        "notification_smtp_port": 587,
        "notification_smtp_user": None,
        "notification_smtp_password": None,
        "notification_from_email": "noreply@test.local",
    }
    defaults.update(overrides)

    class FakeSettings:
        def __getattr__(self, name):
            if name in defaults:
                return defaults[name]
            raise AttributeError(f"FakeSettings has no attribute '{name}'")

    return FakeSettings()
