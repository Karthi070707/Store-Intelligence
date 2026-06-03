"""
app/middleware/error_handler.py — Graceful error handling

Converts exceptions to structured JSON responses.
Never exposes stack traces in HTTP responses.
Logs full traceback internally via structlog.
"""
from __future__ import annotations

import traceback
from typing import Optional

import structlog
from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.database import DBUnavailableError

logger = structlog.get_logger(__name__)


def _get_trace_id(request: Request) -> Optional[str]:
    return getattr(request.state, "trace_id", None)


async def db_unavailable_handler(request: Request, exc: DBUnavailableError) -> JSONResponse:
    """Handle DBUnavailableError → HTTP 503."""
    trace_id = _get_trace_id(request)
    logger.error(
        "db_unavailable",
        trace_id=trace_id,
        error=str(exc),
        path=request.url.path,
    )
    return JSONResponse(
        status_code=503,
        content={
            "error": "service_unavailable",
            "message": "Database is temporarily unavailable. Please retry shortly.",
            "trace_id": trace_id,
        },
    )


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Handle Pydantic RequestValidationError → HTTP 422 with structured body."""
    trace_id = _get_trace_id(request)
    fields = []
    for err in exc.errors():
        fields.append({
            "loc": list(err.get("loc", [])),
            "msg": err.get("msg", ""),
            "type": err.get("type", ""),
        })
    logger.warning(
        "validation_error",
        trace_id=trace_id,
        path=request.url.path,
        field_count=len(fields),
    )
    return JSONResponse(
        status_code=422,
        content={
            "error": "validation_error",
            "message": "Request validation failed. Check the 'fields' array for details.",
            "fields": fields,
            "trace_id": trace_id,
        },
    )


async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Handle all unhandled exceptions → HTTP 500. Never expose stack traces."""
    trace_id = _get_trace_id(request)
    logger.error(
        "unhandled_exception",
        trace_id=trace_id,
        path=request.url.path,
        exc_type=type(exc).__name__,
        traceback=traceback.format_exc(),
    )
    return JSONResponse(
        status_code=500,
        content={
            "error": "internal_error",
            "message": "An unexpected error occurred. Please include trace_id when reporting.",
            "trace_id": trace_id,
        },
    )


def register_error_handlers(app) -> None:
    """Register all exception handlers on the FastAPI app."""
    app.add_exception_handler(DBUnavailableError, db_unavailable_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(Exception, generic_exception_handler)
