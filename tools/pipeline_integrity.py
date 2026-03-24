"""
Pipeline integrity checks for Callisto.

Two tiers:
  1. Lightweight checks (used by /health) — fast SQL counts and timestamp checks.
     These run on every health poll and must complete in <500ms.

  2. Deep checks (used by /health/deep) — thorough analysis of pipeline output
     quality, edge distributions, hypothesis flow, paper trade accuracy, etc.
     These can take several seconds and are called on-demand.

Status levels per pipeline:
  "ok"      — producing expected output within normal parameters
  "WARNING" — output exists but quantity/quality is suboptimal
  "BROKEN"  — critical failure, pipeline not producing expected output

Overall status:
  "ok"       — all pipelines ok
  "DEGRADED" — at least one WARNING, no BROKEN
  "BROKEN"   — at least one BROKEN pipeline
"""

import logging
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import aiosqlite

logger = logging.getLogger("callisto.pipeline_integrity")

DB_PATH = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")

# ---------------------------------------------------------------------------
# Thresholds (tune as the system matures)
# ---------------------------------------------------------------------------
# Paper trading
PAPER_TRADE_STALE_HOURS = 72       # BROKEN if no trades in this window
PAPER_TRADE_WARNING_HOURS = 24     # WARNING if no trades in this window

# Backtest edges
MIN_POSITIVE_EDGE_RATE = 0.01      # BROKEN if < 1% of events have positive edge
WARN_POSITIVE_EDGE_RATE = 0.05     # WARNING if < 5% positive edge rate

# Hypothesis flow
HYPOTHESIS_PROMOTION_DAYS = 30     # WARNING if 0 promoted in this window
HYPOTHESIS_STALE_DAYS = 60         # BROKEN if 0 promoted in this window

# Data collection
DATA_STALE_MINUTES = 60            # WARNING if last snapshot > 60 min
DATA_BROKEN_MINUTES = 360          # BROKEN if last snapshot > 6 hours

# Odds snapshots
SNAPSHOT_STALE_MINUTES = 30        # WARNING if no snapshot in 30 min
SNAPSHOT_BROKEN_MINUTES = 120      # BROKEN if no snapshot in 2 hours


# ---------------------------------------------------------------------------
# Lightweight checks — called by /health
# ---------------------------------------------------------------------------

async def _get_db() -> aiosqlite.Connection:
    """Open a read-only connection for integrity checks."""
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    return db


async def check_paper_trading() -> dict:
    """Check if paper trading pipeline is producing trades."""
    try:
        db = await _get_db()
        try:
            # Total paper trades
            cursor = await db.execute("SELECT COUNT(*) FROM paper_trades")
            total = (await cursor.fetchone())[0]

            # Recent paper trades
            cutoff_broken = (
                datetime.now(timezone.utc) - timedelta(hours=PAPER_TRADE_STALE_HOURS)
            ).isoformat()
            cutoff_warn = (
                datetime.now(timezone.utc) - timedelta(hours=PAPER_TRADE_WARNING_HOURS)
            ).isoformat()

            cursor = await db.execute(
                "SELECT COUNT(*) FROM paper_trades WHERE created_at > ?",
                (cutoff_broken,),
            )
            recent_72h = (await cursor.fetchone())[0]

            cursor = await db.execute(
                "SELECT COUNT(*) FROM paper_trades WHERE created_at > ?",
                (cutoff_warn,),
            )
            recent_24h = (await cursor.fetchone())[0]

            # Count hypotheses in paper_trading status
            cursor = await db.execute(
                "SELECT COUNT(*) FROM hypotheses WHERE status = 'paper_trading'"
            )
            paper_hypos = (await cursor.fetchone())[0]

            if paper_hypos == 0 and total == 0:
                return {
                    "status": "WARNING",
                    "message": f"No hypotheses in paper_trading stage, 0 trades total",
                    "total_trades": total,
                    "recent_72h": recent_72h,
                    "paper_hypotheses": paper_hypos,
                }

            if paper_hypos > 0 and recent_72h == 0:
                return {
                    "status": "BROKEN",
                    "message": f"{paper_hypos} hypotheses in paper_trading, 0 trades in {PAPER_TRADE_STALE_HOURS}h",
                    "total_trades": total,
                    "recent_72h": recent_72h,
                    "paper_hypotheses": paper_hypos,
                }

            if paper_hypos > 0 and recent_24h == 0:
                return {
                    "status": "WARNING",
                    "message": f"{paper_hypos} hypotheses in paper_trading, 0 trades in {PAPER_TRADE_WARNING_HOURS}h",
                    "total_trades": total,
                    "recent_24h": recent_24h,
                    "paper_hypotheses": paper_hypos,
                }

            return {
                "status": "ok",
                "message": f"{total} total trades, {recent_24h} in last 24h",
                "total_trades": total,
                "recent_24h": recent_24h,
                "paper_hypotheses": paper_hypos,
            }
        finally:
            await db.close()
    except Exception as e:
        return {"status": "WARNING", "message": f"check error: {e}"}


