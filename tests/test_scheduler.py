"""
Tests for the M4 event-driven scheduler stack.

All tests use an in-memory SQLite database — no live Postgres or running
scheduler required.

Covers:
  - app/scheduler/orchestrator.py  — compute_daily_schedule, enqueue_*
  - app/scheduler/worker.py        — execute_job (run_sync mocked)
  - app/db/models/scheduler_job.py — SchedulerJob model

M4 acceptance criterion:
  scheduler enqueues sync runs at +15 min after each class-end on the test
  schedule; jobs execute in order.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models.base import Base
from app.db.models.location import Location
from app.db.models.scheduler_job import SchedulerJob
from app.db.models.sessions import Session as EversportsSession
from app.scheduler.orchestrator import (
    CLASS_END_OFFSET_MINUTES,
    CATCHUP_END_HOUR,
    CATCHUP_START_HOUR,
    compute_daily_schedule,
    enqueue_event_driven_jobs,
    enqueue_hourly_catchup,
    enqueue_overnight,
)
from app.scheduler.worker import execute_job, recover_stuck_jobs


# ── DB fixtures ────────────────────────────────────────────────────────────────


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
    """Insert a minimal Location with Europe/Vienna timezone."""
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
        historical_sync_flag="pending",
    )
    db.add(loc)
    await db.flush()
    return loc


# ── Helpers ────────────────────────────────────────────────────────────────────


def _utc(hour: int, minute: int = 0, day_offset: int = 0) -> datetime:
    """Return a UTC datetime for today (+ day_offset days) at HH:MM."""
    base = datetime.now(timezone.utc).replace(hour=hour, minute=minute, second=0, microsecond=0)
    return base + timedelta(days=day_offset)


def _make_session(
    location_id: uuid.UUID,
    end_hour: int,
    end_minute: int = 0,
    day_offset: int = 0,
) -> EversportsSession:
    """Build an EversportsSession ending at the given UTC hour/minute."""
    end = _utc(end_hour, end_minute, day_offset=day_offset)
    start = end - timedelta(hours=1)
    return EversportsSession(
        id=uuid.uuid4(),
        location_id=location_id,
        start_time=start,
        end_time=end,
        activity_name="Yoga",
    )


# ══════════════════════════════════════════════════════════════════════════════
#  compute_daily_schedule
# ══════════════════════════════════════════════════════════════════════════════


class TestComputeDailySchedule:
    async def test_empty_returns_empty_list(self, db: AsyncSession, location: Location):
        """No sessions → no scheduled times."""
        result = await compute_daily_schedule(location.id, db)
        assert result == []

    async def test_single_session_offset(self, db: AsyncSession, location: Location):
        """One session ending at 10:00 → one job at 10:15."""
        db.add(_make_session(location.id, end_hour=10))
        await db.flush()
        result = await compute_daily_schedule(location.id, db)
        assert len(result) == 1
        expected = _utc(10) + timedelta(minutes=CLASS_END_OFFSET_MINUTES)
        assert result[0] == expected

    async def test_deduplicates_same_end_time(self, db: AsyncSession, location: Location):
        """Three sessions ending at 11:00 → one job (not three)."""
        for _ in range(3):
            db.add(_make_session(location.id, end_hour=11))
        await db.flush()
        result = await compute_daily_schedule(location.id, db)
        assert len(result) == 1

    async def test_multiple_distinct_end_times(self, db: AsyncSession, location: Location):
        """Two different end times → two jobs."""
        db.add(_make_session(location.id, end_hour=9))
        db.add(_make_session(location.id, end_hour=17))
        await db.flush()
        result = await compute_daily_schedule(location.id, db)
        assert len(result) == 2

    async def test_result_is_sorted(self, db: AsyncSession, location: Location):
        """Output is sorted ascending by scheduled_at."""
        db.add(_make_session(location.id, end_hour=18))
        db.add(_make_session(location.id, end_hour=8))
        db.add(_make_session(location.id, end_hour=12))
        await db.flush()
        result = await compute_daily_schedule(location.id, db)
        assert result == sorted(result)

    async def test_filters_by_target_date(self, db: AsyncSession, location: Location):
        """Sessions on a different day are excluded."""
        today = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)
        tomorrow_end = today + timedelta(days=1)
        tomorrow_session = EversportsSession(
            id=uuid.uuid4(),
            location_id=location.id,
            start_time=tomorrow_end - timedelta(hours=1),
            end_time=tomorrow_end,
            activity_name="Yoga",
        )
        db.add(tomorrow_session)
        await db.flush()
        # Pass today as target_date → tomorrow's session should be excluded
        result = await compute_daily_schedule(location.id, db, target_date=today)
        assert result == []

    async def test_null_end_times_are_ignored(self, db: AsyncSession, location: Location):
        """Sessions with NULL end_time are excluded."""
        session = EversportsSession(
            id=uuid.uuid4(),
            location_id=location.id,
            start_time=_utc(9),
            end_time=None,
            activity_name="Yoga",
        )
        db.add(session)
        await db.flush()
        result = await compute_daily_schedule(location.id, db)
        assert result == []

    async def test_offset_is_class_end_offset_minutes(self, db: AsyncSession, location: Location):
        """Verifies the exact offset constant applied to class end times."""
        db.add(_make_session(location.id, end_hour=14, end_minute=30))
        await db.flush()
        result = await compute_daily_schedule(location.id, db)
        expected_end = _utc(14, 30)
        assert result[0] == expected_end + timedelta(minutes=CLASS_END_OFFSET_MINUTES)

    async def test_isolation_between_locations(self, db: AsyncSession, location: Location):
        """Sessions from a different location are not returned."""
        other_location_id = uuid.uuid4()
        db.add(_make_session(other_location_id, end_hour=10))
        await db.flush()
        result = await compute_daily_schedule(location.id, db)
        assert result == []


# ══════════════════════════════════════════════════════════════════════════════
#  enqueue_event_driven_jobs
# ══════════════════════════════════════════════════════════════════════════════


class TestEnqueueEventDrivenJobs:
    async def test_enqueues_correct_count(self, db: AsyncSession, location: Location):
        """3 distinct class ends → 3 jobs inserted."""
        for h in (9, 11, 17):
            db.add(_make_session(location.id, end_hour=h))
        await db.flush()
        count = await enqueue_event_driven_jobs(location.id, db)
        assert count == 3

    async def test_jobs_are_pending(self, db: AsyncSession, location: Location):
        """New jobs have status='pending'."""
        db.add(_make_session(location.id, end_hour=10))
        await db.flush()
        await enqueue_event_driven_jobs(location.id, db)
        await db.flush()
        jobs = (await db.execute(select(SchedulerJob))).scalars().all()
        assert all(j.status == "pending" for j in jobs)

    async def test_jobs_have_correct_run_type(self, db: AsyncSession, location: Location):
        """Event-driven jobs use run_type='incremental'."""
        db.add(_make_session(location.id, end_hour=10))
        await db.flush()
        await enqueue_event_driven_jobs(location.id, db)
        await db.flush()
        jobs = (await db.execute(select(SchedulerJob))).scalars().all()
        assert all(j.run_type == "incremental" for j in jobs)

    async def test_idempotent_second_call_returns_zero(self, db: AsyncSession, location: Location):
        """Calling twice does not create duplicate jobs."""
        db.add(_make_session(location.id, end_hour=10))
        await db.flush()
        first = await enqueue_event_driven_jobs(location.id, db)
        await db.flush()
        second = await enqueue_event_driven_jobs(location.id, db)
        assert first == 1
        assert second == 0

    async def test_idempotent_db_row_count(self, db: AsyncSession, location: Location):
        """After two calls, only one row exists in scheduler_jobs."""
        db.add(_make_session(location.id, end_hour=12))
        await db.flush()
        await enqueue_event_driven_jobs(location.id, db)
        await db.flush()
        await enqueue_event_driven_jobs(location.id, db)
        await db.flush()
        jobs = (await db.execute(select(SchedulerJob))).scalars().all()
        assert len(jobs) == 1

    async def test_empty_sessions_returns_zero(self, db: AsyncSession, location: Location):
        """No sessions → 0 jobs."""
        count = await enqueue_event_driven_jobs(location.id, db)
        assert count == 0

    async def test_job_type_is_event_driven(self, db: AsyncSession, location: Location):
        """Inserted jobs have job_type='event_driven'."""
        db.add(_make_session(location.id, end_hour=10))
        await db.flush()
        await enqueue_event_driven_jobs(location.id, db)
        await db.flush()
        jobs = (await db.execute(select(SchedulerJob))).scalars().all()
        assert all(j.job_type == "event_driven" for j in jobs)

    async def test_scheduled_at_is_class_end_plus_offset(self, db: AsyncSession, location: Location):
        """scheduled_at equals class-end + CLASS_END_OFFSET_MINUTES."""
        db.add(_make_session(location.id, end_hour=16, end_minute=0))
        await db.flush()
        await enqueue_event_driven_jobs(location.id, db)
        await db.flush()
        jobs = (await db.execute(select(SchedulerJob))).scalars().all()
        expected = _utc(16) + timedelta(minutes=CLASS_END_OFFSET_MINUTES)
        # Normalise DB value (SQLite stores without tzinfo)
        actual = jobs[0].scheduled_at
        if actual.tzinfo is None:
            actual = actual.replace(tzinfo=timezone.utc)
        assert actual == expected


# ══════════════════════════════════════════════════════════════════════════════
#  enqueue_hourly_catchup
# ══════════════════════════════════════════════════════════════════════════════


class TestEnqueueHourlyCatchup:
    def _local_hour_to_utc(self, tz_offset_hours: int, local_hour: int) -> datetime:
        """Return a UTC datetime corresponding to local_hour in a UTC+tz_offset timezone."""
        utc_hour = (local_hour - tz_offset_hours) % 24
        return _utc(utc_hour)

    async def test_within_window_enqueues(self, db: AsyncSession, location: Location):
        """12:00 local Vienna (UTC+1 in winter / UTC+2 in summer) → within window → enqueues."""
        # Use a UTC time that maps to 12:00 in any European timezone
        at = _utc(10)  # UTC 10:00 = 11:00 or 12:00 Vienna — always within 07–22 window
        result = await enqueue_hourly_catchup(location.id, db, at=at)
        assert result is True

    async def test_within_window_inserts_job(self, db: AsyncSession, location: Location):
        """A job row is created in the DB when within the window."""
        at = _utc(10)
        await enqueue_hourly_catchup(location.id, db, at=at)
        await db.flush()
        jobs = (await db.execute(select(SchedulerJob))).scalars().all()
        assert len(jobs) == 1
        assert jobs[0].job_type == "hourly_catchup"

    async def test_outside_window_early_skips(self, db: AsyncSession, location: Location):
        """UTC 02:00 = 03:00 Vienna — before the 07:00 window start → skip."""
        at = _utc(2)  # 02:00 UTC = 03:00 Vienna — below CATCHUP_START_HOUR
        result = await enqueue_hourly_catchup(location.id, db, at=at)
        assert result is False

    async def test_outside_window_late_skips(self, db: AsyncSession, location: Location):
        """UTC 22:00 = 23:00 Vienna — past the 22:00 window end → skip."""
        at = _utc(22)  # 22:00 UTC = 23:00 Vienna — above CATCHUP_END_HOUR
        result = await enqueue_hourly_catchup(location.id, db, at=at)
        assert result is False

    async def test_idempotent_same_slot(self, db: AsyncSession, location: Location):
        """Two calls for the same hour slot → only one job inserted."""
        at = _utc(10)
        first = await enqueue_hourly_catchup(location.id, db, at=at)
        await db.flush()
        second = await enqueue_hourly_catchup(location.id, db, at=at)
        assert first is True
        assert second is False

    async def test_run_type_is_incremental(self, db: AsyncSession, location: Location):
        """Hourly catchup jobs always use run_type='incremental'."""
        await enqueue_hourly_catchup(location.id, db, at=_utc(10))
        await db.flush()
        jobs = (await db.execute(select(SchedulerJob))).scalars().all()
        assert jobs[0].run_type == "incremental"

    async def test_different_hours_create_separate_jobs(self, db: AsyncSession, location: Location):
        """Two different hours → two separate jobs."""
        await enqueue_hourly_catchup(location.id, db, at=_utc(10))
        await db.flush()
        await enqueue_hourly_catchup(location.id, db, at=_utc(11))
        await db.flush()
        jobs = (await db.execute(select(SchedulerJob))).scalars().all()
        assert len(jobs) == 2

    async def test_default_at_is_current_whole_hour(self, db: AsyncSession, location: Location):
        """
        at=None default should schedule for the current UTC whole hour — not
        the next hour.  This ensures cron-triggered catchups run immediately,
        not with a 1-hour delay.
        """
        from unittest.mock import patch  # noqa: PLC0415
        from datetime import timezone  # noqa: PLC0415

        # Fix 'now' to 10:45:30 UTC so the expected `at` is 10:00:00 UTC.
        frozen_now = datetime(2026, 5, 28, 10, 45, 30, tzinfo=timezone.utc)
        with patch("app.scheduler.orchestrator._now_utc", return_value=frozen_now):
            result = await enqueue_hourly_catchup(location.id, db)

        await db.flush()
        assert result is True
        jobs = (await db.execute(select(SchedulerJob))).scalars().all()
        scheduled = jobs[0].scheduled_at
        if scheduled.tzinfo is None:
            scheduled = scheduled.replace(tzinfo=timezone.utc)
        assert scheduled.hour == 10
        assert scheduled.minute == 0
        assert scheduled.second == 0


# ══════════════════════════════════════════════════════════════════════════════
#  enqueue_overnight
# ══════════════════════════════════════════════════════════════════════════════


class TestEnqueueOvernight:
    async def test_historical_run_type_when_pending(self, db: AsyncSession, location: Location):
        """historical_sync_flag='pending' → run_type='historical_backfill'."""
        assert location.historical_sync_flag == "pending"
        at = _utc(2)  # arbitrary UTC time (03:00 Vienna area)
        await enqueue_overnight(location.id, db, at=at)
        await db.flush()
        jobs = (await db.execute(select(SchedulerJob))).scalars().all()
        assert jobs[0].run_type == "historical_backfill"

    async def test_incremental_run_type_when_complete(self, db: AsyncSession, location: Location):
        """historical_sync_flag='complete' → run_type='incremental'."""
        location.historical_sync_flag = "complete"
        await db.flush()
        at = _utc(2)
        await enqueue_overnight(location.id, db, at=at)
        await db.flush()
        jobs = (await db.execute(select(SchedulerJob))).scalars().all()
        assert jobs[0].run_type == "incremental"

    async def test_inserts_job(self, db: AsyncSession, location: Location):
        """A SchedulerJob row is created."""
        at = _utc(2)
        result = await enqueue_overnight(location.id, db, at=at)
        await db.flush()
        assert result is True
        jobs = (await db.execute(select(SchedulerJob))).scalars().all()
        assert len(jobs) == 1
        assert jobs[0].job_type == "overnight"

    async def test_idempotent_30min_bucket(self, db: AsyncSession, location: Location):
        """Two calls within 30 minutes → one job (overnight uses a 30-min bucket)."""
        at1 = _utc(2, 0)
        at2 = _utc(2, 20)  # 20 minutes later — within the ±30 min bucket
        first = await enqueue_overnight(location.id, db, at=at1)
        await db.flush()
        second = await enqueue_overnight(location.id, db, at=at2)
        assert first is True
        assert second is False

    async def test_outside_bucket_creates_new_job(self, db: AsyncSession, location: Location):
        """Two calls 2 hours apart → two jobs (outside the 30-min bucket)."""
        at1 = _utc(2)
        at2 = _utc(4)  # 2 hours later — outside the ±30 min bucket
        await enqueue_overnight(location.id, db, at=at1)
        await db.flush()
        await enqueue_overnight(location.id, db, at=at2)
        await db.flush()
        jobs = (await db.execute(select(SchedulerJob))).scalars().all()
        assert len(jobs) == 2

    async def test_location_not_found_returns_false(self, db: AsyncSession):
        """Nonexistent location_id → False, no exception."""
        result = await enqueue_overnight(uuid.uuid4(), db, at=_utc(2))
        assert result is False

    async def test_default_at_computes_next_3am_local(self, db: AsyncSession, location: Location):
        """
        When at=None, the scheduled_at should be within 24 hours from now
        and correspond to 03:00 in Europe/Vienna timezone.
        """
        from zoneinfo import ZoneInfo  # noqa: PLC0415

        result = await enqueue_overnight(location.id, db)
        await db.flush()
        assert result is True
        jobs = (await db.execute(select(SchedulerJob))).scalars().all()
        scheduled = jobs[0].scheduled_at
        if scheduled.tzinfo is None:
            scheduled = scheduled.replace(tzinfo=timezone.utc)
        local = scheduled.astimezone(ZoneInfo("Europe/Vienna"))
        assert local.hour == 3
        assert local.minute == 0


# ══════════════════════════════════════════════════════════════════════════════
#  execute_job (worker)
# ══════════════════════════════════════════════════════════════════════════════


class TestExecuteJob:
    """Tests for the worker's execute_job function."""

    async def _get_job(self, factory, job_id: uuid.UUID) -> SchedulerJob:
        async with factory() as db:
            result = await db.execute(
                select(SchedulerJob).where(SchedulerJob.id == job_id)
            )
            return result.scalar_one()

    async def test_marks_done_on_success(
        self, factory: async_sessionmaker[AsyncSession], location: Location
    ):
        """Successful run_sync → job status becomes 'done'."""
        async with factory() as db:
            job = SchedulerJob(
                id=uuid.uuid4(),
                location_id=location.id,
                job_type="event_driven",
                run_type="incremental",
                scheduled_at=_utc(10),
                status="running",
            )
            db.add(job)
            await db.commit()
            job_id = job.id

        with patch(
            "app.scrapers.sync_runner.run_sync",
            new_callable=AsyncMock,
            return_value={"contacts_seeded": 0, "bookings_seeded": 0, "sessions_seeded": 0},
        ):
            await execute_job(
                job_id=job_id,
                location_id=location.id,
                job_type="event_driven",
                run_type="incremental",
                factory=factory,
            )

        fetched = await self._get_job(factory, job_id)
        assert fetched.status == "done"
        assert fetched.error is None
        assert fetched.completed_at is not None

    async def test_marks_failed_on_session_expired(
        self, factory: async_sessionmaker[AsyncSession], location: Location
    ):
        """SessionExpiredError → status='failed', error captured."""
        from app.scrapers.exceptions import SessionExpiredError  # noqa: PLC0415

        async with factory() as db:
            job = SchedulerJob(
                id=uuid.uuid4(),
                location_id=location.id,
                job_type="hourly_catchup",
                run_type="incremental",
                scheduled_at=_utc(10),
                status="running",
            )
            db.add(job)
            await db.commit()
            job_id = job.id

        with patch(
            "app.scrapers.sync_runner.run_sync",
            new_callable=AsyncMock,
            side_effect=SessionExpiredError("session is expired"),
        ):
            await execute_job(
                job_id=job_id,
                location_id=location.id,
                job_type="hourly_catchup",
                run_type="incremental",
                factory=factory,
            )

        fetched = await self._get_job(factory, job_id)
        assert fetched.status == "failed"
        assert "SessionExpiredError" in (fetched.error or "")

    async def test_marks_failed_on_generic_error(
        self, factory: async_sessionmaker[AsyncSession], location: Location
    ):
        """Any other exception → status='failed', error message stored."""
        async with factory() as db:
            job = SchedulerJob(
                id=uuid.uuid4(),
                location_id=location.id,
                job_type="overnight",
                run_type="incremental",
                scheduled_at=_utc(3),
                status="running",
            )
            db.add(job)
            await db.commit()
            job_id = job.id

        with patch(
            "app.scrapers.sync_runner.run_sync",
            new_callable=AsyncMock,
            side_effect=RuntimeError("scraper exploded"),
        ):
            await execute_job(
                job_id=job_id,
                location_id=location.id,
                job_type="overnight",
                run_type="incremental",
                factory=factory,
            )

        fetched = await self._get_job(factory, job_id)
        assert fetched.status == "failed"
        assert "scraper exploded" in (fetched.error or "")

    async def test_completed_at_set_on_success(
        self, factory: async_sessionmaker[AsyncSession], location: Location
    ):
        """completed_at is populated on success."""
        async with factory() as db:
            job = SchedulerJob(
                id=uuid.uuid4(),
                location_id=location.id,
                job_type="event_driven",
                run_type="incremental",
                scheduled_at=_utc(10),
                status="running",
            )
            db.add(job)
            await db.commit()
            job_id = job.id

        with patch(
            "app.scrapers.sync_runner.run_sync",
            new_callable=AsyncMock,
            return_value={},
        ):
            await execute_job(
                job_id=job_id,
                location_id=location.id,
                job_type="event_driven",
                run_type="incremental",
                factory=factory,
            )

        fetched = await self._get_job(factory, job_id)
        assert fetched.completed_at is not None

    async def test_run_sync_called_with_correct_args(
        self, factory: async_sessionmaker[AsyncSession], location: Location
    ):
        """execute_job passes location_id and run_type to run_sync."""
        async with factory() as db:
            job = SchedulerJob(
                id=uuid.uuid4(),
                location_id=location.id,
                job_type="overnight",
                run_type="historical_backfill",
                scheduled_at=_utc(3),
                status="running",
            )
            db.add(job)
            await db.commit()
            job_id = job.id

        with patch(
            "app.scrapers.sync_runner.run_sync",
            new_callable=AsyncMock,
            return_value={},
        ) as mock_sync:
            await execute_job(
                job_id=job_id,
                location_id=location.id,
                job_type="overnight",
                run_type="historical_backfill",
                factory=factory,
            )

        mock_sync.assert_called_once()
        call_kwargs = mock_sync.call_args.kwargs
        assert call_kwargs["location_id"] == location.id
        assert call_kwargs["run_type"] == "historical_backfill"


