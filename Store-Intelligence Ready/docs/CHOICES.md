# CHOICES.md - Engineering Decisions & Trade-offs

> **Purplle Tech Challenge 2026 — Round 2 Submission**

This document outlines the architectural rationale behind the Store Intelligence platform. As an end-to-end telemetry system meant for retail edge deployments, every decision balances computational constraint (performance) against business reliability (accuracy).

Each decision follows a consistent structure: the problem being solved, options considered, what the AI assistant suggested, what was actually chosen, why, and what tradeoffs were accepted.

---

## 1. Detection Model Choice

**Problem Being Solved:** We need to accurately detect human bounding boxes across 4 CCTV cameras per store in real-time, dealing with poor lighting, severe occlusion by retail shelving, varying focal lengths, and crowded aisles — without a proprietary labelled dataset.

**Options Considered:**

| Model | Speed (15fps) | Accuracy | Notes |
|---|---|---|---|
| Custom PyTorch ResNet | Slow to build | High (in theory) | Requires thousands of labelled store images; high overfitting risk to one store's layout |
| YOLOv8n | Very fast | Lower | Too many missed detections in crowded billing queue footage |
| **YOLOv8m (Pre-trained COCO)** | Fast | Good | ByteTrack natively integrated via `ultralytics`; zero training data needed |
| YOLOv8x | Slow | Best | Would not sustain 15fps on standard edge hardware |
| RT-DETR | Medium | Very good on occlusion | No native ByteTrack integration; higher integration overhead |
| MediaPipe | Very fast | Limited | Optimised for faces and poses, not full-body crowd tracking |
| VLM (GPT-4V / Claude Vision) | Very slow (100–500ms/frame) | High on complex scenes | Not viable for real-time 15fps processing; API cost prohibitive at edge scale |

**What AI Suggested:** Build a custom detection model pipeline from scratch using a ResNet backbone to "ensure maximum accuracy for this specific store environment."

**Final Decision:** YOLOv8m (Medium) using pre-trained COCO weights, zero-shot. CLAHE pre-processing applied per-frame to normalise fluorescent vs. natural lighting variation.

**Why:** Simplicity over theoretical perfection. We strictly need the `person` class — a class that COCO covers extremely well with a `person` AP of ~56%. Training a custom model introduces massive technical debt: thousands of labelled store-specific frames, a training pipeline to maintain, and a model that overfits to one store's specific camera angles. YOLOv8m delivers 95% of that accuracy for 5% of the engineering effort. The `ultralytics` API handles NMS and confidence thresholding cleanly out of the box, and the built-in ByteTrack integration (`model.track(source, tracker="bytetrack.yaml")`) eliminates the need for a separate tracking library entirely — this was the deciding practical factor.

RT-DETR was a serious contender for the billing zone camera where occlusion is heaviest, but the lack of native ByteTrack integration and slower inference speed outweighed the accuracy benefit at this scope. At 40 live stores, I would revisit RT-DETR specifically for billing counter cameras.

**On VLM for Staff Detection and Zone Classification:** We explicitly evaluated using GPT-4V for classifying staff vs. customers (since uniform detection seems like a natural language task) and for zone classification. We rejected both use cases: (1) per-frame API latency of 100–500ms makes it impossible to run at 15fps on edge hardware, (2) HSV colour-space thresholding on known uniform colours is fully deterministic, runs in microseconds with no external dependency, and achieves comparable accuracy since staff uniforms are visually consistent, and (3) zone classification is a pure geometric problem — Shapely point-in-polygon is faster, cheaper, and more reliable than a VLM prompt for this structured task.

**Tradeoffs Accepted:** Occasional false positives on mannequins or large posters of people. Heavier GPU VRAM requirement compared to YOLOv8n. We accept both in exchange for zero training data requirements and immediate deployment. YOLOv8m will occasionally produce merged bounding boxes when two people stand very close together in the billing queue — the mitigation is to never drop low-confidence detections (see Decision 2).

---

## 2. Tracking and Re-ID Strategy

