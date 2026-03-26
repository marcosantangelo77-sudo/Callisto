"""
Tiered memory cache — hot/warm/cold storage for agent context management.

THREE TIERS:

  HOT  — Auto-loaded into agent context every invocation (~2K tokens max).
         Current state only. Rebuilt fresh each run.

  WARM — Queryable via Hermes function calls. NOT auto-loaded.
         Recent history (last 30 days). Agents pull specific slices.
         Returns SUMMARIES, not raw rows.

  COLD — No agent access. Historical archive (30+ days old).
         Accessed only by Tier 2 (Claude Code) for backtesting/retraining.

STORAGE:
  Hot:  JSON file, overwritten each run → data/hot_cache.json
  Warm: SQLite (the existing callisto.db)
  Cold: Same SQLite tables, partitioned by date. Agents can't query them.
"""

import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import aiosqlite
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("callisto.cache_manager")

DB_PATH = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")
HOT_CACHE_PATH = os.getenv("CALLISTO_HOT_CACHE", "data/hot_cache.json")

# Budget: ~2000 tokens ≈ ~1500 words ≈ ~6000 chars
MAX_HOT_CACHE_CHARS = 6000


# ──────────────────────────────────────────────────
# HOT CACHE: ~2K tokens, injected into agent system prompt
# ──────────────────────────────────────────────────

async def build_hot_cache(db_path: str = DB_PATH) -> dict:
    """
    Called at start of every agent invocation.
    Produces a compact JSON payload that goes into the system prompt.

    Contains ONLY:
      - System identity (who Callisto is, what it does)
      - Current bankroll + today's P/L
      - Today's pending/active signals from AGP SIGNAL
      - Unresolved Sentinel flags
      - Today's date and active sports schedule summary

    Does NOT contain:
      - Historical CLV data (that's warm cache)
      - Past boost evaluations (warm)
      - Old simulation outputs (warm/cold)
      - Full conversation history (never)
    """
    hot = {
        "identity": {
            "system": "Callisto",
            "owner": "Marco Santangelo",
            "books": ["DraftKings", "Fanatics"],
            "reference": ["Pinnacle (via the-odds-api)"],
            "method": "Devig sharp books for true probability, compare to retail for mispricing",
            "primary_edge": "Profit boosts + player props",
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        },
    }

    try:
        async with aiosqlite.connect(db_path) as db:
            await db.execute("PRAGMA busy_timeout = 60000")
            # Current bankroll
            hot["bankroll"] = await _get_latest_bankroll(db)

            # Today's active signals (open bets)
            hot["active_bets"] = await _get_active_bets(db)

            # Unresolved flags from sentinel
            hot["sentinel_flags"] = await _get_unresolved_flags(db)

            # Quick performance snapshot
            hot["performance"] = await _get_performance_snapshot(db)

    except Exception as e:
        logger.error(f"Hot cache build error: {e}")

    # Write to disk for debugging / Tier 2 inspection
    try:
        os.makedirs(os.path.dirname(HOT_CACHE_PATH), exist_ok=True)
        with open(HOT_CACHE_PATH, "w") as f:
            json.dump(hot, f, indent=2, default=str)
    except Exception as e:
        logger.warning(f"Hot cache file write failed: {e}")

    return hot


def hot_cache_to_prompt(hot: dict) -> str:
    """
    Serializes hot cache to a compact string for injection into
    the agent's system prompt. Target: < 2000 tokens (~6000 chars).
    Truncates if necessary.
    """
    text = json.dumps(hot, indent=None, default=str)
    if len(text) > MAX_HOT_CACHE_CHARS:
        text = text[:MAX_HOT_CACHE_CHARS] + "..."
    return f"<context type=\"hot_cache\">\n{text}\n</context>"


async def _get_latest_bankroll(db: aiosqlite.Connection) -> dict:
    """Get current bankroll state."""
    try:
        row = await db.execute_fetchall(
            "SELECT balance, timestamp FROM bankroll ORDER BY timestamp DESC LIMIT 1"
        )
        if row:
            balance, ts = row[0]
            return {"balance": balance, "as_of": ts}
        return {"balance": "unknown", "as_of": None}
    except Exception:
        return {"balance": "unknown", "as_of": None}


