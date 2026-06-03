"""
pipeline/load_dataset.py — Dataset Integration Script

Run this ONCE after extracting the challenge ZIP to load all dataset files
into the correct places and ingest the sample events.

Usage:
    python pipeline/load_dataset.py --zip-dir /path/to/extracted/zip
    python pipeline/load_dataset.py  # auto-detect from ./data/
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import urllib.request

# ─── Path detection ──────────────────────────────────────────────────────────
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA_DIR = os.path.join(ROOT, "data")

API_URL = os.getenv("API_URL", "http://localhost:8000")


def copy_if_missing(src: str, dst: str, label: str) -> bool:
    """Copy src → dst only if dst doesn't exist or is a placeholder."""
    if not os.path.exists(src):
        print(f"  [ERROR] Not found: {src}")
        return False
    if os.path.abspath(src) == os.path.abspath(dst):
        print(f"  [OK] {label}: {os.path.basename(src)} is already in place")
        return True
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)
    print(f"  [OK] {label}: {os.path.basename(src)} -> {dst}")
    return True


def detect_dataset_files(zip_dir: str) -> dict:
    """Find dataset files in the given directory (supports nested paths)."""
    found = {}
    for dirpath, _, filenames in os.walk(zip_dir):
        for fname in filenames:
            fpath = os.path.join(dirpath, fname)
            if fname == "store_layout.json" and "store_layout" not in found:
                found["store_layout"] = fpath
            elif fname == "pos_transactions.csv" and "pos_transactions" not in found:
                found["pos_transactions"] = fpath
            elif fname == "sample_events.jsonl" and "sample_events" not in found:
                found["sample_events"] = fpath
            elif fname == "assertions.py" and "assertions" not in found:
                found["assertions"] = fpath
    return found


