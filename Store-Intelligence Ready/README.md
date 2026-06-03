# Store Intelligence

**Edge-native retail analytics pipeline — from raw CCTV footage to live business metrics.**

Built for the UpGrad / Purplle Tech Challenge 2026 Round 2.

---

## 🔍 For Reviewers / Evaluators — Start Here

> **This section tells you exactly where to put the datasets and how to run the full system end-to-end for both Store 1 and Store 2.**

### Step A — Place the Dataset Files

Copy the challenge-provided datasets into the designated paths inside this repository so your folder structure looks exactly like this:

```text
store-intelligence/
├── events_output/
│   ├── ST1008/
│   │   ├── CAM 1 - zone.jsonl       <-- Store 1 dataset
│   │   ├── CAM 2 - zone.jsonl       <-- Store 1 dataset
│   │   ├── CAM 3 - entry.jsonl      <-- Store 1 dataset
│   │   └── CAM 5 - billing.jsonl    <-- Store 1 dataset
│   └── ST1076/
│       ├── entry 1.jsonl            <-- Store 2 dataset
│       ├── zone.jsonl               <-- Store 2 dataset
│       └── billing_area.jsonl       <-- Store 2 dataset
└── data/
    ├── New Data/
    │   ├── POS - sample transactionsb1e826f.csv   ← Store 1 POS transaction log
    │   ├── sample_eventsbe42122.jsonl             ← Store 2 raw event sample (optional fallback)
    │   ├── Store 1/                               ← Store 1 raw CCTV footage
    │   │   ├── CAM 1 - zone.mp4
    │   │   ├── CAM 2 - zone.mp4
    │   │   ├── CAM 3 - entry.mp4
    │   │   ├── CAM 5 - billing.mp4
    │   │   └── Store 1 - layout.png
    │   └── Store 2/                               ← Store 2 raw CCTV footage
    │       ├── billing_area.mp4
    │       ├── entry 1.mp4
    │       ├── entry 2.mp4
    │       ├── zone.mp4
    │       └── store 2 - layout.png
    └── store_layout.json
```

#### 1. Store 1 (`ST1008` — Brigade Road)
* **CCTV Events Datasets (JSONL)**: Place datasets in `events_output/ST1008/` (as shown in the structure above).
* **POS Transactions Dataset (CSV)**: Place POS transaction dataset in `data/New Data/POS - sample transactionsb1e826f.csv`.

#### 2. Store 2 (`ST1076` — Mumbai Hub)
* **Dynamic CCTV Events Datasets (JSONL)**: Place datasets in `events_output/ST1076/`. If any dataset file is present here, the ingestion pipeline automatically switches to **Dynamic Ingestion Mode** for Store 2.
* **Appended JSONL Events Dataset**: Alternatively, append custom events directly to the dataset at `data/New Data/sample_eventsbe42122.jsonl`. If this file contains more than the default 14 events, the script will parse them dynamically.
* **Fallback Mode (Automatic)**: If no dataset files are placed in `events_output/ST1076/` and `sample_eventsbe42122.jsonl` is at default size, the script programmatically generates 15 visitors and 1 transaction for the dashboard metrics.

---

### Step B — Run the System (3 Commands)

Open a terminal in the `store-intelligence/` directory and run these commands **in order**:

```bash
# 1. Start the API + database (Docker must be running)
docker-compose up --build -d

# 2. Run the ingestion pipeline (clears database, writes layouts, processes events, and correlates POS)
venv\Scripts\python ingest_new_data.py

# 3. Serve the live dashboard
python -m http.server 8080 --directory dashboard/web
```

### What You Will See After Step 3

