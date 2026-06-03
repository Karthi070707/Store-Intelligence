# PROMPT: Create unit tests for the pipeline components: emit.py, zone_mapper.py, and
# the event schema. Tests should not require real video files. Also run all assertions
# from data/assertions.py if present.
# CHANGES MADE: Added test_validate_against_sample to compare with sample_events.jsonl
# schema; added test_low_confidence_not_dropped; fixed import paths for project structure.

"""
tests/test_pipeline.py — Unit tests for pipeline components (no video required)
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import uuid

import pytest

# Add project root to sys.path for pipeline imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# emit.py tests
# ---------------------------------------------------------------------------

class TestEmitEvent:

    def test_emit_event_schema_valid(self, tmp_path, monkeypatch):
        """emit_event() with valid params → output is valid JSON matching schema."""
        monkeypatch.setenv("EVENTS_OUTPUT_DIR", str(tmp_path))

        import pipeline.emit as emit_module
        # Patch the output directory
        original_dir = emit_module.EVENTS_OUTPUT_DIR
        emit_module.EVENTS_OUTPUT_DIR = str(tmp_path)

        try:
            result = emit_module.emit_event(
                store_id="STORE_BLR_002",
                camera_id="CAM_ENTRY_01",
                visitor_id="VIS_aabbcc",
                event_type="ENTRY",
                clip_start_time="2026-03-03T08:00:00Z",
                frame_number=15,
                fps=15.0,
                zone_id=None,
                is_staff=False,
                confidence=0.92,
                session_seq=1,
            )
        finally:
            emit_module.EVENTS_OUTPUT_DIR = original_dir

        assert result is not None
        assert "event_id" in result
        assert result["event_type"] == "ENTRY"
        assert result["store_id"] == "STORE_BLR_002"
        assert result["zone_id"] is None
        assert 0.0 <= result["confidence"] <= 1.0

        # Validate the UUID
        parsed = uuid.UUID(result["event_id"])
        assert str(parsed) == result["event_id"]

    def test_event_id_unique(self, tmp_path):
        """emit_event() called 100 times → all event_ids are different UUID v4."""
        import pipeline.emit as emit_module
        original_dir = emit_module.EVENTS_OUTPUT_DIR
        emit_module.EVENTS_OUTPUT_DIR = str(tmp_path)

        event_ids = set()
        try:
            for i in range(100):
                result = emit_module.emit_event(
                    store_id="STORE_BLR_002",
                    camera_id="CAM_ENTRY_01",
                    visitor_id="VIS_aabbcc",
                    event_type="ENTRY",
                    clip_start_time="2026-03-03T08:00:00Z",
                    frame_number=i,
                    fps=15.0,
                    zone_id=None,
                    confidence=0.9,
                    session_seq=i,
                )
                if result:
                    event_ids.add(result["event_id"])
        finally:
            emit_module.EVENTS_OUTPUT_DIR = original_dir

        assert len(event_ids) == 100, "All event_ids must be unique"

    def test_entry_exit_event_types(self, tmp_path):
        """ENTRY event has zone_id=None; EXIT event has zone_id=None."""
        import pipeline.emit as emit_module
        original_dir = emit_module.EVENTS_OUTPUT_DIR
        emit_module.EVENTS_OUTPUT_DIR = str(tmp_path)

        try:
            entry = emit_module.emit_event(
                store_id="STORE_BLR_002", camera_id="CAM_ENTRY_01",
                visitor_id="VIS_aabbcc", event_type="ENTRY",
                clip_start_time="2026-03-03T08:00:00Z",
                frame_number=1, fps=15.0, zone_id=None, confidence=0.9,
            )
            exit_evt = emit_module.emit_event(
                store_id="STORE_BLR_002", camera_id="CAM_ENTRY_01",
                visitor_id="VIS_aabbcc", event_type="EXIT",
                clip_start_time="2026-03-03T08:00:00Z",
                frame_number=100, fps=15.0, zone_id=None, confidence=0.9,
            )
        finally:
            emit_module.EVENTS_OUTPUT_DIR = original_dir

        assert entry is not None
        assert exit_evt is not None
        assert entry["event_type"] == "ENTRY"
        assert exit_evt["event_type"] == "EXIT"
        assert entry["zone_id"] is None
        assert exit_evt["zone_id"] is None

    def test_zone_id_null_for_entry_exit(self, tmp_path):
        """ENTRY and EXIT events → zone_id is None/null (validator enforces this)."""
        import pipeline.emit as emit_module
        original_dir = emit_module.EVENTS_OUTPUT_DIR
        emit_module.EVENTS_OUTPUT_DIR = str(tmp_path)

        try:
            # Try to emit ENTRY with zone_id — should be rejected by schema
            result = emit_module.emit_event(
                store_id="STORE_BLR_002", camera_id="CAM_ENTRY_01",
                visitor_id="VIS_aabbcc", event_type="ENTRY",
                clip_start_time="2026-03-03T08:00:00Z",
                frame_number=1, fps=15.0,
                zone_id="SKINCARE",  # Invalid for ENTRY
                confidence=0.9,
            )
        finally:
            emit_module.EVENTS_OUTPUT_DIR = original_dir

        # Schema validation should reject this (zone_id must be null for ENTRY)
        assert result is None, "ENTRY event with zone_id should be rejected by schema validator"

    def test_zone_dwell_at_30s(self, tmp_path):
        """Visitor in zone 31 seconds → ZONE_DWELL event emitted with dwell_ms>=30000."""
        import pipeline.emit as emit_module
        original_dir = emit_module.EVENTS_OUTPUT_DIR
        emit_module.EVENTS_OUTPUT_DIR = str(tmp_path)
        emit_module._dwell_trackers.clear()

        fps = 15.0
        visitor_id = "VIS_dddddd"
        zone_id = "SKINCARE"

        try:
            # Simulate 31 seconds in zone (at 15fps = 465 frames)
            # First call initialises the tracker
            should_dwell, dwell_ms = emit_module.should_emit_dwell(visitor_id, zone_id, 0, fps)
            assert not should_dwell

            # Call at 31 seconds = 465 frames
            should_dwell, dwell_ms = emit_module.should_emit_dwell(visitor_id, zone_id, 465, fps)
            assert should_dwell, "ZONE_DWELL should be emitted after 31 seconds"
            assert dwell_ms >= 30000, f"dwell_ms should be >= 30000, got {dwell_ms}"

            # Emit the ZONE_DWELL event
            result = emit_module.emit_event(
                store_id="STORE_BLR_002", camera_id="CAM_FLOOR_01",
                visitor_id=visitor_id, event_type="ZONE_DWELL",
                clip_start_time="2026-03-03T08:00:00Z",
                frame_number=465, fps=fps,
                zone_id=zone_id, dwell_ms=dwell_ms, confidence=0.9,
            )
        finally:
            emit_module.EVENTS_OUTPUT_DIR = original_dir
            emit_module._dwell_trackers.clear()

        assert result is not None
        assert result["event_type"] == "ZONE_DWELL"
        assert result["dwell_ms"] >= 30000

    def test_low_confidence_not_dropped(self, tmp_path):
        """confidence=0.2 → event still emitted (not filtered out)."""
        import pipeline.emit as emit_module
        original_dir = emit_module.EVENTS_OUTPUT_DIR
        emit_module.EVENTS_OUTPUT_DIR = str(tmp_path)

        try:
            result = emit_module.emit_event(
                store_id="STORE_BLR_002", camera_id="CAM_ENTRY_01",
                visitor_id="VIS_aabbcc", event_type="ZONE_ENTER",
                clip_start_time="2026-03-03T08:00:00Z",
                frame_number=1, fps=15.0, zone_id="SKINCARE",
                confidence=0.2,  # Low confidence — must NOT be dropped
            )
        finally:
            emit_module.EVENTS_OUTPUT_DIR = original_dir

        assert result is not None, "Low confidence events must NOT be dropped"
        assert result["confidence"] == pytest.approx(0.2, abs=0.01)


# ---------------------------------------------------------------------------
# validate_output.py tests
# ---------------------------------------------------------------------------

class TestValidateOutput:

    def test_validate_against_sample(self):
        """Pipeline output schema matches sample_events.jsonl structure."""
        sample_path = os.path.join("data", "sample_events.jsonl")
        if not os.path.exists(sample_path):
            pytest.skip("sample_events.jsonl not available")

        from pipeline.validate_output import load_events, validate_events
        events = load_events(sample_path)
        assert len(events) > 0

        result = validate_events(events, "sample_events.jsonl")
        assert result["all_passed"], f"Sample events failed validation: {result['errors']}"

    def test_assertions_py_all_pass(self):
        """
        Load and run all assertions from data/assertions.py → all pass.
        This test requires the API to be running at http://localhost:8000.
        Skip if the API is not available (unit test mode).
        """
        assertions_path = os.path.join("data", "assertions.py")
        if not os.path.exists(assertions_path):
            pytest.skip("data/assertions.py not available")

        # Check if API is reachable before running
        import urllib.request
        try:
            urllib.request.urlopen("http://localhost:8000/health", timeout=2)
        except Exception:
            pytest.skip("API not running at localhost:8000 — run 'docker compose up' first")

        spec = importlib.util.spec_from_file_location("assertions", assertions_path)
        assertions_module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(assertions_module)
        except SystemExit as exc:
            if exc.code != 0:
                pytest.fail(f"assertions.py reported failures (exit code {exc.code})")
        except Exception as exc:
            pytest.fail(f"assertions.py failed to load: {exc}")
