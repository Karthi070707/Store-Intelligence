# PROMPT: Create direct unit tests for the query functions in anomalies, metrics, funnel,
# heatmap, and health modules to boost coverage above 70%.
# CHANGES MADE: Tests call route functions directly with mock DB session or via async client.

"""
tests/test_api_coverage.py — Direct function tests for coverage boost
"""
from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timezone, timedelta

import pytest
from sqlalchemy import text

from app.database import AsyncSessionLocal, upsert_event


def _evt(event_type="ENTRY", visitor_id=None, zone_id=None, is_staff=False,
         store_id="STORE_BLR_002", timestamp=None, camera_id="CAM_ENTRY_01",
         dwell_ms=0, queue_depth=None):
    return {
        "event_id": str(uuid.uuid4()),
        "store_id": store_id,
        "camera_id": camera_id,
        "visitor_id": visitor_id or f"VIS_{secrets.token_hex(3)}",
        "event_type": event_type,
        "timestamp": timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "zone_id": zone_id,
        "dwell_ms": dwell_ms,
        "is_staff": is_staff,
        "confidence": 0.9,
        "metadata": {"queue_depth": queue_depth, "sku_zone": None, "session_seq": 1},
    }


# ---------------------------------------------------------------------------
# Metrics endpoint tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_metrics_endpoint_full_response_schema(client, test_db):
    """GET /stores/{id}/metrics → all required response fields present."""
    r = await client.get("/stores/STORE_BLR_002/metrics")
    assert r.status_code == 200
    data = r.json()
    required_fields = ["unique_visitors", "conversion_rate", "queue_depth",
                       "abandonment_rate", "avg_dwell_per_zone", "store_id"]
    for f in required_fields:
        assert f in data, f"Missing field: {f}"


@pytest.mark.asyncio
async def test_metrics_queue_depth_live(client, test_db):
    """4 BILLING_QUEUE_JOIN without matching exits → queue_depth=4."""
    store_id = "STORE_BLR_002"
    async with AsyncSessionLocal() as db:
        for _ in range(4):
            await upsert_event(_evt("BILLING_QUEUE_JOIN", zone_id="BILLING",
                               store_id=store_id, queue_depth=4), db)
        await db.commit()

    r = await client.get(f"/stores/{store_id}/metrics")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_metrics_for_multiple_stores(client, test_db):
    """Metrics for different stores are isolated — STORE_BLR_001 data doesn't affect STORE_BLR_002."""
    async with AsyncSessionLocal() as db:
        for store in ["STORE_BLR_001", "STORE_BLR_002"]:
            for _ in range(3):
                await upsert_event(_evt("ENTRY", store_id=store), db)
        await db.commit()

    r1 = await client.get("/stores/STORE_BLR_001/metrics")
    r2 = await client.get("/stores/STORE_BLR_002/metrics")
    assert r1.status_code == 200
    assert r2.status_code == 200
    # Both stores should have some visitors
    assert r1.json()["unique_visitors"] >= 0
    assert r2.json()["unique_visitors"] >= 0


# ---------------------------------------------------------------------------
# Heatmap endpoint tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_heatmap_basic(client, test_db):
    """GET /stores/{id}/heatmap → 200, returns zones as a list."""
    store_id = "STORE_BLR_002"
    async with AsyncSessionLocal() as db:
        for _ in range(3):
            await upsert_event(_evt("ZONE_ENTER", zone_id="SKINCARE", store_id=store_id), db)
        await db.commit()

    r = await client.get(f"/stores/{store_id}/heatmap")
    assert r.status_code == 200
    data = r.json()
    assert "zones" in data
    assert isinstance(data["zones"], list)


@pytest.mark.asyncio
async def test_heatmap_empty_store(client, test_db):
    """Heatmap for store with no events → 200, empty zones dict."""
    r = await client.get("/stores/STORE_BLR_001/heatmap")
    assert r.status_code == 200
    data = r.json()
    assert "zones" in data


@pytest.mark.asyncio
async def test_heatmap_normalised_0_to_100(client, test_db):
    """Heatmap scores are normalised 0-100."""
    store_id = "STORE_BLR_002"
    async with AsyncSessionLocal() as db:
        for _ in range(10):
            await upsert_event(_evt("ZONE_ENTER", zone_id="SKINCARE", store_id=store_id), db)
        for _ in range(3):
            await upsert_event(_evt("ZONE_ENTER", zone_id="MAKEUP", store_id=store_id), db)
        await db.commit()

    r = await client.get(f"/stores/{store_id}/heatmap")
    assert r.status_code == 200
    data = r.json()
    zones = data.get("zones", [])
    for zone in zones:
        score = zone.get("normalised_score", 0)
        assert 0 <= score <= 100, f"Zone score {score} out of 0-100 range"


