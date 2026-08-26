"""Reporting / read-side methods for CLVTracker."""

import logging
from datetime import datetime, timezone
from typing import Optional

from tools.clv.odds_math import interpret_clv

logger = logging.getLogger("callisto.clv_tracker")


class CLVReportingMixin:
    """Mixin for CLVTracker: reports, bankroll history, forecasts, bet queries.

    Requires the host class to provide ``self._db`` (aiosqlite connection).
    """

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
                "interpretation": interpret_clv(avg_clv_implied),
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