async def _get_active_bets(db: aiosqlite.Connection) -> list:
    """Get currently open/pending bets — compact format."""
    try:
        rows = await db.execute_fetchall(
            "SELECT id, team, market, bookmaker, placement_odds, stake "
            "FROM bets WHERE result = 'pending' ORDER BY placed_at DESC LIMIT 10"
        )
        return [
            {
                "id": r[0], "team": r[1], "market": r[2],
                "book": r[3], "odds": r[4], "stake": r[5],
            }
            for r in rows
        ]
    except Exception:
        return []


async def _get_unresolved_flags(db: aiosqlite.Connection) -> list:
    """Get unresolved sentinel flags (divergences, warnings)."""
    try:
        # Check if the table exists
        check = await db.execute_fetchall(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sentinel_flags'"
        )
        if not check:
            return []
        rows = await db.execute_fetchall(
            "SELECT flag_type, description, created_at "
            "FROM sentinel_flags WHERE resolved = 0 ORDER BY created_at DESC LIMIT 5"
        )
        return [
            {"type": r[0], "desc": r[1], "at": r[2]}
            for r in rows
        ]
    except Exception:
        return []


async def _get_performance_snapshot(db: aiosqlite.Connection) -> dict:
    """Quick W/L/CLV snapshot — just the numbers, no details."""
    try:
        total = await db.execute_fetchall("SELECT COUNT(*) FROM bets")
        won = await db.execute_fetchall("SELECT COUNT(*) FROM bets WHERE result='won'")
        lost = await db.execute_fetchall("SELECT COUNT(*) FROM bets WHERE result='lost'")
        pending = await db.execute_fetchall("SELECT COUNT(*) FROM bets WHERE result='pending'")

        total_n = total[0][0] if total else 0
        won_n = won[0][0] if won else 0
        lost_n = lost[0][0] if lost else 0
        pending_n = pending[0][0] if pending else 0

        # P/L
        pl = await db.execute_fetchall(
            "SELECT COALESCE(SUM(CASE WHEN result='won' THEN payout - stake "
            "WHEN result='lost' THEN -stake ELSE 0 END), 0) FROM bets"
        )
        pnl = pl[0][0] if pl else 0

        # Average CLV
        clv = await db.execute_fetchall(
            "SELECT AVG(clv_implied) FROM bets WHERE clv_implied IS NOT NULL"
        )
        avg_clv = clv[0][0] if clv and clv[0][0] is not None else None

        result = {
            "record": f"{won_n}W-{lost_n}L",
            "pending": pending_n,
            "pnl": round(pnl, 2),
        }
        if won_n + lost_n > 0:
            result["win_rate"] = round(won_n / (won_n + lost_n) * 100, 1)
        if avg_clv is not None:
            result["avg_clv"] = round(avg_clv, 4)
            result["clv_status"] = "BEATING" if avg_clv > 0 else "BEHIND"

        return result
    except Exception:
        return {}


# ──────────────────────────────────────────────────
# WARM CACHE: Hermes function tools, returns summaries
# ──────────────────────────────────────────────────
# These are registered as Hermes-callable functions.
# Agents invoke them when they need historical context.
# KEY: return AGGREGATED SUMMARIES, not raw data.

