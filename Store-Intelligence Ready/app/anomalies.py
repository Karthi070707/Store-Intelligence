"""
app/anomalies.py — GET /stores/{store_id}/anomalies

Detects 4 types of anomalies:
1. BILLING_QUEUE_SPIKE — queue depth > 5 (WARN) or > 10 (CRITICAL)
2. CONVERSION_DROP — today's rate < 80% of 7-day avg (WARN)
3. DEAD_ZONE — no ZONE_ENTER in last 30 min during open hours (INFO)
4. STALE_FEED — no events from a camera in > 10 min (CRITICAL)

Never crashes — all errors are caught and empty list returned.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Anomaly, AnomalyResponse, AnomalySeverity

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/stores", tags=["anomalies"])

STORE_LAYOUT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "store_layout.json",
)
STALE_MINUTES = int(os.getenv("STALE_FEED_THRESHOLD_MINUTES", "10"))


def _load_layout(store_id: str) -> dict:
    if not os.path.exists(STORE_LAYOUT_PATH):
        return {}
    with open(STORE_LAYOUT_PATH, encoding="utf-8") as f:
        layout = json.load(f)
    return layout.get("stores", {}).get(store_id, {})


@router.get("/{store_id}/anomalies", response_model=AnomalyResponse)
async def get_anomalies(
    store_id: str,
    db: AsyncSession = Depends(get_db),
) -> AnomalyResponse:
    """
    Detect and return active anomalies for the given store.
    Returns empty list if no anomalies — never crashes.
    """
    anomalies: List[Anomaly] = []
    now = datetime.now(timezone.utc)
    now_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        # -------------------------------------------------------------------
        # 1. BILLING_QUEUE_SPIKE
        # -------------------------------------------------------------------
        try:
            queue_result = await db.execute(
                text("""
                    SELECT COUNT(DISTINCT visitor_id)
                    FROM events
                    WHERE store_id = :store_id
                      AND event_type = 'BILLING_QUEUE_JOIN'
                      AND is_staff = 0
                      AND timestamp >= datetime('now', '-30 minutes')
                      AND visitor_id NOT IN (
                          SELECT DISTINCT visitor_id FROM events
                          WHERE store_id = :store_id
                            AND event_type IN ('BILLING_QUEUE_ABANDON', 'EXIT')
                            AND timestamp >= datetime('now', '-30 minutes')
                      )
                """),
                {"store_id": store_id},
            )
            queue_depth: int = queue_result.scalar() or 0

            if queue_depth > 10:
                anomalies.append(Anomaly(
                    anomaly_type="BILLING_QUEUE_SPIKE",
                    severity=AnomalySeverity.CRITICAL,
                    description=f"Billing queue depth is {queue_depth} (critical threshold: 10)",
                    suggested_action="Critical queue depth — deploy 2 additional cashiers and consider express lane",
                    detected_at=now,
                ))
            elif queue_depth > 5:
                anomalies.append(Anomaly(
                    anomaly_type="BILLING_QUEUE_SPIKE",
                    severity=AnomalySeverity.WARN,
                    description=f"Billing queue depth is {queue_depth} (warning threshold: 5)",
                    suggested_action="Deploy additional cashier to billing counter immediately",
                    detected_at=now,
                ))
        except Exception as exc:
            logger.warning("BILLING_QUEUE_SPIKE check failed: %s", exc)

        # -------------------------------------------------------------------
        # 2. CONVERSION_DROP — today's rate vs 7-day rolling average (7 days ending yesterday)
        # -------------------------------------------------------------------
        try:
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            today_start_str = today_start.strftime("%Y-%m-%dT%H:%M:%SZ")

            total_today_r = await db.execute(
                text("SELECT COUNT(*) FROM visitor_sessions WHERE store_id=:s AND is_staff=0 AND entry_time>=:t"),
                {"s": store_id, "t": today_start_str},
            )
            total_today = total_today_r.scalar() or 0

            conv_today_r = await db.execute(
                text("SELECT COUNT(*) FROM visitor_sessions WHERE store_id=:s AND is_staff=0 AND is_converted=1 AND entry_time>=:t"),
                {"s": store_id, "t": today_start_str},
            )
            conv_today = conv_today_r.scalar() or 0
            rate_today = conv_today / total_today if total_today > 0 else None

            if rate_today is not None:
                # 7-day rolling average (last 7 days excluding today)
                # Use a proper 7-day window: from 7 days ago up to (but not including) today
                from datetime import timedelta
                seven_days_ago_dt = today_start - timedelta(days=7)
                seven_days_ago_str = seven_days_ago_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                today_start_str_inner = today_start.strftime("%Y-%m-%dT%H:%M:%SZ")

                total_7d_r = await db.execute(
                    text("""
                        SELECT COUNT(*) FROM visitor_sessions
                        WHERE store_id=:s AND is_staff=0
                          AND entry_time >= :seven_ago
                          AND entry_time < :today
                    """),
                    {"s": store_id, "seven_ago": seven_days_ago_str, "today": today_start_str_inner},
                )
                total_7d = total_7d_r.scalar() or 0
                conv_7d_r = await db.execute(
                    text("""
                        SELECT COUNT(*) FROM visitor_sessions
                        WHERE store_id=:s AND is_staff=0 AND is_converted=1
                          AND entry_time >= :seven_ago
                          AND entry_time < :today
                    """),
                    {"s": store_id, "seven_ago": seven_days_ago_str, "today": today_start_str_inner},
                )
                conv_7d = conv_7d_r.scalar() or 0
                rate_7d = conv_7d / total_7d if total_7d > 0 else None

                if rate_7d is not None and rate_today < rate_7d * 0.8:
                    anomalies.append(Anomaly(
                        anomaly_type="CONVERSION_DROP",
                        severity=AnomalySeverity.WARN,
                        description=(
                            f"Today's conversion rate ({rate_today:.1%}) is more than 20% "
                            f"below the 7-day average ({rate_7d:.1%})"
                        ),
                        suggested_action=(
                            "Conversion rate 20%+ below 7-day average — "
                            "review staff placement, signage, and zone layout"
                        ),
                        detected_at=now,
                    ))
        except Exception as exc:
            logger.warning("CONVERSION_DROP check failed: %s", exc)

        # -------------------------------------------------------------------
        # 3. DEAD_ZONE
        # -------------------------------------------------------------------
        try:
            store_layout = _load_layout(store_id)
            zones = store_layout.get("zones", {})
            open_hours = store_layout.get("open_hours", {})
            open_hour = open_hours.get("open", 9)
            close_hour = open_hours.get("close", 21)
            current_hour = now.hour  # UTC-based; adjust with store timezone if available

            if open_hour <= current_hour < close_hour:
                for zone_id in zones:
                    zone_result = await db.execute(
                        text("""
                            SELECT COUNT(*) FROM events
                            WHERE store_id = :store_id
                              AND zone_id = :zone_id
                              AND event_type = 'ZONE_ENTER'
                              AND timestamp >= datetime('now', '-30 minutes')
                        """),
                        {"store_id": store_id, "zone_id": zone_id},
                    )
                    zone_count = zone_result.scalar() or 0
                    if zone_count == 0:
                        anomalies.append(Anomaly(
                            anomaly_type="DEAD_ZONE",
                            severity=AnomalySeverity.INFO,
                            description=f"Zone {zone_id} has had no visitor traffic for 30+ minutes",
                            suggested_action=(
                                f"Zone {zone_id} has had no visitor traffic for 30+ minutes — "
                                "consider repositioning floor staff or checking camera feed"
                            ),
                            detected_at=now,
                        ))
        except Exception as exc:
            logger.warning("DEAD_ZONE check failed: %s", exc)

        # -------------------------------------------------------------------
        # 4. STALE_FEED
        # -------------------------------------------------------------------
        try:
            store_layout = _load_layout(store_id)
            cameras = store_layout.get("cameras", {})

            for camera_id in cameras:
                stale_result = await db.execute(
                    text("""
                        SELECT COUNT(*) FROM events
                        WHERE store_id = :store_id
                          AND camera_id = :camera_id
                          AND timestamp >= datetime('now', :threshold)
                    """),
                    {
                        "store_id": store_id,
                        "camera_id": camera_id,
                        "threshold": f"-{STALE_MINUTES} minutes",
                    },
                )
                recent_count = stale_result.scalar() or 0
                if recent_count == 0:
                    # Also check if camera has any events at all (if new store, no anomaly)
                    any_result = await db.execute(
                        text("SELECT COUNT(*) FROM events WHERE store_id=:s AND camera_id=:c"),
                        {"s": store_id, "c": camera_id},
                    )
                    any_count = any_result.scalar() or 0
                    if any_count > 0:
                        anomalies.append(Anomaly(
                            anomaly_type="STALE_FEED",
                            severity=AnomalySeverity.CRITICAL,
                            description=(
                                f"No events received from {camera_id} in {store_id} "
                                f"for {STALE_MINUTES}+ minutes"
                            ),
                            suggested_action=(
                                f"No events received from {camera_id} in {store_id} "
                                f"for {STALE_MINUTES}+ minutes — "
                                "verify CCTV connectivity and pipeline status"
                            ),
                            detected_at=now,
                        ))
        except Exception as exc:
            logger.warning("STALE_FEED check failed: %s", exc)

    except Exception as exc:  # noqa: BLE001
        logger.error("Anomaly detection failed completely for %s: %s", store_id, exc)
        # Return empty list — never crash

    return AnomalyResponse(
        store_id=store_id,
        checked_at=now,
        anomalies=anomalies,
    )
