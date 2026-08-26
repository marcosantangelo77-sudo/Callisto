"""DB logging + bet-recording orchestration for the executor (slice 2 split).

Extracted from ``tools/bet_executor.py``: the ``executor_log`` audit-trail
writer, the bets-table recorder with duplicate guard, and the bankroll-peak
observation/peak-window queries used by the drawdown kill-switch.

All functions take an explicit ``db`` connection — no module state, no
arming, no live betting.
"""

import logging
from datetime import datetime, timedelta, timezone

from tools.betexec.config import DRAWDOWN_PEAK_WINDOW_DAYS

logger = logging.getLogger("callisto.executor")

EXECUTOR_LOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS executor_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    action TEXT NOT NULL,
    sport TEXT,
    team TEXT,
    market TEXT,
    side TEXT,
    odds INTEGER,
    stake REAL,
    edge REAL,
    hypothesis_id TEXT,
    bet_id INTEGER,
    screenshot_path TEXT,
    status TEXT NOT NULL,
    error TEXT,
    details TEXT
)
"""


async def ensure_executor_log_schema(db) -> None:
    """Create the executor_log table if missing and commit."""
    from tools.db_utils import commit_with_retry
    await db.execute(EXECUTOR_LOG_SCHEMA)
    await commit_with_retry(db, operation="executor schema")


async def log_action(
    db,
    action,
    sport,
    team,
    market,
    side,
    odds,
    stake,
    edge,
    hypothesis_id,
    bet_id=None,
    screenshot=None,
    reason=None,
) -> None:
    """Log executor action for audit trail."""
    from tools.db_utils import execute_with_retry, commit_with_retry
    await execute_with_retry(
        db,
        """INSERT INTO executor_log
        (timestamp, action, sport, team, market, side, odds, stake, edge,
         hypothesis_id, bet_id, screenshot_path, status, error)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            datetime.now(timezone.utc).isoformat(),
            action, sport, team, market, side, odds, stake, edge,
            hypothesis_id, bet_id, screenshot,
            "success" if action == "BET_PLACED" else "failed",
            reason,
        ),
        max_retries=10,
        operation="executor log_action",
    )
    await commit_with_retry(db, max_retries=10, operation="executor log_action")


def implied_probability(fair_prob: float, edge: float) -> float:
    """Implied (market) probability from fair prob minus edge, clamped to [0,1].

    SECURITY/CORRECTNESS (audit C-3, 2026-04-18): implied = fair - edge. The
    prior formula ``1.0 - fair_prob + edge`` was inverted, poisoning CLV.
    """
    return max(0.0, min(1.0, fair_prob - edge))


def build_bet_insert_params(
    now_iso: str,
    sport,
    event_id,
    game_description,
    team,
    market,
    bookmaker,
    odds,
    point,
    stake,
    edge,
    fair_prob,
    hypothesis_id,
    bankroll: float,
) -> tuple[str, tuple]:
    """Build the bets-table INSERT SQL + params tuple.

    Factored out so tests can pin the recorded row shape without a DB.
    Returns ``(sql, params_tuple)``.
    """
    implied = implied_probability(fair_prob, edge)
    kelly_at_placement = round(stake / max(bankroll, 1), 4)
    sql = """INSERT INTO bets
        (placed_at, sport, event_id, game_description, bet_type,
         team, market, bookmaker, placement_odds, placement_point,
         placement_implied_prob, stake, result, edge_at_placement,
         kelly_at_placement, notes, tags)
        VALUES (?, ?, ?, ?, 'single', ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)"""
    params = (
        now_iso, sport, event_id, game_description,
        team, market, bookmaker, odds, point,
        round(implied, 6), stake, round(edge, 6),
        kelly_at_placement,
        f"Auto-executed by Callisto. hypothesis={hypothesis_id}",
        f"auto,hypothesis:{hypothesis_id}",
    )
    return sql, params


DUP_CHECK_SQL = (
    "SELECT bet_id FROM bets WHERE event_id = ? AND team = ? AND market = ? "
    "AND result = 'pending' AND placed_at > datetime('now', '-1 hour')"
)