async def query_clv_summary(
    db_path: str = DB_PATH,
    days_back: int = 30,
    sport: str = None,
    book: str = None,
) -> dict:
    """
    Returns CLV summary stats, NOT individual bet records.
    ~50 tokens in agent context instead of ~5000.
    """
    try:
        async with aiosqlite.connect(db_path) as db:
            await db.execute("PRAGMA busy_timeout = 60000")
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()
            where = ["placed_at > ?", "clv_implied IS NOT NULL"]
            params = [cutoff]

            if sport:
                where.append("sport = ?")
                params.append(sport)
            if book:
                where.append("bookmaker = ?")
                params.append(book)

            where_str = " AND ".join(where)

            # Total bets with CLV
            rows = await db.execute_fetchall(
                f"SELECT COUNT(*), AVG(clv_implied), "
                f"SUM(CASE WHEN result='won' THEN 1 ELSE 0 END), "
                f"SUM(CASE WHEN result='lost' THEN 1 ELSE 0 END), "
                f"COALESCE(SUM(CASE WHEN result='won' THEN payout - stake "
                f"WHEN result='lost' THEN -stake ELSE 0 END), 0) "
                f"FROM bets WHERE {where_str}",
                params,
            )

            if not rows or rows[0][0] == 0:
                return {"period": f"last_{days_back}_days", "total_bets": 0}

            count, avg_clv, wins, losses, pnl = rows[0]
            total_resolved = (wins or 0) + (losses or 0)

            # CLV by confidence tier
            clv_by_conf = await db.execute_fetchall(
                f"SELECT "
                f"CASE WHEN edge_at_placement >= 0.05 THEN 'high' "
                f"WHEN edge_at_placement >= 0.03 THEN 'medium' "
                f"ELSE 'low' END as tier, "
                f"AVG(clv_implied), COUNT(*) "
                f"FROM bets WHERE {where_str} GROUP BY tier",
                params,
            )

            return {
                "period": f"last_{days_back}_days",
                "total_bets": count,
                "avg_clv_cents": round((avg_clv or 0) * 100, 1),
                "win_rate": round(wins / total_resolved * 100, 1) if total_resolved > 0 else 0,
                "total_pnl": round(pnl, 2),
                "roi_pct": round(pnl / (count * 100) * 100, 1) if count > 0 else 0,
                "clv_by_confidence": {
                    r[0]: {"avg_cents": round((r[1] or 0) * 100, 1), "n": r[2]}
                    for r in (clv_by_conf or [])
                },
            }
    except Exception as e:
        logger.error(f"CLV summary query failed: {e}")
        return {"error": str(e)}


async def query_recent_signals(db_path: str = DB_PATH, n: int = 10) -> list:
    """
    Returns last N signals with outcomes.
    For Sentinel to spot-check recent performance.
    """
    try:
        async with aiosqlite.connect(db_path) as db:
            await db.execute("PRAGMA busy_timeout = 60000")
            rows = await db.execute_fetchall(
                "SELECT id, team, market, bookmaker, placement_odds, "
                "result, payout, stake, clv_implied, edge_at_placement "
                "FROM bets ORDER BY placed_at DESC LIMIT ?",
                (n,),
            )
            return [
                {
                    "id": r[0], "team": r[1], "market": r[2], "book": r[3],
                    "odds": r[4], "result": r[5], "payout": r[6], "stake": r[7],
                    "clv": round(r[8], 4) if r[8] is not None else None,
                    "edge": round(r[9], 4) if r[9] is not None else None,
                }
                for r in rows
            ]
    except Exception as e:
        logger.error(f"Recent signals query failed: {e}")
        return []


async def query_model_calibration(
    db_path: str = DB_PATH,
    sport: str = None,
    days_back: int = 30,
) -> dict:
    """
    Returns model calibration stats:
    - Predicted prob vs actual win rate (binned)
    - Mean absolute error
    - Bias direction (over/under-predicting?)

    Used by Sentinel for weekly calibration check.
    """
    try:
        async with aiosqlite.connect(db_path) as db:
            await db.execute("PRAGMA busy_timeout = 60000")
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()
            where = ["placed_at > ?", "result IN ('won', 'lost')", "placement_implied_prob IS NOT NULL"]
            params = [cutoff]

            if sport:
                where.append("sport = ?")
                params.append(sport)

            where_str = " AND ".join(where)

            rows = await db.execute_fetchall(
                f"SELECT placement_implied_prob, result FROM bets WHERE {where_str}",
                params,
            )

            if not rows or len(rows) < 5:
                return {"error": "Insufficient data for calibration", "n": len(rows) if rows else 0}

            # Bin by predicted probability
            bins = {}
            for prob, result in rows:
                bucket = round(prob * 10) / 10  # 0.0, 0.1, ..., 1.0
                bucket = max(0.0, min(1.0, bucket))
                if bucket not in bins:
                    bins[bucket] = {"total": 0, "won": 0}
                bins[bucket]["total"] += 1
                if result == "won":
                    bins[bucket]["won"] += 1

            calibration = {}
            total_error = 0
            total_bias = 0
            n = 0

            for bucket, data in sorted(bins.items()):
                if data["total"] >= 2:
                    actual = data["won"] / data["total"]
                    calibration[f"{bucket:.1f}"] = {
                        "predicted": bucket,
                        "actual": round(actual, 3),
                        "n": data["total"],
                        "error": round(abs(actual - bucket), 3),
                    }
                    total_error += abs(actual - bucket)
                    total_bias += (actual - bucket)
                    n += 1

            return {
                "period": f"last_{days_back}_days",
                "total_bets": len(rows),
                "mean_abs_error": round(total_error / n, 3) if n > 0 else None,
                "bias": round(total_bias / n, 3) if n > 0 else None,
                "bias_direction": (
                    "over-predicting" if total_bias > 0
                    else "under-predicting" if total_bias < 0
                    else "neutral"
                ) if n > 0 else "unknown",
                "bins": calibration,
            }
    except Exception as e:
        logger.error(f"Calibration query failed: {e}")
        return {"error": str(e)}


