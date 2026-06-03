"""
app/database.py — Async SQLAlchemy setup with SQLite (or PostgreSQL via env)

Uses aiosqlite for async SQLite. Creates tables on startup with
CREATE TABLE IF NOT EXISTS (no Alembic dependency).
"""
from __future__ import annotations

import csv
import logging
import os
import uuid
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional

from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Custom exception for graceful degradation
# ---------------------------------------------------------------------------

class DBUnavailableError(Exception):
    """Raised when the database is unavailable. Converted to HTTP 503."""
    pass


# ---------------------------------------------------------------------------
# Engine + Session factory
# ---------------------------------------------------------------------------

DATABASE_URL = os.getenv(
    "DATABASE_URL", "sqlite+aiosqlite:///./store_intelligence.db"
)

connect_args: dict = {}
if DATABASE_URL.startswith("sqlite"):
    connect_args = {"check_same_thread": False}

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    connect_args=connect_args,
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency — yields an AsyncSession per request."""
    try:
        async with AsyncSessionLocal() as session:
            yield session
    except OperationalError as exc:
        logger.error("Database connection failed: %s", exc)
        raise DBUnavailableError("Cannot connect to database") from exc


# ---------------------------------------------------------------------------
# DDL — tables created on startup
# ---------------------------------------------------------------------------

CREATE_EVENTS_TABLE = """
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    store_id TEXT NOT NULL,
    camera_id TEXT NOT NULL,
    visitor_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    zone_id TEXT,
    dwell_ms INTEGER DEFAULT 0,
    is_staff INTEGER DEFAULT 0,
    confidence REAL NOT NULL,
    queue_depth INTEGER,
    sku_zone TEXT,
    session_seq INTEGER DEFAULT 0,
    partial_occlusion INTEGER DEFAULT 0,
    camera_overlap INTEGER DEFAULT 0,
    ingested_at TEXT DEFAULT (datetime('now'))
)
"""

CREATE_VISITOR_SESSIONS_TABLE = """
CREATE TABLE IF NOT EXISTS visitor_sessions (
    session_id TEXT PRIMARY KEY,
    store_id TEXT NOT NULL,
    visitor_id TEXT NOT NULL,
    entry_time TEXT,
    exit_time TEXT,
    is_converted INTEGER DEFAULT 0,
    is_staff INTEGER DEFAULT 0,
    reentry_count INTEGER DEFAULT 0
)
"""

CREATE_POS_TRANSACTIONS_TABLE = """
CREATE TABLE IF NOT EXISTS pos_transactions (
    transaction_id TEXT PRIMARY KEY,
    store_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    basket_value_inr REAL NOT NULL,
    matched_visitor_id TEXT
)
"""

CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_events_store_ts ON events (store_id, timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_events_visitor ON events (visitor_id)",
    "CREATE INDEX IF NOT EXISTS idx_events_type_store ON events (event_type, store_id)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_visitor ON visitor_sessions (visitor_id)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_store ON visitor_sessions (store_id)",
]


async def init_db() -> None:
    """Create all tables and indexes if they do not exist."""
    try:
        async with engine.begin() as conn:
            await conn.execute(text(CREATE_EVENTS_TABLE))
            await conn.execute(text(CREATE_VISITOR_SESSIONS_TABLE))
            await conn.execute(text(CREATE_POS_TRANSACTIONS_TABLE))
            for idx_sql in CREATE_INDEXES:
                await conn.execute(text(idx_sql))
        logger.info("Database tables initialised.")
    except OperationalError as exc:
        logger.error("Failed to initialise database: %s", exc)
        raise DBUnavailableError("Cannot initialise database") from exc


# ---------------------------------------------------------------------------
# Upsert (idempotent by event_id)
# ---------------------------------------------------------------------------

async def upsert_event(event_dict: dict, db: AsyncSession) -> bool:
    """
    Insert an event row. If event_id already exists, silently ignore.
    Returns True if inserted, False if already existed (idempotent).
    """
    sql = text("""
        INSERT OR IGNORE INTO events (
            event_id, store_id, camera_id, visitor_id, event_type,
            timestamp, zone_id, dwell_ms, is_staff, confidence,
            queue_depth, sku_zone, session_seq,
            partial_occlusion, camera_overlap
        ) VALUES (
            :event_id, :store_id, :camera_id, :visitor_id, :event_type,
            :timestamp, :zone_id, :dwell_ms, :is_staff, :confidence,
            :queue_depth, :sku_zone, :session_seq,
            :partial_occlusion, :camera_overlap
        )
    """)

    meta = event_dict.get("metadata") or {}
    # Helper: coerce None/bool to int safely
    def _to_int(val, default=0) -> int:
        if val is None:
            return default
        return int(bool(val))

    params = {
        "event_id": event_dict["event_id"],
        "store_id": event_dict["store_id"],
        "camera_id": event_dict["camera_id"],
        "visitor_id": event_dict["visitor_id"],
        "event_type": str(event_dict["event_type"]),  # handles EventType enum
        "timestamp": event_dict["timestamp"],
        "zone_id": event_dict.get("zone_id"),
        "dwell_ms": event_dict.get("dwell_ms") or 0,
        "is_staff": _to_int(event_dict.get("is_staff")),
        "confidence": float(event_dict["confidence"]),
        "queue_depth": meta.get("queue_depth"),
        "sku_zone": meta.get("sku_zone"),
        "session_seq": meta.get("session_seq") or 0,
        "partial_occlusion": _to_int(meta.get("partial_occlusion")),
        "camera_overlap": _to_int(meta.get("camera_overlap")),
    }
    result = await db.execute(sql, params)
    return result.rowcount > 0  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# POS CSV loader — runs at startup if table is empty
# ---------------------------------------------------------------------------

async def load_pos_csv(db: AsyncSession) -> None:
    """Load data/pos_transactions.csv into pos_transactions table if empty."""
    result = await db.execute(text("SELECT COUNT(*) FROM pos_transactions"))
    count = result.scalar()
    if count and count > 0:
        logger.info("POS transactions already loaded (%d rows), skipping.", count)
        return

    csv_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data",
        "pos_transactions.csv",
    )
    if not os.path.exists(csv_path):
        logger.warning("pos_transactions.csv not found at %s — skipping POS load.", csv_path)
        return

    rows_inserted = 0
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            txn_id = row.get("transaction_id") or str(uuid.uuid4())
            await db.execute(
                text("""
                    INSERT OR IGNORE INTO pos_transactions
                        (transaction_id, store_id, timestamp, basket_value_inr)
                    VALUES (:txn_id, :store_id, :timestamp, :basket_value)
                """),
                {
                    "txn_id": txn_id,
                    "store_id": row.get("store_id", ""),
                    "timestamp": row.get("timestamp", ""),
                    "basket_value": float(row.get("basket_value_inr", 0) or 0),
                },
            )
            rows_inserted += 1
    await db.commit()
    logger.info("Loaded %d POS transactions from CSV.", rows_inserted)


async def close_db() -> None:
    """Dispose engine on shutdown."""
    await engine.dispose()
    logger.info("Database engine disposed.")
