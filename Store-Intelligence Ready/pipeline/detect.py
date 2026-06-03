"""
pipeline/detect.py — Main detection and tracking script

Processes a single video clip using:
- YOLOv8m for person detection (pre-trained COCO, no training needed)
- ByteTrack for multi-object tracking (built into ultralytics)
- torchreid OSNet for Re-ID (via tracker.py)
- HSV staff classification (via staff_classifier.py)
- Shapely zone mapping (via zone_mapper.py)
- Event emission (via emit.py)

Usage:
    python pipeline/detect.py \
        --clip data/clips/STORE_BLR_002/CAM_ENTRY_01.mp4 \
        --store-layout data/store_layout.json \
        --store-id STORE_BLR_002 \
        --camera-id CAM_ENTRY_01

Edge case handling (all inline comments explain decisions for interview questions):
- Group entry: YOLOv8 detects individual bboxes naturally — groups handled automatically
- Empty periods: zero events during empty frames — API returns 0s, not errors
- Partial occlusion: low-confidence detections kept, not dropped
- Camera overlap: CAM_FLOOR defers ENTRY/EXIT to CAM_ENTRY
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from typing import Dict, Optional, Set, Tuple

import numpy as np

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

try:
    import cv2
    CV2_AVAILABLE = True
except ImportError:
    CV2_AVAILABLE = False
    logger.error("OpenCV not installed. Cannot process video.")

try:
    from ultralytics import YOLO
    YOLO_AVAILABLE = True
except ImportError:
    YOLO_AVAILABLE = False
    logger.error("ultralytics not installed. Cannot run detection.")

from pipeline.tracker import VisitorTracker
from pipeline.staff_classifier import classify_staff
from pipeline.zone_mapper import ZoneMapper
from pipeline import emit as event_emitter


def _load_clip_start_time(layout_path: str, store_id: str, camera_id: str) -> str:
    """Load the clip start timestamp from store_layout.json."""
    if not os.path.exists(layout_path):
        return "2026-03-03T08:00:00Z"  # Fallback
    with open(layout_path) as f:
        layout = json.load(f)
    store_data = layout.get("stores", {}).get(store_id, {})
    cameras = store_data.get("cameras", {})
    camera_data = cameras.get(camera_id, {})
    return camera_data.get("clip_start_time", "2026-03-03T08:00:00Z")


def _get_entry_line(layout_path: str, store_id: str, camera_id: str) -> Optional[Tuple]:
    """
    Load the entry threshold line from store_layout.json.
    Returns ((x1, y1), (x2, y2)) or None.
    """
    if not os.path.exists(layout_path):
        return None
    with open(layout_path) as f:
        layout = json.load(f)
    store_data = layout.get("stores", {}).get(store_id, {})
    cameras = store_data.get("cameras", {})
    camera_data = cameras.get(camera_id, {})
    line = camera_data.get("entry_line")
    if line and len(line) == 2:
        return tuple(line[0]), tuple(line[1])
    return None


def _crosses_line(
    prev_centre: Tuple[float, float],
    curr_centre: Tuple[float, float],
    line: Tuple[Tuple, Tuple],
) -> Optional[str]:
    """
    Detect if bbox centre crosses the entry threshold line using cross-product.
    Returns 'ENTRY' (crosses line from top-to-bottom direction) or 'EXIT' (reverse), or None.

    Uses the sign-of-side-of-line method: point P is on a side of line (A→B)
    based on sign of (B.x - A.x)*(P.y - A.y) - (B.y - A.y)*(P.x - A.x).
    A sign change between prev and curr means the line was crossed.

    This handles horizontal, vertical, and diagonal entry lines correctly.
    Direction convention: positive-to-negative cross product = ENTRY (inbound).
    """
    if line is None:
        return None

    (lx1, ly1), (lx2, ly2) = line
    dx = lx2 - lx1
    dy = ly2 - ly1

    def _side(px: float, py: float) -> float:
        """Signed side of line: positive = one side, negative = other side."""
        return dx * (py - ly1) - dy * (px - lx1)

    prev_side = _side(prev_centre[0], prev_centre[1])
    curr_side = _side(curr_centre[0], curr_centre[1])

    # No crossing if both on same side or either exactly on the line
    if prev_side == 0 or curr_side == 0:
        return None
    if (prev_side > 0) == (curr_side > 0):
        return None

    # Crossing detected — determine direction
    # prev_side > 0 and curr_side < 0 → top-to-bottom → ENTRY
    if prev_side > 0 and curr_side < 0:
        return "ENTRY"
    # prev_side < 0 and curr_side > 0 → bottom-to-top → EXIT
    return "EXIT"


def process_clip(
    clip_path: str,
    store_layout_path: str,
    store_id: str,
    camera_id: str,
    model_path: str = "yolov8m.pt",
) -> int:
    """
    Process a single video clip and emit all events.
    Returns the total number of events emitted.
    """
    if not CV2_AVAILABLE or not YOLO_AVAILABLE:
        logger.error("Required libraries not available. Skipping clip: %s", clip_path)
        return 0

    if not os.path.exists(clip_path):
        logger.error("Clip not found: %s", clip_path)
        return 0

    # Load metadata
    clip_start_time = _load_clip_start_time(store_layout_path, store_id, camera_id)
    entry_line = _get_entry_line(store_layout_path, store_id, camera_id)

    # Initialise components
    zone_mapper = ZoneMapper(store_layout_path, store_id)
    tracker = VisitorTracker()

    logger.info("Processing %s / %s — %s", store_id, camera_id, clip_path)

    # Load YOLOv8m model (inference only — NO training)
    model = YOLO(model_path)

    cap = cv2.VideoCapture(clip_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 15.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    # Track previous frame centres for line crossing detection
    prev_centres: Dict[int, Tuple[float, float]] = {}
    # Track current zones per track_id for ZONE_ENTER/EXIT/DWELL detection
    current_zones: Dict[int, Optional[str]] = {}
    # Track which track IDs were visible last frame (for exit detection)
    last_seen_tracks: Set[int] = set()

    events_emitted = 0
    frame_number = 0

    logger.info("FPS: %.1f, Total frames: %d", fps, total_frames)

    # Process using ByteTrack (built into ultralytics — no separate library needed)
    results_gen = model.track(
        source=clip_path,
        tracker="bytetrack.yaml",
        classes=[0],  # Person class only
        stream=True,
        verbose=False,
    )

    for result in results_gen:
        frame_number += 1

        # Get the actual frame for staff classification and Re-ID
        frame = result.orig_img  # BGR numpy array

        # Apply CLAHE for lighting normalisation (handles fluorescent vs natural light)
        if frame is not None and frame.size > 0:
            lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
            clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
            lab[:, :, 0] = clahe.apply(lab[:, :, 0])
            frame = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)

        current_track_ids: Set[int] = set()

        if result.boxes is not None and result.boxes.id is not None:
            boxes = result.boxes
            for i, track_id_tensor in enumerate(boxes.id):
                track_id = int(track_id_tensor.item())
                current_track_ids.add(track_id)

                # Extract bbox
                xyxy = boxes.xyxy[i].cpu().numpy()
                x1, y1, x2, y2 = int(xyxy[0]), int(xyxy[1]), int(xyxy[2]), int(xyxy[3])
                bbox = (x1, y1, x2, y2)
                bbox_cx = (x1 + x2) / 2.0
                bbox_cy = (y1 + y2) / 2.0
                det_confidence = float(boxes.conf[i].item())

                # Check camera overlap region
                # Cross-camera deduplication — floor camera defers entry/exit counting to entry camera
                in_overlap = zone_mapper.is_in_overlap_region(bbox_cx, bbox_cy)
                if in_overlap and camera_id == "CAM_FLOOR_01":
                    # In overlap region: suppress ENTRY/EXIT from floor camera
                    camera_overlap = True
                else:
                    camera_overlap = False

                # Staff classification
                is_staff, staff_confidence = classify_staff(
                    frame, bbox, store_layout_path, store_id
                )
                confidence = (det_confidence + staff_confidence) / 2.0

                # Partial occlusion: if bbox confidence < 0.5, still emit the event
                # Low confidence detections are kept not dropped — confidence calibration requirement
                partial_occlusion = det_confidence < 0.5

                # Tracker update → get visitor_id, session_seq, initial event_type
                visitor_id, session_seq, initial_event_type = tracker.update(
                    track_id, frame, bbox
                )

                # Zone mapping
                current_zone = zone_mapper.get_zone(bbox_cx, bbox_cy, camera_id)
                prev_zone = current_zones.get(track_id)

                # Entry/Exit line crossing detection (only on entry camera or non-overlap)
                if not camera_overlap and entry_line is not None:
                    prev_centre = prev_centres.get(track_id)
                    if prev_centre:
                        crossing = _crosses_line(prev_centre, (bbox_cx, bbox_cy), entry_line)
                        if crossing == "ENTRY" and initial_event_type in ("ENTRY", "SEEN"):
                            # Each person gets individual bbox from YOLOv8 — groups handled automatically
                            if initial_event_type == "ENTRY":
                                event_emitter.emit_event(
                                    store_id=store_id, camera_id=camera_id,
                                    visitor_id=visitor_id, event_type="ENTRY",
                                    clip_start_time=clip_start_time,
                                    frame_number=frame_number, fps=fps,
                                    zone_id=None, is_staff=is_staff,
                                    confidence=det_confidence, session_seq=session_seq,
                                    partial_occlusion=partial_occlusion,
                                )
                                events_emitted += 1
                        elif crossing == "EXIT":
                            event_emitter.emit_event(
                                store_id=store_id, camera_id=camera_id,
                                visitor_id=visitor_id, event_type="EXIT",
                                clip_start_time=clip_start_time,
                                frame_number=frame_number, fps=fps,
                                zone_id=None, is_staff=is_staff,
                                confidence=det_confidence, session_seq=session_seq,
                                partial_occlusion=partial_occlusion,
                            )
                            tracker.mark_exited(track_id)
                            events_emitted += 1

                # REENTRY event — emitted on first detection of returning visitor
                if initial_event_type == "REENTRY":
                    event_emitter.emit_event(
                        store_id=store_id, camera_id=camera_id,
                        visitor_id=visitor_id, event_type="REENTRY",
                        clip_start_time=clip_start_time,
                        frame_number=frame_number, fps=fps,
                        zone_id=None, is_staff=is_staff,
                        confidence=det_confidence, session_seq=session_seq,
                    )
                    events_emitted += 1

                # Zone transitions — ZONE_ENTER and ZONE_EXIT
                if current_zone != prev_zone:
                    if prev_zone is not None:
                        # Leaving previous zone
                        event_emitter.emit_event(
                            store_id=store_id, camera_id=camera_id,
                            visitor_id=visitor_id, event_type="ZONE_EXIT",
                            clip_start_time=clip_start_time,
                            frame_number=frame_number, fps=fps,
                            zone_id=prev_zone, is_staff=is_staff,
                            confidence=det_confidence, session_seq=session_seq,
                            camera_overlap=camera_overlap,
                        )
                        event_emitter.reset_dwell(visitor_id, prev_zone)
                        events_emitted += 1

                    if current_zone is not None:
                        # Entering new zone
                        billing_zone = zone_mapper.billing_zone_id
                        is_billing = (current_zone == billing_zone)
                        queue_depth = zone_mapper.get_queue_depth(store_id) if is_billing else None

                        if is_billing and queue_depth is not None and queue_depth > 0:
                            evt_type = "BILLING_QUEUE_JOIN"
                        else:
                            evt_type = "ZONE_ENTER"

                        sku_zone = zone_mapper.get_sku_zone(current_zone)
                        event_emitter.emit_event(
                            store_id=store_id, camera_id=camera_id,
                            visitor_id=visitor_id, event_type=evt_type,
                            clip_start_time=clip_start_time,
                            frame_number=frame_number, fps=fps,
                            zone_id=current_zone, is_staff=is_staff,
                            confidence=det_confidence, session_seq=session_seq,
                            queue_depth=queue_depth, sku_zone=sku_zone,
                            camera_overlap=camera_overlap,
                            partial_occlusion=partial_occlusion,
                        )
                        events_emitted += 1

                    current_zones[track_id] = current_zone

                # Zone dwell: emit every 30 continuous seconds in same zone
                if current_zone is not None:
                    should_dwell, dwell_ms = event_emitter.should_emit_dwell(
                        visitor_id, current_zone, frame_number, fps
                    )
                    if should_dwell:
                        event_emitter.emit_event(
                            store_id=store_id, camera_id=camera_id,
                            visitor_id=visitor_id, event_type="ZONE_DWELL",
                            clip_start_time=clip_start_time,
                            frame_number=frame_number, fps=fps,
                            zone_id=current_zone, dwell_ms=dwell_ms,
                            is_staff=is_staff, confidence=det_confidence,
                            session_seq=session_seq, sku_zone=zone_mapper.get_sku_zone(current_zone),
                        )
                        events_emitted += 1

                # Update previous centre for next frame
                prev_centres[track_id] = (bbox_cx, bbox_cy)

        # Handle track exits (tracks visible last frame but not this frame)
        # Empty periods: if no detections on a frame, emit nothing
        # Empty periods produce zero events — API handles zero-traffic correctly
        disappeared = last_seen_tracks - current_track_ids
        for gone_track_id in disappeared:
            visitor_id = tracker.get_visitor_id(gone_track_id)
            if visitor_id:
                prev_zone = current_zones.get(gone_track_id)
                if prev_zone:
                    # Emit ZONE_EXIT for the zone they were in when they disappeared
                    is_staff_val = False  # Best effort
                    event_emitter.emit_event(
                        store_id=store_id, camera_id=camera_id,
                        visitor_id=visitor_id, event_type="ZONE_EXIT",
                        clip_start_time=clip_start_time,
                        frame_number=frame_number, fps=fps,
                        zone_id=prev_zone, is_staff=is_staff_val,
                        confidence=0.5, session_seq=0,
                    )
                    event_emitter.reset_dwell(visitor_id, prev_zone)
                    events_emitted += 1
                    current_zones.pop(gone_track_id, None)
            tracker.mark_exited(gone_track_id)

        last_seen_tracks = current_track_ids.copy()

        if frame_number % 450 == 0:  # Log every 30 seconds at 15fps
            logger.info(
                "Processed %d/%d frames, %d events emitted",
                frame_number, total_frames, events_emitted,
            )

    cap.release()
    logger.info("Clip complete: %d events emitted for %s/%s", events_emitted, store_id, camera_id)
    return events_emitted


def main():
    parser = argparse.ArgumentParser(description="Retail store detection pipeline")
    parser.add_argument("--clip", required=True, help="Path to video clip")
    parser.add_argument("--store-layout", required=True, help="Path to store_layout.json")
    parser.add_argument("--store-id", required=True, help="Store ID (e.g. STORE_BLR_002)")
    parser.add_argument("--camera-id", required=True, help="Camera ID (e.g. CAM_ENTRY_01)")
    parser.add_argument("--model", default="yolov8m.pt", help="YOLO model path or name")
    args = parser.parse_args()

    n_events = process_clip(
        clip_path=args.clip,
        store_layout_path=args.store_layout,
        store_id=args.store_id,
        camera_id=args.camera_id,
        model_path=args.model,
    )
    print(f"Done. {n_events} events written to events_output/{args.store_id}/{args.camera_id}.jsonl")


if __name__ == "__main__":
    main()
