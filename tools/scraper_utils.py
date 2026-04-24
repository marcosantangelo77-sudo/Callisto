"""
Shared utilities for sportsbook / odds scrapers.

Provides a common surface for:

    - Exponential, jittered retry on 5xx / transient errors
    - 429 Retry-After honouring
    - Non-retry on 4xx (logged once)
    - Explicit timeouts on every request
    - Rotating User-Agent pool
    - Per-scraper health / last-success tracking
    - A ``SCRAPER_REGISTRY`` that the API layer reads for
      ``/odds/scrapers/health``

The helpers here are intentionally minimal -- each scraper keeps its
existing rate-limit clock, endpoint logic, and parsing. Callers opt in by
wrapping their low-level HTTP invocation in ``retry_async`` /
``retry_sync`` and pinging the health tracker on success / failure.
"""

from __future__ import annotations

import asyncio
import logging
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

import httpx

logger = logging.getLogger("callisto.scraper_utils")


DEFAULT_TIMEOUT_S = 15.0
DEFAULT_MAX_ATTEMPTS = 4
DEFAULT_BASE_DELAY_S = 0.5
DEFAULT_MAX_DELAY_S = 8.0
DEFAULT_HEALTH_STALE_S = 300


_USER_AGENTS: tuple[str, ...] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_2) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
)


def pick_user_agent() -> str:
    """Return a random browser-looking User-Agent."""
    return random.choice(_USER_AGENTS)


class RetryableStatusError(Exception):
    """Raised internally by retry helpers when a 5xx / 429 should retry."""

    def __init__(self, status_code: int, message: str = "", retry_after: Optional[float] = None):
        super().__init__(message or f"HTTP {status_code}")
        self.status_code = status_code
        self.retry_after = retry_after


class FatalStatusError(Exception):
    """4xx (other than 429) -- log once, don't retry."""

    def __init__(self, status_code: int, message: str = ""):
        super().__init__(message or f"HTTP {status_code}")
        self.status_code = status_code


