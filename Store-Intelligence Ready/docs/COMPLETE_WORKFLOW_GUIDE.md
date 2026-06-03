# Store Intelligence — End-to-End System Workflow & Architecture Guide

> **Apex Retail Store Intelligence Platform**  
> Comprehensive documentation of the computer vision pipeline, data flows, backend analytics API, session management, and engineering tradeoffs.

---

## 1. System Pipeline Architecture

The Store Intelligence system processes raw CCTV footage from retail store cameras to derive actionable real-time business telemetry (such as visitor traffic, conversion rates, zone hot-spots, and checkout queue latency). 

The platform is designed around a strictly decoupled, **event-driven architecture**. High-frequency computer vision inferences are translated into discrete, schema-validated JSON events at the edge, which are then batch-ingested into a low-latency relational backend database to serve real-time dashboard analytics.

### End-to-End Data Flow

```mermaid
graph TD
    %% Source Cameras
    subgraph CCTV CCTV Cameras (15fps)
        CAM1[CAM 1: Entry/Exit]
        CAM2[CAM 2: Skincare Zone]
        CAM3[CAM 3: Makeup Zone]
        CAM4[CAM 4: Haircare Zone]
        CAM5[CAM 5: Billing Counter]
    end

    %% Computer Vision Pipeline
    subgraph CV_Inference Edge CV Pipeline (Outside Docker)
        Pre[CLAHE Lighting Normalisation] --> Det[YOLOv8m Person Detection]
        Det --> NMS[Group Entry NMS Filtering]
        NMS --> Track[ByteTrack MOT Tracking]
        Track --> Staff[HSV Torso Staff Detection]
        Track --> ReID[Torchreid OSNet Cross-Camera Re-ID]
        ReID --> Geo[Shapely Polygon Zone Mapping]
        Geo --> Emit[Emit Event Generator]
    end

    %% Decoupled Transport
    subgraph Transport Transport Layer
        JSONL[(events_output/*.jsonl)]
    end

    %% Database & Core API
    subgraph API_Layer Intelligence API & Storage (Docker Compose)
        Ingest[FastAPI /events/ingest Batch Ingestion]
        DB[(SQLite / aiosqlite database)]
        VS[visitor_sessions table]
        POS[pos_transactions table]
        Sync[Session & POS Correlation Worker]
    end

    %% Dashboards
    subgraph UI Dashboards
        Web[Web Dashboard Vanilla HTML/JS]
        CLI[Terminal Dashboard Rich CLI]
    end

    %% Links
    CAM1 & CAM2 & CAM3 & CAM4 & CAM5 --> Pre
    Emit --> JSONL
    JSONL --> Ingest
    Ingest --> DB
    Sync <--> DB
    DB --> VS & POS
    DB --> Web & CLI
```

### Camera Coverage & Store Layout (5-Camera Topology)

Each Apex Retail store is mapped using a 5-camera topology to cover the complete customer funnel:

| Camera ID | Primary Zone | Role in Customer Journey | Events Emitted |
| :--- | :--- | :--- | :--- |
| **CAM 3** | `ENTRY_AREA` | Funnel Stage 1 (Entry / Exit / Re-entry tracking) | `ENTRY`, `EXIT`, `REENTRY` |
| **CAM 1** | `SKINCARE` | Funnel Stage 2 (Engagement / Product Browsing) | `ZONE_ENTER`, `ZONE_DWELL`, `ZONE_EXIT` |
| **CAM 2** | `MAKEUP` | Funnel Stage 2 (Engagement / Product Browsing) | `ZONE_ENTER`, `ZONE_DWELL`, `ZONE_EXIT` |
| **CAM 4** | `HAIRCARE` | Funnel Stage 2 (Engagement / Product Browsing) | `ZONE_ENTER`, `ZONE_DWELL`, `ZONE_EXIT` |
| **CAM 5** | `BILLING` | Funnel Stage 3 (Checkout / Queue Buildup) | `BILLING_QUEUE_JOIN`, `BILLING_QUEUE_ABANDON` |

