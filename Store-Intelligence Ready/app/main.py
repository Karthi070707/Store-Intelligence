"""
app/main.py — FastAPI entrypoint for Store Intelligence API

Mounts all middleware, registers all routers, and handles startup/shutdown.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import structlog
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()

# Configure structlog for JSON output
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.stdlib.add_log_level,
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    logger_factory=structlog.PrintLoggerFactory(),
)

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

from app.middleware.error_handler import register_error_handlers  # noqa: E402
from app.middleware.logging_middleware import LoggingMiddleware  # noqa: E402
from app.database import init_db, close_db, AsyncSessionLocal, load_pos_csv  # noqa: E402

# Routers
from app.ingestion import router as ingestion_router  # noqa: E402
from app.metrics import router as metrics_router  # noqa: E402
from app.funnel import router as funnel_router  # noqa: E402
from app.heatmap import router as heatmap_router  # noqa: E402
from app.anomalies import router as anomalies_router  # noqa: E402
from app.health import router as health_router  # noqa: E402

# ---------------------------------------------------------------------------
# Create app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Store Intelligence API",
    description="Retail analytics API for Apex Retail — powered by CCTV event streams.",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# ---------------------------------------------------------------------------
# Middleware (order matters: CORS first, then logging)
# ---------------------------------------------------------------------------

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(LoggingMiddleware)

# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------

register_error_handlers(app)

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

app.include_router(ingestion_router)
app.include_router(metrics_router)
app.include_router(funnel_router)
app.include_router(heatmap_router)
app.include_router(anomalies_router)
app.include_router(health_router)


# ---------------------------------------------------------------------------
# Root endpoint
# ---------------------------------------------------------------------------

@app.get("/", tags=["root"])
async def root():
    return {"service": "store-intelligence-api", "version": "1.0.0", "status": "ok"}


# ---------------------------------------------------------------------------
# Startup / Shutdown lifecycle
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup_event():
    app.state.start_time = datetime.now(timezone.utc)
    await init_db()
    async with AsyncSessionLocal() as db:
        await load_pos_csv(db)


@app.on_event("shutdown")
async def shutdown_event():
    await close_db()