**Problem Being Solved:** Bounding boxes alone do not track an individual's journey. We need to persist identities (1) across consecutive frames on the same camera (Multi-Object Tracking) and (2) when a visitor crosses into a different camera's field of view across all cameras (cross-camera Re-ID). Both are essential for accurate unique visitor counts and conversion funnels.

**Options Considered:**

| Component | Option A | Option B | Decision |
|---|---|---|---|
| MOT (intra-camera) | DeepSORT | **ByteTrack** | ByteTrack |
| Re-ID (cross-camera) | **Torchreid OSNet** | FastReID | OSNet |
| Re-ID signal weighting | Bounding box trajectory first | **Embedding distance first** | Embedding distance |

**What AI Suggested:** FastReID for maximum cross-camera Re-ID accuracy. For Re-ID signal weighting, AI initially suggested using bounding box trajectory as the primary signal and embedding distance as secondary verification.

**Final Decision:** ByteTrack for MOT + Torchreid OSNet (`osnet_x1_0`) for cross-camera Re-ID, with embedding distance weighted as the primary Re-ID signal.

**Why ByteTrack over DeepSORT:** DeepSORT discards low-confidence detection boxes entirely. In a retail environment, a person stepping behind a display shelf regularly drops to low-confidence detections for several seconds. DeepSORT kills the track and reassigns a new ID when the person re-emerges — inflating visitor counts. ByteTrack holds low-confidence boxes in a secondary buffer and re-associates them to the existing track ID when confidence recovers. This is the single most important difference for retail aisle footage.

**Why OSNet over FastReID:** OSNet uses omni-scale feature learning — it captures appearance features at multiple spatial scales simultaneously. This matters because the store cameras have varying focal lengths: the entry camera captures people at roughly full-body size while the billing camera sees them much closer. A Re-ID model that learns at a single scale will struggle with this size mismatch across cameras. OSNet handles it natively and is significantly lighter than FastReID, making it viable on edge hardware without a batch GPU inference queue.

**Why embedding distance over trajectory as primary signal:** Trajectory alone breaks when two different people enter from the same direction 3 seconds apart — a scenario explicitly flagged in the challenge problem statement as an edge case. Appearance embedding distance is more discriminative for this case. The 0.4 cosine distance threshold and 5-minute re-identification window were chosen empirically to reliably distinguish brief exits (phone calls, quick breaks) from genuine new visits.

**Tradeoffs Accepted:** OSNet occasionally fails when a visitor's clothing is highly generic (plain black top, dark jeans) under variable fluorescent lighting. The Re-ID confidence drops below threshold and the system may fragment one visitor's journey into two partial sessions, slightly inflating `unique_visitors` and deflating `conversion_rate`. We accept this in exchange for real-time inference speeds on edge hardware.

---

## 3. Event Schema Design

**Problem Being Solved:** The detection pipeline produces 15fps of bounding box coordinates across 4 cameras — continuous, unstructured, and enormous in volume. We need to translate this raw visual stream into discrete, queryable business facts that the API can ingest and aggregate without being overwhelmed.

**Options Considered:**

| Approach | Description | Outcome |
|---|---|---|
| Raw coordinate streaming | Push every bounding box at 15fps to the backend via WebSockets | 15fps × 5 cameras × N bounding boxes per frame = network collapse; tightly couples CV pipeline to API |
| State delta streaming | Only push when bounding box position changes significantly | Still requires a live connection; backend must reconstruct zone logic from coordinates |
| **Discrete Event-Driven Architecture** | Pipeline evaluates zone rules internally and only emits typed stateful events | ~99% payload reduction; fully decoupled; API receives clean business facts |

**What AI Suggested:** Continuously stream raw bounding box coordinates to the backend via WebSockets for real-time state calculation server-side.

**Final Decision:** Discrete Event-Driven Architecture using typed event schemas (`ENTRY`, `EXIT`, `ZONE_ENTER`, `ZONE_DWELL`, `BILLING_QUEUE_JOIN`, etc.) decoupled via `.jsonl` files.

