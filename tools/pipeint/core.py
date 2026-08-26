"""Core constants and the IntegrityIssue type for pipeline integrity checks."""

import os
import logging
from datetime import datetime, timezone
from typing import Optional


DB_PATH = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")

from typing import Optional

import aiosqlite
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("callisto.pipeline_integrity")

DB_PATH = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")

# ── Thresholds ──
PAPER_TRADE_STALL_HOURS = 24       # Alert if paper_trading hypotheses but 0 trades after this
HYPOTHESIS_STALL_HOURS = 48        # Alert if hypothesis stuck in same state this long
BACKTEST_MIN_EVENTS_FOR_EDGE_CHECK = 50  # Need this many events before checking edge rate
BACKTEST_ZERO_EDGE_IS_BROKEN = True  # 0% positive edges across 50+ events = broken methodology
ODDS_SNAPSHOT_STALE_HOURS = 2      # Alert if line_monitor "running" but no new snapshots
SIGNAL_PIPELINE_MIN_HYPOTHESES = 40  # Need this many in backtesting to check signal rate
REJECTION_RATE_BROKEN_THRESHOLD = 0.95  # > 95% rejection suggests broken evaluation
PHASE_ERROR_RATE_THRESHOLD = 0.50  # > 50% error rate over last 10 runs = broken phase
METRIC_STALE_HOURS = 24            # Alert if a metric hasn't changed in this long
INTEGRITY_CHECK_INTERVAL_CYCLES = 5  # Run every N research loop cycles (coprime with injury=4, regime=7, improvement=11)

# ── Issue severity levels ──
SEVERITY_CRITICAL = "CRITICAL"  # Pipeline is broken, producing wrong output
SEVERITY_WARNING = "WARNING"    # Pipeline is degraded, may produce wrong output
SEVERITY_INFO = "INFO"          # Something is unusual but not necessarily broken


class IntegrityIssue:
    """A single detected integrity problem."""

    def __init__(self, check_name: str, severity: str, message: str,
                 details: Optional[dict] = None, auto_fix: Optional[str] = None):
        self.check_name = check_name
        self.severity = severity
        self.message = message
        self.details = details or {}
        self.auto_fix = auto_fix  # Description of auto-fix attempted, if any
        self.timestamp = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict:
        return {
            "check": self.check_name,
            "severity": self.severity,
            "message": self.message,
            "details": self.details,
            "auto_fix": self.auto_fix,
            "timestamp": self.timestamp,
        }