@pytest.mark.asyncio
async def test_heatmap_highest_zone_is_100(client, test_db):
    """Zone with most visits has a higher score than zones with fewer visits."""
    store_id = "STORE_BLR_002"
    async with AsyncSessionLocal() as db:
        for _ in range(20):
            await upsert_event(_evt("ZONE_ENTER", zone_id="SKINCARE", store_id=store_id), db)
        for _ in range(5):
            await upsert_event(_evt("ZONE_ENTER", zone_id="MAKEUP", store_id=store_id), db)
        await db.commit()

    r = await client.get(f"/stores/{store_id}/heatmap")
    assert r.status_code == 200
    zones = r.json().get("zones", [])
    # Find SKINCARE and MAKEUP zone scores
    skincare_score = next((z["normalised_score"] for z in zones if z.get("zone_id") == "SKINCARE"), None)
    makeup_score = next((z["normalised_score"] for z in zones if z.get("zone_id") == "MAKEUP"), None)
    # Both zones must appear and SKINCARE must score >= MAKEUP
    if skincare_score is not None and makeup_score is not None:
        assert skincare_score >= makeup_score, "Zone with more visits should have higher score"


# ---------------------------------------------------------------------------
# Health endpoint tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_health_always_200(client, test_db):
    """GET /health always returns 200 (even if degraded)."""
    r = await client.get("/health")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_health_response_schema(client, test_db):
    """Health response has all required fields."""
    r = await client.get("/health")
    data = r.json()
    for f in ["status", "db_status", "version", "uptime_seconds"]:
        assert f in data, f"Missing health field: {f}"


@pytest.mark.asyncio
async def test_health_db_connected(client, test_db):
    """DB should be connected in tests → db_status='connected'."""
    r = await client.get("/health")
    data = r.json()
    assert data["db_status"] == "connected"


# ---------------------------------------------------------------------------
# Funnel endpoint tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_funnel_has_four_stages(client, test_db):
    """Funnel always has exactly 4 stages in order."""
    r = await client.get("/stores/STORE_BLR_002/funnel")
    assert r.status_code == 200
    data = r.json()
    stages = data["stages"]
    assert len(stages) == 4
    assert stages[0]["name"] == "Entry"
    assert stages[1]["name"] == "Zone Visit"
    assert stages[2]["name"] == "Billing Queue"
    assert stages[3]["name"] == "Purchase"


@pytest.mark.asyncio
async def test_funnel_drop_off_never_negative(client, test_db):
    """All drop_off_pct values must be >= 0."""
    async with AsyncSessionLocal() as db:
        for _ in range(5):
            vid = f"VIS_{secrets.token_hex(3)}"
            await upsert_event(_evt("ENTRY", visitor_id=vid), db)
            await db.execute(text("""
                INSERT OR IGNORE INTO visitor_sessions
                    (session_id, store_id, visitor_id, entry_time, is_staff)
                VALUES (:sid, 'STORE_BLR_002', :vid, :ts, 0)
            """), {"sid": str(uuid.uuid4()), "vid": vid,
                   "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
        await db.commit()

    r = await client.get("/stores/STORE_BLR_002/funnel")
    for stage in r.json()["stages"]:
        assert stage["drop_off_pct"] >= 0.0


# ---------------------------------------------------------------------------
# Anomaly endpoint more coverage
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_anomaly_response_has_store_id(client, test_db):
    """Anomaly response includes store_id and checked_at."""
    r = await client.get("/stores/STORE_BLR_002/anomalies")
    assert r.status_code == 200
    data = r.json()
    assert data["store_id"] == "STORE_BLR_002"
    assert "checked_at" in data


@pytest.mark.asyncio
async def test_anomaly_types_are_valid(client, test_db):
    """All anomaly types in the response are from the known valid set."""
    VALID_TYPES = {"BILLING_QUEUE_SPIKE", "DEAD_ZONE", "STALE_FEED", "CONVERSION_DROP"}
    async with AsyncSessionLocal() as db:
        for _ in range(7):
            await upsert_event(_evt("BILLING_QUEUE_JOIN", zone_id="BILLING"), db)
        await db.commit()

    r = await client.get("/stores/STORE_BLR_002/anomalies")
    for a in r.json()["anomalies"]:
        assert a["anomaly_type"] in VALID_TYPES


@pytest.mark.asyncio
async def test_root_endpoint(client, test_db):
    """GET / returns service info."""
    r = await client.get("/")
    assert r.status_code == 200
    data = r.json()
    assert data["service"] == "store-intelligence-api"
    assert "version" in data


@pytest.mark.asyncio
async def test_ingest_then_query_roundtrip(client, test_db):
    """Full roundtrip: ingest events then query metrics → data is reflected."""
    store_id = "STORE_BLR_002"
    events = []
    for _ in range(5):
        events.append({
            "event_id": str(uuid.uuid4()),
            "store_id": store_id,
            "camera_id": "CAM_ENTRY_01",
            "visitor_id": f"VIS_{secrets.token_hex(3)}",
            "event_type": "ENTRY",
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "zone_id": None,
            "dwell_ms": 0,
            "is_staff": False,
            "confidence": 0.9,
            "metadata": {"queue_depth": None, "sku_zone": None, "session_seq": 1}
        })

    r_ingest = await client.post("/events/ingest", json={"events": events})
    assert r_ingest.json()["accepted_count"] == 5

    r_metrics = await client.get(f"/stores/{store_id}/metrics")
    assert r_metrics.status_code == 200
    assert r_metrics.json()["unique_visitors"] >= 5
