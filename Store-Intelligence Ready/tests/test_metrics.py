# PROMPT: Create tests for the /stores/{store_id}/metrics endpoint covering staff exclusion,
# zero purchases, empty stores, avg dwell per zone, and real-time updates.
# CHANGES MADE: Used conftest fixtures, added test_metrics_real_time to verify live query
# behavior, fixed UTC timestamp generation for today's window.

"""
tests/test_metrics.py — Tests for GET /stores/{store_id}/metrics
"""
from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timezone, timedelta

import pytest

from app.database import AsyncSessionLocal, upsert_event


def _evt(event_type="ENTRY", visitor_id=None, zone_id=None, dwell_ms=0,
         is_staff=False, confidence=0.9, store_id="STORE_BLR_002",
         timestamp=None, queue_depth=None):
    from datetime import datetime, timezone
    return {
        "event_id": str(uuid.uuid4()),
        "store_id": store_id,
        "camera_id": "CAM_ENTRY_01",
        "visitor_id": visitor_id or f"VIS_{secrets.token_hex(3)}",
        "event_type": event_type,
        "timestamp": timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "zone_id": zone_id,
        "dwell_ms": dwell_ms,
        "is_staff": is_staff,
        "confidence": confidence,
        "metadata": {"queue_depth": queue_depth, "sku_zone": None, "session_seq": 1},
    }


@pytest.mark.asyncio
async def test_metrics_basic(client, test_db):
    """5 ENTRY events + 2 converted sessions → unique_visitors=5, conversion_rate=0.4."""
    from sqlalchemy import text

    store_id = "STORE_BLR_002"
    visitor_ids = [f"VIS_{secrets.token_hex(3)}" for _ in range(5)]
    now = datetime.now(timezone.utc)

    async with AsyncSessionLocal() as db:
        for vid in visitor_ids:
            await upsert_event(_evt(event_type="ENTRY", visitor_id=vid, store_id=store_id), db)
            ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            await db.execute(text("""
                INSERT OR IGNORE INTO visitor_sessions
                    (session_id, store_id, visitor_id, entry_time, is_staff)
                VALUES (:sid, :s, :v, :ts, 0)
            """), {"sid": str(uuid.uuid4()), "s": store_id, "v": vid, "ts": ts})

        # Mark 2 as converted
        for vid in visitor_ids[:2]:
            await db.execute(text("""
                UPDATE visitor_sessions SET is_converted=1 WHERE visitor_id=:v AND store_id=:s
            """), {"v": vid, "s": store_id})
        await db.commit()

    response = await client.get(f"/stores/{store_id}/metrics")
    assert response.status_code == 200
    data = response.json()
    assert data["unique_visitors"] == 5
    assert data["conversion_rate"] == pytest.approx(0.4, abs=0.01)


@pytest.mark.asyncio
async def test_metrics_staff_excluded(client, test_db):
    """3 customer ENTRY + 2 staff ENTRY → unique_visitors=3 (staff not counted)."""
    store_id = "STORE_BLR_002"
    async with AsyncSessionLocal() as db:
        for _ in range(3):
            await upsert_event(_evt(event_type="ENTRY", store_id=store_id, is_staff=False), db)
        for _ in range(2):
            await upsert_event(_evt(event_type="ENTRY", store_id=store_id, is_staff=True), db)
        await db.commit()

    # Trigger visitor session sync
    from app.ingestion import _update_visitor_sessions
    await _update_visitor_sessions(store_id)

    response = await client.get(f"/stores/{store_id}/metrics")
    assert response.status_code == 200
    data = response.json()
    assert data["unique_visitors"] == 3


