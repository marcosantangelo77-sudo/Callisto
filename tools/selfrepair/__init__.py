"""tools.selfrepair — self-repair engine, split out of tools/self_repair.py.

Modules:
    config       — shared constants and mutable scraper-disable state
    gate_policy  — GATE_WRITE_PATTERNS / GATE_WEAKENING_STRATEGIES / env opt-in
    heartbeat    — independent watchdog over the research loop + Claude
    detectors    — DetectorsMixin (issue detection)
    fixes        — FixesMixin (repair strategies)
    findings     — FindingsMixin (Claude deep-work finding handlers)
    engine       — SelfRepairEngine + get_repair_engine singleton
"""

from .config import (
    BETMGM_ALT_SUBDOMAINS,
    DB_BLOAT_ROWS,
    DB_PATH,
    EMPTY_BACKTEST_LOOKBACK,
    HEARTBEAT_INTERVAL,
    PRUNE_SAFE,
    REJECTION_RATE_THRESHOLD,
    SCRAPERS,
    SCRAPER_DISABLE_SECONDS,
    SIGNAL_DROUGHT_EVENTS,
    STALE_ODDS_MINUTES,
    LOOP_STALL_THRESHOLD,
    _disabled_scrapers,
)
from .gate_policy import (
    ALLOW_REQUEUE_ENV,
    GATE_STATUS_TRANSITIONS,
    GATE_WEAKENING_STRATEGIES,
    GATE_WRITE_PATTERNS,
)
from .heartbeat import Heartbeat
from .detectors import DetectorsMixin
from .fixes import FixesMixin
from .findings import FindingsMixin
from .engine import SelfRepairEngine, get_repair_engine

# Backwards-compatible alias: the prune-safe table was `_PRUNE_SAFE` in the
# original single-module layout.
_PRUNE_SAFE = PRUNE_SAFE

__all__ = [
    "ALLOW_REQUEUE_ENV",
    "BETMGM_ALT_SUBDOMAINS",
    "DB_BLOAT_ROWS",
    "DB_PATH",
    "EMPTY_BACKTEST_LOOKBACK",
    "GATE_STATUS_TRANSITIONS",
    "GATE_WEAKENING_STRATEGIES",
    "GATE_WRITE_PATTERNS",
    "HEARTBEAT_INTERVAL",
    "LOOP_STALL_THRESHOLD",
    "PRUNE_SAFE",
    "_PRUNE_SAFE",
    "REJECTION_RATE_THRESHOLD",
    "SCRAPERS",
    "SCRAPER_DISABLE_SECONDS",
    "SIGNAL_DROUGHT_EVENTS",
    "STALE_ODDS_MINUTES",
    "_disabled_scrapers",
    "DetectorsMixin",
    "FindingsMixin",
    "FixesMixin",
    "Heartbeat",
    "SelfRepairEngine",
    "get_repair_engine",
]
