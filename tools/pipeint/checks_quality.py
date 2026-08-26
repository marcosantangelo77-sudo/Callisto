"""Output-sanity and quality integrity checks for the Callisto pipeline."""

import json
import logging
from datetime import datetime, timedelta, timezone

import aiosqlite

from tools.pipeint.core import (
    DB_PATH,
    METRIC_STALE_HOURS,
    REJECTION_RATE_BROKEN_THRESHOLD,
    SEVERITY_CRITICAL,
    SEVERITY_WARNING,
    IntegrityIssue,
)

logger = logging.getLogger("callisto.pipeline_integrity")


class QualityChecks:
    """Mixin with output-sanity and quality checks."""

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
                        except ValueError:
                            pass
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

