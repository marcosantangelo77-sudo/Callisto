"""tools.healthz — split modules extracted from tools/health.py.

Public API is re-exported so ``from tools.healthz import SystemHealth``
works identically to the original monolith.
"""

from .breakers import CircuitBreaker, ErrorTracker
from .checks import (
    MemoryLeakDetector,
    check_data_collector,
    check_disk,
    check_memory,
    check_network,
    check_ollama,
    check_sqlite,
    db_path,
)
from .config import (
    BREAKER_COOLDOWN,
    BREAKER_FAIL_THRESHOLD,
    CHECK_INTERVAL,
    CRITICAL_MULTIPLIER,
    DB_PATH,
    FAST_BREAKER_FAIL_THRESHOLD,
    FAST_BREAKER_MIN_INTERVAL_S,
    MAX_DB_SIZE_GB,
    MAX_ERRORS_PER_HOUR,
    MAX_MEMORY_MB,
    MEMORY_GROWTH_MB_PER_HOUR,
    MIN_DISK_GB,
    NETWORK_CACHE_TTL_S,
    NETWORK_ESCALATE_AFTER_S,
    OLLAMA_HEALTH_TIMEOUT,
    OLLAMA_HOST,
    SOURCE_SLA_DEFAULTS,
    SOURCE_SLAS,
    SUBSYSTEMS,
    SUBSYSTEM_BREAKER_CFG,
    resolve_sla_seconds,
)
from .monitor import SystemHealth

__all__ = [
    "CircuitBreaker",
    "ErrorTracker",
    "SystemHealth",
    "MemoryLeakDetector",
    "check_data_collector",
    "check_disk",
    "check_memory",
    "check_network",
    "check_ollama",
    "check_sqlite",
    "db_path",
    "BREAKER_COOLDOWN",
    "BREAKER_FAIL_THRESHOLD",
    "CHECK_INTERVAL",
    "CRITICAL_MULTIPLIER",
    "DB_PATH",
    "FAST_BREAKER_FAIL_THRESHOLD",
    "FAST_BREAKER_MIN_INTERVAL_S",
    "MAX_DB_SIZE_GB",
    "MAX_ERRORS_PER_HOUR",
    "MAX_MEMORY_MB",
    "MEMORY_GROWTH_MB_PER_HOUR",
    "MIN_DISK_GB",
    "NETWORK_CACHE_TTL_S",
    "NETWORK_ESCALATE_AFTER_S",
    "OLLAMA_HEALTH_TIMEOUT",
    "OLLAMA_HOST",
    "SOURCE_SLA_DEFAULTS",
    "SOURCE_SLAS",
    "SUBSYSTEMS",
    "SUBSYSTEM_BREAKER_CFG",
    "resolve_sla_seconds",
]