async def record_bet(
    db,
    get_bankroll,
    bankroll_lock,
    *,
    sport,
    event_id,
    game_description,
    team,
    market,
    bookmaker,
    odds,
    point,
    stake,
    edge,
    fair_prob,
    hypothesis_id,
) -> int:
    """Record bet in the bets table and update bankroll; returns bet_id.

    ``get_bankroll`` is an async callable returning the current balance;
    ``bankroll_lock`` is the asyncio.Lock serializing read→write of bankroll
    across concurrent placements (audit H-4).
    """
    now = datetime.now(timezone.utc).isoformat()
    from tools.db_utils import execute_with_retry, commit_with_retry

    # Guard against duplicate bets: check if we already have a pending bet
    # on this event+team+market within the last hour
    dup_check = await execute_with_retry(db, DUP_CHECK_SQL,
                                         (event_id, team, market),
                                         operation="executor dup_check")
    existing = await dup_check.fetchone()
    if existing:
        logger.warning(
            f"Duplicate bet prevented: event={event_id} team={team} "
            f"market={market} (existing bet_id={existing[0]})"
        )
        return existing[0]

    sql, params = build_bet_insert_params(
        now, sport, event_id, game_description, team, market,
        bookmaker, odds, point, stake, edge, fair_prob, hypothesis_id,
        await get_bankroll(),
    )
    cursor = await execute_with_retry(
        db, sql, params,
        max_retries=10,
        operation="executor record_bet insert",
    )
    bet_id = cursor.lastrowid

    # Update bankroll (deduct stake) under the same lock that gated sizing,
    # wrapped in BEGIN IMMEDIATE so SQLite's own locking also serializes even
    # if two callers somehow race past the asyncio lock (audit H-4).
    async with bankroll_lock:
        try:
            await db.execute("BEGIN IMMEDIATE")
        except Exception:
            # Transaction already open (WriteCoordinator path) — fine.
            pass
        try:
            bankroll = await get_bankroll()
            await execute_with_retry(
                db,
                "INSERT INTO bankroll (timestamp, balance, change, bet_id, description) "
                "VALUES (?, ?, ?, ?, ?)",
                (now, bankroll - stake, -stake, bet_id,
                 f"Auto bet #{bet_id}: {team} {market}"),
                max_retries=10,
                operation="executor record_bet bankroll",
            )
            await commit_with_retry(db, max_retries=10, operation="executor record_bet")
        except Exception:
            try:
                await db.rollback()
            except Exception:
                pass
            raise
    return bet_id


# ---------------------------------------------------------------------------
# Drawdown kill-switch DB surface
# ---------------------------------------------------------------------------

async def record_bankroll_peak(db, bankroll: float) -> None:
    """Append a bankroll observation to the peak table (best-effort)."""
    try:
        from tools.db_utils import execute_with_retry, commit_with_retry
        await execute_with_retry(
            db,
            "INSERT INTO bankroll_peak (observed_at, balance, note) VALUES (?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), float(bankroll), "auto-observed"),
            operation="bankroll_peak insert",
        )
        await commit_with_retry(db, operation="bankroll_peak insert")
    except Exception as e:
        logger.debug(f"bankroll_peak insert skipped: {e}")


async def rolling_peak(db, window_days: int | None = None) -> float:
    """Return MAX(balance) over the rolling peak window."""
    window = window_days or DRAWDOWN_PEAK_WINDOW_DAYS
    cutoff = (datetime.now(timezone.utc) - timedelta(days=window)).isoformat()
    peak = 0.0
    try:
        cursor = await db.execute(
            "SELECT COALESCE(MAX(balance), 0) FROM bankroll_peak WHERE observed_at >= ?",
            (cutoff,),
        )
        row = await cursor.fetchone()
        peak = float(row[0]) if row and row[0] is not None else 0.0
    except Exception:
        peak = 0.0
    # Fallback to bankroll history if bankroll_peak is empty / missing.
    if peak <= 0:
        try:
            cursor = await db.execute(
                "SELECT COALESCE(MAX(balance), 0) FROM bankroll WHERE timestamp >= ?",
                (cutoff,),
            )
            row = await cursor.fetchone()
            peak = float(row[0]) if row and row[0] is not None else 0.0
        except Exception:
            peak = 0.0
    return peak