Open **[http://localhost:8080](http://localhost:8080)** in your browser. The dashboard includes a **header switcher toolbar** to easily switch between:
- **STORE 1 — BRIGADE ROAD** (`ST1008`)
- **STORE 2 — MUMBAI HUB** (`ST1076`)

| API URL | What it shows |
|---|---|
| `http://localhost:8000/stores/ST1008/metrics` | JSON: unique visitors, conversion rate, queue depth (Store 1) |
| `http://localhost:8000/stores/ST1076/metrics` | JSON: unique visitors, conversion rate, queue depth (Store 2) |
| `http://localhost:8000/stores/ST1008/funnel` | JSON: entry → zone → billing → purchase drop-off (Store 1) |
| `http://localhost:8000/stores/ST1008/heatmap` | JSON: zone dwell scores normalised 0–100 (Store 1) |
| `http://localhost:8000/health` | JSON: service status, last event timestamp, and stale feeds |

> 📖 Full architecture details: **[DESIGN.md](./docs/DESIGN.md)** | Engineering trade-offs: **[CHOICES.md](./docs/CHOICES.md)**

---

## What This System Does

Store Intelligence processes raw CCTV footage using computer vision, tracks individual visitor journeys across multiple cameras, and surfaces actionable business metrics — conversion rate, queue depth, zone heatmaps, and abandonment rate — through a live web dashboard and REST API.

The pipeline runs entirely on-premise. No cloud dependency, no managed services. A single `docker-compose up` starts everything.

---

## Architecture Documentation

Read these before diving into the code:

- **[DESIGN.md](./docs/DESIGN.md)** — System architecture, detection pipeline logic, session management, POS correlation, anomaly detection, and production readiness decisions.
- **[CHOICES.md](./docs/CHOICES.md)** — Engineering trade-offs: model selection, event schema design, database choice, and where AI suggestions were accepted or overridden.

---

## Prerequisites

| Requirement | Purpose |
|---|---|
| Docker Desktop + Docker Compose | Runs the API and database in an isolated container |
| Python 3.9+ | Required only to serve the static dashboard and run ingestion scripts |
| NVIDIA GPU (recommended) | Real-time 30fps YOLOv8m inference. Falls back to CPU automatically if not found. |

---

## Detailed Setup & Operations

### Step 1 — Place Datasets
Ensure your challenge files are placed in their respective locations under `events_output/ST1008/` and `data/New Data/` as detailed in the **[Place the Dataset Files](#step-a--place-the-dataset-files)** section.

### Step 2 — Start the API

```bash
docker-compose up --build -d
```

This builds the Python environment from `Dockerfile.api`, installs all dependencies from `requirements.txt`, mounts the local SQLite database file (`store_intelligence.db`), and starts the FastAPI server on port `8000`.

Verify it is running:

```bash
curl http://localhost:8000/health
# Expected: {"status": "ok", "last_event_timestamp": null}
```

### Step 3 — Ingest Events and Correlate POS

```bash
venv\Scripts\python ingest_new_data.py
```

This script clears the database, configures `store_layout.json` for both `ST1008` and `ST1076`, runs the dynamic ingestion parser to import event streams, creates POS records, and runs the async background tasks to match transactions to active visitor sessions.

### Step 4 — Open the Live Dashboard

```bash
python -m http.server 8080 --directory dashboard/web
```

Open **http://localhost:8080** in your browser. The dashboard polls the API every 3 seconds and updates KPI cards, the zone heatmap, conversion funnel, and anomaly alerts live. Click the store selector button at the top to toggle between **Store 1** and **Store 2**.

---

## Running the Test Suite

```bash
venv\Scripts\pytest -v
```

The test suite covers: API endpoint correctness, event idempotency, SQL metric calculations, funnel session deduplication, and edge cases including empty store periods, all-staff clips, zero purchases, and re-entry within the funnel. Statement coverage exceeds 70%.

---

## Project Structure

```text
store-intelligence/
├── app/
│   ├── main.py             # FastAPI application entry point and route registration
│   ├── database.py         # Async SQLite connection pool (aiosqlite)
│   ├── models.py           # Pydantic event and metric schemas
│   ├── ingestion.py        # Batch ingest, event_id deduplication logic
│   ├── metrics.py          # Raw SQL aggregations for all metric endpoints
│   ├── funnel.py           # Session-based funnel and deduplication logic
│   ├── anomalies.py        # Anomaly detection: stale feeds, queue spikes, conversion drop
│   ├── health.py           # Health endpoint and stale feed detection
│   └── middleware/         # Structured JSON logging and global 503 error handler
├── pipeline/
│   ├── detect.py           # YOLOv8m inference and bounding box extraction
│   ├── tracker.py          # ByteTrack MOT and OSNet cross-camera Re-ID
│   └── emit.py             # Shapely zone intersection and event emission
├── dashboard/web/          # Vanilla JS / HTML live dashboard (no build step)
├── tests/
│   ├── test_pipeline.py    # Detection pipeline unit tests
│   ├── test_metrics.py     # Metric computation and funnel logic tests
│   └── test_anomalies.py   # Anomaly detection tests
├── data/                   # CCTV clips, store_layout.json, pos_transactions.csv
├── events_output/          # Generated .jsonl event files from the pipeline
├── ingest_new_data.py      # Clears DB, ingests layouts, event streams and POS data
├── run_pipeline.py         # Main entry point for CV inference
├── Dockerfile.api          # API container build instructions
├── docker-compose.yml      # Single-command deployment orchestrator
├── docs/
│   ├── DESIGN.md           # System architecture document
│   └── CHOICES.md          # Engineering decisions and trade-offs
```

---

## API Reference

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/events/ingest` | Ingest up to 500 events per request. Idempotent by `event_id`. |
| `GET` | `/stores/{id}/metrics` | Unique visitors, conversion rate, avg dwell, queue depth, abandonment rate. |
| `GET` | `/stores/{id}/funnel` | Returns conversion funnel stages and drop-off percentages. |
| `GET` | `/stores/{id}/heatmap` | Zone visit frequency and avg dwell, normalised 0–100. |
| `GET` | `/stores/{id}/anomalies` | Active anomalies with severity and suggested action. |
| `GET` | `/health` | Service status and last event timestamp per store. |

---

## Sample API Responses (Store 1 Example)

### GET /stores/ST1008/metrics

```json
{
  "store_id": "ST1008",
  "unique_visitors": 4,
  "conversion_rate": 0.50,
  "avg_dwell_per_zone": {
    "ENTRY_AREA": 23.4,
    "SKINCARE": 87.4,
    "MAKEUP": 54.1,
    "BILLING": 210.3
  },
  "queue_depth": 0,
  "abandonment_rate": 0.0
}
```

### GET /stores/ST1008/funnel

```json
{
  "store_id": "ST1008",
  "stages": [
    { "name": "Entry",         "count": 4, "drop_off_pct": 0.0  },
    { "name": "Zone Visit",    "count": 3, "drop_off_pct": 25.0 },
    { "name": "Billing Queue", "count": 2, "drop_off_pct": 33.3 },
    { "name": "Purchase",      "count": 2, "drop_off_pct": 0.0  }
  ]
}
```

### GET /stores/ST1008/anomalies

```json
{
  "store_id": "ST1008",
  "anomalies": [
    {
      "anomaly_type": "STALE_FEED",
      "severity": "CRITICAL",
      "description": "No events received from CAM 1 - zone in ST1008 for 10+ minutes",
      "suggested_action": "No events received from CAM 1 - zone in ST1008 for 10+ minutes — verify CCTV connectivity and pipeline status",
      "detected_at": "2026-06-02T14:32:10Z"
    }
  ]
}
```

### GET /health

```json
{
  "status": "healthy",
  "version": "1.0.0",
  "uptime_seconds": 3612.5,
  "db_status": "connected",
  "last_event_per_store": {
    "ST1008": "2026-04-10T14:31:55Z",
    "ST1076": "2026-03-08T18:25:00Z"
  },
  "stale_feeds": []
}
```

---

## North Star Metric

Every component in this system connects back to a single business number:

> **Offline Store Conversion Rate = Visitors who completed a purchase ÷ Total unique visitors in a session window**

| Business Question | Where the System Answers It |
|---|---|
| How many customers visited today and how many bought? | `/metrics` → `unique_visitors` + `conversion_rate` |
| Where in the store are we losing customers? | `/funnel` → drop-off % by stage |
| Which zones get attention but not sales? | `/heatmap` dwell score vs `/funnel` billing stage count |
| Is there a queue building right now? | `/anomalies` → `BILLING_QUEUE_SPIKE` |
| Is our conversion rate worse than usual today? | `/anomalies` → `CONVERSION_DROP` vs 7-day rolling avg |
| Is any camera feed stale or down? | `/health` → `stale_feeds` list |

---

## Event Schema Reference

Every event emitted by the pipeline follows this schema:

| Field | Type | Description |
|---|---|---|
| `event_id` | UUID v4 | Globally unique event identifier. Used for idempotency. |
| `store_id` | string | e.g. `ST1008` or `ST1076` |
| `camera_id` | string | e.g. `CAM 3 - entry`, `entry 1`, `zone` |
| `visitor_id` | string | Unique per physical person per session |
| `event_type` | enum | `ENTRY`, `EXIT`, `REENTRY`, `ZONE_ENTER`, `ZONE_EXIT`, `ZONE_DWELL`, `BILLING_QUEUE_JOIN`, `BILLING_QUEUE_ABANDON` |
| `timestamp` | ISO-8601 UTC | Derived from `clip_start_time + frame/fps`. Never wall-clock. |
| `zone_id` | string \| null | Zone name from `store_layout.json`. Null for `ENTRY`/`EXIT`. |
| `dwell_ms` | integer | Milliseconds spent in zone. 0 for non-dwell events. |
| `is_staff` | boolean | `true` if HSV torso analysis matched staff uniform. Excluded from all customer metrics. |
| `confidence` | float [0–1] | Raw YOLOv8 detection confidence. Never suppressed or elevated. |
| `metadata` | object | `queue_depth`, `sku_zone`, `session_seq`, `partial_occlusion` |

---

## Environment Variables

The following environment variables can be set in a `.env` file or passed to Docker Compose to customise behaviour:

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `sqlite+aiosqlite:///./store_intelligence.db` | SQLite database file path |
| `STALE_FEED_THRESHOLD_MINUTES` | `10` | Minutes before a camera feed is flagged as stale in `/health` |
| `QUEUE_SPIKE_THRESHOLD` | `5` | Queue depth above which `BILLING_QUEUE_SPIKE` anomaly fires |
| `CONVERSION_DROP_THRESHOLD_PCT` | `15` | % drop below 7-day avg that triggers `CONVERSION_DROP` anomaly |
| `DEAD_ZONE_MINUTES` | `30` | Minutes of zone inactivity before `DEAD_ZONE` anomaly fires |
| `REENTRY_WINDOW_MINUTES` | `5` | OSNet Re-ID matching window for cross-camera identity resolution |

> Copy `.env.example` to `.env` and adjust values before running `docker-compose up`.

---

## Troubleshooting

### `docker-compose up` fails with port conflict
```bash
# Find what is using port 8000
netstat -ano | findstr :8000   # Windows
lsof -i :8000                  # Linux/macOS

# Change the exposed port in docker-compose.yml if needed
ports:
  - "8001:8000"
```

### `ingest_new_data.py` fails or returns NameError
- Confirm your local environment has `asyncio` available.
- Ensure the database is created: run `docker-compose up -d` before running ingestion.

### `/metrics` returns all zeros after ingestion
- Verify events were ingested: `curl http://localhost:8000/health` — check `last_event_per_store`.
- Confirm the `store_id` in the events matches the URL: must be `ST1008` or `ST1076`.
- All-staff clips will return `unique_visitors: 0` by design — staff are excluded from metrics.

### YOLOv8m model not downloading automatically
```bash
# Pre-download the model weights manually
python -c "from ultralytics import YOLO; YOLO('yolov8m.pt')"
```
The model file (`yolov8m.pt`) is not committed to the repository. It is automatically downloaded on first run by the `ultralytics` library.

### Ingest returns HTTP 503
- The API container is not running. Run `docker ps` to verify.
- If the container exited, check logs: `docker logs store-intelligence-api-1`
- Restart with: `docker-compose up -d`

---

## Stopping the System

```bash
docker-compose down
```

To also delete the database and start completely fresh:

```bash
docker-compose down -v
rm -f store_intelligence.db
```

---

## Known Limitations

- **CV pipeline runs outside Docker on Windows:** Docker GPU-passthrough on Windows introduces significant PyTorch inference penalties — the pipeline cannot sustain 15fps across 4 cameras inside a Windows container. The pipeline runs as a native Python process instead. On Linux with `nvidia-docker`, it can be fully containerised with no code changes.
- **CPU fallback:** On CPU-only hardware, the pipeline automatically falls back to YOLOv8n (nano), which reduces bounding box accuracy by approximately 15–20%.
- **Dashboard eventual consistency:** The dashboard lags reality by up to 3 seconds due to the 3-second HTTP polling interval and async batch ingestion cycle. This is acceptable for macro-level business metrics.
- **Queue abandonment lag:** `BILLING_QUEUE_ABANDON` is emitted only after a 10-minute inactivity timeout. If a visitor walks away without triggering a clean `EXIT`, `queue_depth` temporarily over-reports until the timeout fires.
- **Single-writer SQLite ceiling:** The architecture cannot support multiple pipeline instances writing simultaneously. A multi-store cloud deployment would require PostgreSQL and read replicas.

For a full discussion of limitations and architectural trade-offs, see [CHOICES.md](./docs/CHOICES.md) and [DESIGN.md](./docs/DESIGN.md).
