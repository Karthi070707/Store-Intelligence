"""
pipeline/validate_output.py — Validates pipeline output against required schema

Loads sample_events.jsonl (200 example events) and the pipeline output,
then runs schema compliance checks.
"""
from __future__ import annotations

import json
import os
import re
import sys
import uuid
from datetime import datetime
from typing import List

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SAMPLE_EVENTS_PATH = os.path.join("data", "sample_events.jsonl")
EVENTS_OUTPUT_DIR = "events_output"
REQUIRED_FIELDS = [
    "event_id", "store_id", "camera_id", "visitor_id",
    "event_type", "timestamp", "is_staff", "confidence", "metadata"
]
VALID_EVENT_TYPES = {
    "ENTRY", "EXIT", "ZONE_ENTER", "ZONE_EXIT", "ZONE_DWELL",
    "BILLING_QUEUE_JOIN", "BILLING_QUEUE_ABANDON", "REENTRY"
}
NULL_ZONE_TYPES = {"ENTRY", "EXIT", "REENTRY"}
STORE_ID_RE = re.compile(r"^STORE_[A-Z]{3}_[0-9]{3}$")
ISO8601_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def load_events(path: str) -> List[dict]:
    events = []
    if not os.path.exists(path):
        return events
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return events


def load_sample_events() -> list:
    """Load sample events from data/sample_events.jsonl. Returns [] if file is absent or empty."""
    if not os.path.exists(SAMPLE_EVENTS_PATH):
        print(f"  [SKIP] sample_events.jsonl not found at {SAMPLE_EVENTS_PATH} — skipping sample validation")
        return []
    if os.path.getsize(SAMPLE_EVENTS_PATH) == 0:
        print(f"  [SKIP] sample_events.jsonl is empty — skipping sample validation (will be populated after dataset download)")
        return []
    events = []
    with open(SAMPLE_EVENTS_PATH, encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"  [WARN] Line {line_num}: JSON parse error — {e}")
    print(f"  [OK] Loaded {len(events)} sample events for schema validation")
    return events


def load_all_output_events() -> List[dict]:
    all_events = []
    if not os.path.exists(EVENTS_OUTPUT_DIR):
        return all_events
    for store_dir in os.listdir(EVENTS_OUTPUT_DIR):
        store_path = os.path.join(EVENTS_OUTPUT_DIR, store_dir)
        if not os.path.isdir(store_path):
            continue
        for fname in os.listdir(store_path):
            if fname.endswith(".jsonl"):
                all_events.extend(load_events(os.path.join(store_path, fname)))
    return all_events


def validate_events(events: List[dict], label: str = "events") -> dict:
    checks = {
        "all_required_fields_present": True,
        "event_ids_unique": True,
        "valid_event_types": True,
        "confidence_in_range": True,
        "zone_id_null_for_entry_exit": True,
        "timestamps_iso8601": True,
        "event_id_valid_uuid": True,
    }
    errors = []
    seen_event_ids = set()

    for i, event in enumerate(events):
        eid = event.get("event_id", f"<index {i}>")

        # Required fields
        for field in REQUIRED_FIELDS:
            if field not in event:
                checks["all_required_fields_present"] = False
                errors.append(f"Event {eid}: missing required field '{field}'")

        # Unique event_ids
        if eid in seen_event_ids:
            checks["event_ids_unique"] = False
            errors.append(f"Duplicate event_id: {eid}")
        seen_event_ids.add(eid)

        # Valid UUID v4
        try:
            parsed = uuid.UUID(str(eid), version=4)
            if str(parsed) != str(eid):
                raise ValueError("Not canonical UUID v4")
        except Exception:
            checks["event_id_valid_uuid"] = False
            errors.append(f"Event {eid}: event_id is not a valid UUID v4")

        # Event type
        et = event.get("event_type", "")
        if et not in VALID_EVENT_TYPES:
            checks["valid_event_types"] = False
            errors.append(f"Event {eid}: invalid event_type '{et}'")

        # Confidence
        conf = event.get("confidence", -1)
        if not (0.0 <= conf <= 1.0):
            checks["confidence_in_range"] = False
            errors.append(f"Event {eid}: confidence {conf} out of range [0, 1]")

        # zone_id null for ENTRY/EXIT/REENTRY
        if et in NULL_ZONE_TYPES and event.get("zone_id") is not None:
            checks["zone_id_null_for_entry_exit"] = False
            errors.append(f"Event {eid}: zone_id must be null for {et}")

        # Timestamp format
        ts = event.get("timestamp", "")
        if not ISO8601_RE.match(str(ts)):
            checks["timestamps_iso8601"] = False
            errors.append(f"Event {eid}: timestamp '{ts}' is not ISO-8601 UTC (format: YYYY-MM-DDTHH:MM:SSZ)")

    return {
        "label": label,
        "event_count": len(events),
        "checks": checks,
        "errors": errors[:20],  # Show first 20 errors
        "all_passed": all(checks.values()),
    }


def main():
    print("=" * 60)
    print("Store Intelligence — Pipeline Output Validator")
    print("=" * 60)

    # Validate sample events (sanity check)
    sample_events = load_sample_events()
    if sample_events:
        sample_result = validate_events(sample_events, "sample_events.jsonl")
        print(f"\n[Sample Events] {sample_result['event_count']} events")
        for check, passed in sample_result["checks"].items():
            status = "✓ PASS" if passed else "✗ FAIL"
            print(f"  {status} — {check}")
        if sample_result["errors"]:
            print("  Errors (first 20):")
            for err in sample_result["errors"]:
                print(f"    - {err}")
    else:
        print("\n[WARNING] sample_events.jsonl not found or empty — skipping sample validation")

    # Validate pipeline output
    output_events = load_all_output_events()
    if not output_events:
        print("\n[WARNING] No events found in events_output/ — run pipeline first")
        return

    output_result = validate_events(output_events, "pipeline output")
    print(f"\n[Pipeline Output] {output_result['event_count']} events")
    for check, passed in output_result["checks"].items():
        status = "✓ PASS" if passed else "✗ FAIL"
        print(f"  {status} — {check}")
    if output_result["errors"]:
        print("  Errors (first 20):")
        for err in output_result["errors"]:
            print(f"    - {err}")

    print("\n" + "=" * 60)
    if output_result["all_passed"]:
        print("✓ ALL CHECKS PASSED — pipeline output is schema-compliant")
    else:
        failed = [k for k, v in output_result["checks"].items() if not v]
        print(f"✗ FAILED CHECKS: {', '.join(failed)}")
    print("=" * 60)


if __name__ == "__main__":
    main()