async def query_boost_history(
    db_path: str = DB_PATH,
    book: str = None,
    days_back: int = 30,
) -> dict:
    """
    Returns boost evaluation summary:
    - Total boosts evaluated, total acted on
    - Average EV of acted boosts
    - Actual P/L from boosts
    - Best/worst boost outcomes
    """
    try:
        async with aiosqlite.connect(db_path) as db:
            await db.execute("PRAGMA busy_timeout = 60000")
            # Check if boosts table exists
            check = await db.execute_fetchall(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='boosts'"
            )
            if not check:
                return {"total_evaluated": 0, "message": "No boosts table yet"}

            cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()
            where = ["created_at > ?"]
            params = [cutoff]

            if book:
                where.append("book = ?")
                params.append(book)

            where_str = " AND ".join(where)

            rows = await db.execute_fetchall(
                f"SELECT COUNT(*), "
                f"SUM(CASE WHEN evaluated = 1 THEN 1 ELSE 0 END), "
                f"SUM(CASE WHEN recommendation LIKE '%MAX%' OR recommendation LIKE '%BET%' THEN 1 ELSE 0 END), "
                f"AVG(CASE WHEN ev_percent > 0 THEN ev_percent END) "
                f"FROM boosts WHERE {where_str}",
                params,
            )

            if not rows or rows[0][0] == 0:
                return {"total_evaluated": 0}

            total, evaluated, acted, avg_ev = rows[0]

            # P/L from boost bets
            boost_bets = await db.execute_fetchall(
                f"SELECT COALESCE(SUM(CASE WHEN result='won' THEN payout - stake "
                f"WHEN result='lost' THEN -stake ELSE 0 END), 0), COUNT(*) "
                f"FROM bets WHERE tags LIKE '%boost%' AND placed_at > ?",
                (cutoff,),
            )
            boost_pnl = boost_bets[0][0] if boost_bets else 0
            boost_count = boost_bets[0][1] if boost_bets else 0

            return {
                "period": f"last_{days_back}_days",
                "total_evaluated": total,
                "acted_on": acted or 0,
                "avg_ev_pct": round(avg_ev, 2) if avg_ev else 0,
                "boost_bets_placed": boost_count,
                "boost_pnl": round(boost_pnl, 2),
            }
    except Exception as e:
        logger.error(f"Boost history query failed: {e}")
        return {"error": str(e)}


# ──────────────────────────────────────────────────
# CACHE ROTATION: Run daily after all agents complete
# ──────────────────────────────────────────────────