def classify_status(status_code: int, retry_after_header: Optional[str] = None) -> None:
    """
    Translate an HTTP status code into an internal exception.

    - 2xx / 3xx: returns None (caller proceeds)
    - 429: RetryableStatusError with retry_after from header if present
    - 5xx: RetryableStatusError
    - other 4xx: FatalStatusError
    """
    if status_code < 400:
        return None
    if status_code == 429:
        delay = _parse_retry_after(retry_after_header)
        raise RetryableStatusError(status_code, retry_after=delay)
    if 500 <= status_code < 600:
        raise RetryableStatusError(status_code)
    raise FatalStatusError(status_code)


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    """Parse a Retry-After header. Supports delta-seconds and HTTP-dates."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        from datetime import datetime, timezone

        dt = parsedate_to_datetime(value)
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
    except Exception:
        return None


def compute_backoff(attempt: int, base: float = DEFAULT_BASE_DELAY_S, cap: float = DEFAULT_MAX_DELAY_S) -> float:
    """Jittered exponential backoff. attempt is 1-based."""
    expo = min(cap, base * (2 ** max(0, attempt - 1)))
    return expo * (0.5 + random.random() * 0.5)


_TRANSIENT_EXC: tuple[type[BaseException], ...] = (
    httpx.TimeoutException,
    httpx.TransportError,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
    ConnectionError,
    TimeoutError,
    asyncio.TimeoutError,
)


async def retry_async(
    op: Callable[[], Awaitable[Any]],
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    base_delay: float = DEFAULT_BASE_DELAY_S,
    max_delay: float = DEFAULT_MAX_DELAY_S,
    scraper: Optional[str] = None,
) -> Any:
    """
    Execute ``op`` with exponential+jittered backoff retries.

    Retries on:
      - RetryableStatusError (5xx / 429 with Retry-After honoured)
      - httpx timeout / transport / network errors
      - asyncio.TimeoutError / TimeoutError / ConnectionError

    Does NOT retry on:
      - FatalStatusError (4xx other than 429) -- logged once
      - Any other exception -- re-raised

    On exhaustion the last exception is re-raised.
    """
    last_exc: Optional[BaseException] = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await op()
        except FatalStatusError as e:
            logger.warning(
                "scraper %s: HTTP %s (fatal, no retry)",
                scraper or "?", e.status_code,
            )
            raise
        except RetryableStatusError as e:
            last_exc = e
            if attempt >= max_attempts:
                break
            if e.retry_after is not None:
                delay = min(max(e.retry_after, base_delay), max_delay)
            else:
                delay = compute_backoff(attempt, base=base_delay, cap=max_delay)
            logger.info(
                "scraper %s: HTTP %s, retry %d/%d after %.2fs",
                scraper or "?", e.status_code, attempt, max_attempts, delay,
            )
            await asyncio.sleep(delay)
        except _TRANSIENT_EXC as e:
            last_exc = e
            if attempt >= max_attempts:
                break
            delay = compute_backoff(attempt, base=base_delay, cap=max_delay)
            logger.info(
                "scraper %s: transient %s, retry %d/%d after %.2fs",
                scraper or "?", type(e).__name__, attempt, max_attempts, delay,
            )
            await asyncio.sleep(delay)

    assert last_exc is not None
    raise last_exc


def retry_sync(
    op: Callable[[], Any],
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    base_delay: float = DEFAULT_BASE_DELAY_S,
    max_delay: float = DEFAULT_MAX_DELAY_S,
    scraper: Optional[str] = None,
) -> Any:
    """
    Sync sibling of ``retry_async``. Used for the curl_cffi blocking calls
    that we run inside ``asyncio.to_thread``.
    """
    last_exc: Optional[BaseException] = None
    for attempt in range(1, max_attempts + 1):
        try:
            return op()
        except FatalStatusError as e:
            logger.warning(
                "scraper %s: HTTP %s (fatal, no retry)",
                scraper or "?", e.status_code,
            )
            raise
        except RetryableStatusError as e:
            last_exc = e
            if attempt >= max_attempts:
                break
            if e.retry_after is not None:
                delay = min(max(e.retry_after, base_delay), max_delay)
            else:
                delay = compute_backoff(attempt, base=base_delay, cap=max_delay)
            time.sleep(delay)
        except _TRANSIENT_EXC as e:
            last_exc = e
            if attempt >= max_attempts:
                break
            time.sleep(compute_backoff(attempt, base=base_delay, cap=max_delay))

    assert last_exc is not None
    raise last_exc


@dataclass
class ScraperHealth:
    """Per-scraper liveness tracker. Updated on every successful pull."""

    name: str
    last_success_ts: float = 0.0
    last_attempt_ts: float = 0.0
    last_error: str = ""
    consecutive_errors: int = 0
    success_count: int = 0
    error_count: int = 0
    stale_after_s: int = DEFAULT_HEALTH_STALE_S
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def mark_success(self) -> None:
        with self._lock:
            now = time.time()
            self.last_success_ts = now
            self.last_attempt_ts = now
            self.last_error = ""
            self.consecutive_errors = 0
            self.success_count += 1

    def mark_error(self, err: str) -> None:
        with self._lock:
            self.last_attempt_ts = time.time()
            self.last_error = (err or "")[:256]
            self.consecutive_errors += 1
            self.error_count += 1

    def snapshot(self) -> dict:
        with self._lock:
            now = time.time()
            staleness = int(now - self.last_success_ts) if self.last_success_ts > 0 else None
            healthy = (
                self.last_success_ts > 0
                and staleness is not None
                and staleness < self.stale_after_s
            )
            return {
                "name": self.name,
                "healthy": bool(healthy),
                "staleness_s": staleness,
                "stale_after_s": self.stale_after_s,
                "last_successful_pull": self.last_success_ts or None,
                "last_attempt_ts": self.last_attempt_ts or None,
                "last_error": self.last_error or None,
                "consecutive_errors": self.consecutive_errors,
                "success_count": self.success_count,
                "error_count": self.error_count,
            }


SCRAPER_REGISTRY: dict[str, ScraperHealth] = {}
_REG_LOCK = threading.Lock()


def register_scraper(name: str, stale_after_s: int = DEFAULT_HEALTH_STALE_S) -> ScraperHealth:
    """Register or fetch the health tracker for ``name``.

    Safe to call from module import time -- idempotent.
    """
    with _REG_LOCK:
        h = SCRAPER_REGISTRY.get(name)
        if h is None:
            h = ScraperHealth(name=name, stale_after_s=stale_after_s)
            SCRAPER_REGISTRY[name] = h
        return h


def mark_success(name: str) -> None:
    register_scraper(name).mark_success()


def mark_error(name: str, err: str) -> None:
    register_scraper(name).mark_error(err)


def health(name: str) -> dict:
    """Return the health snapshot for a single scraper."""
    h = register_scraper(name)
    return h.snapshot()


def all_health() -> dict:
    """Return ``{healthy, scrapers: [...], stale: N, down: N}`` for all registered scrapers."""
    with _REG_LOCK:
        snaps = [h.snapshot() for h in SCRAPER_REGISTRY.values()]
    stale = sum(1 for s in snaps if not s["healthy"] and s["last_successful_pull"])
    never = sum(1 for s in snaps if not s["last_successful_pull"])
    overall = bool(snaps) and all(s["healthy"] for s in snaps)
    return {
        "healthy": overall,
        "total": len(snaps),
        "stale_count": stale,
        "never_pulled_count": never,
        "scrapers": sorted(snaps, key=lambda s: s["name"]),
    }


__all__ = [
    "DEFAULT_TIMEOUT_S",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_BASE_DELAY_S",
    "DEFAULT_MAX_DELAY_S",
    "DEFAULT_HEALTH_STALE_S",
    "RetryableStatusError",
    "FatalStatusError",
    "classify_status",
    "compute_backoff",
    "pick_user_agent",
    "retry_async",
    "retry_sync",
    "ScraperHealth",
    "SCRAPER_REGISTRY",
    "register_scraper",
    "mark_success",
    "mark_error",
    "health",
    "all_health",
]
