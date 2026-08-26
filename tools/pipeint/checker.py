"""PipelineIntegrityChecker: orchestrates all pipeline integrity checks."""

import asyncio
import json
import logging
import time
import traceback
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

import aiosqlite

from tools.pipeint.checks_data_flow import DataFlowChecks
from tools.pipeint.checks_quality import QualityChecks
from tools.pipeint.core import (
    DB_PATH,
    INTEGRITY_CHECK_INTERVAL_CYCLES,
    PHASE_ERROR_RATE_THRESHOLD,
    SEVERITY_CRITICAL,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    IntegrityIssue,
)

logger = logging.getLogger("callisto.pipeline_integrity")


class PipelineIntegrityChecker(DataFlowChecks, QualityChecks):
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