async def rotate_caches(db_path: str = DB_PATH, warm_days: int = 30):
    """
    Daily maintenance:
      1. Rebuild hot cache from current state
      2. Entries older than warm_days → cold (add 'archived' flag)
      3. Cold entries are still in SQLite but excluded from warm queries
         via WHERE clause: archived = FALSE AND date > cutoff

    This keeps warm queries fast and hot cache fresh.
    """
    try:
        async with aiosqlite.connect(db_path) as db:
            await db.execute("PRAGMA busy_timeout = 60000")
            cutoff = (datetime.now(timezone.utc) - timedelta(days=warm_days)).isoformat()

            # Ensure archived column exists on key tables
            for table in ["bets", "ev_opportunities", "line_movements", "odds_snapshots"]:
                try:
                    await db.execute(f"ALTER TABLE {table} ADD COLUMN archived BOOLEAN DEFAULT 0")
                except Exception:
                    pass  # Expected: column already exists (ALTER TABLE doesn't have IF NOT EXISTS)

            # Archive old entries
            for table in ["ev_opportunities", "line_movements", "odds_snapshots"]:
                try:
                    col = "detected_at" if table != "odds_snapshots" else "timestamp"
                    await db.execute(
                        f"UPDATE {table} SET archived = 1 WHERE {col} < ? AND archived = 0",
                        (cutoff,),
                    )
                except Exception as e:
                    logger.warning(f"Archive {table} failed: {e}")

            await db.commit()
            logger.info(f"Cache rotation complete: archived entries older than {warm_days} days")

    except Exception as e:
        logger.error(f"Cache rotation failed: {e}")

    # Rebuild hot cache
    await build_hot_cache(db_path)


# ──────────────────────────────────────────────────
# SENTINEL FLAGS TABLE (created on demand)
# ──────────────────────────────────────────────────

async def ensure_sentinel_table(db_path: str = DB_PATH):
    """Create sentinel_flags table if it doesn't exist."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout = 60000")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sentinel_flags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                flag_type TEXT NOT NULL,
                description TEXT NOT NULL,
                sport TEXT,
                market TEXT,
                severity TEXT DEFAULT 'warning',
                resolved BOOLEAN DEFAULT 0,
                resolved_at TEXT,
                created_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS boosts (
                boost_id TEXT PRIMARY KEY,
                book TEXT NOT NULL,
                description TEXT NOT NULL,
                boost_type TEXT NOT NULL,
                boosted_odds_american INTEGER,
                original_odds_american INTEGER,
                max_stake REAL,
                sport TEXT,
                date TEXT,
                evaluated BOOLEAN DEFAULT FALSE,
                ev_percent REAL,
                recommendation TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()


async def record_sentinel_flag(
    db_path: str = DB_PATH,
    flag_type: str = "divergence",
    description: str = "",
    sport: str = "",
    market: str = "",
    severity: str = "warning",
):
    """Record a sentinel flag for review."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout = 60000")
        await ensure_sentinel_table(db_path)
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            "INSERT INTO sentinel_flags (flag_type, description, sport, market, severity, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (flag_type, description, sport, market, severity, now),
        )
        await db.commit()


# ──────────────────────────────────────────────────
# BACKWARD COMPATIBILITY — drop-in for hermes_memory
# ──────────────────────────────────────────────────

class CacheManager:
    """
    Drop-in replacement for HermesMemory.
    Provides the same get_memory_context() interface but uses
    the tiered hot/warm/cold architecture.
    """

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._hot_cache: Optional[dict] = None
        self._hot_cache_time: float = 0
        self._hot_cache_ttl: float = 120  # Refresh every 2 minutes

    async def get_memory_context(self, force_refresh: bool = False) -> str:
        """
        Build the hot cache context string for injection into agent prompts.
        This is the ONLY thing that goes into the system prompt automatically.
        Warm cache is accessed via function calls, not auto-loaded.
        """
        now = time.time()
        if (
            self._hot_cache is not None
            and not force_refresh
            and (now - self._hot_cache_time) < self._hot_cache_ttl
        ):
            return hot_cache_to_prompt(self._hot_cache)

        self._hot_cache = await build_hot_cache(self.db_path)
        self._hot_cache_time = now
        return hot_cache_to_prompt(self._hot_cache)

    async def get_warm_data(self, query_type: str, **kwargs) -> dict:
        """
        Route warm cache queries. Agents call this via function calling
        when they need historical context beyond what's in the hot cache.
        """
        handlers = {
            "clv_summary": query_clv_summary,
            "recent_signals": query_recent_signals,
            "model_calibration": query_model_calibration,
            "boost_history": query_boost_history,
        }
        handler = handlers.get(query_type)
        if not handler:
            return {"error": f"Unknown warm cache query: {query_type}"}
        return await handler(db_path=self.db_path, **kwargs)


# Singleton
_instance: Optional[CacheManager] = None


def get_cache_manager() -> CacheManager:
    """Get the singleton CacheManager instance."""
    global _instance
    if _instance is None:
        _instance = CacheManager()
    return _instance