**Why:** Streaming 15fps coordinate data from multiple cameras to an API chokes the network layer and deeply couples the CV pipeline to the database. By forcing the CV pipeline to evaluate spatial rules internally — using Shapely polygon intersection against `store_layout.json` — and only emitting stateful events (e.g., "Visitor A dwelled in ZONE_SKINCARE for 45 seconds"), we reduce network payload by approximately 99%. The API then only receives clean, typed business facts and never needs to know what a bounding box coordinate is.

**Why `.jsonl` files as the transport layer:** Rather than calling the API directly from the pipeline, events are written to a `.jsonl` file first. This decouples pipeline execution speed from API availability entirely. The pipeline can process recorded clips faster than real-time and the API ingests at its own pace via the batch endpoint. A direct API call from the pipeline would create a hard dependency that fails on any network blip — with `.jsonl` files, events safely accumulate and are batch-ingested once the API recovers.

**Schema structure — nested metadata over flat schema:** AI recommended a flat schema for query performance, arguing that `json_extract()` in SQLite adds overhead for frequently-filtered fields. I chose nested metadata matching the `sample_events.jsonl` format exactly — core queryable fields (`event_type`, `timestamp`, `visitor_id`, `zone_id`, `dwell_ms`, `is_staff`, `confidence`) are top-level indexed columns in the database, while rarely-queried fields (`queue_depth`, `sku_zone`, `session_seq`, `partial_occlusion`) live in the `metadata` object in the event schema but are extracted to individual database columns on ingest. This gives the query performance of a flat schema while keeping the schema extensible — adding new metadata fields in future requires no database migration.

**Key principle — never suppress low-confidence detections:** A `confidence=0.2` event is stored and served with `metadata.partial_occlusion=True`. The scoring rubric explicitly penalises silently dropping low-confidence detections. Dropping events loses calibration data; the consumer (API or analyst) decides how to weight confidence at query time.

**Tradeoffs Accepted:** We lose the ability to replay exact coordinate paths or render precise visitor movement trails in the dashboard. We trade high-fidelity movement visualisation for massive scalability and complete pipeline-API decoupling.

---

## 4. Session Deduplication Strategy

**Problem Being Solved:** Multiple events can arrive for the same physical visitor across all cameras and multiple re-entries. We need to ensure the conversion funnel counts sessions accurately — a visitor who re-enters twice and makes one purchase must count as exactly 1 session and 1 conversion, not 3 sessions.

**Options Considered:**

| Approach | Description | Problem |
|---|---|---|
| Count raw ENTRY events | Simple | Double-counts re-entering visitors; inflates funnel Stage 1 |
| Count DISTINCT visitor_ids from events | Better | Doesn't capture session duration, conversion status, or re-entry count |
| **Explicit visitor_sessions table** | Maintains session state — entry_time, exit_time, is_converted, reentry_count | Clean separation of session lifecycle from raw event storage |

**What AI Suggested:** Maintain an explicit `visitor_sessions` table updated via background tasks after each ingest, separating session lifecycle management from raw event storage.

**Final Decision:** Explicit `visitor_sessions` table. This matches the AI suggestion — one of the few decisions where I agreed fully.

**Why:** The key insight is that `REENTRY` events increment `reentry_count` on the existing session row rather than creating a new one. This means `COUNT(DISTINCT visitor_id) WHERE is_staff=0` from `visitor_sessions` gives the correct Stage 1 funnel count regardless of how many times a visitor enters and exits. The funnel's purchase stage uses `is_converted=1` set by the POS correlation background task — a visitor who re-enters twice and makes one purchase is counted as exactly 1 converted session.

**At Production Scale:** At 40 stores with continuous event ingestion, this design would require read replicas for funnel and heatmap queries to avoid write contention, database sharding by `store_id`, and a Redis cache for funnel computation (stale by 60 seconds is acceptable). The current SQLite implementation is deliberately scoped to the challenge's single-server demo — the session table design is production-compatible; only the database engine would change.

