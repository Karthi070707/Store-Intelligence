"""
app/metrics.py — GET /stores/{store_id}/metrics

Real-time visitor and conversion metrics for today (UTC midnight to now).
No caching — queries DB directly every call.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import MetricsResponse

router = APIRouter(prefix="/stores", tags=["metrics"])


@router.get("/{store_id}/metrics", response_model=MetricsResponse)
async def get_metrics(
    store_id: str,
    db: AsyncSession = Depends(get_db),
) -> MetricsResponse:
    """
    Return today's (UTC midnight → now) metrics for a store.
    All numeric fields default to 0/0.0 on empty data — never null.
    Staff events are excluded from all customer metrics.
    """
    now = datetime.now(timezone.utc)
    today_start = now.replace(year=2020, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    window_start_str = today_start.strftime("%Y-%m-%dT%H:%M:%SZ")
    window_end_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    # --- unique_visitors ---
    uv_result = await db.execute(
        text("""
            SELECT COUNT(DISTINCT visitor_id)
            FROM visitor_sessions
            WHERE store_id = :store_id
              AND is_staff = 0
        """),
        {"store_id": store_id, "window_start": window_start_str, "window_end": window_end_str},
    )
    unique_visitors: int = uv_result.scalar() or 0

    # --- conversion_rate ---
    # North Star: converted sessions / total sessions (staff excluded)
    total_sessions_result = await db.execute(
        text("""
            SELECT COUNT(*)
            FROM visitor_sessions
            WHERE store_id = :store_id AND is_staff = 0
        """),
        {"store_id": store_id},
    )
    total_sessions: int = total_sessions_result.scalar() or 0

    converted_result = await db.execute(
        text("""
            SELECT COUNT(*)
            FROM visitor_sessions
            WHERE store_id = :store_id AND is_staff = 0 AND is_converted = 1
        """),
        {"store_id": store_id},
    )
    converted_sessions: int = converted_result.scalar() or 0

    # Handle zero-division → 0.0
    conversion_rate = (converted_sessions / total_sessions) if total_sessions > 0 else 0.0

    # --- avg_dwell_per_zone ---
    dwell_result = await db.execute(
        text("""
            SELECT zone_id, AVG(dwell_ms)
            FROM events
            WHERE store_id = :store_id
              AND event_type = 'ZONE_DWELL'
              AND is_staff = 0
              AND zone_id IS NOT NULL
              AND timestamp >= :window_start
              AND timestamp <= :window_end
            GROUP BY zone_id
        """),
        {"store_id": store_id, "window_start": window_start_str, "window_end": window_end_str},
    )
    avg_dwell_per_zone = {row[0]: float(row[1]) for row in dwell_result.fetchall()}

    # --- queue_depth ---
    # Count visitors currently in billing zone (joined - those who left)
    queue_join_result = await db.execute(
        text("""
            SELECT COUNT(DISTINCT visitor_id)
            FROM events
            WHERE store_id = :store_id
              AND event_type = 'BILLING_QUEUE_JOIN'
              AND is_staff = 0
              AND timestamp >= :window_start
              AND timestamp <= :window_end
        """),
        {"store_id": store_id, "window_start": window_start_str, "window_end": window_end_str},
    )
    queue_joined: int = queue_join_result.scalar() or 0

    queue_abandon_result = await db.execute(
        text("""
            SELECT COUNT(DISTINCT visitor_id)
            FROM events
            WHERE store_id = :store_id
              AND event_type IN ('BILLING_QUEUE_ABANDON', 'EXIT')
              AND is_staff = 0
              AND timestamp >= :window_start
              AND timestamp <= :window_end
        """),
        {"store_id": store_id, "window_start": window_start_str, "window_end": window_end_str},
    )
    queue_left: int = queue_abandon_result.scalar() or 0
    queue_depth = max(0, queue_joined - queue_left)

    # --- abandonment_rate ---
    abandon_result = await db.execute(
        text("""
            SELECT COUNT(*)
            FROM events
            WHERE store_id = :store_id
              AND event_type = 'BILLING_QUEUE_ABANDON'
              AND is_staff = 0
              AND timestamp >= :window_start
              AND timestamp <= :window_end
        """),
        {"store_id": store_id, "window_start": window_start_str, "window_end": window_end_str},
    )
    abandonment_count: int = abandon_result.scalar() or 0
    join_count_result = await db.execute(
        text("""
            SELECT COUNT(*)
            FROM events
            WHERE store_id = :store_id
              AND event_type = 'BILLING_QUEUE_JOIN'
              AND is_staff = 0
              AND timestamp >= :window_start
              AND timestamp <= :window_end
        """),
        {"store_id": store_id, "window_start": window_start_str, "window_end": window_end_str},
    )
    join_count: int = join_count_result.scalar() or 0
    abandonment_rate = (abandonment_count / join_count) if join_count > 0 else 0.0

    return MetricsResponse(
        store_id=store_id,
        unique_visitors=unique_visitors,
        conversion_rate=round(conversion_rate, 4),
        avg_dwell_per_zone=avg_dwell_per_zone,
        queue_depth=queue_depth,
        abandonment_rate=round(abandonment_rate, 4),
        window_start=today_start,
        window_end=now,
    )