def ingest_events_from_file(jsonl_path: str, batch_size: int = 100) -> dict:
    """POST events from a .jsonl file to the ingest API."""
    events = []
    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

    total_accepted = 0
    total_rejected = 0
    all_errors = []

    for i in range(0, len(events), batch_size):
        batch = events[i:i + batch_size]
        payload = json.dumps({"events": batch}).encode()
        req = urllib.request.Request(
            f"{API_URL}/events/ingest",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
                total_accepted += data.get("accepted_count", 0)
                total_rejected += data.get("rejected_count", 0)
                all_errors.extend(data.get("errors", []))
        except urllib.error.HTTPError as e:
            err = json.loads(e.read())
            print(f"  ✗ Batch {i//batch_size + 1} failed: {err.get('message', str(e))}")

    return {
        "total_events": len(events),
        "accepted": total_accepted,
        "rejected": total_rejected,
        "errors": all_errors[:5],  # Show first 5 errors max
    }


def wait_for_api(timeout: int = 30) -> bool:
    """Wait for the API to be available."""
    import time
    start = time.time()
    while time.time() - start < timeout:
        try:
            urllib.request.urlopen(f"{API_URL}/health", timeout=5)
            return True
        except Exception:
            time.sleep(2)
    return False


def main():
    global API_URL
    parser = argparse.ArgumentParser(
        description="Load Purplle challenge dataset into the store-intelligence system"
    )
    parser.add_argument(
        "--zip-dir", default=DATA_DIR,
        help="Path to extracted ZIP directory (default: ./data/)"
    )
    parser.add_argument(
        "--no-ingest", action="store_true",
        help="Skip event ingestion (file copy only)"
    )
    parser.add_argument(
        "--api-url", default=API_URL,
        help=f"API base URL (default: {API_URL})"
    )
    args = parser.parse_args()

    API_URL = args.api_url

    print("\n" + "=" * 60)
    print("  Store Intelligence — Dataset Loader")
    print("=" * 60)
    print(f"\n  Source directory: {args.zip_dir}")
    print(f"  API endpoint:     {API_URL}")
    print()

    # ── Step 1: Detect files ──────────────────────────────────────────────────
    print("Step 1: Detecting dataset files...")
    found = detect_dataset_files(args.zip_dir)

    required = ["store_layout", "pos_transactions", "sample_events"]
    missing = [k for k in required if k not in found]
    if missing:
        print(f"\n  [ERROR] Missing required files: {missing}")
        print("  Place ZIP contents in ./data/ and re-run.")
        sys.exit(1)

    print(f"  Found {len(found)} dataset files:")
    for key, path in found.items():
        print(f"    * {key}: {path}")

    # ── Step 2: Copy to data/ ─────────────────────────────────────────────────
    print("\nStep 2: Installing dataset files...")
    file_map = {
        "store_layout": os.path.join(DATA_DIR, "store_layout.json"),
        "pos_transactions": os.path.join(DATA_DIR, "pos_transactions.csv"),
        "sample_events": os.path.join(DATA_DIR, "sample_events.jsonl"),
        "assertions": os.path.join(DATA_DIR, "assertions.py"),
    }
    for key, dst in file_map.items():
        if key in found:
            copy_if_missing(found[key], dst, key)

    # ── Step 3: Copy CCTV clips ──────────────────────────────────────────────
    print("\nStep 3: Detecting CCTV clips...")
    clips_dir = os.path.join(ROOT, "data", "clips")
    clip_count = 0
    for dirpath, dirnames, filenames in os.walk(args.zip_dir):
        for fname in filenames:
            if fname.endswith((".mp4", ".avi", ".mov", ".mkv")):
                # Detect store from directory or filename
                store_id = None
                rel = os.path.relpath(dirpath, args.zip_dir)
                parts = rel.replace("\\", "/").split("/")
                for part in parts:
                    if part.startswith("STORE_"):
                        store_id = part
                        break
                if not store_id:
                    # Try to find store ID in filename
                    for part in fname.replace("-", "_").split("_"):
                        if part.startswith("STORE"):
                            store_id = "_".join(fname.split("_")[:3])
                            break
                if not store_id:
                    store_id = "STORE_UNKNOWN"

                dst_dir = os.path.join(clips_dir, store_id)
                dst_path = os.path.join(dst_dir, fname)
                if not os.path.exists(dst_path):
                    os.makedirs(dst_dir, exist_ok=True)
                    shutil.copy2(os.path.join(dirpath, fname), dst_path)
                    clip_count += 1

    if clip_count > 0:
        print(f"  [OK] Copied {clip_count} video clips to data/clips/")
    else:
        print("  [INFO] No new video clips found (or already copied)")

    if args.no_ingest:
        print("\n  --no-ingest flag set, skipping event ingestion.")
        print("\n[OK] Dataset files installed. Run detection pipeline:")
        print("  bash pipeline/run.sh")
        return

    # ── Step 4: Wait for API ──────────────────────────────────────────────────
    print(f"\nStep 4: Waiting for API at {API_URL}...")
    if not wait_for_api(timeout=15):
        print("  [ERROR] API not reachable. Start the API first:")
        print("    python -m uvicorn app.main:app --host 0.0.0.0 --port 8000")
        print("  Then re-run: python pipeline/load_dataset.py --no-ingest")
        print("\n  (Dataset files are already installed in ./data/)")
        return
    print("  [OK] API is running")

    # ── Step 5: Ingest sample_events.jsonl ───────────────────────────────────
    sample_path = file_map["sample_events"]
    print(f"\nStep 5: Ingesting events from {os.path.basename(sample_path)}...")
    result = ingest_events_from_file(sample_path)
    print(f"  Total events: {result['total_events']}")
    print(f"  Accepted:     {result['accepted']}")
    print(f"  Rejected:     {result['rejected']}")
    if result["errors"]:
        print(f"  First errors: {result['errors']}")

    # ── Step 6: Verify assertions ─────────────────────────────────────────────
    assertions_path = file_map.get("assertions", os.path.join(DATA_DIR, "assertions.py"))
    if os.path.exists(assertions_path):
        print(f"\nStep 6: Running assertions from {os.path.basename(assertions_path)}...")
        import importlib.util
        spec = importlib.util.spec_from_file_location("assertions", assertions_path)
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
            print("  [OK] All assertions passed!")
        except SystemExit as e:
            if e.code != 0:
                print(f"  [ERROR] Some assertions failed (exit code {e.code})")
            else:
                print("  [OK] All assertions passed!")
        except Exception as exc:
            print(f"  [ERROR] Assertions error: {exc}")

    print("\n" + "=" * 60)
    print("  Setup complete! Next steps:")
    print("  1. Run detection pipeline: bash pipeline/run.sh")
    print("  2. Open dashboard: http://localhost:8000")
    print("  3. Run test suite:  python -m pytest tests/ -v")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
