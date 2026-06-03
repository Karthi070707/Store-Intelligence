"""
pipeline/tracker.py — Visitor tracking with Re-ID using torchreid OSNet

Maintains a registry of active and recently-exited visitors.
Assigns visitor_id tokens of format VIS_{6-char hex}.
Implements Re-ID: if a person exits and re-enters within 5 minutes
AND embedding distance < 0.4, assign SAME visitor_id and emit REENTRY.

Design notes:
# Re-ID window: 5 min chosen to catch brief exits like phone calls
# without inflating re-entry count for next-day visits

# Threshold 0.4 chosen empirically — lower = too strict (misses real re-entries),
# higher = false matches between different people in same direction
"""
from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

REID_WINDOW_SECONDS = 300  # 5 minutes
REID_DISTANCE_THRESHOLD = 0.4  # cosine distance

# Attempt to load torchreid (optional dependency)
try:
    import torchreid
    import torch
    from torchvision import transforms
    from PIL import Image
    TORCHREID_AVAILABLE = True
except ImportError:
    TORCHREID_AVAILABLE = False
    logger.warning(
        "torchreid not installed — Re-ID will use track trajectory only (no embedding distance)"
    )


@dataclass
class VisitorRecord:
    visitor_id: str
    track_id: int
    last_seen: float  # unix timestamp
    embedding: Optional[np.ndarray]
    session_seq: int
    zone_history: List[str] = field(default_factory=list)
    is_exited: bool = False
    exit_time: Optional[float] = None


class VisitorTracker:
    """
    Assigns and manages visitor_id tokens across track IDs.

    Maintains two registries:
    - active_tracks: {track_id: VisitorRecord} for currently visible visitors
    - exited_visitors: {visitor_id: VisitorRecord} for recently exited visitors (Re-ID candidates)
    """

    def __init__(self):
        self.active_tracks: Dict[int, VisitorRecord] = {}
        self.exited_visitors: Dict[str, VisitorRecord] = {}
        self._reid_model = None
        self._reid_transform = None
        self._init_reid_model()

    def _init_reid_model(self) -> None:
        """Load torchreid OSNet model (pre-trained, NO training)."""
        if not TORCHREID_AVAILABLE:
            return
        try:
            self._reid_model = torchreid.models.build_model(
                name="osnet_x1_0",
                num_classes=1000,
                pretrained=True,
            )
            self._reid_model.eval()
            self._reid_transform = transforms.Compose([
                transforms.Resize((256, 128)),
                transforms.ToTensor(),
                transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
            ])
            logger.info("torchreid OSNet loaded successfully.")
        except Exception as exc:
            logger.warning("Failed to load torchreid model: %s — using trajectory Re-ID only", exc)
            self._reid_model = None

    def get_embedding(self, frame: np.ndarray, bbox: Tuple[int, int, int, int]) -> Optional[np.ndarray]:
        """
        Extract appearance embedding using torchreid OSNet (pre-trained weights).
        Returns None if model unavailable or extraction fails.
        """
        if self._reid_model is None or frame is None:
            return None

        try:
            import cv2
            x1, y1, x2, y2 = bbox
            crop = frame[max(0, y1):y2, max(0, x1):x2]
            if crop.size == 0:
                return None
            crop_rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
            pil_img = Image.fromarray(crop_rgb)
            tensor = self._reid_transform(pil_img).unsqueeze(0)
            import torch
            with torch.no_grad():
                embedding = self._reid_model(tensor).squeeze().numpy()
            # L2 normalise
            norm = np.linalg.norm(embedding)
            if norm > 0:
                embedding = embedding / norm
            return embedding
        except Exception as exc:
            logger.debug("Embedding extraction failed: %s", exc)
            return None

    @staticmethod
    def _cosine_distance(a: np.ndarray, b: np.ndarray) -> float:
        """Compute cosine distance between two L2-normalised embeddings."""
        similarity = float(np.dot(a, b))
        return 1.0 - similarity  # distance = 1 - cosine_similarity

    def update(
        self,
        track_id: int,
        frame: Optional[np.ndarray],
        bbox: Tuple[int, int, int, int],
        timestamp: Optional[float] = None,
    ) -> Tuple[str, int, str]:
        """
        Update tracker for a given track_id.

        Returns:
            (visitor_id, session_seq, event_type)
            where event_type is 'ENTRY', 'REENTRY', or 'SEEN' (no new event needed)
        """
        now = timestamp or time.time()

        # Prune expired exited visitors (> 5 min old)
        # Re-ID window: 5 min chosen to catch brief exits like phone calls
        # without inflating re-entry count for next-day visits
        expired = [
            vid for vid, rec in self.exited_visitors.items()
            if rec.exit_time and (now - rec.exit_time) > REID_WINDOW_SECONDS
        ]
        for vid in expired:
            del self.exited_visitors[vid]

        if track_id in self.active_tracks:
            # Known active track — increment session_seq
            record = self.active_tracks[track_id]
            record.last_seen = now
            record.session_seq += 1
            return record.visitor_id, record.session_seq, "SEEN"

        # New track_id — attempt Re-ID
        embedding = self.get_embedding(frame, bbox) if frame is not None else None
        event_type = "ENTRY"
        assigned_visitor_id = None
        assigned_record = None

        if self.exited_visitors and embedding is not None:
            # Compare against all recently exited visitors
            best_distance = float("inf")
            best_vid = None
            for vid, exited_rec in self.exited_visitors.items():
                if exited_rec.embedding is not None:
                    dist = self._cosine_distance(embedding, exited_rec.embedding)
                    # Threshold 0.4 chosen empirically — lower = too strict (misses real re-entries),
                    # higher = false matches between different people in same direction
                    if dist < REID_DISTANCE_THRESHOLD and dist < best_distance:
                        best_distance = dist
                        best_vid = vid

            if best_vid is not None:
                # Re-ID match — same person returning
                assigned_visitor_id = best_vid
                assigned_record = self.exited_visitors.pop(best_vid)
                event_type = "REENTRY"
                logger.debug(
                    "Re-ID: track %d → visitor %s (cosine dist=%.3f)",
                    track_id, assigned_visitor_id, best_distance,
                )

        if assigned_visitor_id is None:
            # New visitor
            assigned_visitor_id = f"VIS_{secrets.token_hex(3)}"
            assigned_record = VisitorRecord(
                visitor_id=assigned_visitor_id,
                track_id=track_id,
                last_seen=now,
                embedding=embedding,
                session_seq=1,
            )
        else:
            # Returning visitor — update embedding and track_id
            assigned_record.track_id = track_id
            assigned_record.last_seen = now
            assigned_record.embedding = embedding or assigned_record.embedding
            assigned_record.session_seq += 1
            assigned_record.is_exited = False
            assigned_record.exit_time = None

        self.active_tracks[track_id] = assigned_record
        return assigned_visitor_id, assigned_record.session_seq, event_type

    def mark_exited(self, track_id: int) -> Optional[str]:
        """
        Mark a track as exited. Moves the record to exited_visitors for Re-ID.
        Returns the visitor_id, or None if track was unknown.
        """
        if track_id not in self.active_tracks:
            return None

        record = self.active_tracks.pop(track_id)
        record.is_exited = True
        record.exit_time = time.time()
        self.exited_visitors[record.visitor_id] = record
        return record.visitor_id

    def get_visitor_id(self, track_id: int) -> Optional[str]:
        """Get current visitor_id for a track, or None if not tracked."""
        rec = self.active_tracks.get(track_id)
        return rec.visitor_id if rec else None
