# PROMPT: Generate shared fixtures and async test client setup for a FastAPI retail analytics API
# using in-memory SQLite, pytest-asyncio, and httpx.AsyncClient.
# CHANGES MADE: Added populated_store fixture with realistic event mix; adjusted fixture
# scoping to function level for test isolation; added staff_event and events_batch fixtures.
# Also ensures DB tables exist before each test via init_db().

"""
tests/conftest.py — Shared fixtures and test DB setup
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone, timedelta
from typing import AsyncGenerator, List

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Override DATABASE_URL before importing app modules
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./test_store_intelligence.db"

import sqlalchemy.pool as pool_module
from sqlalchemy.ext.asyncio import create_async_engine as _create_async_engine

import app.database as _db_module

_test_engine = _create_async_engine(
    "sqlite+aiosqlite:///./test_store_intelligence.db",
    connect_args={"check_same_thread": False},
    echo=False,
)
_db_module.engine = _test_engine
_db_module.AsyncSessionLocal = async_sessionmaker(
    _test_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)

from app.main import app
from app.database import init_db, AsyncSessionLocal, upsert_event

engine = _test_engine


@pytest_asyncio.fixture(scope="function")
async def test_db():
    """
    Create in-memory test database tables for each test.
    Uses init_db() to create all tables, then cleans up after.
    """
    # Always initialise DB before each test (idempotent)
    await init_db()
    yield
    # Cleanup: drop all rows (keeps tables for next test)
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM events"))
        await conn.execute(text("DELETE FROM visitor_sessions"))
        await conn.execute(text("DELETE FROM pos_transactions"))


@pytest_asyncio.fixture(scope="function")
async def client(test_db) -> AsyncGenerator[AsyncClient, None]:
    """Async HTTP test client with the FastAPI app."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as c:
        yield c


def _make_event(
    event_type: str = "ENTRY",
    store_id: str = "STORE_BLR_002",
    camera_id: str = "CAM_ENTRY_01",
    visitor_id: str = None,
    zone_id: str = None,
    dwell_ms: int = 0,
    is_staff: bool = False,
    confidence: float = 0.92,
    event_id: str = None,
    timestamp: str = None,
    queue_depth: int = None,
) -> dict:
    """Build a valid event dict."""
    if event_id is None:
        event_id = str(uuid.uuid4())
    if visitor_id is None:
        import secrets
        visitor_id = f"VIS_{secrets.token_hex(3)}"
    if timestamp is None:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # zone_id must be None for ENTRY/EXIT/REENTRY
    if event_type in ("ENTRY", "EXIT", "REENTRY"):
        zone_id = None

    return {
        "event_id": event_id,
        "store_id": store_id,
        "camera_id": camera_id,
        "visitor_id": visitor_id,
        "event_type": event_type,
        "timestamp": timestamp,
        "zone_id": zone_id,
        "dwell_ms": dwell_ms,
        "is_staff": is_staff,
        "confidence": confidence,
        "metadata": {
            "queue_depth": queue_depth,
            "sku_zone": None,
            "session_seq": 1,
        },
    }


@pytest.fixture
def sample_event() -> dict:
    """Valid ENTRY event for STORE_BLR_002."""
    return _make_event(event_type="ENTRY")


@pytest.fixture
def sample_entry_event() -> dict:
    return _make_event(event_type="ENTRY")


@pytest.fixture
def sample_exit_event() -> dict:
    return _make_event(event_type="EXIT", visitor_id="VIS_aabbcc")


@pytest.fixture
def sample_zone_dwell_event() -> dict:
    """Valid ZONE_DWELL event with zone_id='SKINCARE', dwell_ms=35000."""
    return _make_event(event_type="ZONE_DWELL", zone_id="SKINCARE", dwell_ms=35000)


@pytest.fixture
def staff_event() -> dict:
    """Valid event with is_staff=True."""
    return _make_event(event_type="ENTRY", is_staff=True)


@pytest.fixture
def events_batch() -> List[dict]:
    """10 varied valid events (mix of types)."""
    import secrets
    events = []
    for i, et in enumerate(
        ["ENTRY", "EXIT", "ZONE_ENTER", "ZONE_EXIT", "ZONE_DWELL",
         "BILLING_QUEUE_JOIN", "ENTRY", "EXIT", "ZONE_ENTER", "ZONE_DWELL"]
    ):
        zone = None
        if et not in ("ENTRY", "EXIT", "REENTRY"):
            zone = "SKINCARE"
        dwell = 35000 if et == "ZONE_DWELL" else 0
        events.append(_make_event(
            event_type=et,
            visitor_id=f"VIS_{secrets.token_hex(3)}",
            zone_id=zone,
            dwell_ms=dwell,
        ))
    return events


@pytest_asyncio.fixture
async def populated_store(test_db):
    """
    Insert 20 realistic events: 10 ENTRY, 7 ZONE_ENTER, 4 BILLING_QUEUE_JOIN, 3 conversions.
    Also inserts visitor_sessions with 3 converted sessions.
    """
    import secrets
    from sqlalchemy import text

    store_id = "STORE_BLR_002"
    visitor_ids = [f"VIS_{secrets.token_hex(3)}" for _ in range(10)]
    now = datetime.now(timezone.utc)

    async with AsyncSessionLocal() as db:
        # 10 ENTRY events
        for i, vid in enumerate(visitor_ids):
            ts = (now - timedelta(minutes=30 - i)).strftime("%Y-%m-%dT%H:%M:%SZ")
            await upsert_event(_make_event(
                event_type="ENTRY", visitor_id=vid, timestamp=ts, store_id=store_id
            ), db)
            # Add session
            await db.execute(text("""
                INSERT OR IGNORE INTO visitor_sessions
                    (session_id, store_id, visitor_id, entry_time, is_staff)
                VALUES (:sid, :store_id, :vid, :ts, 0)
            """), {"sid": str(uuid.uuid4()), "store_id": store_id, "vid": vid, "ts": ts})

        # 7 ZONE_ENTER events
        for vid in visitor_ids[:7]:
            ts = (now - timedelta(minutes=25)).strftime("%Y-%m-%dT%H:%M:%SZ")
            await upsert_event(_make_event(
                event_type="ZONE_ENTER", visitor_id=vid, zone_id="SKINCARE",
                timestamp=ts, store_id=store_id
            ), db)

        # 4 BILLING_QUEUE_JOIN
        for vid in visitor_ids[:4]:
            ts = (now - timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
            await upsert_event(_make_event(
                event_type="BILLING_QUEUE_JOIN", visitor_id=vid, zone_id="BILLING",
                timestamp=ts, store_id=store_id, queue_depth=2
            ), db)

        # 3 ZONE_DWELL events
        for vid in visitor_ids[:3]:
            ts = (now - timedelta(minutes=22)).strftime("%Y-%m-%dT%H:%M:%SZ")
            await upsert_event(_make_event(
                event_type="ZONE_DWELL", visitor_id=vid, zone_id="SKINCARE",
                dwell_ms=35000, timestamp=ts, store_id=store_id
            ), db)

        # Mark 3 sessions as converted
        for vid in visitor_ids[:3]:
            await db.execute(text("""
                UPDATE visitor_sessions
                SET is_converted = 1
                WHERE visitor_id = :vid AND store_id = :store_id
            """), {"vid": vid, "store_id": store_id})

        await db.commit()

    return {"store_id": store_id, "visitor_ids": visitor_ids}
