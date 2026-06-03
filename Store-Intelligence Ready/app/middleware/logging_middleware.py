"""
app/middleware/logging_middleware.py — Structured JSON logging middleware

Generates a UUID v4 trace_id per request, adds X-Trace-ID to response headers,
and logs structured JSON with latency, store_id, endpoint, method, status, and event_count.
"""
from __future__ import annotations

import re
import time
import uuid
from typing import Optional

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = structlog.get_logger(__name__)

STORE_ID_RE = re.compile(r"/stores/([^/]+)")


def _extract_store_id(path: str) -> Optional[str]:
    match = STORE_ID_RE.search(path)
    return match.group(1) if match else None


class LoggingMiddleware(BaseHTTPMiddleware):
    """
    Starlette middleware that logs every request with structured JSON via structlog.
    Adds X-Trace-ID header to every response.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        trace_id = str(uuid.uuid4())
        request.state.trace_id = trace_id

        start_time = time.perf_counter()

        # For POST /events/ingest, capture event_count before calling next
        event_count: Optional[int] = None
        if request.method == "POST" and "/events/ingest" in request.url.path:
            try:
                body = await request.body()
                import json
                payload = json.loads(body)
                event_count = len(payload.get("events", []))
                # Rebuild the request body so downstream handlers can still read it
                from starlette.datastructures import Headers
                from io import BytesIO
                request._body = body  # type: ignore[attr-defined]
            except Exception:
                pass

        response: Response = await call_next(request)

        latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
        store_id = _extract_store_id(request.url.path)

        response.headers["X-Trace-ID"] = trace_id

        logger.info(
            "request",
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            trace_id=trace_id,
            store_id=store_id,
            endpoint=str(request.url.path),
            method=request.method,
            latency_ms=latency_ms,
            event_count=event_count,
            status_code=response.status_code,
        )

        return response