---

## 2. CCTV Video Processing Workflow

Retail store feeds are processed at a calibrated frame rate of **15 FPS** (Frames Per Second). The video pipeline runs at the edge and follows three sequential steps before person detection:

```text
[Raw 1080p CCTV Stream] ──> [CLAHE Pre-Processing] ──> [Deterministic Frame Timestamping]
```

### Contrast Limited Adaptive Histogram Equalisation (CLAHE)
In-store environments suffer from shifting ambient light (such as changing natural sunlight through shop windows clashing with internal fluorescent lighting). To maintain detection stability, **CLAHE** is applied in the pre-processing stage. 
- It divides each video frame into small, non-overlapping contextual regions (called tiles, usually 8x8).
- It performs histogram equalisation on each tile to normalise local contrast.
- A clip limit is enforced to prevent noise amplification in dark or highly reflective corners of the store.
- Tile boundaries are blended using bilinear interpolation to eliminate artificial block borders.

### Deterministic Timestamp Synchronization
Retail telemetry relies on matching video timestamps with Point-of-Sale (POS) cashier receipts. Since files may be processed out of real-time, frame times are synchronised deterministically:
$$\text{Event Timestamp} = \text{Clip Start Time} + \left( \frac{\text{Frame Number}}{\text{FPS}} \right)$$

For example, if `CAM_ENTRY_01` has a `clip_start_time` of `2026-04-10T08:00:00Z` in `store_layout.json`, frame `18000` is assigned a timestamp exactly 20 minutes later: `2026-04-10T08:20:00Z`.

---

## 3. Human Detection & Tracking

Once the frame is pre-processed, it is piped to the object detection model to isolate human coordinate trajectories.

```text
[CLAHE Frame] ──> [YOLOv8m Object Detector] ──> [NMS Post-Processing] ──> [ByteTrack MOT]
```

### Person Detection (YOLOv8m)
The system uses **YOLOv8m** (medium size, pre-trained on the COCO dataset) for zero-shot object detection. Since the goal is store telemetry, the detection pipeline restricts output classification strictly to the `person` class (COCO class ID `0`). 

### Group Entry & Non-Maximum Suppression (NMS)
A key edge case in retail tracking is group entry (e.g., families or couples walking closely together). A naive detector might merge their shapes or drop one person. The pipeline solves this via:
- **High-iou NMS thresholding**: Calibrated to `0.45` to prevent merging distinct, overlapping bounding boxes of shoulder-to-shoulder walkers.
- **Bounding Box Isolation**: Individual bounding boxes are kept for each distinct shopper, resulting in separate, concurrent `ENTRY` events (e.g., 3 people entering together emit 3 `ENTRY` events, preventing artificial visitor undercounting).

### Occlusion Management & Track Persistence (ByteTrack)
In crowded retail aisles, shoppers are frequently occluded by columns, endcaps, and display shelves. Standard trackers (like DeepSORT) discard tracks as soon as the bounding box confidence falls below a primary threshold, generating a new ID when the customer re-emerges. This severely inflates unique visitor counts.

**ByteTrack** resolves this by maintaining two detection buffers:
1. **High-Confidence Buffer**: Detections with scores $\ge 0.6$ are associated with active tracks using Kalman filters.
2. **Low-Confidence Buffer**: Detections with scores between $0.1$ and $0.6$ are not discarded. If a shopper steps behind a shelf and their detection confidence drops to $0.2$, ByteTrack keeps the track active in the background. It uses motion momentum from the Kalman filter to search the low-confidence buffer and stitch the track back together when they reappear.

### Confidence Calibration (No Silent Suppression)
To maintain data integrity, **low-confidence events are never silently dropped**. The event schema records the raw YOLO confidence score (e.g., `confidence: 0.32`) and tags the metadata with `"partial_occlusion": true`. This allows the API to store the complete raw telemetry and lets the querying analyst apply threshold filters based on the specific analytical task.