**Tradeoffs Accepted:** The `visitor_sessions` table introduces an extra write on every `ENTRY`, `EXIT`, and `REENTRY` event. At single-store volumes this is negligible. At 40 stores it would require architectural revision.

---

## 5. Database Choice

**Problem Being Solved:** The backend needs to ingest thousands of asynchronous telemetry events from all cameras and execute complex aggregate queries — conversion funnels, zone dwell averages, queue depth — in near real-time, on edge hardware with constrained RAM and no guaranteed network uptime.

**Options Considered:**

| Database | Pros | Cons |
|---|---|---|
| PostgreSQL | Unmatched concurrency; robust analytical window functions | Heavy RAM footprint; requires separate container management; overkill for a single-store deployment |
| Redis | Blazing fast in-memory pub/sub | No native relational querying; would require a second persistent store for session data |
| **SQLite** | Zero-config; single-file portability; low RAM usage; full SQL | Poor concurrent write performance due to database-level locking |

**What AI Suggested:** PostgreSQL combined with a Redis caching layer for maximum scalability and pub/sub event streaming. AI also suggested Redis Streams as the event pipeline transport, arguing that SQLite write contention would be a bottleneck at 40 stores.

**Final Decision:** SQLite using the `aiosqlite` async driver.

**Why:** Retail stores are edge environments — typically a backroom PC with constrained RAM, no guaranteed network uptime, and a single operator who cannot manage database containers. A PostgreSQL + Redis stack requires persistent container management, network configuration between containers, and RAM that a backroom PC may not have. SQLite keeps the entire database as a single portable file (`store_intelligence.db`). Backup is a file copy. Redeploy is a file copy. Zero services to manage.

SQLite's notorious write-locking weakness is mitigated by the batch ingestion design: the API accepts up to 500 events per POST and commits them in a single atomic transaction, dramatically reducing lock frequency.

**Why I overrode the AI recommendation:** AI was optimising for a cloud deployment scenario at 40 stores. I was optimising for a retail back-office PC with one operator who cannot manage database containers. Scalability to 40 stores is a future problem; reliable one-command deployment on constrained hardware is the immediate constraint. Redis Streams adds a third infrastructure dependency for a problem that batch ingestion already solves at this scope.

**Tradeoffs Accepted:** The system cannot horizontally scale across multiple API instances sharing the same database. For a single-store edge deployment this is not a real constraint. For a multi-store cloud rollout, migrating to PostgreSQL would be necessary.

---

## 6. API Framework Choice

**Problem Being Solved:** The backend must concurrently handle heavy batch ingestion POST requests from the CV pipeline (up to 500 events across cameras per request) while simultaneously serving metric GET requests from the dashboard, without blocking the event loop.

**Options Considered:** Flask, Django, FastAPI.

**What AI Suggested:** FastAPI.

**Final Decision:** FastAPI.

**Why:** This is one decision where AI and I agreed — but for specific reasons worth stating. FastAPI natively supports Python's `asyncio`. Since the primary bottleneck is SQLite disk I/O rather than CPU, async endpoints allow the server to release the event loop while waiting for a batch write to flush, immediately handling an incoming dashboard read request. Flask's synchronous WSGI model would block the process on every SQLite write. Django is far heavier than needed and its ORM would abstract away the precise SQL aggregations required for metric computation.

Pydantic (built into FastAPI) also handles automatic validation of incoming event payloads — malformed events are rejected with structured error messages before they touch the database.

**Tradeoffs Accepted:** We use raw SQL queries (`sqlalchemy.text`) rather than a full ORM. This increases the verbosity of metric and funnel queries but gives full control over aggregation logic and avoids ORM translation overhead on latency-sensitive endpoints.

---

## 7. Real-Time Communication Choice

**Problem Being Solved:** The dashboard needs to show metrics updating live as events flow in from the detection pipeline across the cameras.

**Options Considered:**

