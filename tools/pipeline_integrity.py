"""
Pipeline integrity checker — detects silent failures, stalled pipelines,
and broken data flows that the standard health check misses.

The standard /health endpoint only checks if processes are running.
This module checks if they are PRODUCING VALID OUTPUT.

Three bugs that motivated this module:
  1. Paper trading had a wrong import — silently swallowed by bare
     `except Exception`, 194 hypotheses stuck with 0 trades for days
  2. Backtest engine found 0 edges across 734 events because it compared
     consensus against itself — nobody checked that 0% positive edge rate
     is abnormal
  3. Composite TCI was flat at 51.9% but kept being used as a signal

Design: Run as part of the autonomous loop. Log all results to a
dedicated table for trend analysis. Surface issues in /health and
/system/full-status.
"""

import asyncio
import json
import logging
import os
import time
import traceback
from collections import defaultdict
from datetime import datetime, timedelta, timezone
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


class PipelineIntegrityChecker:
    """
    Comprehensive pipeline integrity checker.

    Checks that pipelines are not just running but producing valid,
    changing, non-zero output. Detects silent failures that standard
    health checks miss.
    """

    def __init__(self):
        self._issues: list[IntegrityIssue] = []
        self._last_run: Optional[str] = None
        self._run_count = 0
        self._phase_errors: dict[str, list[bool]] = defaultdict(list)  # phase -> [success/fail]
        self._metric_history: dict[str, list[tuple[str, float]]] = defaultdict(list)  # metric -> [(timestamp, value)]
        self._known_issues: set[str] = set()  # Dedup for alerts

    async def ensure_table(self) -> None:
        """Create the integrity_checks log table if it doesn't exist."""
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("PRAGMA busy_timeout = 60000")
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS integrity_checks (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id INTEGER NOT NULL,
                        check_name TEXT NOT NULL,
                        severity TEXT NOT NULL,
                        message TEXT NOT NULL,
                        details_json TEXT,
                        auto_fix TEXT,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                await db.execute("""
                    CREATE INDEX IF NOT EXISTS idx_integrity_checks_run
                    ON integrity_checks(run_id, severity)
                """)
                await db.execute("""
                    CREATE INDEX IF NOT EXISTS idx_integrity_checks_name
                    ON integrity_checks(check_name, created_at)
                """)
                await db.commit()
        except Exception as e:
            logger.error(f"Failed to create integrity_checks table: {e}", exc_info=True)

    async def run_all_checks(self) -> dict:
        """
        Run the full integrity check suite.

        Returns a summary dict suitable for inclusion in /health and
        /system/full-status responses.
        """
        self._issues = []
        self._run_count += 1
        start_time = time.monotonic()

        checks = [
            ("paper_trade_flow", self._check_paper_trade_flow),
            ("hypothesis_progression", self._check_hypothesis_progression),
            ("backtest_edge_rate", self._check_backtest_edge_rate),
            ("odds_snapshot_freshness", self._check_odds_snapshot_freshness),
            ("signal_pipeline", self._check_signal_pipeline),
            ("zero_metric_detection", self._check_zero_metrics),
            ("stale_metric_detection", self._check_stale_metrics),
            ("rejection_rate", self._check_rejection_rate),
            ("temporal_isolation", self._check_temporal_isolation),
            ("calibration_health", self._check_calibration_health),
        ]

        for check_name, check_fn in checks:
            try:
                await check_fn()
            except Exception as e:
                logger.error(
                    f"Integrity check '{check_name}' itself failed: {e}",
                    exc_info=True,
                )
                self._issues.append(IntegrityIssue(
                    check_name=check_name,
                    severity=SEVERITY_WARNING,
                    message=f"Check failed to execute: {e}",
                    details={"traceback": traceback.format_exc()},
                ))

        elapsed = time.monotonic() - start_time
        self._last_run = datetime.now(timezone.utc).isoformat()

        # Log all issues to the database
        await self._log_issues()

        # Build summary
        critical_count = sum(1 for i in self._issues if i.severity == SEVERITY_CRITICAL)
        warning_count = sum(1 for i in self._issues if i.severity == SEVERITY_WARNING)
        info_count = sum(1 for i in self._issues if i.severity == SEVERITY_INFO)

        summary = {
            "healthy": critical_count == 0,
            "degraded": warning_count > 0,
            "run_number": self._run_count,
            "last_run": self._last_run,
            "elapsed_seconds": round(elapsed, 2),
            "issues": {
                "critical": critical_count,
                "warning": warning_count,
                "info": info_count,
                "total": len(self._issues),
            },
            "issue_details": [i.to_dict() for i in self._issues],
        }

        if critical_count > 0:
            logger.error(
                f"PIPELINE INTEGRITY: {critical_count} CRITICAL issues detected! "
                f"System is reporting healthy but pipelines are broken."
            )
        elif warning_count > 0:
            logger.warning(
                f"PIPELINE INTEGRITY: {warning_count} warnings detected."
            )
        else:
            logger.info(
                f"PIPELINE INTEGRITY: all checks passed "
                f"({len(checks)} checks in {elapsed:.1f}s)"
            )

        return summary

    # ──────────────────────────────────────────────────────────
    # DATA FLOW CHECKS
    # ──────────────────────────────────────────────────────────

    async def _check_paper_trade_flow(self) -> None:
        """
        If hypotheses are in paper_trading state, paper_trades count should
        be growing. Alert if paper_trading hypotheses exist but 0 trades
        after 24 hours.
        """
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("PRAGMA busy_timeout = 60000")
                # Count paper_trading hypotheses
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM hypotheses WHERE status = 'paper_trading'"
                )
                paper_trading_count = (await cursor.fetchone())[0]

                if paper_trading_count == 0:
                    return  # No paper trading hypotheses, nothing to check

                # Count total paper trades
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM paper_trades"
                )
                total_trades = (await cursor.fetchone())[0]

                # Check for recent paper trades (last 24 hours)
                cutoff = (datetime.now(timezone.utc) - timedelta(hours=PAPER_TRADE_STALL_HOURS)).isoformat()
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM paper_trades WHERE created_at > ?",
                    (cutoff,)
                )
                recent_trades = (await cursor.fetchone())[0]

                # Get oldest paper_trading hypothesis
                cursor = await db.execute(
                    "SELECT hypothesis_id, name, updated_at FROM hypotheses "
                    "WHERE status = 'paper_trading' "
                    "ORDER BY updated_at ASC LIMIT 1"
                )
                oldest = await cursor.fetchone()
                oldest_age_hours = 0
                if oldest and oldest[2]:
                    try:
                        updated = datetime.fromisoformat(str(oldest[2]).replace("Z", "+00:00"))
                        if updated.tzinfo is None:
                            updated = updated.replace(tzinfo=timezone.utc)
                        oldest_age_hours = (datetime.now(timezone.utc) - updated).total_seconds() / 3600
                    except (ValueError, TypeError) as e:
                        logger.debug(
                            f"integrity: paper_signal updated timestamp parse "
                            f"failed for {oldest[2]!r}: {e}"
                        )

                if total_trades == 0 and oldest_age_hours > PAPER_TRADE_STALL_HOURS:
                    self._issues.append(IntegrityIssue(
                        check_name="paper_trade_flow",
                        severity=SEVERITY_CRITICAL,
                        message=(
                            f"{paper_trading_count} hypotheses in paper_trading state "
                            f"but 0 paper trades exist. Oldest paper_trading hypothesis "
                            f"is {oldest_age_hours:.1f}h old. The paper trading pipeline "
                            f"is silently failing."
                        ),
                        details={
                            "paper_trading_hypotheses": paper_trading_count,
                            "total_paper_trades": total_trades,
                            "oldest_hypothesis_id": oldest[0] if oldest else None,
                            "oldest_hypothesis_age_hours": round(oldest_age_hours, 1),
                        },
                    ))
                elif recent_trades == 0 and paper_trading_count > 0 and oldest_age_hours > PAPER_TRADE_STALL_HOURS:
                    self._issues.append(IntegrityIssue(
                        check_name="paper_trade_flow",
                        severity=SEVERITY_WARNING,
                        message=(
                            f"{paper_trading_count} hypotheses in paper_trading but "
                            f"0 new trades in last {PAPER_TRADE_STALL_HOURS}h "
                            f"({total_trades} total trades exist). Pipeline may be stalled."
                        ),
                        details={
                            "paper_trading_hypotheses": paper_trading_count,
                            "total_paper_trades": total_trades,
                            "recent_trades_24h": recent_trades,
                        },
                    ))

        except Exception as e:
            logger.warning(f"Paper trade flow check failed: {e}", exc_info=True)

    async def _check_hypothesis_progression(self) -> None:
        """
        Flag hypotheses stuck in the same state for too long with no
        evaluation activity.
        """
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("PRAGMA busy_timeout = 60000")
                cutoff = (datetime.now(timezone.utc) - timedelta(hours=HYPOTHESIS_STALL_HOURS)).isoformat()

                # Find hypotheses that haven't been updated in HYPOTHESIS_STALL_HOURS
                cursor = await db.execute(
                    "SELECT hypothesis_id, name, status, updated_at FROM hypotheses "
                    "WHERE status IN ('draft', 'backtesting', 'paper_trading') "
                    "AND updated_at < ? ",
                    (cutoff,)
                )
                stalled = await cursor.fetchall()

                if not stalled:
                    return

                stalled_by_status: dict[str, int] = defaultdict(int)
                for row in stalled:
                    stalled_by_status[row[2]] += 1

                # Only alert if a significant number are stalled
                total_stalled = len(stalled)
                if total_stalled >= 5:
                    self._issues.append(IntegrityIssue(
                        check_name="hypothesis_progression",
                        severity=SEVERITY_WARNING,
                        message=(
                            f"{total_stalled} hypotheses stuck in same state for "
                            f">{HYPOTHESIS_STALL_HOURS}h with no evaluation: "
                            f"{dict(stalled_by_status)}"
                        ),
                        details={
                            "stalled_count": total_stalled,
                            "by_status": dict(stalled_by_status),
                            "sample_ids": [row[0] for row in stalled[:5]],
                        },
                    ))

        except Exception as e:
            logger.warning(f"Hypothesis progression check failed: {e}", exc_info=True)

    async def _check_backtest_edge_rate(self) -> None:
        """
        If backtests run but find 0% positive edges across many events,
        the meaning depends on temporal isolation:

        - WITH temporal isolation: 0% edge rate is a legitimate finding
          (hypothesis has no forward edge) — INFO severity
        - WITHOUT temporal isolation: 0% edge rate likely means the
          methodology is comparing consensus against itself — CRITICAL

        This distinction prevents false alarms on properly isolated
        hypotheses while still catching circular testing bugs.
        """
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("PRAGMA busy_timeout = 60000")
                # Get aggregate backtest stats
                cursor = await db.execute(
                    "SELECT COUNT(*) as total, "
                    "SUM(CASE WHEN edge > 0 THEN 1 ELSE 0 END) as positive_edges, "
                    "SUM(CASE WHEN signal_generated = 1 THEN 1 ELSE 0 END) as signals "
                    "FROM backtest_events"
                )
                row = await cursor.fetchone()
                if not row:
                    return

                total_events = row[0] or 0
                positive_edges = row[1] or 0
                signals = row[2] or 0

                if total_events < BACKTEST_MIN_EVENTS_FOR_EDGE_CHECK:
                    return  # Not enough data to judge

                positive_edge_rate = positive_edges / total_events if total_events > 0 else 0
                signal_rate = signals / total_events if total_events > 0 else 0

                if positive_edge_rate == 0 and total_events >= BACKTEST_MIN_EVENTS_FOR_EDGE_CHECK:
                    # Determine if hypotheses have temporal isolation
                    # If they do, 0% is a legitimate result, not a bug
                    cursor2 = await db.execute(
                        "SELECT h.model_config FROM hypotheses h "
                        "INNER JOIN backtest_events be ON be.hypothesis_id = h.hypothesis_id "
                        "WHERE h.status NOT IN ('rejected', 'retired') "
                        "LIMIT 20"
                    )
                    config_rows = await cursor2.fetchall()

                    isolated_count = 0
                    non_isolated_count = 0
                    for (mc_raw,) in config_rows:
                        if mc_raw:
                            try:
                                mc = json.loads(mc_raw) if isinstance(mc_raw, str) else mc_raw
                                if mc.get("temporal_isolation") is True:
                                    isolated_count += 1
                                else:
                                    non_isolated_count += 1
                            except (json.JSONDecodeError, TypeError):
                                non_isolated_count += 1
                        else:
                            non_isolated_count += 1

                    if isolated_count > 0 and non_isolated_count == 0:
                        # All backtested hypotheses have proper temporal isolation.
                        # 0% edge rate is a legitimate finding: no forward edge exists.
                        self._issues.append(IntegrityIssue(
                            check_name="backtest_edge_rate",
                            severity=SEVERITY_INFO,
                            message=(
                                f"0% positive edge rate across {total_events} backtest events, "
                                f"but all hypotheses have proper temporal isolation. This is a "
                                f"legitimate finding — no forward edge detected, not a bug."
                            ),
                            details={
                                "total_events": total_events,
                                "positive_edges": positive_edges,
                                "signals": signals,
                                "positive_edge_rate": positive_edge_rate,
                                "signal_rate": signal_rate,
                                "temporal_isolation": True,
                                "isolated_hypotheses_sampled": isolated_count,
                            },
                        ))
                    else:
                        # Some or all hypotheses lack temporal isolation.
                        # 0% edge rate is suspicious — likely circular testing.
                        self._issues.append(IntegrityIssue(
                            check_name="backtest_edge_rate",
                            severity=SEVERITY_CRITICAL,
                            message=(
                                f"0% positive edge rate across {total_events} backtest events. "
                                f"{non_isolated_count} hypotheses lack temporal isolation — "
                                f"the edge detection may be comparing consensus against itself "
                                f"or has a similar systemic bug."
                            ),
                            details={
                                "total_events": total_events,
                                "positive_edges": positive_edges,
                                "signals": signals,
                                "positive_edge_rate": positive_edge_rate,
                                "signal_rate": signal_rate,
                                "temporal_isolation": False,
                                "non_isolated_count": non_isolated_count,
                                "isolated_count": isolated_count,
                            },
                        ))
                elif positive_edge_rate < 0.02 and total_events >= BACKTEST_MIN_EVENTS_FOR_EDGE_CHECK:
                    self._issues.append(IntegrityIssue(
                        check_name="backtest_edge_rate",
                        severity=SEVERITY_WARNING,
                        message=(
                            f"Extremely low positive edge rate: {positive_edge_rate:.1%} "
                            f"across {total_events} events ({positive_edges} positive). "
                            f"Expected 5-20% for healthy hypothesis testing."
                        ),
                        details={
                            "total_events": total_events,
                            "positive_edges": positive_edges,
                            "positive_edge_rate": positive_edge_rate,
                        },
                    ))

                # Also check per-hypothesis: any hypothesis with >50 events and 0 signals
                cursor = await db.execute(
                    "SELECT hypothesis_id, COUNT(*) as events, "
                    "SUM(CASE WHEN signal_generated = 1 THEN 1 ELSE 0 END) as signals "
                    "FROM backtest_events "
                    "GROUP BY hypothesis_id "
                    "HAVING events >= 50 AND signals = 0"
                )
                zero_signal_hypos = await cursor.fetchall()
                if len(zero_signal_hypos) >= 10:
                    self._issues.append(IntegrityIssue(
                        check_name="backtest_edge_rate",
                        severity=SEVERITY_WARNING,
                        message=(
                            f"{len(zero_signal_hypos)} hypotheses with 50+ events "
                            f"but 0 signals each. Systematic failure in signal generation."
                        ),
                        details={
                            "zero_signal_hypothesis_count": len(zero_signal_hypos),
                            "sample_ids": [row[0] for row in zero_signal_hypos[:5]],
                        },
                    ))

        except Exception as e:
            logger.warning(f"Backtest edge rate check failed: {e}", exc_info=True)

    async def _check_odds_snapshot_freshness(self) -> None:
        """
        If line_monitor claims to be running but no new snapshots exist
        in the last ODDS_SNAPSHOT_STALE_HOURS, it's silently failing.
        """
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("PRAGMA busy_timeout = 60000")
                # Check odds_snapshots_v2 (the normalized table)
                try:
                    cursor = await db.execute(
                        "SELECT MAX(snapshot_time) FROM odds_snapshots_v2"
                    )
                    row = await cursor.fetchone()
                    if row and row[0]:
                        latest = datetime.fromisoformat(
                            str(row[0]).replace("Z", "+00:00")
                        )
                        if latest.tzinfo is None:
                            latest = latest.replace(tzinfo=timezone.utc)
                        age_hours = (datetime.now(timezone.utc) - latest).total_seconds() / 3600
                        if age_hours > ODDS_SNAPSHOT_STALE_HOURS:
                            self._issues.append(IntegrityIssue(
                                check_name="odds_snapshot_freshness",
                                severity=SEVERITY_WARNING,
                                message=(
                                    f"Latest odds snapshot is {age_hours:.1f}h old. "
                                    f"Line monitor may be silently failing if it claims to be running."
                                ),
                                details={
                                    "latest_snapshot": str(row[0]),
                                    "age_hours": round(age_hours, 1),
                                    "threshold_hours": ODDS_SNAPSHOT_STALE_HOURS,
                                },
                            ))
                except Exception as e:
                    logger.warning(f"odds_snapshots_v2 freshness check failed: {e}", exc_info=True)

                # Also check the line_monitor's own odds_snapshots table
                try:
                    cursor = await db.execute(
                        "SELECT MAX(timestamp) FROM odds_snapshots"
                    )
                    row = await cursor.fetchone()
                    if row and row[0]:
                        latest = datetime.fromisoformat(
                            str(row[0]).replace("Z", "+00:00")
                        )
                        if latest.tzinfo is None:
                            latest = latest.replace(tzinfo=timezone.utc)
                        age_hours = (datetime.now(timezone.utc) - latest).total_seconds() / 3600
                        if age_hours > ODDS_SNAPSHOT_STALE_HOURS:
                            self._issues.append(IntegrityIssue(
                                check_name="odds_snapshot_freshness",
                                severity=SEVERITY_WARNING,
                                message=(
                                    f"Line monitor odds_snapshots table: latest is "
                                    f"{age_hours:.1f}h old (threshold: {ODDS_SNAPSHOT_STALE_HOURS}h)."
                                ),
                                details={"age_hours": round(age_hours, 1)},
                            ))
                except Exception as e:
                    logger.warning(f"odds_snapshots freshness check failed: {e}", exc_info=True)

        except Exception as e:
            logger.warning(f"Odds snapshot freshness check failed: {e}", exc_info=True)

    async def _check_signal_pipeline(self) -> None:
        """
        If many hypotheses are in backtesting state but 0 signals have been
        generated across all of them, the signal pipeline is broken.
        """
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("PRAGMA busy_timeout = 60000")
                # Count backtesting hypotheses
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM hypotheses WHERE status = 'backtesting'"
                )
                backtesting_count = (await cursor.fetchone())[0]

                if backtesting_count < SIGNAL_PIPELINE_MIN_HYPOTHESES:
                    return  # Not enough to diagnose

                # Count total signals across all backtested hypotheses
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM backtest_events WHERE signal_generated = 1"
                )
                total_signals = (await cursor.fetchone())[0]

                # Count total backtest events
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM backtest_events"
                )
                total_events = (await cursor.fetchone())[0]

                if total_signals == 0 and total_events > 0:
                    self._issues.append(IntegrityIssue(
                        check_name="signal_pipeline",
                        severity=SEVERITY_CRITICAL,
                        message=(
                            f"{backtesting_count} hypotheses in backtesting, "
                            f"{total_events} backtest events evaluated, but 0 signals "
                            f"generated across ALL of them. The signal generation "
                            f"pipeline is fundamentally broken."
                        ),
                        details={
                            "backtesting_hypotheses": backtesting_count,
                            "total_backtest_events": total_events,
                            "total_signals": total_signals,
                        },
                    ))

        except Exception as e:
            logger.warning(f"Signal pipeline check failed: {e}", exc_info=True)

    # ──────────────────────────────────────────────────────────
    # TEMPORAL ISOLATION CHECK
    # ──────────────────────────────────────────────────────────

    async def _check_temporal_isolation(self) -> None:
        """
        Check that no hypothesis has a backtest where the test period
        overlaps the training period. This is the core anti-circular-testing
        check that prevents the system from regressing to training=testing.

        - CRITICAL if overlap found and hypothesis is NOT rejected
        - INFO if hypothesis has proper isolation and 0% edge rate (legitimate)
        """
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("PRAGMA busy_timeout = 60000")
                cursor = await db.execute(
                    "SELECT hypothesis_id, name, status, model_config "
                    "FROM hypotheses "
                    "WHERE status NOT IN ('rejected', 'retired')"
                )
                rows = await cursor.fetchall()

                overlap_count = 0
                no_metadata_count = 0

                for row in rows:
                    h_id, name, status, mc_raw = row[0], row[1], row[2], row[3]

                    if not mc_raw:
                        continue

                    try:
                        mc = json.loads(mc_raw) if isinstance(mc_raw, str) else mc_raw
                    except (json.JSONDecodeError, TypeError):
                        continue

                    training_end = mc.get("training_period_end")
                    backtest_start = mc.get("backtest_period_start")

                    # Check if hypothesis has been backtested (has backtest range)
                    if backtest_start and training_end:
                        try:
                            te = datetime.strptime(str(training_end), "%Y-%m-%d")
                            bs = datetime.strptime(str(backtest_start), "%Y-%m-%d")
                            if bs <= te:
                                overlap_count += 1
                                self._issues.append(IntegrityIssue(
                                    check_name="temporal_isolation",
                                    severity=SEVERITY_CRITICAL,
                                    message=(
                                        f"CIRCULAR TESTING: hypothesis '{name}' ({h_id}) "
                                        f"has backtest starting {backtest_start} but "
                                        f"training ends {training_end}. Backtest results "
                                        f"are contaminated — they include training data."
                                    ),
                                    details={
                                        "hypothesis_id": h_id,
                                        "name": name,
                                        "status": status,
                                        "training_period_end": training_end,
                                        "backtest_period_start": backtest_start,
                                    },
                                ))
                        except ValueError as e:
                            logger.debug(
                                f"integrity: training/backtest period parse failed "
                                f"for hypothesis {h_id}: {e}"
                            )
                    elif status in ("backtesting", "paper_trading", "live"):
                        # Active hypothesis with no temporal metadata at all
                        if not training_end:
                            no_metadata_count += 1

                if no_metadata_count > 0:
                    self._issues.append(IntegrityIssue(
                        check_name="temporal_isolation",
                        severity=SEVERITY_WARNING,
                        message=(
                            f"{no_metadata_count} active hypotheses lack temporal "
                            f"isolation metadata (no training_period_end). These are "
                            f"legacy hypotheses that may have circular backtests."
                        ),
                        details={"count": no_metadata_count},
                    ))

                if overlap_count == 0 and no_metadata_count == 0:
                    logger.info(
                        "TEMPORAL ISOLATION: all active hypotheses have proper "
                        "temporal separation between training and testing data"
                    )

        except Exception as e:
            logger.warning(f"Temporal isolation check failed: {e}", exc_info=True)

    async def _check_calibration_health(self) -> None:
        """
        Check brier scores and information coefficients from hypothesis_stats.
        Poor calibration (brier > 0.30) or anti-predictive IC (< 0) on
        hypotheses approaching promotion are warning signs.

        Brier score context:
            0.25 = coin-flip baseline (predict 0.5 for everything)
            > 0.30 = worse than a coin flip — model is actively miscalibrated
            < 0.20 = good calibration
        """
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("PRAGMA busy_timeout = 60000")

                # Get the latest stats per hypothesis (only active ones)
                cursor = await db.execute(
                    "SELECT hs.hypothesis_id, h.name, h.status, "
                    "hs.brier_score, hs.information_coefficient, hs.sortino, "
                    "hs.signals_n "
                    "FROM hypothesis_stats hs "
                    "JOIN hypotheses h ON h.hypothesis_id = hs.hypothesis_id "
                    "WHERE h.status IN ('backtesting', 'paper_trading', 'live') "
                    "AND hs.brier_score IS NOT NULL "
                    "ORDER BY hs.computed_at DESC"
                )
                rows = await cursor.fetchall()

                if not rows:
                    return  # No calibration data yet

                # Deduplicate: latest per hypothesis
                seen = set()
                poor_calibration = []
                anti_predictive = []
                for row in rows:
                    h_id, name, status, brier, ic, sortino, signals_n = row
                    if h_id in seen:
                        continue
                    seen.add(h_id)

                    if brier is not None and brier > 0.30:
                        poor_calibration.append(
                            f"'{name}' ({status}): brier={brier:.3f}"
                        )
                    if ic is not None and ic < -0.05 and (signals_n or 0) >= 10:
                        anti_predictive.append(
                            f"'{name}' ({status}): IC={ic:.3f}, n={signals_n}"
                        )

                if poor_calibration:
                    severity = (
                        SEVERITY_CRITICAL if len(poor_calibration) > 3
                        else SEVERITY_WARNING
                    )
                    self._issues.append(IntegrityIssue(
                        check_name="calibration_health",
                        severity=severity,
                        message=(
                            f"{len(poor_calibration)} hypotheses have poor calibration "
                            f"(brier > 0.30, worse than coin-flip): "
                            f"{'; '.join(poor_calibration[:5])}"
                        ),
                        details={
                            "poor_calibration_count": len(poor_calibration),
                            "examples": poor_calibration[:10],
                        },
                    ))

                if anti_predictive:
                    self._issues.append(IntegrityIssue(
                        check_name="calibration_health",
                        severity=SEVERITY_WARNING,
                        message=(
                            f"{len(anti_predictive)} hypotheses have anti-predictive "
                            f"information coefficient (IC < -0.05): "
                            f"{'; '.join(anti_predictive[:5])}"
                        ),
                        details={
                            "anti_predictive_count": len(anti_predictive),
                            "examples": anti_predictive[:10],
                        },
                    ))

        except Exception as e:
            logger.warning(f"Calibration health check failed: {e}", exc_info=True)

    # ──────────────────────────────────────────────────────────
    # OUTPUT SANITY CHECKS
    # ──────────────────────────────────────────────────────────

    async def _check_zero_metrics(self) -> None:
        """
        Any metric that is exactly 0 when it shouldn't be — 0 paper trades,
        0 signals, 0 edges, 0 snapshots — when the pipeline claims to be running.
        """
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("PRAGMA busy_timeout = 60000")
                zero_checks = []

                # Paper trades should exist if we have paper_trading hypotheses
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM hypotheses WHERE status = 'paper_trading'"
                )
                pt_hypos = (await cursor.fetchone())[0]

                cursor = await db.execute("SELECT COUNT(*) FROM paper_trades")
                pt_count = (await cursor.fetchone())[0]

                if pt_hypos > 0 and pt_count == 0:
                    zero_checks.append(
                        f"paper_trades=0 but {pt_hypos} paper_trading hypotheses exist"
                    )

                # Signals table should have entries if we've been running
                cursor = await db.execute("SELECT COUNT(*) FROM signals")
                sig_count = (await cursor.fetchone())[0]

                cursor = await db.execute(
                    "SELECT COUNT(*) FROM hypotheses WHERE status IN ('paper_trading', 'live')"
                )
                active_hypos = (await cursor.fetchone())[0]

                if active_hypos > 0 and sig_count == 0:
                    zero_checks.append(
                        f"signals=0 but {active_hypos} active (paper_trading/live) hypotheses"
                    )

                # Backtest events should exist if we have backtesting/paper_trading hypotheses
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM hypotheses WHERE status IN ('backtesting', 'paper_trading', 'live', 'rejected')"
                )
                tested_hypos = (await cursor.fetchone())[0]

                cursor = await db.execute("SELECT COUNT(*) FROM backtest_events")
                bt_count = (await cursor.fetchone())[0]

                if tested_hypos > 5 and bt_count == 0:
                    zero_checks.append(
                        f"backtest_events=0 but {tested_hypos} hypotheses have been through testing"
                    )

                # Check for backtest runs that completed but never resolved results
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM backtest_runs WHERE completed_at IS NOT NULL"
                )
                completed_runs = (await cursor.fetchone())[0]

                if completed_runs > 0:
                    # Distinguish truly broken resolution (unresolved events)
                    # from expected 0-signal runs (events resolved, but no edge found)
                    cursor = await db.execute(
                        "SELECT COUNT(*) FROM backtest_runs "
                        "WHERE completed_at IS NOT NULL "
                        "AND total_events > 0 AND unresolved > 0"
                    )
                    unresolved_runs = (await cursor.fetchone())[0]

                    cursor = await db.execute(
                        "SELECT COUNT(*) FROM backtest_runs "
                        "WHERE completed_at IS NOT NULL "
                        "AND total_events > 0 AND signals_generated = 0 "
                        "AND unresolved = 0"
                    )
                    zero_signal_runs = (await cursor.fetchone())[0]

                    if unresolved_runs > 0:
                        pct = unresolved_runs / completed_runs * 100
                        severity = SEVERITY_CRITICAL if pct > 50 else SEVERITY_WARNING
                        zero_checks.append(
                            f"backtest_unresolved: {unresolved_runs}/{completed_runs} "
                            f"completed runs ({pct:.0f}%) have events awaiting "
                            f"game result resolution"
                        )
                        if severity == SEVERITY_CRITICAL:
                            self._issues.append(IntegrityIssue(
                                check_name="backtest_resolution_failure",
                                severity=SEVERITY_CRITICAL,
                                message=(
                                    f"Backtest resolution failing: "
                                    f"{unresolved_runs}/{completed_runs} runs "
                                    f"have unresolved events (missing game results)"
                                ),
                                details={
                                    "unresolved_runs": unresolved_runs,
                                    "completed_runs": completed_runs,
                                    "pct_broken": round(pct, 1),
                                },
                            ))

                    if zero_signal_runs > 0:
                        pct = zero_signal_runs / completed_runs * 100
                        if pct > 80:
                            zero_checks.append(
                                f"zero_signal_rate: {zero_signal_runs}/{completed_runs} "
                                f"runs ({pct:.0f}%) found 0 signals — edge thresholds "
                                f"may be too high or devig data too thin (check books_used)"
                            )

                if zero_checks:
                    self._issues.append(IntegrityIssue(
                        check_name="zero_metric_detection",
                        severity=SEVERITY_WARNING,
                        message=f"Zero-value metrics detected: {'; '.join(zero_checks)}",
                        details={"zero_checks": zero_checks},
                    ))

        except Exception as e:
            logger.warning(f"Zero metric check failed: {e}", exc_info=True)

    async def _check_stale_metrics(self) -> None:
        """
        Any metric that hasn't changed in METRIC_STALE_HOURS when the
        pipeline claims to be running.
        """
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("PRAGMA busy_timeout = 60000")
                now = datetime.now(timezone.utc)
                cutoff = (now - timedelta(hours=METRIC_STALE_HOURS)).isoformat()
                stale_items = []

                # Check if any new hypotheses have been created recently
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM hypotheses WHERE created_at > ?", (cutoff,)
                )
                recent_hypos = (await cursor.fetchone())[0]

                cursor = await db.execute("SELECT COUNT(*) FROM hypotheses")
                total_hypos = (await cursor.fetchone())[0]

                if total_hypos > 0 and recent_hypos == 0:
                    stale_items.append(
                        f"No new hypotheses created in {METRIC_STALE_HOURS}h "
                        f"({total_hypos} total exist)"
                    )

                # Check if any new backtest events have been created recently
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM backtest_events WHERE created_at > ?", (cutoff,)
                )
                recent_bt = (await cursor.fetchone())[0]

                cursor = await db.execute("SELECT COUNT(*) FROM backtest_events")
                total_bt = (await cursor.fetchone())[0]

                if total_bt > 0 and recent_bt == 0:
                    stale_items.append(
                        f"No new backtest events in {METRIC_STALE_HOURS}h "
                        f"({total_bt} total exist)"
                    )

                # Check game_contexts freshness
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM game_contexts WHERE created_at > ?", (cutoff,)
                )
                recent_gc = (await cursor.fetchone())[0]

                cursor = await db.execute("SELECT COUNT(*) FROM game_contexts")
                total_gc = (await cursor.fetchone())[0]

                if total_gc > 0 and recent_gc == 0:
                    stale_items.append(
                        f"No new game_contexts in {METRIC_STALE_HOURS}h "
                        f"({total_gc} total exist)"
                    )

                if stale_items:
                    self._issues.append(IntegrityIssue(
                        check_name="stale_metric_detection",
                        severity=SEVERITY_WARNING,
                        message=(
                            f"Stale data detected — pipeline claims to be running but "
                            f"no new data in {METRIC_STALE_HOURS}h: {'; '.join(stale_items)}"
                        ),
                        details={"stale_items": stale_items},
                    ))

        except Exception as e:
            logger.warning(f"Stale metric check failed: {e}", exc_info=True)

    async def _check_rejection_rate(self) -> None:
        """
        If hypothesis rejection rate > 95%, it suggests broken evaluation
        rather than consistently bad hypotheses.
        """
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("PRAGMA busy_timeout = 60000")
                cursor = await db.execute(
                    "SELECT status, COUNT(*) FROM hypotheses "
                    "WHERE status IN ('rejected', 'paper_trading', 'live', 'retired') "
                    "GROUP BY status"
                )
                counts = dict(await cursor.fetchall())

                rejected = counts.get("rejected", 0)
                promoted = (
                    counts.get("paper_trading", 0)
                    + counts.get("live", 0)
                    + counts.get("retired", 0)
                )
                total_evaluated = rejected + promoted

                if total_evaluated < 20:
                    return  # Not enough data

                rejection_rate = rejected / total_evaluated

                if rejection_rate > REJECTION_RATE_BROKEN_THRESHOLD:
                    self._issues.append(IntegrityIssue(
                        check_name="rejection_rate",
                        severity=SEVERITY_WARNING,
                        message=(
                            f"Hypothesis rejection rate is {rejection_rate:.1%} "
                            f"({rejected}/{total_evaluated}). >{REJECTION_RATE_BROKEN_THRESHOLD:.0%} "
                            f"suggests broken evaluation criteria, not bad hypotheses."
                        ),
                        details={
                            "rejected": rejected,
                            "promoted": promoted,
                            "total_evaluated": total_evaluated,
                            "rejection_rate": rejection_rate,
                        },
                    ))

        except Exception as e:
            logger.warning(f"Rejection rate check failed: {e}", exc_info=True)

    # ──────────────────────────────────────────────────────────
    # PHASE ERROR TRACKING
    # ──────────────────────────────────────────────────────────

    def record_phase_result(self, phase_name: str, success: bool) -> None:
        """Record the success/failure of a research loop phase for trend analysis."""
        history = self._phase_errors[phase_name]
        history.append(success)
        # Keep last 20 results
        if len(history) > 20:
            self._phase_errors[phase_name] = history[-20:]

    def get_phase_error_rates(self) -> dict[str, dict]:
        """Get error rates per phase over the last N runs."""
        rates = {}
        for phase, history in self._phase_errors.items():
            if not history:
                continue
            recent = history[-10:]  # Last 10 runs
            failures = sum(1 for s in recent if not s)
            error_rate = failures / len(recent)
            rates[phase] = {
                "error_rate": round(error_rate, 2),
                "failures_last_10": failures,
                "total_runs": len(history),
                "is_broken": error_rate > PHASE_ERROR_RATE_THRESHOLD,
            }
        return rates

    def check_phase_error_rates(self) -> list[IntegrityIssue]:
        """Check if any phase has too-high error rate. Returns issues."""
        issues = []
        for phase, rate_info in self.get_phase_error_rates().items():
            if rate_info["is_broken"]:
                issues.append(IntegrityIssue(
                    check_name="phase_error_rate",
                    severity=SEVERITY_CRITICAL,
                    message=(
                        f"Phase '{phase}' has {rate_info['error_rate']:.0%} error rate "
                        f"over last 10 runs ({rate_info['failures_last_10']} failures). "
                        f"This phase is effectively broken."
                    ),
                    details=rate_info,
                ))
        return issues

    # ──────────────────────────────────────────────────────────
    # LOGGING & REPORTING
    # ──────────────────────────────────────────────────────────

    async def _log_issues(self) -> None:
        """Log all issues from this run to the integrity_checks table."""
        if not self._issues:
            return

        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("PRAGMA busy_timeout = 60000")
                for issue in self._issues:
                    await db.execute(
                        "INSERT INTO integrity_checks "
                        "(run_id, check_name, severity, message, details_json, auto_fix) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            self._run_count,
                            issue.check_name,
                            issue.severity,
                            issue.message,
                            json.dumps(issue.details),
                            issue.auto_fix,
                        ),
                    )
                await db.commit()
        except Exception as e:
            logger.error(f"Failed to log integrity issues: {e}", exc_info=True)

    def get_latest_report(self) -> dict:
        """Get the latest integrity check results for API endpoints."""
        if not self._last_run:
            return {
                "status": "not_run",
                "message": "Integrity checks have not run yet",
            }

        critical_count = sum(1 for i in self._issues if i.severity == SEVERITY_CRITICAL)
        warning_count = sum(1 for i in self._issues if i.severity == SEVERITY_WARNING)

        # Also check phase error rates
        phase_issues = self.check_phase_error_rates()
        for pi in phase_issues:
            critical_count += 1

        all_issues = [i.to_dict() for i in self._issues] + [pi.to_dict() for pi in phase_issues]

        if critical_count > 0:
            status = "critical"
        elif warning_count > 0:
            status = "degraded"
        else:
            status = "ok"

        return {
            "status": status,
            "healthy": critical_count == 0,
            "last_run": self._last_run,
            "run_count": self._run_count,
            "critical_issues": critical_count,
            "warning_issues": warning_count,
            "issues": all_issues,
            "phase_error_rates": self.get_phase_error_rates(),
        }

    async def get_history(self, limit: int = 50) -> list[dict]:
        """Get recent integrity check history from the database."""
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("PRAGMA busy_timeout = 60000")
                cursor = await db.execute(
                    "SELECT run_id, check_name, severity, message, details_json, "
                    "auto_fix, created_at "
                    "FROM integrity_checks "
                    "ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                )
                rows = await cursor.fetchall()
                return [
                    {
                        "run_id": r[0],
                        "check_name": r[1],
                        "severity": r[2],
                        "message": r[3],
                        "details": json.loads(r[4]) if r[4] else {},
                        "auto_fix": r[5],
                        "created_at": r[6],
                    }
                    for r in rows
                ]
        except Exception as e:
            logger.error(f"Failed to fetch integrity history: {e}", exc_info=True)
            return []


# ── Singleton for use across the application ──
_checker: Optional[PipelineIntegrityChecker] = None


def get_checker() -> PipelineIntegrityChecker:
    """Get or create the singleton integrity checker."""
    global _checker
    if _checker is None:
        _checker = PipelineIntegrityChecker()
    return _checker


async def initialize() -> PipelineIntegrityChecker:
    """Initialize the integrity checker and ensure its table exists."""
    checker = get_checker()
    await checker.ensure_table()
    logger.info("Pipeline integrity checker initialized")
    return checker