---

## 4. Re-Entry & Cross-Camera Re-Identification (Re-ID)

A major challenge in store-wide tracking is maintaining visitor identities when people move between separate cameras or step out of the store briefly (e.g., to answer a phone call).

```text
[Intra-Camera Track ID] ──> [Torchreid OSNet Crop] ──> [128D Embedding] ──> [Cosine Similarity Search]
```

### Omni-Scale Feature Extraction (Torchreid OSNet)
The system uses **Torchreid OSNet (`osnet_x1_0`)** to extract a 128-dimensional appearance embedding from the cropped bounding box of every tracked individual. 

OSNet is built specifically for Re-ID tasks:
- **Omni-Scale Learning**: It features residual blocks that learn visual features at multiple spatial scales simultaneously.
- **Scale Invariance**: This is critical because the 5 cameras are mounted at different heights and angles—`CAM_ENTRY_01` captures people at high resolution, while `CAM_FLOOR` cameras see them at various distances. OSNet matches appearance vectors across these scale mismatches.

### Session Association & Re-ID Logic
To prevent duplicate visitor sessions, a global in-memory registry of recently active tracks is maintained:
$$\text{Track Registry} = \{ \text{visitor\_id}: (\text{last\_seen\_timestamp}, \vec{E}_{\text{appearance}}, \text{session\_seq}) \}$$

When a new person is detected crossing the entry threshold, their visual embedding vector ($\vec{E}_{\text{new}}$) is checked against active registry embeddings ($\vec{E}_{\text{active}}$) using cosine distance:
$$d_{\text{cosine}} = 1 - \frac{\vec{E}_{\text{new}} \cdot \vec{E}_{\text{active}}}{\|\vec{E}_{\text{new}}\| \|\vec{E}_{\text{active}}\|}$$

* **Matching Threshold**: If $d_{\text{cosine}} < 0.4$ and the difference in time is $\le 5\text{ minutes}$, the track is classified as a match.
* **Re-Entry Event**: The pipeline reuses the existing `visitor_id` and emits a `REENTRY` event instead of a new `ENTRY` event.
* **New Session**: If no match is found, a new `visitor_id` (format: `VIS_{6-char hex}`) is generated.

### Staff Exclusion (Torso HSV Analysis)
To prevent staff movement (e.g., floor managers pacing aisles or cashiers entering/exiting) from inflating conversion and traffic metrics, the pipeline applies a deterministic **HSV color analysis** to the torso region of every bounding box:
- If $>30\%$ of the pixels in the torso crop fall within the HSV range of the staff uniform (e.g., purple/violet uniform bounds defined in `store_layout.json` under `staff_uniform_hsv`), the track is tagged `is_staff: true`.
- Staff-tagged events are committed to the database, but they are strictly excluded from all customer analytics queries via SQL filters (`WHERE is_staff = 0`).

---

## 5. Zone Mapping & Event Generation

Dynamic bounding box coordinate paths are converted into structured business events by intersecting them with static polygonal zones.

```text
[Bounding Box Centroid] ──> [Shapely Point-in-Polygon Check] ──> [Event Generation Logic]
```

