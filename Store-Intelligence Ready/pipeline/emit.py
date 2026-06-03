"""
pipeline/emit.py — Event schema builder and JSONL file writer

Generates valid events matching the required schema exactly.
Writes to events_output/{store_id}/{camera_id}.jsonl (one JSON object per line).
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

from app.models import Event, EventType, EventMetadata

logger = logging.getLogger(__name__)

EVENTS_OUTPUT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "events_output",
)

# Track zone dwell start times per visitor+zone: {visitor_id: {zone_id: (start_frame, last_dwell_emit_frame)}}
_dwell_trackers: dict = {}

# Track in-zone visitors for queue depth: {store_id: {zone_id: set(visitor_ids)}}
_zone_occupancy: dict = {}


def _compute_timestamp(clip_start_time: str, frame_number: int, fps: float) -> str:
    """Compute ISO-8601 UTC timestamp from clip start + frame offset."""
    start_dt = datetime.fromisoformat(clip_start_time.replace("Z", "+00:00"))
    offset_seconds = frame_number / fps
    event_dt = start_dt + timedelta(seconds=offset_seconds)
    return event_dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _get_queue_depth(store_id: str, zone_id: str) -> int:
    """Get current number of visitors in a zone (approximated from billing zone occupancy)."""
    return len(_zone_occupancy.get(store_id, {}).get(zone_id, set()))


def emit_event(
    store_id: str,
    camera_id: str,
    visitor_id: str,
    event_type: str,
    clip_start_time: str,
    frame_number: int,
    fps: float,
    zone_id: Optional[str] = None,
    dwell_ms: int = 0,
    is_staff: bool = False,
    confidence: float = 1.0,
    queue_depth: Optional[int] = None,
    sku_zone: Optional[str] = None,
    session_seq: int = 0,
    partial_occlusion: bool = False,
    camera_overlap: bool = False,
) -> Optional[dict]:
    """
    Build and write one event to events_output/{store_id}/{camera_id}.jsonl.

    Returns the event dict on success, None on schema validation failure.
    """
    event_id = str(uuid.uuid4())
    timestamp = _compute_timestamp(clip_start_time, frame_number, fps)

    # Update zone occupancy for queue depth tracking
    if store_id not in _zone_occupancy:
        _zone_occupancy[store_id] = {}
    if zone_id:
        if zone_id not in _zone_occupancy[store_id]:
            _zone_occupancy[store_id][zone_id] = set()
        if event_type in ("ZONE_ENTER", "BILLING_QUEUE_JOIN"):
            _zone_occupancy[store_id][zone_id].add(visitor_id)
        elif event_type in ("ZONE_EXIT", "EXIT"):
            _zone_occupancy[store_id][zone_id].discard(visitor_id)

    # Auto-compute queue_depth for billing zone
    if event_type == "BILLING_QUEUE_JOIN" and queue_depth is None and zone_id:
        queue_depth = _get_queue_depth(store_id, zone_id)

    event_dict = {
        "event_id": event_id,
        "store_id": store_id,
        "camera_id": camera_id,
        "visitor_id": visitor_id,
        "event_type": event_type,
        "timestamp": timestamp,
        "zone_id": zone_id,
        "dwell_ms": dwell_ms,
        "is_staff": is_staff,
        "confidence": round(confidence, 4),
        "metadata": {
            "queue_depth": queue_depth,
            "sku_zone": sku_zone,
            "session_seq": session_seq,
            "partial_occlusion": partial_occlusion if partial_occlusion else None,
            "camera_overlap": camera_overlap if camera_overlap else None,
        },
    }

    # Validate schema before writing
    try:
        Event(**event_dict)
    except Exception as exc:
        logger.warning("Schema validation failed for event, skipping: %s | event=%s", exc, event_dict)
        return None

    # Write to output file
    output_dir = os.path.join(EVENTS_OUTPUT_DIR, store_id)
    os.makedirs(output_dir, exist_ok=True)
    output_file = os.path.join(output_dir, f"{camera_id}.jsonl")

    with open(output_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(event_dict) + "\n")

    return event_dict


def should_emit_dwell(visitor_id: str, zone_id: str, current_frame: int, fps: float) -> tuple[bool, int]:
    """
    Check if a ZONE_DWELL event should be emitted for a visitor in a zone.
    Emits every 30 continuous seconds in the same zone.

    Returns (should_emit, dwell_ms).
    """
    key = (visitor_id, zone_id)
    if key not in _dwell_trackers:
        _dwell_trackers[key] = {"start_frame": current_frame, "last_emit_frame": current_frame}
        return False, 0

    tracker = _dwell_trackers[key]
    frames_in_zone = current_frame - tracker["start_frame"]
    dwell_ms = int((frames_in_zone / fps) * 1000)
    frames_since_last_emit = current_frame - tracker["last_emit_frame"]
    seconds_since_emit = frames_since_last_emit / fps

    if seconds_since_emit >= 30:
        tracker["last_emit_frame"] = current_frame
        return True, dwell_ms

    return False, dwell_ms


def reset_dwell(visitor_id: str, zone_id: str) -> None:
    """Reset dwell tracker when visitor exits a zone."""
    key = (visitor_id, zone_id)
    _dwell_trackers.pop(key, None)