async def check_backtest_edges() -> dict:
    """Check if backtests are producing any positive edges."""
    try:
        db = await _get_db()
        try:
            # Total backtest events
            cursor = await db.execute("SELECT COUNT(*) FROM backtest_events")
            total = (await cursor.fetchone())[0]

            if total == 0:
                return {
                    "status": "WARNING",
                    "message": "0 backtest events recorded",
                    "total_events": 0,
                    "positive_edge_count": 0,
                    "positive_edge_rate": 0,
                }

            # Positive edge events
            cursor = await db.execute(
                "SELECT COUNT(*) FROM backtest_events WHERE edge > 0"
            )
            positive = (await cursor.fetchone())[0]
            rate = positive / total if total > 0 else 0

            # Average edge across all events
            cursor = await db.execute(
                "SELECT AVG(edge) FROM backtest_events WHERE signal_generated = TRUE"
            )
            row = await cursor.fetchone()
            avg_edge = row[0] if row[0] is not None else 0

            if rate < MIN_POSITIVE_EDGE_RATE:
                return {
                    "status": "BROKEN",
                    "message": f"{rate:.1%} positive edge rate across {total} events",
                    "total_events": total,
                    "positive_edge_count": positive,
                    "positive_edge_rate": round(rate, 4),
                    "avg_signal_edge": round(avg_edge, 4),
                }

            if rate < WARN_POSITIVE_EDGE_RATE:
                return {
                    "status": "WARNING",
                    "message": f"{rate:.1%} positive edge rate ({positive}/{total})",
                    "total_events": total,
                    "positive_edge_count": positive,
                    "positive_edge_rate": round(rate, 4),
                    "avg_signal_edge": round(avg_edge, 4),
                }

            return {
                "status": "ok",
                "message": f"{rate:.1%} positive edge rate ({positive}/{total}), avg edge {avg_edge:.3f}",
                "total_events": total,
                "positive_edge_count": positive,
                "positive_edge_rate": round(rate, 4),
                "avg_signal_edge": round(avg_edge, 4),
            }
        finally:
            await db.close()
    except Exception as e:
        return {"status": "WARNING", "message": f"check error: {e}"}


