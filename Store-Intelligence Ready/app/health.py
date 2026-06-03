"""
app/health.py — GET /health

Service health endpoint. ALWAYS returns HTTP 200.
Even if DB is down, returns status: "degraded" with db_status: "unavailable".
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter
from sqlalchemy import text

from app.database import AsyncSessionLocal
from app.models import HealthResponse, StaleFeeds

logger = logging.getLogger(__name__)
router = APIRouter(tags=["health"])

STALE_MINUTES = 10


@router.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    """
    Returns 200 always. DB down → status: "degraded", db_status: "unavailable".
    Includes per-store last event timestamps and stale camera feed detection.
    """
    from app.main import app  # import here to avoid circular

    # Uptime
    start_time: Optional[datetime] = getattr(app.state, "start_time", None)
    now = datetime.now(timezone.utc)
    uptime_seconds = (now - start_time).total_seconds() if start_time else 0.0

    version = "1.0.0"
    db_status = "connected"
    status = "healthy"
    last_event_per_store: dict = {}
    stale_feeds: list = []

    try:
        async with AsyncSessionLocal() as db:
            # Last event per store
            result = await db.execute(
                text("""
                    SELECT store_id, MAX(timestamp) as last_ts
                    FROM events
                    GROUP BY store_id
                """)
            )
            for row in result.fetchall():
                last_event_per_store[row[0]] = row[1]

            # Stale camera feeds
            stale_result = await db.execute(
                text(f"""
                    SELECT store_id, camera_id, MAX(timestamp) as last_event_at
                    FROM events
                    GROUP BY store_id, camera_id
                    HAVING last_event_at < datetime('now', '-{STALE_MINUTES} minutes')
                """)
            )
            for row in stale_result.fetchall():
                stale_feeds.append(
                    StaleFeeds(
                        store_id=row[0],
                        camera_id=row[1],
                        last_event_at=row[2],
                    )
                )

    except Exception as exc:  # noqa: BLE001
        logger.error("Health check DB query failed: %s", exc)
        db_status = "unavailable"
        status = "degraded"

    return HealthResponse(
        status=status,
        version=version,
        uptime_seconds=round(uptime_seconds, 2),
        last_event_per_store=last_event_per_store,
        stale_feeds=stale_feeds,
        db_status=db_status,
    )
