---
name: project-m4-complete
description: M4 complete — event-driven scheduler; next is M5 (writeback executor)
metadata:
  type: project
---

M4 (event-driven scheduler) merged to master on 2026-05-28 (commit b533a4c).

## What was built

**New files:**
- `app/db/models/scheduler_job.py` — `SchedulerJob` model; `scheduler_jobs` table
- `alembic/versions/g2h3i4j5k6l7_m4_scheduler_jobs.py` — migration (down_revision: f1a2b3c4d5e6)
- `app/scheduler/orchestrator.py` — `compute_daily_schedule`, `enqueue_event_driven_jobs`, `enqueue_hourly_catchup`, `enqueue_overnight`
- `app/scheduler/worker.py` — `execute_job`, `run_worker` (polling loop, semaphore, graceful shutdown)
- `app/scheduler/cron.py` — APScheduler 3.x `AsyncIOScheduler`; 3 cron jobs
- `app/api/v1/admin/scheduler.py` — status/jobs/trigger/cancel-pending endpoints
- `tests/test_scheduler.py` — 37 tests, all passing

**Modified:**
- `app/main.py` — lifespan starts worker task + APScheduler; graceful stop
- `pyproject.toml` — added `apscheduler>=3.10,<4`

## Key design decisions
- SQLite-compatible: no `SELECT FOR UPDATE SKIP LOCKED` in worker (single-process safe; comment notes the Postgres upgrade path)
- Idempotency buckets: ±5 min for event_driven/hourly_catchup; ±30 min for overnight
- `enqueue_overnight` uses `run_type='historical_backfill'` when `historical_sync_flag != 'complete'`
- `execute_job` does the import of `run_sync` lazily inside the function to avoid circular imports
- APScheduler fires globally at UTC times; per-location timezone filtering happens in the orchestrator

## Acceptance criterion met
> "scheduler enqueues sync runs at +15min after each class-end on the test schedule; jobs execute in order."
Covered by `TestScheduleRoundTrip.test_enqueues_at_class_end_plus_15_and_executes_in_order`.

## Migration chain
d3f8b2a4c1e9 → d4e5f6a7b8c9 → c3d4e5f6a7b8 → a1b2c3d4e5f6 → e5f6a7b8c9d0 → f1a2b3c4d5e6 → g2h3i4j5k6l7

## What's next
M5: Writeback executor (Eversports writeback for reschedule/cancellation actions from GHL).
Use the `eversports-scraper-specialist` agent.
