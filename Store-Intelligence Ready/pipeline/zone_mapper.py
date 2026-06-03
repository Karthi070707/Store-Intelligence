"""
pipeline/zone_mapper.py — Shapely point-in-polygon zone mapping

Loads zone polygon definitions from store_layout.json.
Maps bounding box centre coordinates to zone IDs per camera.

Design note:
- Camera-scoped zones prevent false assignments when cameras overlap.
- Shapely is used for accurate polygon intersection.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Dict, List, Optional, Set

try:
    from shapely.geometry import Point, Polygon
    SHAPELY_AVAILABLE = True
except ImportError:
    SHAPELY_AVAILABLE = False
    logging.warning("shapely not installed — zone mapping will use bounding box fallback")

logger = logging.getLogger(__name__)


class ZoneMapper:
    """
    Maps pixel coordinates to zone IDs using polygon containment checks.

    # Camera-scoped zones prevent false zone assignments when cameras overlap
    """

    def __init__(self, store_layout_path: str, store_id: str):
        self.store_id = store_id
        self.zones: Dict[str, dict] = {}  # zone_id -> {polygon, camera_ids}
        self.camera_zones: Dict[str, List[str]] = {}  # camera_id -> [zone_ids]
        self.overlap_regions: List[dict] = []
        self.billing_zone_id: Optional[str] = None
        self._load_layout(store_layout_path)

    def _load_layout(self, layout_path: str) -> None:
        if not os.path.exists(layout_path):
            logger.warning("store_layout.json not found at %s — zone mapping disabled", layout_path)
            return

        with open(layout_path, encoding="utf-8") as f:
            layout = json.load(f)

        store_data = layout.get("stores", {}).get(self.store_id, {})
        raw_zones = store_data.get("zones", {})

        for zone_id, zone_def in raw_zones.items():
            polygon_coords = zone_def.get("polygon", [])
            camera_ids = zone_def.get("camera_ids", [])
            zone_type = zone_def.get("type", "")

            if zone_type == "billing":
                self.billing_zone_id = zone_id

            poly = None
            if SHAPELY_AVAILABLE and len(polygon_coords) >= 3:
                try:
                    poly = Polygon(polygon_coords)
                except Exception as exc:
                    logger.warning("Invalid polygon for zone %s: %s", zone_id, exc)

            self.zones[zone_id] = {
                "polygon": poly,
                "raw_coords": polygon_coords,
                "camera_ids": camera_ids,
                "sku_zone": zone_def.get("sku_zone"),
            }

            for cam_id in camera_ids:
                if cam_id not in self.camera_zones:
                    self.camera_zones[cam_id] = []
                self.camera_zones[cam_id].append(zone_id)

        # Load overlap regions if defined
        self.overlap_regions = store_data.get("overlap_regions", [])

    def get_zone(self, bbox_cx: float, bbox_cy: float, camera_id: str) -> Optional[str]:
        """
        Return the zone_id for the given bounding box centre, scoped to camera.

        Returns None if the point is in no zone.
        # Camera-scoped zones prevent false zone assignments when cameras overlap
        """
        if not self.zones:
            return None

        point = Point(bbox_cx, bbox_cy) if SHAPELY_AVAILABLE else None
        candidate_zones = self.camera_zones.get(camera_id, list(self.zones.keys()))

        for zone_id in candidate_zones:
            zone = self.zones[zone_id]
            poly = zone.get("polygon")

            if SHAPELY_AVAILABLE and poly is not None and point is not None:
                if poly.contains(point):
                    return zone_id
            else:
                # Fallback: simple bounding box check
                coords = zone.get("raw_coords", [])
                if coords and _point_in_bbox(bbox_cx, bbox_cy, coords):
                    return zone_id

        return None

    def is_in_overlap_region(self, bbox_cx: float, bbox_cy: float) -> bool:
        """Check if the point is in a camera overlap region."""
        if not SHAPELY_AVAILABLE:
            return False
        point = Point(bbox_cx, bbox_cy)
        for region in self.overlap_regions:
            coords = region.get("polygon", [])
            if len(coords) >= 3:
                try:
                    poly = Polygon(coords)
                    if poly.contains(point):
                        return True
                except Exception:
                    pass
        return False

    def get_queue_depth(self, store_id: str) -> int:
        """
        Get the current billing zone queue depth from emit.py's occupancy tracker.
        """
        from pipeline.emit import _zone_occupancy
        if self.billing_zone_id is None:
            return 0
        return len(_zone_occupancy.get(store_id, {}).get(self.billing_zone_id, set()))

    def get_sku_zone(self, zone_id: Optional[str]) -> Optional[str]:
        if zone_id and zone_id in self.zones:
            return self.zones[zone_id].get("sku_zone")
        return None

    @property
    def all_zone_ids(self) -> Set[str]:
        return set(self.zones.keys())


def _point_in_bbox(cx: float, cy: float, coords: list) -> bool:
    """Axis-aligned bounding box fallback for when shapely is unavailable."""
    xs = [c[0] for c in coords]
    ys = [c[1] for c in coords]
    return min(xs) <= cx <= max(xs) and min(ys) <= cy <= max(ys)
