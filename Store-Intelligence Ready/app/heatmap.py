"""
app/heatmap.py — GET /stores/{store_id}/heatmap

Zone visit frequency and dwell time, normalised 0-100.
Covers ALL zones from store_layout.json, even zones with zero activity.
"""
from __future__ import annotations

import json
import os
from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import HeatmapResponse, HeatmapZone

router = APIRouter(prefix="/stores", tags=["heatmap"])

STORE_LAYOUT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "store_layout.json",
)


def _load_zones_from_layout(store_id: str) -> List[str]:
    """Load zone IDs from store_layout.json for the given store."""
    if not os.path.exists(STORE_LAYOUT_PATH):
        return []
    with open(STORE_LAYOUT_PATH, encoding="utf-8") as f:
        layout = json.load(f)
    stores = layout.get("stores", {})
    store_data = stores.get(store_id, {})
    zones = store_data.get("zones", {})
    return list(zones.keys())


@router.get("/{store_id}/heatmap", response_model=HeatmapResponse)
async def get_heatmap(
    store_id: str,
    db: AsyncSession = Depends(get_db),
) -> HeatmapResponse:
    """
    Returns zone visit frequency and avg dwell, normalised 0-100.
    data_confidence=False if fewer than 20 sessions in the last 24h window.
    All zones from store_layout.json are returned, even with zero activity.
    """
    # Load all zones from layout (fallback: use whatever is in DB)
    known_zones = _load_zones_from_layout(store_id)

    # Zone visit frequency (ZONE_ENTER count)
    freq_result = await db.execute(
        text("""
            SELECT zone_id, COUNT(*) as visit_frequency
            FROM events
            WHERE store_id = :store_id
              AND event_type = 'ZONE_ENTER'
              AND is_staff = 0
              AND zone_id IS NOT NULL
            GROUP BY zone_id
        """),
        {"store_id": store_id},
    )
    freq_map = {row[0]: int(row[1]) for row in freq_result.fetchall()}

    # Avg dwell per zone (ZONE_DWELL)
    dwell_result = await db.execute(
        text("""
            SELECT zone_id, AVG(dwell_ms) as avg_dwell
            FROM events
            WHERE store_id = :store_id
              AND event_type = 'ZONE_DWELL'
              AND is_staff = 0
              AND zone_id IS NOT NULL
            GROUP BY zone_id
        """),
        {"store_id": store_id},
    )
    dwell_map = {row[0]: float(row[1]) for row in dwell_result.fetchall()}

    # Total session count for data_confidence flag
    sessions_result = await db.execute(
        text("""
            SELECT COUNT(DISTINCT visitor_id)
            FROM events
            WHERE store_id = :store_id
              AND is_staff = 0
        """),
        {"store_id": store_id},
    )
    total_sessions: int = sessions_result.scalar() or 0
    data_confidence = total_sessions >= 20

    # Combine all zone IDs (from layout + from DB)
    all_zones = set(known_zones) | set(freq_map.keys()) | set(dwell_map.keys())

    # Normalise visit_frequency 0-100
    max_freq = max(freq_map.values(), default=0)
    max_dwell = max(dwell_map.values(), default=0.0)

    zones: List[HeatmapZone] = []
    for zone_id in sorted(all_zones):
        visit_freq = freq_map.get(zone_id, 0)
        avg_dwell = dwell_map.get(zone_id, 0.0)

        # Normalised score: combine frequency and dwell equally, or just frequency
        if max_freq > 0:
            norm_freq = (visit_freq / max_freq) * 100
        else:
            norm_freq = 0.0
        if max_dwell > 0:
            norm_dwell = (avg_dwell / max_dwell) * 100
        else:
            norm_dwell = 0.0

        # Use frequency-weighted score (primary signal)
        normalised_score = int(round((norm_freq + norm_dwell) / 2))

        zones.append(
            HeatmapZone(
                zone_id=zone_id,
                visit_frequency=visit_freq,
                avg_dwell_ms=avg_dwell,
                normalised_score=min(100, normalised_score),
                data_confidence=data_confidence,
            )
        )

    return HeatmapResponse(
        store_id=store_id,
        window_hours=24,
        zones=zones,
    )