### Geometric Zone Mapping
The pipeline uses the **Shapely** geometry library to run point-in-polygon calculations. The bottom-center coordinate of a bounding box (representing the shopper's feet on the floor) is evaluated against 2D polygonal coordinate lists loaded from `store_layout.json` (such as `SKINCARE`, `HAIRCARE`, and `BILLING`).
- Centroid inside polygon $\rightarrow$ `ZONE_ENTER` event.
- Centroid leaves polygon $\rightarrow$ `ZONE_EXIT` event.
- Continuous presence inside polygon $\ge 30\text{ seconds}$ $\rightarrow$ `ZONE_DWELL` event (emitted every 30s with accumulated `dwell_ms`).

### Overlap Deduplication Rules
Because the 5-camera topology has overlapping fields of view (e.g., the center aisle is visible on both floor cameras), a customer could trigger concurrent events on two cameras. The pipeline resolves this using a strict **camera priority hierarchy** defined for overlapping regions:
$$\text{CAM\_ENTRY\_01} > \text{CAM\_FLOOR\_01} > \text{CAM\_FLOOR\_02} > \text{CAM\_OVERVIEW\_01} > \text{CAM\_BILLING\_01}$$

If a coordinates centroid falls into an overlap zone, events from the lower-priority camera are suppressed.

---

## 6. Billing Queue, Conversion Rate & POS Correlation

The Intelligence API acts as the central correlation engine, connecting physical visual events with transactions from the cash registers.

```text
                                  ┌──────────────────────────┐
                                  │   pos_transactions CSV   │
                                  └─────────────┬────────────┘
                                                │ (Match Transaction)
                                                ▼
┌──────────────────────────┐      ┌──────────────────────────┐      ┌──────────────────────────┐
│   BILLING_QUEUE_JOIN     │ ───> │ POS Correlation Engine   │ ───> │ Mark Session: Converted  │
│ (Within 5 Min Before Tx) │      │ (5-Min Time-Window Match)│      │    (is_converted = 1)    │
└──────────────────────────┘      └──────────────────────────┘      └──────────────────────────┘
```

### POS Transaction Correlation Engine
POS transactions from `pos_transactions.csv` do not contain customer photos or tracking tokens, only a `store_id`, `transaction_id`, `timestamp`, and `basket_value_inr`. The system bridges this gap using temporal-spatial proximity matching.

When an ingestion batch completes, an async background task searches for unmatched transactions:
1. **Window Filtering**: For each unmatched transaction at timestamp $T_{\text{txn}}$, the engine queries the `events` database for visitors present in the billing zone (`CAM_BILLING_01`) who triggered a `BILLING_QUEUE_JOIN` within the **5-minute window before the purchase**:
$$T_{\text{txn}} - 5\text{ minutes} \le T_{\text{join}} \le T_{\text{txn}}$$
2. **Deterministic Resolution**:
   * **Single Match**: If exactly one unique `visitor_id` is found in the queue window, they receive credit. Their session is marked `is_converted = 1` in `visitor_sessions`, and their `visitor_id` is written to `pos_transactions.matched_visitor_id`.
   * **Multiple Matches**: If multiple shoppers were in the queue, the transaction is correlated to the shopper with the **most recent** `BILLING_QUEUE_JOIN` event (closest to the transaction time).
   * **Zero Matches**: If no shoppers are found, the transaction remains unmatched (no conversion credit is assigned).

### Real-Time Queue Depth & Abandonment Tracking
The system tracks checkout queue efficiency using the billing counter camera (`CAM 5`):
* **Live Queue Depth**: Because queue depth changes dynamically, the API computes it on-the-fly:
$$\text{Queue Depth} = \max\Big(0,\ \text{Count}(\text{BILLING\_QUEUE\_JOIN}) - \text{Count}(\text{BILLING\_QUEUE\_ABANDON} \cup \text{EXIT})\Big)$$
* **Queue Join Event**: Triggered when a shopper crosses into the billing counter polygon.
* **Queue Abandonment**: Shoppers who join the queue but leave the store without purchasing represent lost revenue. If a customer triggers `BILLING_QUEUE_JOIN` but has **no correlated POS transaction within 10 minutes** (the queue timeout threshold), a background worker automatically generates and inserts a `BILLING_QUEUE_ABANDON` event.
* **Abandonment Rate**: Computed directly from the database:
$$\text{Abandonment Rate} = \frac{\text{Count}(\text{BILLING\_QUEUE\_ABANDON})}{\text{Count}(\text{BILLING\_QUEUE\_JOIN})}$$

### Conversion Rate Computation
The store conversion rate is calculated dynamically from the `visitor_sessions` table, excluding staff:
$$\text{Conversion Rate} = \frac{\sum(\text{is\_converted} = 1)}{\text{Count}(\text{visitor\_sessions} \mid \text{is\_staff} = 0)}$$

This design ensures that if a customer exits and re-enters the store multiple times, they are counted as a single session in the denominator, preserving the accuracy of the metric.

---

## 7. Technology Stack

The platform is divided into specialised tech stacks chosen for edge resilience, execution speed, and low operational overhead.

| Pipeline Stage | Technology | Selected Libraries | Rationale |
| :--- | :--- | :--- | :--- |
| **Video & CV Inference** | Python 3.11 | OpenCV, PyTorch, Ultralytics (YOLOv8), Torchreid, Shapely | Standard computer vision stack. High frame processing rates and robust point-in-polygon math. |
| **Transport Layer** | File System | NDJSON (`.jsonl` files) | Decouples CV processing from API ingestion. Safe against network drops; easily queryable and readable. |
| **Intelligence API** | FastAPI | Uvicorn, SQLAlchemy | High-performance asynchronous execution. Prevents blockages during heavy SQLite batch write commits. |
| **Storage Engine** | Relational DB | SQLite (aiosqlite async driver) | Zero-configuration. Entire database resides in a single portable file, making edge backups simple. |
| **Dashboards** | Frontend & CLI | Vanilla HTML5/JS (polling), Rich (Terminal UI) | Zero build steps, zero NPM dependencies. Low footprint, high portability. |

---

## 8. Technical Choices & Trade-offs

Every design decision in the Store Intelligence platform was evaluated against retail edge constraints: poor hardware, lack of local IT support, and intermittent internet connectivity.

### 1. Object Detection: YOLOv8m vs. Custom ResNet
* **AI Suggestion**: Train a custom ResNet bounding box model on store-specific footage.
* **Actual Choice**: Pre-trained YOLOv8m (Medium) using zero-shot inference.
* **Trade-off / Rationale**: Training a custom model requires thousands of manually labeled frames, introducing high training debt. As soon as the store layout changes or a camera is nudged, accuracy degrades, requiring retraining. YOLOv8m is pre-trained on the COCO dataset, which already contains millions of highly accurate human bounding boxes. It runs at near real-time speeds on standard GPUs and requires zero store-specific training data.

### 2. Multi-Object Tracking: ByteTrack vs. DeepSORT
* **AI Suggestion**: Use DeepSORT for intra-camera tracking.
* **Actual Choice**: ByteTrack.
* **Trade-off / Rationale**: DeepSORT drops tracks as soon as occlusion drops confidence below a high threshold (typically $0.6$). In retail, where shoppers frequently walk behind display stands, this causes track fragmentation, inflating unique visitor counts. ByteTrack's dual-buffer strategy maintains tracks using motion momentum during brief occlusions, resulting in highly stable `visitor_id` sessions.

### 3. Re-ID Signal: Embedding-First vs. Trajectory-First
* **AI Suggestion**: Use bounding box trajectories as the primary Re-ID signal, verifying with appearance embeddings.
* **Actual Choice**: OSNet appearance embeddings as the primary signal; trajectory-distance as a secondary check.
* **Trade-off / Rationale**: Bounding box trajectories work well for continuous motion, but they fail when tracks are broken (e.g., when a person is hidden behind a wide shelving column) or when multiple shoppers cross paths. OSNet embedding similarity represents a shopper's visual appearance and remains stable across cameras and occlusions, even when spatial trajectories are disconnected.

### 4. Database Engine: SQLite vs. PostgreSQL + Redis
* **AI Suggestion**: Deploy a production PostgreSQL database with Redis Streams for message buffering and caching.
* **Actual Choice**: SQLite (via async `aiosqlite`) coupled with batch event ingestion.
* **Trade-off / Rationale**: Deploying PostgreSQL and Redis at the edge adds heavy RAM usage and increases container management overhead. A single corrupt container or a network mismatch can bring down the entire local database. SQLite stores everything in a single, standard file (`store_intelligence.db`). It is zero-maintenance and low on RAM.
* **Mitigating SQLite Lock Write Performance**: SQLite's main limitation is database-level locking during writes. The system handles this by using a batch ingest endpoint (`POST /events/ingest`) that groups up to 500 events into a single atomic transaction. This reduces write-lock frequency and handles high event volume efficiently.

### 5. Frontend Communication: HTTP Long Polling vs. WebSockets
* **AI Suggestion**: Implement WebSockets for real-time dashboard streaming.
* **Actual Choice**: HTTP Long Polling at a 3-second interval.
* **Trade-off / Rationale**: WebSockets require the API server to maintain persistent connection states for every connected screen. On edge machines with limited resources, managing this connection state can exhaust sockets and crash the service.
* **Real-world requirement**: Store managers looking at retail metrics do not need sub-second updates. Telemetry like conversion rates or hourly traffic averages remain useful even with a 3-second lag. HTTP polling keeps the API stateless and highly resilient, avoiding connection cleanup overhead.

### 6. UI Implementation: Vanilla HTML/JS vs. React/Redux
* **AI Suggestion**: Build a full React.js SPA with Redux state management.
* **Actual Choice**: Vanilla JS + HTML5 static file served directly from the FastAPI container.
* **Trade-off / Rationale**: React introduces complex NPM builds, node dependency vulnerabilities, and larger package sizes. Vanilla JS allows the entire web interface to be packaged into a single HTML file under `dashboard/web/`. It has zero dependencies, loads instantly, and runs on any standard web browser with no compilation step.

### 7. Event Schema: Flat DB Columns vs. Extensible JSON
* **AI Suggestion**: Use a flat database schema for high-speed indexing.
* **Actual Choice**: Core query fields (e.g., `event_type`, `visitor_id`, `timestamp`) are mapped to indexed columns, while unstructured details (e.g., `queue_depth`, `sku_zone`) are stored in a flexible JSON metadata column.
* **Trade-off / Rationale**: A flat database is fast but rigid. If the edge team adds a new sensor or camera metadata field later, the database schema must be migrated, which can cause table locks and potential data loss. Storing secondary attributes in a JSON metadata field keeps the schema flexible. New attributes can be ingested immediately without changing the database structure, while core query speeds remain fast due to primary indexes.

---

## 9. Key Edge Cases Handled

The system is configured to handle the common challenges of real-world retail video telemetry:

| Edge Case | Detection Pipeline Solution | API & Database Solution |
| :--- | :--- | :--- |
| **Group Entries (Families/Couples)** | Low-iou NMS threshold (`0.45`) preserves individual bounding boxes. | Each box is assigned a distinct track, creating separate sessions. |
| **Staff Movement** | Torso HSV color analysis flags staff members (`is_staff: true`). | SQL queries filter out staff records (`WHERE is_staff = 0`). |
| **Partial Occlusion (Shelves/Racks)** | ByteTrack keeps tracks active in a secondary low-confidence buffer. | Low-confidence events are saved with a `partial_occlusion` flag. |
| **Brief Exits & Re-entries** | Cosine distance matches embeddings under 0.4 within a 5-minute window. | Emits a `REENTRY` event and increments `reentry_count` on the session. |
| **Camera Angle Overlap** | Geometric suppression drops duplicate events based on camera priority. | DB constraints enforce a `UNIQUE` index on event IDs to ignore duplicates. |
| **Empty Store Periods** | Pipeline runs normally but emits no events, avoiding crashes. | API returns `0` and `0.0` values instead of returning null or crashing. |
| **Cashier Queue Buildup** | Dynamic bounding box tracking monitors active presence in the billing zone. | Real-time query computes queue depth based on active entry and exit events. |
