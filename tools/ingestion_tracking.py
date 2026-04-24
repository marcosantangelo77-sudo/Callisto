"""
Ingestion observability — track every data-collector call with a per-run row.

WHY THIS EXISTS
---------------
Callisto's ESPN / NHL / nflverse / odds-api collectors historically caught every
exception and returned empty lists. That meant a broken scraper could fail for
hours (or days) with zero visibility — the downstream pipeline just saw "no
new data" and kept running against a stale cache. Silent data drops are the
hardest class of bugs to diagnose because nothing crashes; the system just
quietly stops learning.

This module adds one primitive (`@tracked_ingestion`) that wraps any async
ingestion function and records a row in `ingestion_runs` capturing:
  - what source was attempted
  - whether it succeeded / failed / was rate-limited
  - how long it took
  - how many rows it produced
  - the exception class + message, if any

The downstream `_check_data_collector` health probe reads from this table to
detect stale sources and trip the circuit breaker when an SLA is breached.

DESIGN CONSTRAINTS
------------------
1. NEVER let tracking failures break the wrapped function. The tracker is
   strictly observational — if the write fails (DB lock, disk full, schema
   drift), we log and continue. Silent data drops are worse than silent
   tracker drops; the wrapped function's result is what the caller depends on.
2. Source tags are stable identifiers. Changing them loses history for SLA
   evaluation, so they're documented and versioned in a comment block.
3. Rate-limit detection is heuristic: we inspect HTTPStatusError status codes
   AND inspect the wrapped function's return value for dict payloads
   containing "rate limit" / "429" strings (many ingestion functions catch
   the exception themselves and return a sentinel dict).
4. Row count extraction checks common keys: `rows`, `games`, `players`,
   `events`, `snapshots`, `count`, `completed`. First hit wins.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

import aiosqlite

logger = logging.getLogger("callisto.ingestion_tracking")

DB_PATH = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")

# Keys the decorator inspects on the wrapped function's return value to
# derive `rows_ingested`. Order matters — first-match wins.
_ROWS_KEYS = ("rows", "games", "players", "events", "snapshots", "count", "completed")

# Substrings (lowercased) that identify rate-limit sentinels in return values.
_RATE_LIMIT_HINTS = ("rate limit", "rate-limit", "rate_limit", "429", "too many requests")


def _classify_http_status(status: int) -> str:
    """Bucket an HTTP status code into an ingestion status.

    429 is its own bucket because rate-limited runs are usually transient and
    shouldn't count against the breaker the same way as a 500 loop would.
    5xx ⇒ failed (server side, retryable). 4xx ⇒ failed (client / permanent).
    """
    if status == 429:
        return "rate_limited"
    if 500 <= status < 600:
        return "failed"
    if 400 <= status < 500:
        return "failed"
    return "failed"


def _extract_rows(result: Any) -> int:
    """Pull a row-count out of common return-value shapes."""
    if result is None:
        return 0
    if isinstance(result, (list, tuple)):
        return len(result)
    if isinstance(result, dict):
        for k in _ROWS_KEYS:
            v = result.get(k)
            if isinstance(v, int):
                return v
            if isinstance(v, list):
                return len(v)
    return 0


def _looks_rate_limited(result: Any) -> bool:
    """Heuristic: does the return value say 'rate limited'?"""
    if not isinstance(result, dict):
        return False
    err = result.get("error")
    if not isinstance(err, str):
        return False
    low = err.lower()
    return any(h in low for h in _RATE_LIMIT_HINTS)


def _is_error_payload(result: Any) -> bool:
    """Detect `{"error": "..."}` sentinels — many ingestion funcs return these
    instead of raising, so we translate them into status='failed'."""
    if not isinstance(result, dict):
        return False
    err = result.get("error")
    return isinstance(err, str) and bool(err)


async def _write_run_start(source: str, started_at: str) -> Optional[int]:
    """Insert a `running` row and return its id.

    Returns None on any failure. The wrapped function MUST continue either
    way — see design constraint #1.
    """
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("PRAGMA busy_timeout = 5000")
            cursor = await db.execute(
                "INSERT INTO ingestion_runs (source, started_at, status) "
                "VALUES (?, ?, 'running')",
                (source, started_at),
            )
            await db.commit()
            return cursor.lastrowid
    except Exception as e:
        # Don't spam: most common case is schema hasn't been migrated yet
        # on an older process. Log once per process per source via the
        # module logger (callers can filter by name).
        logger.debug(f"ingestion_runs insert skipped for {source}: {e}")
        return None


def _emit_metrics(
    source: str, status: str, duration_ms: int, reason: Optional[str] = None,
) -> None:
    """Mirror terminal-status tags into the in-process metrics registry.

    Isolated in its own helper so the wrapped ingestion function never sees
    a metrics-registry crash — the outer ``except Exception`` here is the
    same belt-and-braces guarantee as the DB-write paths above.
    """
    try:
        from tools.metrics import (
            observe_scraper_latency,
            record_ingestion_result,
        )
        observe_scraper_latency(source, duration_ms / 1000.0)
        record_ingestion_result(source, status, reason)
    except Exception:
        logger.debug("metrics emit skipped", exc_info=True)


async def _write_run_finish(
    run_id: Optional[int],
    source: str,
    started_at: str,
    status: str,
    rows: int,
    duration_ms: int,
    error_class: Optional[str],
    error_message: Optional[str],
    extra: Optional[dict] = None,
) -> None:
    """Update the run row (or insert a fresh terminal row if the start-insert
    failed)."""
    finished_at = datetime.now(timezone.utc).isoformat()
    extra_json = json.dumps(extra) if extra else None
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("PRAGMA busy_timeout = 5000")
            if run_id is not None:
                await db.execute(
                    "UPDATE ingestion_runs SET finished_at = ?, status = ?, "
                    "rows_ingested = ?, duration_ms = ?, error_class = ?, "
                    "error_message = ?, extra_json = ? WHERE id = ?",
                    (finished_at, status, rows, duration_ms, error_class,
                     error_message, extra_json, run_id),
                )
            else:
                # Start-insert failed — still write a terminal row so the
                # health check has evidence the call happened.
                await db.execute(
                    "INSERT INTO ingestion_runs "
                    "(source, started_at, finished_at, status, rows_ingested, "
                    "duration_ms, error_class, error_message, extra_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (source, started_at, finished_at, status, rows,
                     duration_ms, error_class, error_message, extra_json),
                )
            await db.commit()
    except Exception as e:
        logger.debug(f"ingestion_runs update skipped for {source}: {e}")


def tracked_ingestion(
    source: str | Callable[..., str],
    *,
    sla_seconds: Optional[int] = None,
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """Decorator factory — wraps an async ingestion function in tracking.

    Usage:
        @tracked_ingestion(source="espn.scoreboard.mlb")
        async def collect_scores(self, sport, date=None) -> dict:
            ...

        # Dynamic source tag (derived from call args):
        @tracked_ingestion(
            source=lambda self, sport, **_: f"espn.scoreboard.{sport}",
        )
        async def collect_scores(self, sport, date=None): ...

    The decorator preserves the wrapped function's signature (via functools.wraps)
    and never raises on its own. If the wrapped function raises, the exception
    is re-raised AFTER the `failed` row is written.

    Args:
        source: Hierarchical identifier string OR a callable that receives the
                same args/kwargs as the wrapped function and returns the tag.
                STABLE — changing produced tags loses history.
        sla_seconds: Optional hint for the health check; not enforced here.
    """
    # Import lazily so this module doesn't pull httpx at import time.
    import httpx

    def _resolve_source(args: tuple, kwargs: dict) -> str:
        if callable(source):
            try:
                return source(*args, **kwargs)
            except Exception:
                return "unknown.dynamic_source_error"
        return source

    def decorator(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            resolved_source = _resolve_source(args, kwargs)
            started_at = datetime.now(timezone.utc).isoformat()
            t0 = time.monotonic()
            run_id = await _write_run_start(resolved_source, started_at)

            rows = 0
            status = "ok"
            error_class: Optional[str] = None
            error_message: Optional[str] = None
            extra: Optional[dict] = None

            try:
                result = await fn(*args, **kwargs)
            except httpx.HTTPStatusError as e:  # type: ignore[attr-defined]
                status = _classify_http_status(e.response.status_code)
                error_class = type(e).__name__
                error_message = f"HTTP {e.response.status_code}: {str(e)[:200]}"
                duration_ms = int((time.monotonic() - t0) * 1000)
                await _write_run_finish(
                    run_id, resolved_source, started_at, status, 0, duration_ms,
                    error_class, error_message,
                )
                _emit_metrics(resolved_source, status, duration_ms, error_class)
                raise
            except asyncio.CancelledError:
                # Cooperative cancel — record as such, then propagate.
                duration_ms = int((time.monotonic() - t0) * 1000)
                await _write_run_finish(
                    run_id, resolved_source, started_at, "failed", 0, duration_ms,
                    "CancelledError", "cancelled",
                )
                _emit_metrics(resolved_source, "failed", duration_ms, "CancelledError")
                raise
            except Exception as e:
                status = "failed"
                error_class = type(e).__name__
                error_message = str(e)[:500]
                duration_ms = int((time.monotonic() - t0) * 1000)
                await _write_run_finish(
                    run_id, resolved_source, started_at, status, 0, duration_ms,
                    error_class, error_message,
                )
                _emit_metrics(resolved_source, status, duration_ms, error_class)
                raise

            # Function returned normally. Inspect the payload to distinguish
            # clean success vs caller-swallowed error.
            rows = _extract_rows(result)
            if _looks_rate_limited(result):
                status = "rate_limited"
                if isinstance(result, dict):
                    error_message = str(result.get("error"))[:500]
            elif _is_error_payload(result):
                status = "failed"
                if isinstance(result, dict):
                    error_message = str(result.get("error"))[:500]
            elif rows == 0:
                # Empty-result runs are common (no games on date) and are not
                # failures. Tag them partial so the health check can
                # differentiate "ingested 0 rows" from "never ran".
                status = "partial"

            duration_ms = int((time.monotonic() - t0) * 1000)
            await _write_run_finish(
                run_id, resolved_source, started_at, status, rows, duration_ms,
                error_class, error_message, extra,
            )
            _emit_metrics(
                resolved_source, status, duration_ms, error_class or error_message,
            )
            return result

        # Attach the source tag so introspection tooling can enumerate
        # wrapped functions without importing the registry. For callable
        # sources we expose the callable itself.
        wrapper._ingestion_source = source  # type: ignore[attr-defined]
        wrapper._ingestion_sla_seconds = sla_seconds  # type: ignore[attr-defined]
        return wrapper

    return decorator


__all__ = ["tracked_ingestion"]
