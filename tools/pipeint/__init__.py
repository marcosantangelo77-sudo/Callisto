"""Pipeline integrity checks split into focused modules.

- tools.pipeint.core: constants, thresholds, IntegrityIssue
- tools.pipeint.checks_data_flow: data-flow checks (paper trades, hypotheses,
  backtests, odds snapshots, signal pipeline)
- tools.pipeint.checks_quality: output-sanity checks (temporal isolation,
  calibration, zero/stale metrics, rejection rate)
- tools.pipeint.checker: PipelineIntegrityChecker orchestrator + singleton
"""

from tools.pipeint.checker import (
    PipelineIntegrityChecker,
    get_checker,
    initialize,
)
from tools.pipeint.core import (
    BACKTEST_MIN_EVENTS_FOR_EDGE_CHECK,
    BACKTEST_ZERO_EDGE_IS_BROKEN,
    DB_PATH,
    HYPOTHESIS_STALL_HOURS,
    INTEGRITY_CHECK_INTERVAL_CYCLES,
    METRIC_STALE_HOURS,
    ODDS_SNAPSHOT_STALE_HOURS,
    PAPER_TRADE_STALL_HOURS,
    PHASE_ERROR_RATE_THRESHOLD,
    REJECTION_RATE_BROKEN_THRESHOLD,
    SEVERITY_CRITICAL,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    SIGNAL_PIPELINE_MIN_HYPOTHESES,
    IntegrityIssue,
)

__all__ = [
    "BACKTEST_MIN_EVENTS_FOR_EDGE_CHECK",
    "BACKTEST_ZERO_EDGE_IS_BROKEN",
    "DB_PATH",
    "HYPOTHESIS_STALL_HOURS",
    "INTEGRITY_CHECK_INTERVAL_CYCLES",
    "METRIC_STALE_HOURS",
    "ODDS_SNAPSHOT_STALE_HOURS",
    "PAPER_TRADE_STALL_HOURS",
    "PHASE_ERROR_RATE_THRESHOLD",
    "REJECTION_RATE_BROKEN_THRESHOLD",
    "SEVERITY_CRITICAL",
    "SEVERITY_INFO",
    "SEVERITY_WARNING",
    "SIGNAL_PIPELINE_MIN_HYPOTHESES",
    "IntegrityIssue",
    "PipelineIntegrityChecker",
    "get_checker",
    "initialize",
]
