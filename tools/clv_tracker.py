"""
Closing Line Value (CLV) tracker — the single most important metric.

If you consistently beat the closing line, you're +EV even during losing streaks.
CLV is the only reliable predictor of long-term profitability.

How it works:
1. Record the line when a bet is placed
2. Record the closing line (Pinnacle/sharp book) at game start
3. CLV = placement line - closing line (positive = you got a better number)

Sustained positive CLV = edge exists, regardless of short-term results.
This is how sharps measure themselves.
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import aiosqlite
from dotenv import load_dotenv

from tools.odds_api import calculate_implied_probability, calculate_ev
from tools import telegram
from tools.book_keys import canonicalize_book, canonicalize_book_set

load_dotenv()

logger = logging.getLogger("callisto.clv_tracker")

DB_PATH = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")


# Raw implied-prob numbers from two books carry DIFFERENT vig loads, so you
# cannot subtract them directly — a 1% gap may be entirely vig-difference,
# not signal. These per-book half-vig estimates let the devig routine produce
# a fair-probability estimate from a single leg. Values are rough field
# averages on two-way MLB/NBA markets; tuned for bias-reduction, not precision.
_BOOK_VIG_ESTIMATE: dict[str, float] = {
    "pinnacle": 0.025,
    "lowvig": 0.02,
    "circa": 0.03,
    "betfair_exchange": 0.02,
    "draftkings": 0.05,
    "fanduel": 0.05,
    "betmgm": 0.06,
    "caesars": 0.06,
    "fanatics": 0.05,
}

# Sources whose closing number we trust as the "real" market close. Anything
# else still gets logged, but close_reliable=False so analysis queries can
# filter it out. Stored as canonical keys; callers MUST canonicalize the
# incoming `closing_source` before membership-testing here — otherwise
# "Pinnacle", "pinnacle ", and "pinnacle" are three different values and
# close_reliable will be wrong for two of them.
_RELIABLE_CLOSE_SOURCES: frozenset[str] = canonicalize_book_set(
    {"pinnacle", "lowvig.ag", "circa", "betfair_exchange", "Betfair Exchange"}
)


def _half_vig_devig(implied: Optional[float], vig: float) -> Optional[float]:
    """Half-vig approximation: fair = implied / (1 + vig/2). Bounded to (0,1).

    Returns the input untouched for non-positive or None values so call sites
    can safely chain it without extra guards.
    """
    try:
        if implied is None or implied <= 0:
            return implied
        return max(0.0, min(1.0, float(implied) / (1.0 + max(0.0, vig) / 2.0)))
    except (TypeError, ValueError):
        return implied


def _regime_stamp(sport: str) -> Optional[str]:
    """Return a compact ``<sport>|<season_phase>`` stamp or None on failure.

    Uses ``_classify_phase`` (pure date-math, no DB) rather than
    ``detect_regime`` so the stamp computation never opens a separate DB
    connection while a write is in flight on the primary aiosqlite one —
    cross-connection contention would otherwise stall a bet resolution
    under concurrent load. Callers write this into
    ``clv_log.regime_phase_at_placement`` so downstream analysis can bucket
    CLV by regime. Any error degrades to None so CLV writes never fail
    due to regime lookup.
    """
    if not sport:
        return None
    try:
        from tools.market_regime import (
            _classify_phase as _mr_classify,
            _canonical_sport as _mr_canon,
        )
        from datetime import date as _date
        sp_norm = _mr_canon(sport)
        phase, _win, _bounds = _mr_classify(sp_norm, _date.today())
        return f"{sp_norm}|{phase}"
    except Exception as e:
        logger.debug(f"regime_stamp failed for {sport!r}: {e}")
        return None


def _american_to_decimal(odds: Optional[int]) -> Optional[float]:
    """American → decimal odds. None/0 → None (can't convert)."""
    if odds is None:
        return None
    try:
        o = int(odds)
    except (TypeError, ValueError):
        return None
    if o == 0:
        return None
    if o > 0:
        return 1.0 + o / 100.0
    return 1.0 + 100.0 / abs(o)


class CLVTracker:
    """Track bets and measure CLV against closing lines."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def initialize(self) -> None:
        """Create bet tracking tables."""
        self._db = await aiosqlite.connect(self.db_path)
        # Tag for WriteCoordinator routing (single-writer pattern).
        from tools.db_writer import tag_connection as _tag
        _tag(self._db, self.db_path)
        await self._db.execute("PRAGMA busy_timeout = 60000")
        # SECURITY (audit C-6): per-statement DDL avoids the EXCLUSIVE lock that
        # executescript() takes for the duration of the whole script.
        for stmt in (
            """CREATE TABLE IF NOT EXISTS bets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                placed_at TEXT NOT NULL,
                sport TEXT NOT NULL,
                event_id TEXT,
                game_description TEXT,
                bet_type TEXT NOT NULL,
                team TEXT,
                market TEXT NOT NULL,
                bookmaker TEXT NOT NULL,
                placement_odds INTEGER NOT NULL,
                placement_point REAL,
                placement_implied_prob REAL,
                closing_odds INTEGER,
                closing_point REAL,
                closing_implied_prob REAL,
                closing_source TEXT DEFAULT 'pinnacle',
                clv_odds INTEGER,
                clv_implied REAL,
                stake REAL DEFAULT 100,
                result TEXT DEFAULT 'pending',
                payout REAL,
                edge_at_placement REAL,
                kelly_at_placement REAL,
                notes TEXT,
                tags TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS bankroll (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                balance REAL NOT NULL,
                change REAL,
                bet_id INTEGER,
                description TEXT,
                FOREIGN KEY (bet_id) REFERENCES bets(id)
            )""",
            """CREATE TABLE IF NOT EXISTS closing_lines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL,
                sport TEXT,
                captured_at TEXT NOT NULL,
                source TEXT DEFAULT 'pinnacle',
                market TEXT,
                team TEXT,
                closing_odds INTEGER,
                closing_point REAL,
                closing_implied REAL
            )""",
            "CREATE INDEX IF NOT EXISTS idx_bets_result ON bets(result, placed_at)",
            "CREATE INDEX IF NOT EXISTS idx_bets_sport ON bets(sport, placed_at)",
            "CREATE INDEX IF NOT EXISTS idx_closing_event ON closing_lines(event_id, market)",
            "CREATE INDEX IF NOT EXISTS idx_bankroll_ts ON bankroll(timestamp)",
        ):
            await self._db.execute(stmt)
        await self._db.commit()
        logger.info("CLV tracker initialized")

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    async def record_bet(
        self,
        sport: str,
        game_description: str,
        team: str,
        market: str,
        bookmaker: str,
        placement_odds: int,
        placement_point: Optional[float] = None,
        stake: float = 100,
        event_id: str = "",
        edge_estimate: Optional[float] = None,
        notes: str = "",
        tags: str = "",
    ) -> int:
        """
        Record a bet at placement time.

        Returns the bet ID for later CLV measurement.
        """
        now = datetime.now(timezone.utc).isoformat()
        implied = calculate_implied_probability(placement_odds)

        # Calculate Kelly if we have an edge estimate
        kelly = 0.0
        if edge_estimate and edge_estimate > 0:
            ev_result = calculate_ev(
                probability=implied + edge_estimate,
                american_odds=placement_odds,
                stake=stake,
            )
            kelly = ev_result.get("kelly_fraction", 0)

        from tools.db_utils import execute_with_retry, commit_with_retry
        cursor = await execute_with_retry(
            self._db,
            "INSERT INTO bets "
            "(placed_at, sport, event_id, game_description, bet_type, team, market, "
            "bookmaker, placement_odds, placement_point, placement_implied_prob, "
            "stake, edge_at_placement, kelly_at_placement, notes, tags) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                now, sport, event_id, game_description, "single", team, market,
                bookmaker, placement_odds, placement_point, round(implied, 4),
                stake, edge_estimate, round(kelly, 4), notes, tags,
            ),
            max_retries=10,
            operation="clv_tracker record_bet",
        )
        await commit_with_retry(self._db, max_retries=10, operation="clv_tracker record_bet")
        bet_id = cursor.lastrowid

        logger.info(
            f"Bet #{bet_id} recorded: {team} {market} @ {placement_odds} "
            f"({bookmaker}) implied={implied:.1%}"
        )
        return bet_id

    async def record_closing_line(
        self,
        event_id: str,
        market: str,
        team: str,
        closing_odds: int,
        closing_point: Optional[float] = None,
        source: str = "pinnacle",
        sport: str = "",
    ) -> None:
        """Record the closing line for an event from a sharp source."""
        now = datetime.now(timezone.utc).isoformat()
        implied = calculate_implied_probability(closing_odds)

        from tools.db_utils import execute_with_retry, commit_with_retry
        await execute_with_retry(
            self._db,
            "INSERT INTO closing_lines "
            "(event_id, sport, captured_at, source, market, team, "
            "closing_odds, closing_point, closing_implied) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (event_id, sport, now, source, market, team,
             closing_odds, closing_point, round(implied, 4)),
            max_retries=10,
            operation="clv_tracker record_closing_line insert",
        )

        # Update any pending bets for this event. LOWER()-based team match —
        # closing-line capture speaks odds-api-io's casing ("Pinnacle",
        # "Boston Celtics"), bet_executor wrote the team string as received
        # from whoever called it, and a single-letter case difference silently
        # drops every CLV update. Without this, bets stay NULL-closed forever.
        await execute_with_retry(
            self._db,
            "UPDATE bets SET "
            "closing_odds = ?, closing_point = ?, closing_implied_prob = ?, "
            "closing_source = ?, "
            "clv_odds = placement_odds - ?, "
            "clv_implied = ? - placement_implied_prob "
            "WHERE event_id = ? AND market = ? "
            "AND LOWER(team) = LOWER(?) AND result = 'pending'",
            (closing_odds, closing_point, round(implied, 4), source,
             closing_odds, round(implied, 4),
             event_id, market, team),
            max_retries=10,
            operation="clv_tracker record_closing_line update",
        )

        # Same LOWER() match for paper trades so their CLV backfill stays
        # in sync with the real-bet path.
        await execute_with_retry(
            self._db,
            "UPDATE paper_trades SET "
            "closing_odds = ?, closing_implied = ?, "
            "clv_implied = ? - signal_implied_prob "
            "WHERE event_id = ? AND market = ? "
            "AND LOWER(side) = LOWER(?) "
            "AND signal_implied_prob IS NOT NULL",
            (closing_odds, round(implied, 4),
             round(implied, 4),
             event_id, market, team),
            max_retries=10,
            operation="clv_tracker record_closing_line paper_trades",
        )

        await commit_with_retry(self._db, max_retries=10, operation="clv_tracker record_closing_line")

        logger.info(
            f"Closing line recorded: {team} {market} @ {closing_odds} "
            f"(source={source}, implied={implied:.1%})"
        )

    async def resolve_bet(
        self,
        bet_id: int,
        result: str,
        payout: Optional[float] = None,
    ) -> dict:
        """
        Resolve a bet as won/lost/push.

        Computes CLV metrics and logs to clv_log table — the permanent record
        of whether our signals beat the closing line.

        Args:
            bet_id: The bet ID
            result: 'won', 'lost', 'push'
            payout: Actual payout (if won)
        """
        cursor = await self._db.execute(
            "SELECT * FROM bets WHERE id = ?", (bet_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return {"error": f"Bet #{bet_id} not found"}

        cols = [d[0] for d in cursor.description]
        bet = dict(zip(cols, row))

        from tools.db_utils import execute_with_retry, commit_with_retry
        await execute_with_retry(
            self._db,
            "UPDATE bets SET result = ?, payout = ? WHERE id = ?",
            (result, payout, bet_id),
            max_retries=10,
            operation="clv_tracker resolve_bet update",
        )

        # Update bankroll
        if result == "won" and payout:
            change = payout - bet["stake"]
        elif result == "lost":
            change = -bet["stake"]
        else:
            change = 0

        if change != 0:
            bal_cursor = await self._db.execute(
                "SELECT balance FROM bankroll ORDER BY timestamp DESC LIMIT 1"
            )
            bal_row = await bal_cursor.fetchone()
            current_balance = bal_row[0] if bal_row else 0

            now = datetime.now(timezone.utc).isoformat()
            await execute_with_retry(
                self._db,
                "INSERT INTO bankroll (timestamp, balance, change, bet_id, description) "
                "VALUES (?, ?, ?, ?, ?)",
                (now, current_balance + change, change, bet_id,
                 f"Bet #{bet_id} {result}: {bet['game_description']}"),
                max_retries=10,
                operation="clv_tracker resolve_bet bankroll",
            )

        # ── CLV LOG ──
        # This is THE permanent record. Every resolved bet gets a clv_log entry.
        await self._log_clv(bet, result, payout, change)

        await commit_with_retry(self._db, max_retries=10, operation="clv_tracker resolve_bet")

        logger.info(f"Bet #{bet_id} resolved: {result}, change={change}")

        # Telegram notification
        await telegram.alert_bet_result(
            bet_id=bet_id,
            game=bet.get("game_description", ""),
            team=bet.get("team", ""),
            result=result,
            placement_odds=bet.get("placement_odds", 0),
            stake=bet.get("stake", 0),
            payout=payout,
            clv_implied=bet.get("clv_implied"),
        )

        return {"bet_id": bet_id, "result": result, "payout": payout, "change": change}

    async def _log_clv(
        self,
        bet: dict,
        result: str,
        payout: Optional[float],
        change: float,
    ) -> None:
        """
        Write CLV metrics to the clv_log table.

        This is called on every bet resolution. The clv_log is the permanent,
        append-only record of signal quality. If we consistently show positive
        CLV, the edge is real regardless of short-term variance.
        """
        bet_id = bet.get("id")
        placement_odds = bet.get("placement_odds")
        closing_odds = bet.get("closing_odds")
        closing_implied = bet.get("closing_implied_prob")

        our_decimal = _american_to_decimal(placement_odds)

        # Canonicalize books BEFORE any lookup — 'Betfair Exchange',
        # 'betfair exchange', 'betfair_exchange' must all collapse onto
        # one key. Previously `closing_source.lower()` left spaces
        # intact and dropped matches with the odds-api.io underscore form.
        closing_source = canonicalize_book(bet.get("closing_source") or "pinnacle")
        bookmaker = canonicalize_book(bet.get("bookmaker") or "")
        closing_vig = _BOOK_VIG_ESTIMATE.get(closing_source, 0.025)
        placement_vig = _BOOK_VIG_ESTIMATE.get(bookmaker, 0.05)

        pinnacle_fair_prob = _half_vig_devig(closing_implied, closing_vig)
        pinnacle_fair_decimal = None
        if pinnacle_fair_prob and pinnacle_fair_prob > 0:
            pinnacle_fair_decimal = 1 / pinnacle_fair_prob

        # CANONICAL CLV UNIT: probability-basis-points.
        #   clv_prob_bp = (closing_implied_prob - placement_implied_prob) * 10000
        # Positive = we got a better implied price than the close.
        # This is the ONLY supported unit going forward. The legacy
        # clv_cents column is preserved for backward-compat readers but
        # marked deprecated — it previously held American-point deltas
        # on one path (line 414) and prob×10000 on another (line 419),
        # producing a column with mixed units that silently poisoned
        # every aggregate.
        raw_placement_implied = bet.get("placement_implied_prob", 0)
        placement_fair = (
            _half_vig_devig(raw_placement_implied, placement_vig)
            if raw_placement_implied else None
        )

        clv_prob_bp = None
        if pinnacle_fair_prob is not None and placement_fair is not None:
            clv_prob_bp = round((pinnacle_fair_prob - placement_fair) * 10000, 1)

        # Legacy clv_cents: populate with prob-bp for NEW rows so mixed-units
        # data stops accruing. Historical rows keep whatever they have.
        clv_cents = clv_prob_bp

        actual_pnl = change
        close_reliable = (
            closing_odds is not None
            and closing_source in _RELIABLE_CLOSE_SOURCES
        )

        now = datetime.now(timezone.utc).isoformat()

        # Regime stamp at placement (feat/regime-aware-sizing, 2026-04-22).
        # Format: "<sport>|<season_phase>" — compact so downstream GROUP BY
        # queries can split on '|' to get both dimensions. Errors degrade
        # to None (no stamp) so clv_log writes never fail on regime lookup.
        regime_stamp = _regime_stamp(bet.get("sport", ""))

        try:
            from tools.db_utils import execute_with_retry
            await execute_with_retry(
                self._db,
                "INSERT OR REPLACE INTO clv_log "
                "(bet_id, event, outcome, point, book, our_odds_decimal, "
                "pinnacle_close_fair_prob, pinnacle_close_fair_decimal, "
                "clv_cents, clv_prob_bp, actual_result, actual_pnl, "
                "close_reliable, logged_at, regime_phase_at_placement) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(bet_id),
                    bet.get("event_id", ""),
                    bet.get("team", ""),
                    bet.get("placement_point"),
                    # Store canonicalized bookmaker so downstream group-by
                    # queries don't fragment on casing.
                    canonicalize_book(bet.get("bookmaker", "")),
                    our_decimal,
                    pinnacle_fair_prob,
                    pinnacle_fair_decimal,
                    clv_cents,
                    clv_prob_bp,
                    result,
                    actual_pnl,
                    close_reliable,
                    now,
                    regime_stamp,
                ),
                max_retries=10,
                operation="clv_tracker log_clv",
            )
            logger.info(
                f"CLV logged: bet #{bet_id}, clv_prob_bp={clv_prob_bp}, "
                f"result={result}, pnl={actual_pnl}, reliable={close_reliable}, "
                f"regime={regime_stamp}"
            )
        except Exception as e:
            logger.warning(f"Failed to log CLV for bet #{bet_id}: {e}")

    async def log_paper_trade_clv(self, trade: dict) -> bool:
        """Write a resolved paper trade to ``clv_log`` — same schema as real bets.

        Paper trades are Callisto's only bet-like data while the real executor
        is disabled. Without this entry, the clv_log — our "permanent record
        of signal quality" — stays empty and every promotion gate that
        consults it has nothing to grade. bet_id is namespaced ``pt:<trade_id>``
        so paper IDs never collide with real bet integer ids.

        Returns True if a row was written, False if the trade lacked the
        minimum inputs (no signal implied prob, or no actual_result).
        """
        if not trade.get("actual_result"):
            return False
        signal_imp = trade.get("signal_implied_prob")
        if signal_imp is None:
            return False

        trade_id = trade.get("trade_id")
        if not trade_id:
            return False
        bet_key = f"pt:{trade_id}"

        signal_odds = trade.get("signal_odds_american")
        closing_odds = trade.get("closing_odds")
        closing_implied = trade.get("closing_implied")
        bookmaker = canonicalize_book(trade.get("book") or "")

        our_decimal = _american_to_decimal(signal_odds)

        # Paper trades don't store which book supplied the close — the
        # backfill in data_collector prefers Pinnacle/LowVig, so treat
        # closing_implied as already close to fair and use a sharp-tier vig.
        placement_vig = _BOOK_VIG_ESTIMATE.get(bookmaker, 0.05)
        closing_vig = 0.025

        signal_fair = _half_vig_devig(signal_imp, placement_vig)
        close_fair = _half_vig_devig(closing_implied, closing_vig)
        close_fair_decimal = (1 / close_fair) if close_fair and close_fair > 0 else None

        # CANONICAL CLV UNIT: prob-basis-points. See _log_clv for rationale.
        # The legacy American-points path (signal_odds - closing_odds) is
        # intentionally removed: it made clv_cents incompatible with the
        # other write path and silently poisoned every aggregate that
        # grouped over bet_id.
        clv_prob_bp = None
        if close_fair is not None and signal_fair is not None:
            clv_prob_bp = round((close_fair - signal_fair) * 10000, 1)
        clv_cents = clv_prob_bp

        # We don't know the closing source for paper trades, only that the
        # backfill prefers sharp books when available. Mark reliable iff a
        # closing number was actually matched — this mirrors how downstream
        # queries treat "unknown but present" close data.
        close_reliable = closing_odds is not None or closing_implied is not None

        now = datetime.now(timezone.utc).isoformat()

        regime_stamp = _regime_stamp(trade.get("sport", ""))

        try:
            from tools.db_utils import execute_with_retry
            await execute_with_retry(
                self._db,
                "INSERT OR REPLACE INTO clv_log "
                "(bet_id, event, outcome, point, book, our_odds_decimal, "
                "pinnacle_close_fair_prob, pinnacle_close_fair_decimal, "
                "clv_cents, clv_prob_bp, actual_result, actual_pnl, "
                "close_reliable, logged_at, regime_phase_at_placement) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    bet_key,
                    trade.get("event_id", ""),
                    trade.get("side", ""),
                    trade.get("line"),
                    bookmaker,
                    our_decimal,
                    close_fair,
                    close_fair_decimal,
                    clv_cents,
                    clv_prob_bp,
                    trade.get("actual_result"),
                    trade.get("hypothetical_pnl"),
                    close_reliable,
                    now,
                    regime_stamp,
                ),
                max_retries=10,
                operation="clv_tracker log_paper_trade_clv",
            )
            return True
        except Exception as e:
            logger.warning(f"Failed to log paper-trade CLV for {trade_id}: {e}")
            return False

    async def sync_paper_trades_to_clv_log(self, limit: Optional[int] = None) -> int:
        """Idempotent: copy every resolved paper_trade missing from clv_log.

        Finds rows where ``actual_result`` is populated but the corresponding
        ``clv_log`` entry (``pt:<trade_id>``) doesn't exist. Safe to call on
        every resolution pass — the anti-join skips already-logged trades and
        the INSERT uses INSERT OR REPLACE as a final safeguard.

        Returns the count of clv_log rows written.
        """
        sql = (
            "SELECT pt.* FROM paper_trades pt "
            "WHERE pt.actual_result IS NOT NULL "
            "AND pt.signal_implied_prob IS NOT NULL "
            "AND NOT EXISTS ("
            "    SELECT 1 FROM clv_log cl WHERE cl.bet_id = 'pt:' || pt.trade_id"
            ")"
        )
        if limit and limit > 0:
            sql += f" LIMIT {int(limit)}"

        cursor = await self._db.execute(sql)
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]

        written = 0
        for row in rows:
            trade = dict(zip(cols, row))
            if await self.log_paper_trade_clv(trade):
                written += 1

        if written > 0:
            from tools.db_utils import commit_with_retry
            await commit_with_retry(
                self._db,
                max_retries=10,
                operation="clv_tracker sync_paper_trades_to_clv_log",
            )
            logger.info(
                f"clv_log sync: wrote {written} paper-trade entries "
                f"(of {len(rows)} candidates)"
            )
        return written

    async def backfill_clv_log(self) -> int:
        """
        Backfill clv_log for all resolved bets that don't have entries.

        Call this once to populate clv_log from existing bets data.
        Safe to call multiple times (INSERT OR REPLACE).
        """
        cursor = await self._db.execute(
            "SELECT * FROM bets WHERE result IN ('won', 'lost', 'push')"
        )
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]

        count = 0
        for row in rows:
            bet = dict(zip(cols, row))
            result = bet["result"]
            payout = bet.get("payout")
            stake = bet.get("stake", 100)
            if result == "won" and payout:
                change = payout - stake
            elif result == "lost":
                change = -stake
            else:
                change = 0
            await self._log_clv(bet, result, payout, change)
            count += 1

        from tools.db_utils import commit_with_retry
        await commit_with_retry(self._db, max_retries=10, operation="clv_tracker backfill_clv_log")

        # Paper trades are the dominant source of bet-like data today (real
        # executor is off). Sweep them in the same pass so a single call to
        # backfill_clv_log leaves clv_log fully caught up.
        paper_written = await self.sync_paper_trades_to_clv_log()

        logger.info(
            f"Backfilled CLV log: {count} real bets, {paper_written} paper trades"
        )
        return count + paper_written

    async def get_clv_report(self, sport: Optional[str] = None, days: int = 30) -> dict:
        """
        Generate CLV performance report.

        This is THE metric. Sustained positive CLV = you have an edge.
        """
        where = "WHERE closing_odds IS NOT NULL"
        params = []
        if sport:
            where += " AND sport = ?"
            params.append(sport)

        cursor = await self._db.execute(
            f"SELECT * FROM bets {where} ORDER BY placed_at DESC",
            params,
        )
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        bets = [dict(zip(cols, row)) for row in rows]

        if not bets:
            return {
                "total_bets": 0,
                "message": "No bets with closing lines recorded yet",
            }

        # CLV analysis
        clv_values = []
        clv_implied_values = []
        won = lost = push = pending = 0
        total_staked = 0
        total_returned = 0

        for bet in bets:
            if bet.get("clv_implied") is not None:
                clv_implied_values.append(bet["clv_implied"])
            if bet.get("clv_odds") is not None:
                clv_values.append(bet["clv_odds"])

            if bet["result"] == "won":
                won += 1
                total_returned += bet.get("payout", 0)
            elif bet["result"] == "lost":
                lost += 1
            elif bet["result"] == "push":
                push += 1
            else:
                pending += 1

            total_staked += bet.get("stake", 0)

        avg_clv_odds = sum(clv_values) / len(clv_values) if clv_values else 0
        avg_clv_implied = sum(clv_implied_values) / len(clv_implied_values) if clv_implied_values else 0
        positive_clv_rate = sum(1 for v in clv_implied_values if v > 0) / len(clv_implied_values) if clv_implied_values else 0

        roi = ((total_returned - total_staked) / total_staked * 100) if total_staked > 0 else 0

        return {
            "total_bets": len(bets),
            "with_closing_line": len(clv_values),
            "results": {
                "won": won,
                "lost": lost,
                "push": push,
                "pending": pending,
                "win_rate": round(won / (won + lost) * 100, 1) if (won + lost) > 0 else 0,
            },
            "clv": {
                "avg_clv_odds": round(avg_clv_odds, 1),
                "avg_clv_implied": round(avg_clv_implied, 4),
                "avg_clv_implied_pct": round(avg_clv_implied * 100, 2),
                "positive_clv_rate": round(positive_clv_rate * 100, 1),
                "interpretation": _interpret_clv(avg_clv_implied),
            },
            "financials": {
                "total_staked": round(total_staked, 2),
                "total_returned": round(total_returned, 2),
                "profit_loss": round(total_returned - total_staked, 2),
                "roi_pct": round(roi, 2),
            },
            "recent_bets": bets[:10],
        }

    async def get_bankroll_history(self, limit: int = 50) -> list[dict]:
        """Get bankroll balance history."""
        cursor = await self._db.execute(
            "SELECT * FROM bankroll ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in rows]

    async def set_initial_bankroll(self, balance: float) -> None:
        """Set initial bankroll balance."""
        now = datetime.now(timezone.utc).isoformat()
        from tools.db_utils import execute_with_retry, commit_with_retry
        await execute_with_retry(
            self._db,
            "INSERT INTO bankroll (timestamp, balance, change, description) "
            "VALUES (?, ?, ?, ?)",
            (now, balance, balance, "Initial bankroll"),
            max_retries=10,
            operation="clv_tracker set_initial_bankroll",
        )
        await commit_with_retry(self._db, max_retries=10, operation="clv_tracker set_initial_bankroll")
        logger.info(f"Initial bankroll set: ${balance}")

    async def forecast_clv(
        self,
        bet_id: Optional[int] = None,
        sport: Optional[str] = None,
    ) -> list[dict]:
        """Forecast CLV for pending bets using predict_closing_line.

        For each pending bet, estimates the closing line and pre-game CLV.
        This is useful for paper-trading evaluation: know whether your bet
        is likely +CLV before the game even starts.

        Returns a list of dicts, one per bet, with the forecasted CLV.
        """
        from tools.market_psychology import predict_closing_line
        from datetime import datetime as _dt

        where = "WHERE result = 'pending'"
        params: list = []
        if bet_id is not None:
            where += " AND id = ?"
            params.append(bet_id)
        if sport:
            where += " AND sport = ?"
            params.append(sport)

        cursor = await self._db.execute(
            f"SELECT * FROM bets {where} ORDER BY placed_at DESC LIMIT 50",
            params,
        )
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        bets = [dict(zip(cols, row)) for row in rows]

        forecasts = []
        now = datetime.now(timezone.utc)

        for bet in bets:
            point = bet.get("placement_point")
            if point is None:
                # Moneyline bets don't have a point — skip CLV point forecast
                forecasts.append({
                    "bet_id": bet["id"],
                    "sport": bet.get("sport", ""),
                    "team": bet.get("team", ""),
                    "market": bet.get("market", ""),
                    "forecast_skipped": True,
                    "reason": "No placement point (moneyline) — CLV forecast requires spread/total",
                })
                continue

            bet_sport = bet.get("sport", "basketball_nba")
            bet_market = bet.get("market", "spreads")

            # Estimate hours to game — use placed_at timestamp + typical lead time
            # In reality the game start time would be in the events table; we
            # approximate with a default of 4 hours if we can't determine it.
            hours_to_game = 4.0  # conservative default

            try:
                prediction = predict_closing_line(
                    current_line=point,
                    hours_to_game=hours_to_game,
                    sport=bet_sport,
                    market=bet_market,
                    current_price=bet.get("placement_odds"),
                )

                forecasts.append({
                    "bet_id": bet["id"],
                    "sport": bet_sport,
                    "team": bet.get("team", ""),
                    "market": bet_market,
                    "placement_odds": bet.get("placement_odds"),
                    "placement_point": point,
                    "predicted_closing_line": prediction.get("predicted_close"),
                    "expected_movement": prediction.get("expected_movement"),
                    "prediction_confidence": prediction.get("prediction_confidence"),
                    "clv_estimate": prediction.get("clv_estimate"),
                    "recommendation": prediction.get("recommendation"),
                    "confidence_interval_68": prediction.get("confidence_interval_68"),
                })
            except Exception as e:
                logger.warning(f"CLV forecast failed for bet #{bet['id']}: {e}")
                forecasts.append({
                    "bet_id": bet["id"],
                    "forecast_skipped": True,
                    "reason": str(e),
                })

        return forecasts

    async def get_all_bets(
        self,
        result: Optional[str] = None,
        sport: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]:
        """Get bet history with optional filters."""
        where_clauses = []
        params = []
        if result:
            where_clauses.append("result = ?")
            params.append(result)
        if sport:
            where_clauses.append("sport = ?")
            params.append(sport)

        where = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""
        cursor = await self._db.execute(
            f"SELECT * FROM bets {where} ORDER BY placed_at DESC LIMIT ?",
            params + [limit],
        )
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in rows]


def _interpret_clv(avg_clv_implied: float) -> str:
    """Interpret CLV performance."""
    if avg_clv_implied > 0.03:
        return "STRONG EDGE — consistently beating closing lines by 3%+. Sharp-level performance."
    elif avg_clv_implied > 0.015:
        return "POSITIVE EDGE — beating closing lines. Maintain approach, scale cautiously."
    elif avg_clv_implied > 0.005:
        return "SLIGHT EDGE — marginally beating close. Edge exists but thin. Increase volume."
    elif avg_clv_implied > -0.005:
        return "BREAK EVEN — tracking close to closing lines. No clear edge yet."
    elif avg_clv_implied > -0.015:
        return "SLIGHT NEGATIVE — slightly behind closing lines. Review bet selection process."
    else:
        return "NEGATIVE — consistently worse than closing lines. Current approach is -EV."
