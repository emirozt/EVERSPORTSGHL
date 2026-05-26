import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import sentry_sdk
from fastapi import FastAPI

from app.api.health import router as health_router
from app.config import get_settings
from app.db.session import get_engine

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

    yield

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
    return app


app = create_app()