# ══════════════════════════════════════════════════════════════════════════════
#  recover_stuck_jobs (worker startup sweep)
# ══════════════════════════════════════════════════════════════════════════════


class TestRecoverStuckJobs:
    """Tests for the zombie-job recovery run at worker startup."""

    async def test_marks_running_jobs_as_failed(
        self, factory: async_sessionmaker[AsyncSession], location: Location
    ):
        """Jobs stuck at 'running' are marked 'failed' with recovery error."""
        async with factory() as db:
            job = SchedulerJob(
                id=uuid.uuid4(),
                location_id=location.id,
                job_type="event_driven",
                run_type="incremental",
                scheduled_at=_utc(10),
                status="running",
            )
            db.add(job)
            await db.commit()
            job_id = job.id

        count = await recover_stuck_jobs(factory)
        assert count == 1

        async with factory() as db:
            result = await db.execute(select(SchedulerJob).where(SchedulerJob.id == job_id))
            fetched = result.scalar_one()
        assert fetched.status == "failed"
        assert fetched.error == "recovered_stale_at_startup"
        assert fetched.completed_at is not None

    async def test_does_not_touch_pending_or_done_jobs(
        self, factory: async_sessionmaker[AsyncSession], location: Location
    ):
        """pending and done jobs are not affected by the recovery sweep."""
        async with factory() as db:
            pending_job = SchedulerJob(
                id=uuid.uuid4(),
                location_id=location.id,
                job_type="hourly_catchup",
                run_type="incremental",
                scheduled_at=_utc(10),
                status="pending",
            )
            done_job = SchedulerJob(
                id=uuid.uuid4(),
                location_id=location.id,
                job_type="overnight",
                run_type="incremental",
                scheduled_at=_utc(3),
                status="done",
            )
            db.add(pending_job)
            db.add(done_job)
            await db.commit()

        count = await recover_stuck_jobs(factory)
        assert count == 0

        async with factory() as db:
            jobs = (await db.execute(select(SchedulerJob))).scalars().all()
        statuses = {j.status for j in jobs}
        assert statuses == {"pending", "done"}

    async def test_returns_zero_when_no_stuck_jobs(
        self, factory: async_sessionmaker[AsyncSession]
    ):
        """Empty scheduler_jobs table → returns 0, no error."""
        count = await recover_stuck_jobs(factory)
        assert count == 0

    async def test_recovers_multiple_stuck_jobs(
        self, factory: async_sessionmaker[AsyncSession], location: Location
    ):
        """Multiple 'running' jobs are all recovered in one call."""
        async with factory() as db:
            for i in range(3):
                db.add(
                    SchedulerJob(
                        id=uuid.uuid4(),
                        location_id=location.id,
                        job_type="event_driven",
                        run_type="incremental",
                        scheduled_at=_utc(9 + i),
                        status="running",
                    )
                )
            await db.commit()

        count = await recover_stuck_jobs(factory)
        assert count == 3

        async with factory() as db:
            jobs = (await db.execute(select(SchedulerJob))).scalars().all()
        assert all(j.status == "failed" for j in jobs)


