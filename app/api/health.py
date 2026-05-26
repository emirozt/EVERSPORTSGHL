from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "timestamp": datetime.now(UTC).isoformat()}


@router.get("/health/sync")
async def health_sync(
    location_id: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> dict:
    result = await db.execute(text("SELECT 1"))
    db_ok = result.scalar() == 1

    payload: dict = {
        "status": "ok" if db_ok else "error",
        "db": "connected" if db_ok else "disconnected",
        "timestamp": datetime.now(UTC).isoformat(),
    }

    if location_id:
        from sqlalchemy import select

        from app.db.models.location import Location

        row = await db.execute(select(Location).where(Location.id == location_id))  # type: ignore[arg-type]
        payload["location"] = "found" if row.scalar_one_or_none() is not None else "not_found"

    return payload
