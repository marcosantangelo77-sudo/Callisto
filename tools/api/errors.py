"""Global FastAPI exception handlers for api.py (moved from api.py).

Slice 6 of the api.py split: the catch-all 500 converter and the clean
422 validation handler live here; api.py registers them on the app via
``@app.exception_handler(...)`` wrappers that delegate to these
functions.

Behaviour contract (pinned by tests/test_api_slice6.py):
  * HTTPException passes through untouched (status + detail preserved).
  * Any other unhandled exception becomes a structured JSON 500 with
    ``error`` / ``type`` / ``path`` and is logged at ERROR with the full
    traceback.
  * RequestValidationError becomes a compact JSON 422.
"""

from __future__ import annotations

import logging
import traceback as _traceback

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger("callisto.api")


async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch any unhandled exception and return a structured JSON error."""
    # Don't intercept FastAPI's own HTTPException — let it pass through
    if isinstance(exc, HTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.detail, "status": exc.status_code},
        )
    tb = _traceback.format_exc()
    logger.error(
        f"Unhandled exception in {request.method} {request.url.path}: {exc}\n{tb}"
    )
    return JSONResponse(
        status_code=500,
        content={
            "error": str(exc),
            "type": type(exc).__name__,
            "path": request.url.path,
        },
    )


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Return clean 422 instead of FastAPI's default verbose error."""
    return JSONResponse(
        status_code=422,
        content={"error": "Validation failed", "details": exc.errors()},
    )
