#!/bin/bash
# pipeline/run.sh — Process all clips and validate output
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_ROOT"

echo "======================================"
echo "Store Intelligence — Detection Pipeline"
echo "======================================"

if [ ! -d "data/clips" ]; then
    echo "ERROR: data/clips/ directory not found."
    echo "Please place the dataset ZIP contents in the data/ folder."
    exit 1
fi

for store_dir in data/clips/*/; do
    store_id=$(basename "$store_dir")
    echo ""
    echo "Processing store: $store_id"
    for clip in "$store_dir"*.mp4; do
        if [ ! -f "$clip" ]; then
            continue
        fi
        camera_id=$(basename "$clip" .mp4)
        echo "  → $camera_id"
        python pipeline/detect.py \
            --clip "$clip" \
            --store-layout data/store_layout.json \
            --store-id "$store_id" \
            --camera-id "$camera_id"
    done
done

echo ""
echo "Pipeline complete. Events written to events_output/"
echo ""
echo "Running schema validation..."
python pipeline/validate_output.py
echo "Validation complete."
