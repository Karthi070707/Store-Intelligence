"""
pipeline/staff_classifier.py — HSV-based staff uniform detection

Classifies whether a detected person is a staff member based on
uniform colour analysis in HSV colour space.

Design note:
# 30% threshold chosen to handle partial uniform coverage (aprons, vests)
# while avoiding false positives from customers in similar colours
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional, Tuple

import numpy as np

try:
    import cv2
    CV2_AVAILABLE = True
except ImportError:
    CV2_AVAILABLE = False
    logging.warning("opencv not available — staff classification disabled")

logger = logging.getLogger(__name__)

# Default HSV ranges for staff uniform (magenta/purple — typical retail uniform)
DEFAULT_STAFF_HSV = [
    {"h_min": 130, "h_max": 170, "s_min": 50, "s_max": 255, "v_min": 50, "v_max": 255}
]

# Cache for loaded layout
_layout_cache: Optional[dict] = None


def _load_staff_hsv(store_layout_path: str, store_id: str) -> list:
    """Load staff uniform HSV ranges from store_layout.json."""
    global _layout_cache
    if _layout_cache is None:
        if os.path.exists(store_layout_path):
            with open(store_layout_path, encoding="utf-8") as f:
                _layout_cache = json.load(f)
        else:
            _layout_cache = {}

    store_data = _layout_cache.get("stores", {}).get(store_id, {})
    return store_data.get("staff_uniform_hsv", [])


def classify_staff(
    frame: "np.ndarray",
    bbox: Tuple[int, int, int, int],
    store_layout_path: str = "data/store_layout.json",
    store_id: str = "",
) -> Tuple[bool, float]:
    """
    Determine if a detected person is staff based on uniform colour.

    Args:
        frame: Full video frame (BGR, HxWx3)
        bbox: Bounding box (x1, y1, x2, y2) in pixel coordinates
        store_layout_path: Path to store_layout.json
        store_id: Store identifier for layout lookup

    Returns:
        (is_staff: bool, confidence: float)

    # 30% threshold chosen to handle partial uniform coverage (aprons, vests)
    # while avoiding false positives from customers in similar colours
    """
    if not CV2_AVAILABLE or frame is None:
        return False, 0.5  # Fallback: no OpenCV

    x1, y1, x2, y2 = bbox
    h = y2 - y1

    if h <= 0 or (x2 - x1) <= 0:
        return False, 0.5  # Invalid bbox

    # Crop the torso region: top 40% to 70% of bounding box height
    torso_y1 = y1 + int(h * 0.4)
    torso_y2 = y1 + int(h * 0.70)
    torso_x1 = max(0, x1)
    torso_x2 = min(frame.shape[1], x2)
    torso_y1 = max(0, torso_y1)
    torso_y2 = min(frame.shape[0], torso_y2)

    if torso_y2 <= torso_y1 or torso_x2 <= torso_x1:
        return False, 0.5

    torso_region = frame[torso_y1:torso_y2, torso_x1:torso_x2]
    if torso_region.size == 0:
        return False, 0.5

    # Convert to HSV
    hsv = cv2.cvtColor(torso_region, cv2.COLOR_BGR2HSV)

    # Load staff uniform HSV ranges
    staff_hsv_ranges = _load_staff_hsv(store_layout_path, store_id)
    if not staff_hsv_ranges:
        # If no config: default is_staff=False with confidence=0.5
        return False, 0.5

    total_pixels = hsv.shape[0] * hsv.shape[1]
    if total_pixels == 0:
        return False, 0.5

    # Count pixels matching any uniform HSV range
    match_mask = np.zeros((hsv.shape[0], hsv.shape[1]), dtype=np.uint8)
    for hsv_range in staff_hsv_ranges:
        lower = np.array([
            hsv_range.get("h_min", 0),
            hsv_range.get("s_min", 0),
            hsv_range.get("v_min", 0),
        ], dtype=np.uint8)
        upper = np.array([
            hsv_range.get("h_max", 180),
            hsv_range.get("s_max", 255),
            hsv_range.get("v_max", 255),
        ], dtype=np.uint8)
        range_mask = cv2.inRange(hsv, lower, upper)
        match_mask = cv2.bitwise_or(match_mask, range_mask)

    match_count = int(np.count_nonzero(match_mask))
    match_ratio = match_count / total_pixels

    # If >30% of torso pixels match: is_staff=True, confidence = match_ratio
    if match_ratio > 0.30:
        return True, min(1.0, round(match_ratio, 4))
    else:
        # Confidence = 1.0 - match_ratio (certainty it's NOT staff)
        return False, min(1.0, round(1.0 - match_ratio, 4))
