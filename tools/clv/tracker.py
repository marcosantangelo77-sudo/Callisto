"""CLVTracker core — lifecycle, bet recording, resolution.

The reporting read-side lives in ``tools.clv.reporting`` and the clv_log
writers in ``tools.clv.clv_log``; both are mixed in below.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

import aiosqlite

from tools.odds_api import calculate_implied_probability, calculate_ev
from tools import telegram

from tools.clv.constants import DB_PATH
from tools.clv.clv_log import CLVLogMixin
from tools.clv.reporting import CLVReportingMixin

logger = logging.getLogger("callisto.clv_tracker")

_SCHEMA_STATEMENTS = (
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
)


class CLVTracker(CLVLogMixin, CLVReportingMixin):
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
        for stmt in _SCHEMA_STATEMENTS:
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
