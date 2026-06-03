"""
app/models.py — All Pydantic v2 schemas for Store Intelligence API
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class EventType(str, Enum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"
    ZONE_ENTER = "ZONE_ENTER"
    ZONE_EXIT = "ZONE_EXIT"
    ZONE_DWELL = "ZONE_DWELL"
    BILLING_QUEUE_JOIN = "BILLING_QUEUE_JOIN"
    BILLING_QUEUE_ABANDON = "BILLING_QUEUE_ABANDON"
    REENTRY = "REENTRY"


class AnomalySeverity(str, Enum):
    INFO = "INFO"
    WARN = "WARN"
    CRITICAL = "CRITICAL"


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------

class EventMetadata(BaseModel):
    queue_depth: Optional[int] = None
    sku_zone: Optional[str] = None
    session_seq: int = 0
    partial_occlusion: Optional[bool] = None
    camera_overlap: Optional[bool] = None

    model_config = {"extra": "allow"}


# ---------------------------------------------------------------------------
# Core Event model
# ---------------------------------------------------------------------------

STORE_ID_PATTERN = re.compile(r"^[A-Z0-9_]+$")
VISITOR_ID_PATTERN = re.compile(r"^VIS_[a-f0-9]{6}$")

EVENTS_WITH_NULL_ZONE = {EventType.ENTRY, EventType.EXIT, EventType.REENTRY}


class Event(BaseModel):
    event_id: str
    store_id: str
    camera_id: str
    visitor_id: str
    event_type: EventType
    timestamp: datetime
    zone_id: Optional[str] = None
    dwell_ms: int = Field(default=0, ge=0)
    is_staff: bool = False
    confidence: float = Field(..., ge=0.0, le=1.0)
    metadata: EventMetadata = Field(default_factory=EventMetadata)

    @field_validator("event_id")
    @classmethod
    def validate_event_id(cls, v: str) -> str:
        try:
            parsed = uuid.UUID(v, version=4)
            if str(parsed) != v:
                raise ValueError("Not a canonical UUID v4")
        except (ValueError, AttributeError):
            raise ValueError(f"event_id must be a valid UUID v4, got: {v!r}")
        return v

    @field_validator("store_id")
    @classmethod
    def validate_store_id(cls, v: str) -> str:
        if not STORE_ID_PATTERN.match(v):
            raise ValueError(f"store_id must be alphanumeric, got: {v!r}")
        return v

    @field_validator("visitor_id")
    @classmethod
    def validate_visitor_id(cls, v: str) -> str:
        if not VISITOR_ID_PATTERN.match(v):
            raise ValueError(f"visitor_id must match VIS_[a-f0-9]{{6}}, got: {v!r}")
        return v

    @field_validator("timestamp")
    @classmethod
    def validate_timestamp(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware (UTC)")
        return v

    @model_validator(mode="after")
    def validate_zone_id_rules(self) -> "Event":
        if self.event_type in EVENTS_WITH_NULL_ZONE and self.zone_id is not None:
            raise ValueError(
                f"zone_id must be null for event_type {self.event_type.value}"
            )
        return self

    model_config = {"populate_by_name": True}


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------

class IngestRequest(BaseModel):
    events: List[Event] = Field(..., min_length=1, max_length=500)


class IngestError(BaseModel):
    event_id: Optional[str] = None
    reason: str


class IngestResponse(BaseModel):
    accepted_count: int
    rejected_count: int
    errors: List[IngestError] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

class MetricsResponse(BaseModel):
    store_id: str
    unique_visitors: int = 0
    conversion_rate: float = 0.0
    avg_dwell_per_zone: Dict[str, float] = Field(default_factory=dict)
    queue_depth: int = 0
    abandonment_rate: float = 0.0
    window_start: datetime
    window_end: datetime


# ---------------------------------------------------------------------------
# Funnel
# ---------------------------------------------------------------------------

class FunnelStage(BaseModel):
    name: str
    count: int
    drop_off_pct: float = 0.0


class FunnelResponse(BaseModel):
    store_id: str
    session_count: int
    stages: List[FunnelStage]


# ---------------------------------------------------------------------------
# Heatmap
# ---------------------------------------------------------------------------

class HeatmapZone(BaseModel):
    zone_id: str
    visit_frequency: int = 0
    avg_dwell_ms: float = 0.0
    normalised_score: int = 0  # 0-100
    data_confidence: bool = True


class HeatmapResponse(BaseModel):
    store_id: str
    window_hours: int = 24
    zones: List[HeatmapZone]


# ---------------------------------------------------------------------------
# Anomalies
# ---------------------------------------------------------------------------

class Anomaly(BaseModel):
    anomaly_type: str
    severity: AnomalySeverity
    description: str
    suggested_action: str
    detected_at: datetime


class AnomalyResponse(BaseModel):
    store_id: str
    checked_at: datetime
    anomalies: List[Anomaly] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

class StaleFeeds(BaseModel):
    store_id: str
    camera_id: str
    last_event_at: Optional[str] = None


class HealthResponse(BaseModel):
    status: str  # "healthy" | "degraded"
    version: str
    uptime_seconds: float
    last_event_per_store: Dict[str, Optional[str]] = Field(default_factory=dict)
    stale_feeds: List[StaleFeeds] = Field(default_factory=list)
    db_status: str  # "connected" | "unavailable"
