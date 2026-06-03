"""
app/ingestion.py — POST /events/ingest

Accepts batches of up to 500 events. Validates, deduplicates, and inserts.
Triggers async background tasks for POS correlation and session tracking.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import DBUnavailableError, get_db, upsert_event
from app.models import Event, IngestError, IngestRequest, IngestResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/events", tags=["events"])


# ---------------------------------------------------------------------------
# Main ingest endpoint
# ---------------------------------------------------------------------------

@router.post("/ingest", response_model=IngestResponse)
async def ingest_events(
    request: IngestRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> IngestResponse:
    """
    Batch ingest up to 500 events.
    - Idempotent by event_id (duplicates are silently ignored).
    - Partial success: valid events accepted even if some are malformed.
    - Triggers POS correlation and session update as background tasks.
    """
    accepted: List[dict] = []
    errors: List[IngestError] = []

    for raw_event in request.events:
        try:
            event_dict = raw_event.model_dump(mode="json")
            # Convert timestamp to ISO-8601 string for storage
            ts = raw_event.timestamp
            if hasattr(ts, "isoformat"):
                event_dict["timestamp"] = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
            inserted = await upsert_event(event_dict, db)
            if inserted:
                # Only count genuinely NEW insertions — idempotent duplicates don't count
                accepted.append(event_dict)
            # No error for duplicates — they are silently ignored (idempotency)
        except Exception as exc:  # noqa: BLE001
            errors.append(
                IngestError(
                    event_id=getattr(raw_event, "event_id", None),
                    reason=str(exc),
                )
            )

    try:
        await db.commit()
    except Exception as exc:
        logger.error("DB commit failed during ingest: %s", exc)
        raise DBUnavailableError("Database commit failed") from exc

    # Collect store_ids to correlate
    store_ids = list({e["store_id"] for e in accepted})

    # Kick off background tasks (non-blocking)
    for store_id in store_ids:
        background_tasks.add_task(_update_visitor_sessions, store_id)
        background_tasks.add_task(_correlate_pos_transactions, store_id)

    return IngestResponse(
        accepted_count=len(accepted),
        rejected_count=len(errors),
        errors=errors,
    )


# ---------------------------------------------------------------------------
# Background task: update visitor_sessions from ENTRY / EXIT / REENTRY events
# ---------------------------------------------------------------------------

async def _update_visitor_sessions(store_id: str) -> None:
    """
    Scan recent ENTRY, EXIT, REENTRY events and keep visitor_sessions table in sync.
    """
    from app.database import AsyncSessionLocal  # avoid circular import

    async with AsyncSessionLocal() as db:
        try:
            # Fetch ENTRY events not yet reflected in sessions
            result = await db.execute(
                text("""
                    SELECT e.visitor_id, e.timestamp, e.is_staff
                    FROM events e
                    LEFT JOIN visitor_sessions vs
                        ON vs.visitor_id = e.visitor_id AND vs.store_id = e.store_id
                    WHERE e.event_type = 'ENTRY'
                      AND e.store_id = :store_id
                      AND vs.session_id IS NULL
                """),
                {"store_id": store_id},
            )
            entry_rows = result.fetchall()
            for row in entry_rows:
                session_id = str(uuid.uuid4())
                await db.execute(
                    text("""
                        INSERT OR IGNORE INTO visitor_sessions
                            (session_id, store_id, visitor_id, entry_time, is_staff)
                        VALUES (:sid, :store_id, :visitor_id, :entry_time, :is_staff)
                    """),
                    {
                        "sid": session_id,
                        "store_id": store_id,
                        "visitor_id": row[0],
                        "entry_time": row[1],
                        "is_staff": row[2],
                    },
                )

            # Update exit_time for EXIT events
            await db.execute(
                text("""
                    UPDATE visitor_sessions
                    SET exit_time = (
                        SELECT e.timestamp FROM events e
                        WHERE e.event_type = 'EXIT'
                          AND e.visitor_id = visitor_sessions.visitor_id
                          AND e.store_id = :store_id
                        ORDER BY e.timestamp DESC LIMIT 1
                    )
                    WHERE store_id = :store_id AND exit_time IS NULL
                """),
                {"store_id": store_id},
            )

            # Increment reentry_count for REENTRY events
            result2 = await db.execute(
                text("""
                    SELECT visitor_id FROM events
                    WHERE event_type = 'REENTRY' AND store_id = :store_id
                """),
                {"store_id": store_id},
            )
            for (vid,) in result2.fetchall():
                await db.execute(
                    text("""
                        UPDATE visitor_sessions
                        SET reentry_count = reentry_count + 1
                        WHERE visitor_id = :visitor_id AND store_id = :store_id
                          AND reentry_count = 0
                    """),
                    {"visitor_id": vid, "store_id": store_id},
                )

            await db.commit()
        except Exception as exc:  # noqa: BLE001
            logger.error("Session update failed for %s: %s", store_id, exc)


# ---------------------------------------------------------------------------
# Background task: POS transaction correlation
# ---------------------------------------------------------------------------

async def _correlate_pos_transactions(store_id: str) -> None:
    """
    For each POS transaction without matched_visitor_id:
      - Find visitors in billing zone within 5 min before the transaction.
      - If exactly one match → set matched_visitor_id, mark session is_converted=True.
      - If multiple → pick most recent BILLING_QUEUE_JOIN.
      - If zero → leave unmatched.

    Also emit BILLING_QUEUE_ABANDON for visitors who joined queue >10 min ago
    but have no matching POS transaction.
    """
    from app.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        try:
            # Fetch unmatched POS transactions for this store
            result = await db.execute(
                text("""
                    SELECT transaction_id, timestamp
                    FROM pos_transactions
                    WHERE store_id = :store_id
                      AND matched_visitor_id IS NULL
                """),
                {"store_id": store_id},
            )
            txns = result.fetchall()

            for txn_id, txn_ts in txns:
                # Find visitors in billing zone 5 min before transaction
                match_result = await db.execute(
                    text("""
                        SELECT visitor_id, timestamp
                        FROM events
                        WHERE event_type = 'BILLING_QUEUE_JOIN'
                          AND store_id = :store_id
                          AND timestamp <= :txn_ts
                          AND timestamp >= datetime(:txn_ts, '-5 minutes')
                          AND is_staff = 0
                        ORDER BY timestamp DESC
                    """),
                    {"store_id": store_id, "txn_ts": txn_ts},
                )
                matches = match_result.fetchall()

                if matches:
                    # Best match: most recent BILLING_QUEUE_JOIN (first result due to ORDER BY DESC)
                    matched_visitor = matches[0][0]
                    # Update pos_transactions
                    await db.execute(
                        text("""
                            UPDATE pos_transactions
                            SET matched_visitor_id = :visitor_id
                            WHERE transaction_id = :txn_id
                        """),
                        {"visitor_id": matched_visitor, "txn_id": txn_id},
                    )
                    # Mark session as converted
                    await db.execute(
                        text("""
                            UPDATE visitor_sessions
                            SET is_converted = 1
                            WHERE visitor_id = :visitor_id
                              AND store_id = :store_id
                        """),
                        {"visitor_id": matched_visitor, "store_id": store_id},
                    )

            # Emit BILLING_QUEUE_ABANDON for visitors who queued >10 min with no POS match
            abandon_result = await db.execute(
                text("""
                    SELECT e.visitor_id, e.camera_id, e.timestamp, e.zone_id
                    FROM events e
                    WHERE e.event_type = 'BILLING_QUEUE_JOIN'
                      AND e.store_id = :store_id
                      AND e.timestamp <= datetime('now', '-10 minutes')
                      AND NOT EXISTS (
                          SELECT 1 FROM pos_transactions pt
                          WHERE pt.matched_visitor_id = e.visitor_id
                            AND pt.store_id = :store_id
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM events e2
                          WHERE e2.event_type = 'BILLING_QUEUE_ABANDON'
                            AND e2.visitor_id = e.visitor_id
                            AND e2.store_id = :store_id
                      )
                """),
                {"store_id": store_id},
            )
            abandon_rows = abandon_result.fetchall()

            for visitor_id, camera_id, join_ts, zone_id in abandon_rows:
                abandon_event_id = str(uuid.uuid4())
                now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                await db.execute(
                    text("""
                        INSERT OR IGNORE INTO events (
                            event_id, store_id, camera_id, visitor_id,
                            event_type, timestamp, zone_id, dwell_ms,
                            is_staff, confidence, session_seq
                        ) VALUES (
                            :event_id, :store_id, :camera_id, :visitor_id,
                            'BILLING_QUEUE_ABANDON', :timestamp, :zone_id, 0,
                            0, 1.0, 0
                        )
                    """),
                    {
                        "event_id": abandon_event_id,
                        "store_id": store_id,
                        "camera_id": camera_id,
                        "visitor_id": visitor_id,
                        "timestamp": now_ts,
                        "zone_id": zone_id,
                    },
                )

            await db.commit()
        except Exception as exc:  # noqa: BLE001
            logger.error("POS correlation failed for %s: %s", store_id, exc)