| Method | Mechanism | Backend Complexity | Latency |
|---|---|---|---|
| WebSockets | Persistent bi-directional TCP connection | High — server must manage connection state per client | ~0ms |
| Server-Sent Events (SSE) | HTTP streaming, server pushes updates | Medium — requires open connections | ~0ms |
| **HTTP Long Polling** | Client fetches on a fixed interval | Zero — stateless HTTP requests | Up to interval length (3s) |

**What AI Suggested:** Implement WebSockets for "true real-time, bi-directional communication."

**Final Decision:** HTTP Long Polling at a 3-second interval using the native `fetch` API.

**Why:** WebSockets require the server to maintain a persistent connection object for every connected dashboard client. On a FastAPI server also handling batch ingestion from all cameras, this adds meaningful state management complexity: dropped sockets must be detected and cleaned up, reconnections must be handled, and connection state consumes memory. Server-Sent Events improve on this slightly but still require open HTTP connections.

The key insight is that store metrics like conversion rate, queue depth, and abandonment rate are macro-level business figures. They do not change meaningfully in under 3 seconds. A store manager watching the dashboard does not need sub-second precision — they need accuracy. A 3-second polling cycle via a standard `fetch` call delivers near-real-time perception with exactly zero backend state management overhead.

**Tradeoffs Accepted:** The dashboard is eventually consistent, lagging reality by up to 3 seconds. This is entirely acceptable for macro-level retail analytics.

---

## 8. Dashboard Technology Choice

**Problem Being Solved:** Visualising conversion funnels, zone heatmaps, and live KPI metrics cleanly for store managers — without adding frontend build complexity to the deployment.

**Options Considered:** React.js (with Redux), Streamlit, Vanilla JS / HTML / CSS.

**What AI Suggested:** A full React frontend with Redux for state management, or a Streamlit Python application.

**Final Decision:** Vanilla JS / HTML / CSS served as a static file from the FastAPI server, plus a terminal dashboard using `rich.live.Live` for operators who prefer a CLI view.

**Why:** Streamlit abstracts too much UI control, produces sluggish behaviour for high-frequency DOM updates, and runs as a separate Python process — another service to manage. React produces excellent UIs but requires a Node.js build step, a separate frontend container, and a non-trivial deployment footprint. For a single-page dashboard that refreshes KPI numbers and redraws a heatmap table every 3 seconds, this overhead is entirely unjustified.

Vanilla JS lets us serve the entire dashboard as a single static HTML file directly from the FastAPI server. Zero additional containers, zero build steps, zero npm dependencies. The terminal dashboard (`rich.live.Live`) gives operators a second option that works over SSH without a browser — useful in retail back-office environments where a GUI may not be available.

**Tradeoffs Accepted:** Manual DOM manipulation (`document.getElementById`, `innerHTML` updates) is more tedious than declarative React components. Future UI expansions — new chart types or complex interactions — will be slower to develop without a component framework.

---

## 9. AI-Assisted Engineering Decisions

Generative AI was used extensively during prototyping and selectively overridden during production hardening. The distinction matters: AI is excellent at generating correct boilerplate quickly; it is poor at understanding deployment constraints it was not explicitly told about.

**Adopted — Schema bootstrapping:** AI generated the initial Pydantic event schema models covering all required fields (`event_id`, `visitor_id`, `zone_id`, `dwell_ms`, `is_staff`, `confidence`, `metadata`). Adopted with minimal changes, saving roughly two hours of boilerplate work.

**Adopted — Synthetic test data:** AI generated a realistic `pos_transactions.csv` with appropriate timestamp distributions and basket value ranges for testing POS correlation logic. Required minor adjustments to align timestamps with session windows in the test clips.

**Adopted — Docker Compose configuration:** AI produced the initial `docker-compose.yml` for the FastAPI service and volume mounts. Adopted with modifications to remove unnecessary services it added by default.

**Adopted — Session table design:** AI recommended the explicit `visitor_sessions` table pattern for funnel deduplication. This was well-reasoned and adopted directly — it cleanly separates session lifecycle from raw event storage and makes `COUNT(DISTINCT visitor_id)` the correct funnel query without complex window functions.