async def check_hypothesis_flow() -> dict:
    """Check if hypotheses are being promoted through the pipeline."""
    try:
        db = await _get_db()
        try:
            # Count by status
            cursor = await db.execute(
                "SELECT status, COUNT(*) FROM hypotheses GROUP BY status"
            )
            rows = await cursor.fetchall()
            counts = {row[0]: row[1] for row in rows}
            total = sum(counts.values())

            draft = counts.get("draft", 0)
            backtesting = counts.get("backtesting", 0)
            paper_trading = counts.get("paper_trading", 0)
            live = counts.get("live", 0)
            rejected = counts.get("rejected", 0)

            # Check recent promotions
            cutoff_warn = (
                datetime.now(timezone.utc) - timedelta(days=HYPOTHESIS_PROMOTION_DAYS)
            ).isoformat()
            cutoff_broken = (
                datetime.now(timezone.utc) - timedelta(days=HYPOTHESIS_STALE_DAYS)
            ).isoformat()

            cursor = await db.execute(
                "SELECT COUNT(*) FROM hypotheses WHERE promoted_at > ?",
                (cutoff_warn,),
            )
            promoted_30d = (await cursor.fetchone())[0]

            cursor = await db.execute(
                "SELECT COUNT(*) FROM hypotheses WHERE promoted_at > ?",
                (cutoff_broken,),
            )
            promoted_60d = (await cursor.fetchone())[0]

            if total == 0:
                return {
                    "status": "WARNING",
                    "message": "No hypotheses in system",
                    "counts": counts,
                    "promoted_30d": 0,
                }

            if draft > 0 and promoted_60d == 0 and total > 10:
                return {
                    "status": "BROKEN",
                    "message": f"{draft} draft, 0 promoted in {HYPOTHESIS_STALE_DAYS} days",
                    "counts": counts,
                    "promoted_30d": promoted_30d,
                    "promoted_60d": promoted_60d,
                }

            if draft > 0 and promoted_30d == 0 and total > 10:
                return {
                    "status": "WARNING",
                    "message": f"{draft} draft, 0 promoted in {HYPOTHESIS_PROMOTION_DAYS} days",
                    "counts": counts,
                    "promoted_30d": promoted_30d,
                }

            return {
                "status": "ok",
                "message": f"{total} total: {draft} draft, {backtesting} backtesting, {paper_trading} paper, {live} live",
                "counts": counts,
                "promoted_30d": promoted_30d,
            }
        finally:
            await db.close()
    except Exception as e:
        return {"status": "WARNING", "message": f"check error: {e}"}


async def check_data_collection() -> dict:
    """Check if data collection is running and producing snapshots."""
    try:
        db = await _get_db()
        try:
            # Check odds_snapshots_v2 for recency
            cursor = await db.execute(
                "SELECT COUNT(*), MAX(snapshot_time) FROM odds_snapshots_v2"
            )
            row = await cursor.fetchone()
            total_snapshots = row[0] if row[0] else 0
            last_snapshot = row[1]

            # Check game_contexts for data collection
            cursor = await db.execute(
                "SELECT COUNT(*), MAX(game_date) FROM game_contexts"
            )
            row = await cursor.fetchone()
            total_contexts = row[0] if row[0] else 0

            if total_snapshots == 0:
                return {
                    "status": "BROKEN",
                    "message": "0 odds snapshots recorded",
                    "total_snapshots": 0,
                    "total_contexts": total_contexts,
                }

            # Parse last snapshot time
            minutes_ago = None
            if last_snapshot:
                try:
                    last_dt = datetime.fromisoformat(
                        str(last_snapshot).replace("Z", "+00:00")
                    )
                    if last_dt.tzinfo is None:
                        last_dt = last_dt.replace(tzinfo=timezone.utc)
                    delta = datetime.now(timezone.utc) - last_dt
                    minutes_ago = delta.total_seconds() / 60
                except Exception:
                    minutes_ago = None

            if minutes_ago is not None and minutes_ago > DATA_BROKEN_MINUTES:
                return {
                    "status": "BROKEN",
                    "message": f"{total_snapshots} snapshots, last {int(minutes_ago)}min ago",
                    "total_snapshots": total_snapshots,
                    "last_snapshot_minutes_ago": round(minutes_ago, 1),
                    "total_contexts": total_contexts,
                }

            if minutes_ago is not None and minutes_ago > DATA_STALE_MINUTES:
                return {
                    "status": "WARNING",
                    "message": f"{total_snapshots} snapshots, last {int(minutes_ago)}min ago",
                    "total_snapshots": total_snapshots,
                    "last_snapshot_minutes_ago": round(minutes_ago, 1),
                    "total_contexts": total_contexts,
                }

            msg = f"{total_snapshots} snapshots"
            if minutes_ago is not None:
                msg += f", last {int(minutes_ago)}min ago"
            return {
                "status": "ok",
                "message": msg,
                "total_snapshots": total_snapshots,
                "last_snapshot_minutes_ago": round(minutes_ago, 1) if minutes_ago else None,
                "total_contexts": total_contexts,
            }
        finally:
            await db.close()
    except Exception as e:
        return {"status": "WARNING", "message": f"check error: {e}"}


