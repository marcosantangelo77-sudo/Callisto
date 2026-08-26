"""
System health monitor — Layer 2 resilience for autonomous operation.

Runs inside the main api.py process. Checks every subsystem periodically
and takes corrective action without human intervention.

Subsystems monitored:
  - Ollama (models responding, not stuck)
  - SQLite (writable, not corrupt, reasonable size)
  - Disk space (enough room for DB growth)
  - Memory (process RSS, leak detection)
  - Network (ESPN, Odds API, Claude CLI reachable)
  - Research loop (phases completing, not stalled)
  - Error rates (per-subsystem, circuit breakers)

Circuit breakers:
  When a subsystem fails N times consecutively, it is DISABLED.
  Other subsystems continue running. The breaker resets after a
  cooldown period. This prevents cascade failures.

Self-repair:
  When a persistent error is detected and local models can't fix it,
  escalate to Claude Code with structured diagnostics.

IMPLEMENTATION NOTE
-------------------
This module is a stable facade. The implementation lives in the
``tools.healthz`` package (named to avoid shadowing this module):

  tools/healthz/config.py    — constants + data-collector SLA tables
  tools/healthz/breakers.py  — CircuitBreaker / ErrorTracker
  tools/healthz/checks.py    — individual subsystem checks
  tools/healthz/monitor.py   — SystemHealth orchestrator

Every public name historically importable from ``tools.health`` is
re-exported here unchanged.
"""

from tools.healthz import (  # noqa: F401
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
    CircuitBreaker,
    ErrorTracker,
    MemoryLeakDetector,
    SystemHealth,
    check_data_collector,
    check_disk,
    check_memory,
    check_network,
    check_ollama,
    check_sqlite,
    db_path,
    resolve_sla_seconds,
)

__all__ = [
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
    "CircuitBreaker",
    "ErrorTracker",
    "MemoryLeakDetector",
    "SystemHealth",
    "check_data_collector",
    "check_disk",
    "check_memory",
    "check_network",
    "check_ollama",
    "check_sqlite",
    "db_path",
    "resolve_sla_seconds",
]