**Rejected — Cloud-native infrastructure:** AI consistently recommended Kubernetes orchestration, PostgreSQL, Redis Streams, and Kafka for "production-grade scalability." I explicitly rejected all of these. The deployment target is a retail back-office PC, not a cloud VM. Kubernetes on backroom hardware would make deployment impossible for a non-DevOps store operator. Kafka is designed for distributed message streaming at a scale a single-store deployment will never reach. I forced the architecture to Docker Compose + SQLite + `.jsonl` files — simpler, more resilient, deployable with a single command.

**Rejected — Custom ResNet detection model:** AI suggested building a custom detection model tailored to store-specific footage. Rejected because it requires a large proprietary labelled dataset, a training pipeline, and ongoing retraining as store layouts change. Pre-trained YOLOv8m delivers equivalent results for the `person` class with zero data requirements.

**Rejected — WebSockets for real-time dashboard:** AI recommended WebSockets. Rejected in favour of 3-second HTTP polling — WebSockets add server-side connection state management with no meaningful accuracy benefit for macro-level business metrics.

**Rejected — Trajectory-first Re-ID:** AI initially suggested bounding box trajectory as the primary Re-ID signal. Rejected because trajectory alone breaks when two different people enter from the same direction 3 seconds apart — a scenario explicitly flagged in the challenge problem statement. OSNet embedding distance is the primary signal; trajectory is secondary.

**Rejected — Flat event schema:** AI recommended a flat schema for query performance. Rejected in favour of nested `metadata` with extracted indexed columns — matching `sample_events.jsonl` exactly while keeping the schema extensible without future database migrations.

---

## 10. Tradeoffs and System Limitations

1. **GPU dependency outside Docker:** The PyTorch CV inference pipeline runs outside Docker to avoid GPU-passthrough performance penalties on Windows edge hardware. This means the pipeline setup requires a manual step rather than being fully contained in `docker-compose up`. On Linux with `nvidia-docker`, the pipeline can be fully containerised.

2. **Queue abandonment lag:** `BILLING_QUEUE_ABANDON` is emitted only after a 10-minute inactivity timeout. If a visitor walks away from the billing queue without triggering a clean `EXIT`, `queue_depth` temporarily over-reports until the timeout fires. This is an accepted limitation of not having a dedicated "queue exit" sensor.

3. **Hardware bound on GPU:** YOLOv8m requires a dedicated NVIDIA GPU to sustain real-time 15fps inference across 4 cameras. On CPU-only edge devices the pipeline falls back to YOLOv8n (nano), trading roughly 15–20% bounding box accuracy for deployability on consumer hardware.

4. **Single-store scalability ceiling:** SQLite's single-writer model means the architecture cannot support multiple pipeline instances writing to the same database simultaneously. A multi-store cloud deployment would require migrating to PostgreSQL, adding read replicas for funnel queries, and introducing a Redis cache for computed metrics.

5. **OSNet fragmentation under generic clothing:** When a visitor's clothing is highly generic under variable lighting, OSNet may assign two `visitor_id` values to the same physical person across camera boundaries, slightly inflating `unique_visitors` and deflating `conversion_rate`. This is the primary source of inaccuracy in the Re-ID layer.

---

## 11. POS Data Ingestion & Correlation Strategy

**Problem Being Solved:** The store's Point of Sale (POS) system generates transactional data (CSV logs) independently of the computer vision pipeline. We need to correlate these offline receipts to active visitor sessions in the database to calculate accurate funnel conversion rates, despite having no shared `customer_id` between the video feeds and the till.

**Options Considered:**

| Approach | Mechanism | Robustness |
|---|---|---|
| Deep Integration | Tie directly into POS API | High overhead, fragile across different store POS vendors |
| Face Re-ID at Checkout | Match face at register to entry face | High privacy risk, fails when looking down or wearing masks |
| **Time-Windowed Spatial Correlation** | Match POS timestamps to visitor presence in the billing zone | Vendor agnostic, privacy-preserving, high accuracy |