def compute_overall_status(checks: dict) -> str:
    """Derive overall status from individual pipeline checks."""
    statuses = [v.get("status", "ok") for v in checks.values()]
    if "BROKEN" in statuses:
        return "BROKEN"
    if "WARNING" in statuses:
        return "DEGRADED"
    return "ok"


async def lightweight_pipeline_check() -> dict:
    """
    Fast pipeline health check for /health endpoint.
    Runs all lightweight checks in parallel and returns summary.
    Must complete in <500ms.
    """
    import asyncio

    results = await asyncio.gather(
        check_paper_trading(),
        check_backtest_edges(),
        check_hypothesis_flow(),
        check_data_collection(),
        return_exceptions=True,
    )

    names = ["paper_trading", "backtest_edges", "hypothesis_flow", "data_collection"]
    checks = {}
    for name, result in zip(names, results):
        if isinstance(result, Exception):
            checks[name] = {"status": "WARNING", "message": f"check failed: {result}"}
        else:
            checks[name] = result

    # Build compact format for /health: just status + message per pipeline
    compact = {}
    for name, check in checks.items():
        status = check.get("status", "ok")
        message = check.get("message", "")
        if status == "ok":
            compact[name] = f"ok: {message}"
        else:
            compact[name] = f"{status}: {message}"

    compact["overall"] = compute_overall_status(checks)
    return compact


# ---------------------------------------------------------------------------
# Deep checks — called by /health/deep (can be slow)
# ---------------------------------------------------------------------------

async def check_backtest_quality() -> dict:
    """Deep analysis of backtest output quality."""
    try:
        db = await _get_db()
        try:
            # Per-hypothesis backtest summary
            cursor = await db.execute("""
                SELECT
                    h.hypothesis_id,
                    h.name,
                    h.status,
                    COUNT(be.id) as event_count,
                    SUM(CASE WHEN be.edge > 0 THEN 1 ELSE 0 END) as positive_edges,
                    AVG(be.edge) as avg_edge,
                    AVG(CASE WHEN be.signal_generated THEN be.ev_pct END) as avg_ev,
                    SUM(CASE WHEN be.actual_result = 'won' THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN be.actual_result = 'lost' THEN 1 ELSE 0 END) as losses
                FROM hypotheses h
                LEFT JOIN backtest_events be ON h.hypothesis_id = be.hypothesis_id
                GROUP BY h.hypothesis_id
                ORDER BY event_count DESC
                LIMIT 20
            """)
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
            hypotheses = [dict(zip(cols, r)) for r in rows]

            # Backtest run summary
            cursor = await db.execute("""
                SELECT
                    COUNT(*) as total_runs,
                    SUM(CASE WHEN is_significant THEN 1 ELSE 0 END) as significant_runs,
                    AVG(hit_rate) as avg_hit_rate,
                    AVG(roi_pct) as avg_roi,
                    AVG(sharpe_ratio) as avg_sharpe
                FROM backtest_runs
                WHERE completed_at IS NOT NULL
            """)
            row = await cursor.fetchone()
            run_summary = dict(zip([d[0] for d in cursor.description], row)) if row else {}

            return {
                "hypotheses": hypotheses,
                "run_summary": run_summary,
            }
        finally:
            await db.close()
    except Exception as e:
        return {"error": str(e)}


async def check_paper_trade_accuracy() -> dict:
    """Deep analysis of paper trade performance."""
    try:
        db = await _get_db()
        try:
            cursor = await db.execute("""
                SELECT
                    h.hypothesis_id,
                    h.name,
                    COUNT(pt.trade_id) as trade_count,
                    SUM(CASE WHEN pt.actual_result = 'won' THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN pt.actual_result = 'lost' THEN 1 ELSE 0 END) as losses,
                    AVG(pt.edge) as avg_edge,
                    AVG(pt.clv_implied) as avg_clv,
                    SUM(pt.hypothetical_pnl) as total_pnl
                FROM hypotheses h
                JOIN paper_trades pt ON h.hypothesis_id = pt.hypothesis_id
                GROUP BY h.hypothesis_id
                ORDER BY trade_count DESC
            """)
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
            return {"hypotheses": [dict(zip(cols, r)) for r in rows]}
        finally:
            await db.close()
    except Exception as e:
        return {"error": str(e)}


