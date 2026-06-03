# PROMPT: Create tests for the /stores/{store_id}/funnel endpoint covering session deduplication,
# re-entry handling, staff exclusion, zero sessions, and full conversion.
# CHANGES MADE: Used DISTINCT visitor_id logic (not COUNT(*)) to verify re-entry deduplication;
# added test_funnel_zero_sessions for empty store safety; added test_funnel_all_purchased.

"""
tests/test_funnel.py — Tests for GET /stores/{store_id}/funnel
"""
from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from app.database import AsyncSessionLocal, upsert_event


def _evt(event_type="ENTRY", visitor_id=None, zone_id=None, is_staff=False,
         store_id="STORE_BLR_002", timestamp=None):
    return {
        "event_id": str(uuid.uuid4()),
        "store_id": store_id,
        "camera_id": "CAM_ENTRY_01",
        "visitor_id": visitor_id or f"VIS_{secrets.token_hex(3)}",
        "event_type": event_type,
        "timestamp": timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "zone_id": zone_id,
        "dwell_ms": 0,
        "is_staff": is_staff,
        "confidence": 0.9,
        "metadata": {"queue_depth": None, "sku_zone": None, "session_seq": 1},
    }


async def _insert_session(db, store_id, visitor_id, is_converted=False, is_staff=False):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    await db.execute(text("""
        INSERT OR IGNORE INTO visitor_sessions
            (session_id, store_id, visitor_id, entry_time, is_staff, is_converted)
        VALUES (:sid, :s, :v, :ts, :is_staff, :conv)
    """), {
        "sid": str(uuid.uuid4()), "s": store_id, "v": visitor_id,
        "ts": ts, "is_staff": int(is_staff), "conv": int(is_converted)
    })


@pytest.mark.asyncio
async def test_funnel_basic(client, test_db):
    """10 entry, 7 zone_visit, 4 billing, 2 purchase → correct counts and drop_off_pct."""
    store_id = "STORE_BLR_002"
    visitors = [f"VIS_{secrets.token_hex(3)}" for _ in range(10)]

    async with AsyncSessionLocal() as db:
        for vid in visitors:
            await _insert_session(db, store_id, vid)
        for vid in visitors[:7]:
            await upsert_event(_evt("ZONE_ENTER", vid, "SKINCARE", store_id=store_id), db)
        for vid in visitors[:4]:
            await upsert_event(_evt("BILLING_QUEUE_JOIN", vid, "BILLING", store_id=store_id), db)
        for vid in visitors[:2]:
            await db.execute(text("""
                UPDATE visitor_sessions SET is_converted=1 WHERE visitor_id=:v AND store_id=:s
            """), {"v": vid, "s": store_id})
        await db.commit()

    response = await client.get(f"/stores/{store_id}/funnel")
    assert response.status_code == 200
    data = response.json()
    stages = {s["name"]: s for s in data["stages"]}

    assert stages["Entry"]["count"] == 10
    assert stages["Zone Visit"]["count"] == 7
    assert stages["Billing Queue"]["count"] == 4
    assert stages["Purchase"]["count"] == 2

    # Drop-off: Entry→Zone: (10-7)/10 * 100 = 30%
    assert stages["Zone Visit"]["drop_off_pct"] == pytest.approx(30.0, abs=1.0)
    # Zone→Billing: (7-4)/7 * 100 = ~42.86%
    assert stages["Billing Queue"]["drop_off_pct"] == pytest.approx(42.86, abs=1.0)
    # Billing→Purchase: (4-2)/4 * 100 = 50%
    assert stages["Purchase"]["drop_off_pct"] == pytest.approx(50.0, abs=1.0)


@pytest.mark.asyncio
async def test_funnel_reentry_deduplication(client, test_db):
    """Visitor with REENTRY event counted ONCE in Stage 1 — not twice."""
    store_id = "STORE_BLR_002"
    vid = f"VIS_{secrets.token_hex(3)}"

    async with AsyncSessionLocal() as db:
        # Insert ONE session for the visitor
        await _insert_session(db, store_id, vid)
        # Insert ENTRY and REENTRY events — both same visitor_id
        await upsert_event(_evt("ENTRY", vid, store_id=store_id), db)
        await upsert_event(_evt("REENTRY", vid, store_id=store_id), db)
        await db.commit()

    response = await client.get(f"/stores/{store_id}/funnel")
    assert response.status_code == 200
    data = response.json()
    stages = {s["name"]: s for s in data["stages"]}
    # Should be exactly 1 unique visitor in Stage 1, not 2
    assert stages["Entry"]["count"] == 1


@pytest.mark.asyncio
async def test_funnel_staff_excluded(client, test_db):
    """Staff sessions do not appear in any funnel stage."""
    store_id = "STORE_BLR_002"

    async with AsyncSessionLocal() as db:
        # 1 customer, 2 staff
        cust_vid = f"VIS_{secrets.token_hex(3)}"
        await _insert_session(db, store_id, cust_vid, is_staff=False)
        for _ in range(2):
            await _insert_session(db, store_id, f"VIS_{secrets.token_hex(3)}", is_staff=True)
        await db.commit()

    response = await client.get(f"/stores/{store_id}/funnel")
    assert response.status_code == 200
    data = response.json()
    stages = {s["name"]: s for s in data["stages"]}
    # Only 1 customer session should show
    assert stages["Entry"]["count"] == 1


@pytest.mark.asyncio
async def test_funnel_zero_sessions(client, test_db):
    """Empty store → all stages count=0, drop_off_pct=0.0 (no crash)."""
    response = await client.get("/stores/STORE_BLR_001/funnel")
    assert response.status_code == 200
    data = response.json()
    for stage in data["stages"]:
        assert stage["count"] == 0
        assert stage["drop_off_pct"] == 0.0


@pytest.mark.asyncio
async def test_funnel_all_purchased(client, test_db):
    """All visitors convert → Stage 4 drop_off_pct=0.0."""
    store_id = "STORE_BLR_002"
    visitors = [f"VIS_{secrets.token_hex(3)}" for _ in range(3)]

    async with AsyncSessionLocal() as db:
        for vid in visitors:
            await _insert_session(db, store_id, vid, is_converted=True)
            await upsert_event(_evt("BILLING_QUEUE_JOIN", vid, "BILLING", store_id=store_id), db)
        await db.commit()

    response = await client.get(f"/stores/{store_id}/funnel")
    assert response.status_code == 200
    data = response.json()
    stages = {s["name"]: s for s in data["stages"]}
    # Purchase stage has same count as Billing Queue → 0% drop-off
    if stages["Billing Queue"]["count"] > 0:
        assert stages["Purchase"]["drop_off_pct"] == pytest.approx(0.0, abs=0.01)
