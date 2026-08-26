"""Self-repair engine — detect, fix, verify, record. Phase 0 of the research loop.

Facade: the implementation now lives in tools/selfrepair/ (config, gate_policy,
heartbeat, detectors, fixes, findings, engine). This module re-exports the full
public surface so every existing importer keeps working unchanged.
"""

from dotenv import load_dotenv

load_dotenv()

from tools.selfrepair import (  # noqa: E402,F401
    ALLOW_REQUEUE_ENV,
    BETMGM_ALT_SUBDOMAINS,
    DB_BLOAT_ROWS,
    DB_PATH,
    DetectorsMixin,
    EMPTY_BACKTEST_LOOKBACK,
    FindingsMixin,
    FixesMixin,
    GATE_STATUS_TRANSITIONS,
    GATE_WEAKENING_STRATEGIES,
    GATE_WRITE_PATTERNS,
    HEARTBEAT_INTERVAL,
    Heartbeat,
    LOOP_STALL_THRESHOLD,
    PRUNE_SAFE,
    REJECTION_RATE_THRESHOLD,
    SCRAPERS,
    SCRAPER_DISABLE_SECONDS,
    SelfRepairEngine,
    SIGNAL_DROUGHT_EVENTS,
    STALE_ODDS_MINUTES,
    _PRUNE_SAFE,
    _disabled_scrapers,
    get_repair_engine,
)

__all__ = [
    "ALLOW_REQUEUE_ENV",
    "BETMGM_ALT_SUBDOMAINS",
    "DB_BLOAT_ROWS",
    "DB_PATH",
    "DetectorsMixin",
    "EMPTY_BACKTEST_LOOKBACK",
    "FindingsMixin",
    "FixesMixin",
    "GATE_STATUS_TRANSITIONS",
    "GATE_WEAKENING_STRATEGIES",
    "GATE_WRITE_PATTERNS",
    "HEARTBEAT_INTERVAL",
    "Heartbeat",
    "LOOP_STALL_THRESHOLD",
    "PRUNE_SAFE",
    "REJECTION_RATE_THRESHOLD",
    "SCRAPERS",
    "SCRAPER_DISABLE_SECONDS",
    "SelfRepairEngine",
    "SIGNAL_DROUGHT_EVENTS",
    "STALE_ODDS_MINUTES",
    "_PRUNE_SAFE",
    "_disabled_scrapers",
    "get_repair_engine",
]
