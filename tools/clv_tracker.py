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

load_dotenv()

logger = logging.getLogger("callisto.clv_tracker")

DB_PATH = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")


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

        # Update any pending bets for this event
        await execute_with_retry(
            self._db,
            "UPDATE bets SET "
            "closing_odds = ?, closing_point = ?, closing_implied_prob = ?, "
            "closing_source = ?, "
            "clv_odds = placement_odds - ?, "
            "clv_implied = ? - placement_implied_prob "
            "WHERE event_id = ? AND market = ? AND team = ? AND result = 'pending'",
            (closing_odds, closing_point, round(implied, 4), source,
             closing_odds, round(implied, 4),
             event_id, market, team),
            max_retries=10,
            operation="clv_tracker record_closing_line update",
        )

        # Update any pending paper trades for this event
        await execute_with_retry(
            self._db,
            "UPDATE paper_trades SET "
            "closing_odds = ?, closing_implied = ?, "
            "clv_implied = ? - signal_implied_prob "
            "WHERE event_id = ? AND market = ? AND side = ? "
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
        stake = bet.get("stake", 100)

        # Convert placement odds to decimal
        if placement_odds and abs(placement_odds) > 0:
            if placement_odds > 0:
                our_decimal = 1 + placement_odds / 100
            else:
                our_decimal = 1 + 100 / abs(placement_odds)
        else:
            our_decimal = None

        # SECURITY (audit H-3): the raw closing_implied / placement_implied probs
        # both still contain their respective books' vig. Comparing them yields
        # apples-to-oranges (DraftKings ~5% vig vs Pinnacle ~2.5%), biasing CLV
        # downward. We devig BOTH sides to a fair probability estimate before
        # computing the spread. Without both legs of the market in the row, we
        # use a half-vig two-way approximation, which is conservative but stops
        # the systematic bias.
        _BOOK_VIG_ESTIMATE = {
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

        def _devig_one_side(implied: float, vig: float) -> float:
            """Half-vig approximation: fair = implied / (1 + vig/2). Bounded to (0,1)."""
            try:
                if implied is None or implied <= 0:
                    return implied
                return max(0.0, min(1.0, float(implied) / (1.0 + max(0.0, vig) / 2.0)))
            except (TypeError, ValueError):
                return implied

        closing_source = (bet.get("closing_source") or "pinnacle").lower()
        bookmaker = (bet.get("bookmaker") or "").lower()
        closing_vig = _BOOK_VIG_ESTIMATE.get(closing_source, 0.025)
        placement_vig = _BOOK_VIG_ESTIMATE.get(bookmaker, 0.05)

        # Compute Pinnacle closing fair probability (devigged)
        pinnacle_fair_prob = _devig_one_side(closing_implied, closing_vig)
        pinnacle_fair_decimal = None
        if pinnacle_fair_prob and pinnacle_fair_prob > 0:
            pinnacle_fair_decimal = 1 / pinnacle_fair_prob

        # CLV in cents: positive = we got a better number
        clv_cents = None
        if closing_odds is not None and placement_odds is not None:
            # American-cents stays a useful first-order proxy and isn't biased by vig.
            clv_cents = placement_odds - closing_odds
        elif closing_implied is not None and our_decimal:
            # Compute from devigged implied: fair-edge vs fair-closing
            raw_placement_implied = bet.get("placement_implied_prob", 0)
            if raw_placement_implied:
                placement_fair = _devig_one_side(raw_placement_implied, placement_vig)
                clv_cents = round((pinnacle_fair_prob - placement_fair) * 10000, 1)

        # Actual PnL
        actual_pnl = change

        # Determine if closing line is reliable (from sharp source)
        close_reliable = closing_odds is not None and bet.get("closing_source") in (
            "pinnacle", "lowvig", "circa", "betfair_exchange"
        )

        now = datetime.now(timezone.utc).isoformat()

        try:
            from tools.db_utils import execute_with_retry
            await execute_with_retry(
                self._db,
                "INSERT OR REPLACE INTO clv_log "
                "(bet_id, event, outcome, point, book, our_odds_decimal, "
                "pinnacle_close_fair_prob, pinnacle_close_fair_decimal, "
                "clv_cents, actual_result, actual_pnl, close_reliable, logged_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(bet_id),
                    bet.get("event_id", ""),
                    bet.get("team", ""),
                    bet.get("placement_point"),
                    bet.get("bookmaker", ""),
                    our_decimal,
                    pinnacle_fair_prob,
                    pinnacle_fair_decimal,
                    clv_cents,
                    result,
                    actual_pnl,
                    close_reliable,
                    now,
                ),
                max_retries=10,
                operation="clv_tracker log_clv",
            )
            logger.info(
                f"CLV logged: bet #{bet_id}, clv_cents={clv_cents}, "
                f"result={result}, pnl={actual_pnl}, reliable={close_reliable}"
            )
        except Exception as e:
            logger.warning(f"Failed to log CLV for bet #{bet_id}: {e}")

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
        logger.info(f"Backfilled CLV log for {count} resolved bets")
        return count

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
