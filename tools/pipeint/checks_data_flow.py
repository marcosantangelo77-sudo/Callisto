"""Data-flow integrity checks for the Callisto research pipeline."""

import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import aiosqlite

from tools.pipeint.core import (
    BACKTEST_MIN_EVENTS_FOR_EDGE_CHECK,
    DB_PATH,
    HYPOTHESIS_STALL_HOURS,
    ODDS_SNAPSHOT_STALE_HOURS,
    PAPER_TRADE_STALL_HOURS,
    SEVERITY_CRITICAL,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    SIGNAL_PIPELINE_MIN_HYPOTHESES,
    IntegrityIssue,
)

logger = logging.getLogger("callisto.pipeline_integrity")


class DataFlowChecks:
    """Mixin with data-flow integrity checks."""

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
                    except (ValueError, TypeError):
                        pass

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