# ══════════════════════════════════════════════════════════════════════════════
#  Integration: full schedule round-trip
# ══════════════════════════════════════════════════════════════════════════════


class TestScheduleRoundTrip:
    """
    End-to-end test: sessions → enqueue → execute in order.
    (M4 acceptance criterion)
    """

    async def test_enqueues_at_class_end_plus_15_and_executes_in_order(
        self,
        db: AsyncSession,
        factory: async_sessionmaker[AsyncSession],
        location: Location,
    ):
        """
        Given 3 sessions ending at 09:00, 11:00, 17:00 UTC:
          - enqueue_event_driven_jobs creates 3 pending jobs
          - scheduled_at values are 09:15, 11:15, 17:15
          - execute_job runs them in order (oldest first)
          - all end up with status='done'
        """
        for h in (9, 11, 17):
            db.add(_make_session(location.id, end_hour=h))
        await db.flush()

        count = await enqueue_event_driven_jobs(location.id, db)
        await db.flush()
        assert count == 3

        # Verify scheduled_at values
        jobs_stmt = (
            select(SchedulerJob)
            .where(SchedulerJob.location_id == location.id)
            .order_by(SchedulerJob.scheduled_at)
        )
        jobs = (await db.execute(jobs_stmt)).scalars().all()
        assert len(jobs) == 3
        expected_hours = [9, 11, 17]
        for job, exp_h in zip(jobs, expected_hours):
            sa = job.scheduled_at
            if sa.tzinfo is None:
                sa = sa.replace(tzinfo=timezone.utc)
            assert sa.hour == exp_h
            assert sa.minute == CLASS_END_OFFSET_MINUTES

        # Execute all jobs (oldest first) via the worker function.
        # Mark each running via the factory session (not the shared `db` session)
        # so that _mark_job_complete's subsequent update is visible in a fresh read.
        with patch(
            "app.scrapers.sync_runner.run_sync",
            new_callable=AsyncMock,
            return_value={},
        ):
            for job in jobs:
                # Mark running in a separate session (mirrors what _claim_next_job does)
                async with factory() as mark_db:
                    await mark_db.execute(
                        __import__("sqlalchemy", fromlist=["update"]).update(SchedulerJob)
                        .where(SchedulerJob.id == job.id)
                        .values(status="running")
                    )
                    await mark_db.commit()

                await execute_job(
                    job_id=job.id,
                    location_id=location.id,
                    job_type=job.job_type,
                    run_type=job.run_type,
                    factory=factory,
                )

        # Verify all done — use a fresh session to bypass the stale identity map
        async with factory() as fresh_db:
            final_jobs = (await fresh_db.execute(jobs_stmt)).scalars().all()
        statuses = [j.status for j in final_jobs]
        assert statuses == ["done", "done", "done"]
