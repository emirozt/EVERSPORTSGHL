import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import sentry_sdk
from fastapi import FastAPI

from app.api.health import router as health_router
from app.api.v1.admin.bootstrap import router as bootstrap_router
from app.api.v1.admin.consent import router as consent_router
from app.api.v1.admin.gatekeeper import router as gatekeeper_admin_router
from app.api.v1.admin.ghl_oauth import router as ghl_oauth_router
from app.api.v1.admin.scheduler import router as scheduler_router
from app.api.v1.admin.sync import router as sync_router
from app.api.v1.admin.writeback import router as writeback_router
from app.api.v1.webhooks.ghl_inbound import router as ghl_inbound_router
from app.config import get_settings
from app.db.session import get_engine
from app.scheduler.cron import start_scheduler, stop_scheduler
from app.scheduler.worker import run_worker
from app.writeback.executor import run_writeback_worker

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    settings = get_settings()

    logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))

    if settings.sentry_dsn:
        sentry_sdk.init(
            dsn=settings.sentry_dsn,
            environment=settings.env,
            traces_sample_rate=0.1,
        )
        logger.info("Sentry initialised (env=%s)", settings.env)

    engine = get_engine()
    logger.info("Database engine initialised")

    # ── M4: start scheduler and worker ────────────────────────────────────────
    stop_event = asyncio.Event()
    worker_task = asyncio.create_task(run_worker(stop_event=stop_event))
    start_scheduler()
    logger.info("Scheduler and worker started")

    # ── M5: start writeback executor ─────────────────────────────────────────
    writeback_task = asyncio.create_task(run_writeback_worker(stop_event=stop_event))
    logger.info("Writeback executor started")

    yield

    # ── Graceful shutdown ─────────────────────────────────────────────────────
    stop_event.set()
    stop_scheduler()
    try:
        await asyncio.wait_for(worker_task, timeout=30)
    except asyncio.TimeoutError:
        logger.warning("Worker did not stop within 30s — cancelling")
        worker_task.cancel()
    try:
        await asyncio.wait_for(writeback_task, timeout=30)
    except asyncio.TimeoutError:
        logger.warning("Writeback worker did not stop within 30s — cancelling")
        writeback_task.cancel()

    await engine.dispose()
    logger.info("Database engine disposed")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Eversports × GHL Connector",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )
    app.include_router(health_router, prefix="/api/v1")
    app.include_router(bootstrap_router, prefix="/api/v1/admin")
    app.include_router(sync_router, prefix="/api/v1/admin")
    app.include_router(ghl_oauth_router, prefix="/api/v1/admin")
    app.include_router(scheduler_router, prefix="/api/v1/admin")
    app.include_router(writeback_router, prefix="/api/v1/admin")
    # ── M6: consent layer + inbound webhook ──────────────────────────────────
    app.include_router(consent_router)          # prefix already on router: /api/v1/consent
    app.include_router(ghl_inbound_router)      # prefix already on router: /api/v1/webhooks/ghl
    # ── M6b: gatekeeper admin ─────────────────────────────────────────────────
    app.include_router(gatekeeper_admin_router) # prefix: /api/v1/admin/gatekeeper
    return app


app = create_app()
