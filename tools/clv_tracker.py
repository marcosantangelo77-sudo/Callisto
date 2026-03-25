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
        await self._db.execute("PRAGMA busy_timeout = 10000")
        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS bets (
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
            );

            CREATE TABLE IF NOT EXISTS bankroll (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                balance REAL NOT NULL,
                change REAL,
                bet_id INTEGER,
                description TEXT,
                FOREIGN KEY (bet_id) REFERENCES bets(id)
            );

            CREATE TABLE IF NOT EXISTS closing_lines (
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
            );

            CREATE INDEX IF NOT EXISTS idx_bets_result ON bets(result, placed_at);
            CREATE INDEX IF NOT EXISTS idx_bets_sport ON bets(sport, placed_at);
            CREATE INDEX IF NOT EXISTS idx_closing_event ON closing_lines(event_id, market);
            CREATE INDEX IF NOT EXISTS idx_bankroll_ts ON bankroll(timestamp);
        """)
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

        cursor = await self._db.execute(
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
        )
        await self._db.commit()
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

        await self._db.execute(
            "INSERT INTO closing_lines "
            "(event_id, sport, captured_at, source, market, team, "
            "closing_odds, closing_point, closing_implied) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (event_id, sport, now, source, market, team,
             closing_odds, closing_point, round(implied, 4)),
        )

        # Update any pending bets for this event
        await self._db.execute(
            "UPDATE bets SET "
            "closing_odds = ?, closing_point = ?, closing_implied_prob = ?, "
            "closing_source = ?, "
            "clv_odds = placement_odds - ?, "
            "clv_implied = ? - placement_implied_prob "
            "WHERE event_id = ? AND market = ? AND team = ? AND result = 'pending'",
            (closing_odds, closing_point, round(implied, 4), source,
             closing_odds, round(implied, 4),
             event_id, market, team),
        )
        await self._db.commit()

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

        await self._db.execute(
            "UPDATE bets SET result = ?, payout = ? WHERE id = ?",
            (result, payout, bet_id),
        )

        # Update bankroll
        if result == "won" and payout:
            change = payout - bet["stake"]
        elif result == "lost":
            change = -bet["stake"]
        else:
            change = 0

        if change != 0:
            # Get current balance
            bal_cursor = await self._db.execute(
                "SELECT balance FROM bankroll ORDER BY timestamp DESC LIMIT 1"
            )
            bal_row = await bal_cursor.fetchone()
            current_balance = bal_row[0] if bal_row else 0

            now = datetime.now(timezone.utc).isoformat()
            await self._db.execute(
                "INSERT INTO bankroll (timestamp, balance, change, bet_id, description) "
                "VALUES (?, ?, ?, ?, ?)",
                (now, current_balance + change, change, bet_id,
                 f"Bet #{bet_id} {result}: {bet['game_description']}"),
            )

        await self._db.commit()

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
        await self._db.execute(
            "INSERT INTO bankroll (timestamp, balance, change, description) "
            "VALUES (?, ?, ?, ?)",
            (now, balance, balance, "Initial bankroll"),
        )
        await self._db.commit()
        logger.info(f"Initial bankroll set: ${balance}")

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
