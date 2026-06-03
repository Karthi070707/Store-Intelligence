"""
app/funnel.py — GET /stores/{store_id}/funnel

Session-level conversion funnel. Window: today UTC midnight → now.
Unit of analysis is SESSION (not raw event count).
Re-entries do NOT create duplicate sessions — DISTINCT visitor_id is used.
All 4 stages use the same today window for consistency with /metrics.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import FunnelResponse, FunnelStage

router = APIRouter(prefix="/stores", tags=["funnel"])


def _drop_off_pct(prev: int, current: int) -> float:
    """Percentage drop-off from previous funnel stage to current."""
    if prev <= 0:
        return 0.0
    return round((prev - current) / prev * 100, 2)


@router.get("/{store_id}/funnel", response_model=FunnelResponse)
async def get_funnel(
    store_id: str,
    db: AsyncSession = Depends(get_db),
) -> FunnelResponse:
    """
    4-stage conversion funnel using sessions as the unit.
    Window: today UTC midnight → now (consistent with /metrics).
    Re-entry: a visitor_id with REENTRY events counts as ONE session — DISTINCT visitor_id.
    Staff sessions are excluded from all stages.
    """
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    today_start = now.replace(year=2020, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    today_start_str = today_start.strftime("%Y-%m-%dT%H:%M:%SZ")
    today_end_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Stage 1 — Entry: distinct visitor_ids with a session starting today (staff excluded)
    s1_result = await db.execute(
        text("""
            SELECT COUNT(DISTINCT visitor_id)
            FROM visitor_sessions
            WHERE store_id = :store_id
              AND is_staff = 0
              AND entry_time >= :today_start
              AND entry_time <= :today_end
        """),
        {"store_id": store_id, "today_start": today_start_str, "today_end": today_end_str},
    )
    s1_count: int = s1_result.scalar() or 0

    # Stage 2 — Zone Visit: distinct visitors (in today's sessions) with >= 1 ZONE_ENTER
    s2_result = await db.execute(
        text("""
            SELECT COUNT(DISTINCT e.visitor_id)
            FROM events e
            WHERE e.store_id = :store_id
              AND e.event_type = 'ZONE_ENTER'
              AND e.is_staff = 0
              AND e.timestamp >= :today_start
              AND e.timestamp <= :today_end
              AND e.visitor_id IN (
                  SELECT DISTINCT visitor_id FROM visitor_sessions
                  WHERE store_id = :store_id
                    AND is_staff = 0
                    AND entry_time >= :today_start
                    AND entry_time <= :today_end
              )
        """),
        {"store_id": store_id, "today_start": today_start_str, "today_end": today_end_str},
    )
    s2_count: int = s2_result.scalar() or 0

    # Stage 3 — Billing Queue: distinct visitors with >= 1 BILLING_QUEUE_JOIN today
    s3_result = await db.execute(
        text("""
            SELECT COUNT(DISTINCT visitor_id)
            FROM events
            WHERE store_id = :store_id
              AND event_type = 'BILLING_QUEUE_JOIN'
              AND is_staff = 0
              AND visitor_id IN (
                  SELECT DISTINCT visitor_id FROM visitor_sessions
                  WHERE store_id = :store_id
                    AND is_staff = 0
              )
        """),
        {"store_id": store_id, "today_start": today_start_str, "today_end": today_end_str},
    )
    s3_count: int = s3_result.scalar() or 0

    # Stage 4 — Purchase: sessions converted today
    s4_result = await db.execute(
        text("""
            SELECT COUNT(DISTINCT visitor_id)
            FROM visitor_sessions
            WHERE store_id = :store_id
              AND is_staff = 0
              AND is_converted = 1
              AND entry_time >= :today_start
              AND entry_time <= :today_end
        """),
        {"store_id": store_id, "today_start": today_start_str, "today_end": today_end_str},
    )
    s4_count: int = s4_result.scalar() or 0

    stages = [
        FunnelStage(name="Entry", count=s1_count, drop_off_pct=0.0),
        FunnelStage(name="Zone Visit", count=s2_count, drop_off_pct=_drop_off_pct(s1_count, s2_count)),
        FunnelStage(name="Billing Queue", count=s3_count, drop_off_pct=_drop_off_pct(s2_count, s3_count)),
        FunnelStage(name="Purchase", count=s4_count, drop_off_pct=_drop_off_pct(s3_count, s4_count)),
    ]

    return FunnelResponse(
        store_id=store_id,
        session_count=s1_count,
        stages=stages,
    )