**What AI Suggested:** Use facial recognition at the checkout counter to establish a definitive link between the purchasing customer and their entry session. 

**Final Decision:** Time-Windowed Spatial Correlation using asynchronous POS CSV parsing.

**Why:** Facial recognition is fraught with privacy concerns, regulatory compliance issues (GDPR/CCPA), and technical failure modes (looking down at wallets, occlusion from the cashier). Instead, our system uses spatial geometry: if a visitor's bounding box is detected within the `BILLING` zone polygon, their session is flagged as "in queue". When a POS transaction occurs, a background job searches for sessions that were active in the billing zone within a 5-minute window leading up to that transaction timestamp. This method is completely POS-vendor agnostic — as long as the POS can output a timestamped CSV, our system can ingest it and calculate conversions without requiring heavy integrations.

**Tradeoffs Accepted:** If two people are standing in the billing zone at the exact same moment and only one makes a purchase, the system may attribute the conversion to the wrong session internally. However, from a macro-level funnel analytics perspective, the aggregate `conversion_rate` remains mathematically identical, which is the primary KPI store managers care about.

---

## 12. Privacy & Data Retention Strategy

**Problem Being Solved:** Retail stores process hundreds of customers daily. Storing raw video feeds or long-term biometric data introduces significant legal liability, storage costs, and customer trust issues. The system must provide business intelligence without becoming a surveillance dragnet.

**Options Considered:**

| Retention Strategy | Data Kept | Privacy Risk | Value for ML Training |
|---|---|---|---|
| Keep Everything | Raw .mp4 files, full embeddings | High (Identifiable) | Maximum |
| **Structural Telemetry Only** | Zone events, anonymised session IDs, zero video | Near Zero | Low |
| Rolling Video Buffer | 7-day .mp4 retention | Medium | Medium |

**What AI Suggested:** Implement a rolling 30-day video archive to allow for future model retraining and manual incident review.

**Final Decision:** Structural Telemetry Only. Raw video frames are discarded from memory the millisecond they are processed by the YOLO head.

**Why:** The primary directive of this project is business telemetry (queue depth, conversions, dwell times), not loss prevention or security. By discarding the visual data instantly and only persisting structural metadata (`visitor_id: e8a2... entered ZONE_MAKEUP at 14:02:10`), the resulting `events_output/*.jsonl` files and the SQLite database contain absolutely zero Personally Identifiable Information (PII). The OSNet appearance embeddings used for cross-camera Re-ID are mathematical vectors that cannot be reverse-engineered into a human face, and even these are purged after the session timeout window expires.

**Tradeoffs Accepted:** We sacrifice the ability to manually audit the system's accuracy after the fact by re-watching the footage. If the detection pipeline misclassifies a mannequin as a person, we cannot look back at the video to debug it. We accept this constraint to guarantee absolute privacy by design.

---

## Summary

| Decision | AI Suggestion | Choice Made | Agreed? |
|---|---|---|---|
| Detection model | Custom ResNet | YOLOv8m pre-trained | No — training overhead unjustified |
| VLM for staff / zones | Not explicitly suggested | Rejected in favour of HSV + Shapely | — |
| MOT tracker | Not specified | ByteTrack | — |
| Cross-camera Re-ID | FastReID | OSNet | No — lighter, omni-scale |
| Re-ID signal | Trajectory-first | Embedding distance-first | No — trajectory breaks on same-direction entries |
| Event schema structure | Flat schema | Nested metadata + flat DB columns | Partially |
| Event transport | WebSocket streaming | `.jsonl` files + batch ingest | No — decoupling and resilience |
| Session deduplication | visitor_sessions table | visitor_sessions table | Yes |
| Database | PostgreSQL + Redis | SQLite | No — edge deployment constraints |
| API framework | FastAPI | FastAPI | Yes |
| Real-time comms | WebSockets | HTTP long polling (3s) | No — state management overhead |
| Dashboard | React + Redux | Vanilla JS + terminal dashboard | No — deployment complexity |