async def check_hypothesis_pipeline_flow() -> dict:
    """Deep check of how hypotheses move through the pipeline stages."""
    try:
        db = await _get_db()
        try:
            # Stage distribution
            cursor = await db.execute(
                "SELECT status, COUNT(*) FROM hypotheses GROUP BY status"
            )
            stage_counts = {row[0]: row[1] for row in await cursor.fetchall()}

            # Recently created vs promoted vs rejected
            cutoff_7d = (
                datetime.now(timezone.utc) - timedelta(days=7)
            ).isoformat()
            cutoff_30d = (
                datetime.now(timezone.utc) - timedelta(days=30)
            ).isoformat()

            cursor = await db.execute(
                "SELECT COUNT(*) FROM hypotheses WHERE created_at > ?", (cutoff_7d,)
            )
            created_7d = (await cursor.fetchone())[0]

            cursor = await db.execute(
                "SELECT COUNT(*) FROM hypotheses WHERE promoted_at > ?", (cutoff_7d,)
            )
            promoted_7d = (await cursor.fetchone())[0]

            cursor = await db.execute(
                "SELECT COUNT(*) FROM hypotheses WHERE promoted_at > ?", (cutoff_30d,)
            )
            promoted_30d = (await cursor.fetchone())[0]

            cursor = await db.execute(
                "SELECT COUNT(*) FROM hypotheses WHERE status = 'rejected' AND updated_at > ?",
                (cutoff_30d,),
            )
            rejected_30d = (await cursor.fetchone())[0]

            # Bottleneck detection: stages with stale hypotheses
            bottlenecks = []
            for stage in ["draft", "backtesting", "paper_trading"]:
                cursor = await db.execute(
                    "SELECT COUNT(*), MIN(updated_at) FROM hypotheses WHERE status = ?",
                    (stage,),
                )
                row = await cursor.fetchone()
                count = row[0] or 0
                oldest = row[1]
                if count > 0 and oldest:
                    try:
                        oldest_dt = datetime.fromisoformat(
                            str(oldest).replace("Z", "+00:00")
                        )
                        if oldest_dt.tzinfo is None:
                            oldest_dt = oldest_dt.replace(tzinfo=timezone.utc)
                        days_old = (datetime.now(timezone.utc) - oldest_dt).days
                        if days_old > 14:
                            bottlenecks.append({
                                "stage": stage,
                                "count": count,
                                "oldest_days": days_old,
                            })
                    except Exception:
                        pass

            return {
                "stage_counts": stage_counts,
                "created_7d": created_7d,
                "promoted_7d": promoted_7d,
                "promoted_30d": promoted_30d,
                "rejected_30d": rejected_30d,
                "bottlenecks": bottlenecks,
            }
        finally:
            await db.close()
    except Exception as e:
        return {"error": str(e)}


async def deep_pipeline_check() -> dict:
    """
    Full integrity suite for /health/deep endpoint.
    Runs all deep checks and returns comprehensive diagnostics.
    Can take several seconds.
    """
    import asyncio

    # Run lightweight checks first for the summary
    lightweight = await lightweight_pipeline_check()

    # Then run deep checks
    deep_results = await asyncio.gather(
        check_backtest_quality(),
        check_paper_trade_accuracy(),
        check_hypothesis_pipeline_flow(),
        return_exceptions=True,
    )

    deep_names = ["backtest_quality", "paper_trade_accuracy", "hypothesis_pipeline_flow"]
    deep_checks = {}
    for name, result in zip(deep_names, deep_results):
        if isinstance(result, Exception):
            deep_checks[name] = {"error": str(result)}
        else:
            deep_checks[name] = result

    return {
        "summary": lightweight,
        "deep": deep_checks,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
