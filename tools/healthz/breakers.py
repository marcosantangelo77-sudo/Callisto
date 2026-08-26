"""Circuit breaker and per-subsystem error tracking."""

import logging
import time
from collections import defaultdict

from .config import (
    BREAKER_COOLDOWN,
    BREAKER_FAIL_THRESHOLD,
    FAST_BREAKER_FAIL_THRESHOLD,
    FAST_BREAKER_MIN_INTERVAL_S,
    MAX_ERRORS_PER_HOUR,
)

logger = logging.getLogger("callisto.health")


class CircuitBreaker:
    """Per-subsystem circuit breaker.

    Supports both a "slow" path (default BREAKER_FAIL_THRESHOLD × CHECK_INTERVAL)
    and a "fast" path for infrastructure checks where rapid signal matters.
    The fast path trips when fast_fail_threshold failures occur within a short
    window, independent of the slow counter.
    """

    def __init__(
        self,
        name: str,
        fail_threshold: int = BREAKER_FAIL_THRESHOLD,
        fast: bool = False,
        fast_fail_threshold: int = FAST_BREAKER_FAIL_THRESHOLD,
        fast_min_interval_s: float = FAST_BREAKER_MIN_INTERVAL_S,
    ):
        self.name = name
        self.fail_threshold = fail_threshold
        self.fast_enabled = fast
        self.fast_fail_threshold = fast_fail_threshold
        self.fast_min_interval_s = fast_min_interval_s
        self.consecutive_failures = 0
        self.is_open = False  # open = disabled
        self.opened_at = 0.0
        self.last_error = ""
        self.total_trips = 0
        # Fast-path: track timestamps of recent consecutive failures
        self._recent_failure_times: list[float] = []

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self._recent_failure_times = []
        if self.is_open:
            self.is_open = False
            logger.info(f"Circuit breaker CLOSED for {self.name} — recovered")

    def record_intermediate(self) -> None:
        """Neither success nor failure — e.g. memory 'warning' state.

        Does NOT reset the consecutive_failures counter (so a genuinely
        degrading subsystem can still trip) but also does NOT increment it.
        """
        return

    def record_failure(self, error: str) -> bool:
        """Record failure. Returns True if breaker just tripped."""
        self.consecutive_failures += 1
        self.last_error = error
        now = time.monotonic()

        # Fast-path: keep a short rolling window of failure timestamps.
        if self.fast_enabled:
            window = self.fast_min_interval_s * self.fast_fail_threshold
            self._recent_failure_times.append(now)
            self._recent_failure_times = [
                t for t in self._recent_failure_times if now - t <= window
            ]
            if (
                not self.is_open
                and len(self._recent_failure_times) >= self.fast_fail_threshold
            ):
                self.is_open = True
                self.opened_at = now
                self.total_trips += 1
                logger.error(
                    f"Circuit breaker OPEN (fast-path) for {self.name} — "
                    f"{len(self._recent_failure_times)} failures in "
                    f"{window:.0f}s. Disabling for {BREAKER_COOLDOWN}s. "
                    f"Error: {error}"
                )
                return True

        if not self.is_open and self.consecutive_failures >= self.fail_threshold:
            self.is_open = True
            self.opened_at = now
            self.total_trips += 1
            logger.error(
                f"Circuit breaker OPEN for {self.name} — "
                f"{self.consecutive_failures} consecutive failures. "
                f"Disabling for {BREAKER_COOLDOWN}s. Error: {error}"
            )
            return True
        return False

    def should_attempt(self) -> bool:
        """Check if we should attempt this subsystem."""
        if not self.is_open:
            return True
        # Check cooldown
        elapsed = time.monotonic() - self.opened_at
        if elapsed >= BREAKER_COOLDOWN:
            logger.info(f"Circuit breaker half-open for {self.name} — attempting retry")
            return True
        return False

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "healthy": not self.is_open,
            "consecutive_failures": self.consecutive_failures,
            "is_open": self.is_open,
            "total_trips": self.total_trips,
            "last_error": self.last_error,
            "cooldown_remaining": max(
                0, BREAKER_COOLDOWN - (time.monotonic() - self.opened_at)
            ) if self.is_open else 0,
            "fast_path": self.fast_enabled,
            "fast_window_failures": len(self._recent_failure_times),
        }


class ErrorTracker:
    """Tracks error rates per subsystem per hour."""

    def __init__(self):
        self._errors: dict[str, list[float]] = defaultdict(list)
        self._total: dict[str, int] = defaultdict(int)

    def record(self, subsystem: str, error: str) -> None:
        now = time.monotonic()
        self._errors[subsystem].append(now)
        self._total[subsystem] += 1
        # Prune old entries
        cutoff = now - 3600
        self._errors[subsystem] = [
            t for t in self._errors[subsystem] if t > cutoff
        ]

    def rate_per_hour(self, subsystem: str) -> int:
        now = time.monotonic()
        cutoff = now - 3600
        return sum(1 for t in self._errors.get(subsystem, []) if t > cutoff)

    def is_rate_exceeded(self, subsystem: str) -> bool:
        return self.rate_per_hour(subsystem) >= MAX_ERRORS_PER_HOUR

    def get_summary(self) -> dict:
        return {
            sub: {
                "errors_last_hour": self.rate_per_hour(sub),
                "total_errors": self._total[sub],
            }
            for sub in self._errors
        }