@pytest.mark.asyncio
async def test_metrics_zero_purchases(client, test_db):
    """Visitors but no conversions → conversion_rate=0.0 (not null, not error)."""
    store_id = "STORE_BLR_002"
    from sqlalchemy import text

    async with AsyncSessionLocal() as db:
        vid = f"VIS_{secrets.token_hex(3)}"
        await upsert_event(_evt(event_type="ENTRY", visitor_id=vid, store_id=store_id), db)
        await db.execute(text("""
            INSERT OR IGNORE INTO visitor_sessions
                (session_id, store_id, visitor_id, entry_time, is_staff)
            VALUES (:sid, :s, :v, :ts, 0)
        """), {
            "sid": str(uuid.uuid4()), "s": store_id, "v": vid,
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        })
        await db.commit()

    response = await client.get(f"/stores/{store_id}/metrics")
    assert response.status_code == 200
    data = response.json()
    assert data["conversion_rate"] == 0.0
    assert data["unique_visitors"] >= 1


@pytest.mark.asyncio
async def test_metrics_empty_store(client, test_db):
    """No events for store → HTTP 200, all values are 0 or 0.0 (no crash)."""
    response = await client.get("/stores/STORE_BLR_999/metrics")
    # Note: STORE_BLR_999 fails validation — use a valid store with no data
    response = await client.get("/stores/STORE_BLR_001/metrics")
    assert response.status_code == 200
    data = response.json()
    assert data["unique_visitors"] == 0
    assert data["conversion_rate"] == 0.0
    assert data["queue_depth"] == 0
    assert data["abandonment_rate"] == 0.0


@pytest.mark.asyncio
async def test_metrics_avg_dwell_per_zone(client, test_db):
    """ZONE_DWELL events → avg_dwell_per_zone has correct averages."""
    store_id = "STORE_BLR_002"
    async with AsyncSessionLocal() as db:
        vid = f"VIS_{secrets.token_hex(3)}"
        await upsert_event(_evt(
            event_type="ZONE_DWELL", visitor_id=vid, zone_id="SKINCARE",
            dwell_ms=30000, store_id=store_id
        ), db)
        await upsert_event(_evt(
            event_type="ZONE_DWELL", visitor_id=f"VIS_{secrets.token_hex(3)}",
            zone_id="SKINCARE", dwell_ms=60000, store_id=store_id
        ), db)
        await db.commit()

    response = await client.get(f"/stores/{store_id}/metrics")
    assert response.status_code == 200
    data = response.json()
    zone_dwell = data.get("avg_dwell_per_zone", {})
    assert "SKINCARE" in zone_dwell
    assert zone_dwell["SKINCARE"] == pytest.approx(45000, rel=0.01)


@pytest.mark.asyncio
async def test_metrics_abandonment_rate(client, test_db):
    """4 BILLING_QUEUE_JOIN + 2 BILLING_QUEUE_ABANDON → abandonment_rate=0.5."""
    store_id = "STORE_BLR_002"
    async with AsyncSessionLocal() as db:
        for _ in range(4):
            await upsert_event(_evt(
                event_type="BILLING_QUEUE_JOIN", zone_id="BILLING",
                store_id=store_id, queue_depth=2
            ), db)
        for _ in range(2):
            await upsert_event(_evt(
                event_type="BILLING_QUEUE_ABANDON", zone_id="BILLING",
                store_id=store_id
            ), db)
        await db.commit()

    response = await client.get(f"/stores/{store_id}/metrics")
    assert response.status_code == 200
    data = response.json()
    assert data["abandonment_rate"] == pytest.approx(0.5, abs=0.01)


@pytest.mark.asyncio
async def test_metrics_real_time(client, test_db):
    """Insert event after first metrics call → second call reflects new data."""
    store_id = "STORE_BLR_002"

    r1 = await client.get(f"/stores/{store_id}/metrics")
    initial_count = r1.json()["unique_visitors"]

    async with AsyncSessionLocal() as db:
        await upsert_event(_evt(event_type="ENTRY", store_id=store_id), db)
        await db.commit()

    # Trigger visitor session sync
    from app.ingestion import _update_visitor_sessions
    await _update_visitor_sessions(store_id)

    r2 = await client.get(f"/stores/{store_id}/metrics")
    assert r2.json()["unique_visitors"] >= initial_count + 1
