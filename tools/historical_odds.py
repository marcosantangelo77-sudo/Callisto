"""
Historical odds fetcher — pull past odds data for backtesting.

Primary source: Odds-API.io Pro (15 books, 30K req/hr, historical + live).
Secondary fallback: OddsPapi (Pinnacle, 250 req/month).
All responses cached in SQLite — repeat fetches cost zero.
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import aiosqlite
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("callisto.historical_odds")

DB_PATH = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")


class HistoricalOddsFetcher:
    """Fetch and cache historical odds via Odds-API.io Pro."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def initialize(self) -> None:
        self._db = await aiosqlite.connect(self.db_path)
        logger.info("Historical odds fetcher initialized")

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    async def fetch_historical_odds(
        self,
        sport: str,
        date: str,
        regions: str = "us",
        markets: str = "h2h,spreads,totals",
        odds_format: str = "american",
    ) -> dict:
        """
        Fetch historical odds for a sport on a specific date.

        Source cascade:
          1. SQLite cache (free)
          2. Odds-API.io Pro (15 books, 30K req/hr)
          3. OddsPapi fallback (Pinnacle, 250 req/month)
        """
        # Check cache first
        cached = await self._get_cached(sport, date, None, markets)
        if cached:
            logger.debug(f"Cache hit: {sport} {date}")
            return cached

        # Source 1: Odds-API.io Pro (15 books, best quality)
        result = await self._fetch_via_odds_api_io(sport, date, markets)
        if result and result.get("games"):
            await self._cache_response(sport, date, None, markets, result, 1)
            return result

        # Source 2: OddsPapi fallback (Pinnacle, 1 credit)
        result = await self._fetch_via_oddspapi(sport, date, markets)
        if result and result.get("games"):
            await self._cache_response(sport, date, None, markets, result, 1)
            return result

        # All sources failed
        source_errors = []
        if result and result.get("error"):
            source_errors.append(result['error'])
        logger.warning(f"Historical odds {sport} {date}: all sources failed — {source_errors}")
        return result or {"error": "All historical odds sources failed", "games": []}

    async def _fetch_via_odds_api_io(
        self, sport: str, date: str, markets: str,
    ) -> dict:
        """Try Odds-API.io Pro for historical odds (15 books, 1 request).

        Uses /historical/events to find events for a date, then
        /historical/odds to get closing odds + scores.
        """
        try:
            from tools.odds_api_io import (
                get_historical_events, get_historical_odds, get_usage_status,
                _normalize_event_odds, SPORT_MAP, SPORT_TITLES,
            )

            usage = get_usage_status()
            if not usage.get("api_key_set"):
                return {"error": "Odds-API.io key not set", "games": []}
            if usage.get("requests_remaining_this_hour", 0) < 5:
                return {"error": "Odds-API.io rate limit low", "games": []}

            # Get historical events for this sport/date
            hist = await get_historical_events(sport, date, date)
            events = hist.get("events", [])
            if not events:
                return {"error": f"No historical events for {sport} on {date}", "games": []}

            # Get closing odds for each event via /historical/odds
            event_ids = [e.get("id") for e in events if e.get("id")]
            games = []

            for eid in event_ids:
                raw = await get_historical_odds(eid)
                if isinstance(raw, dict) and raw.get("bookmakers"):
                    normalized = _normalize_event_odds(raw, {}, sport)
                    if normalized:
                        games.append(normalized)

            if games:
                logger.info(
                    f"Historical odds via Odds-API.io Pro: {sport} {date} → "
                    f"{len(games)} games (15 books)"
                )
            return {
                "sport": sport,
                "date": date,
                "timestamp": f"{date}T00:00:00Z",
                "games": games,
                "game_count": len(games),
                "source": "odds_api_io_pro",
            }
        except Exception as e:
            logger.debug(f"Odds-API.io Pro historical failed: {e}")
            return {"error": str(e), "games": []}

    async def _fetch_via_oddspapi(
        self, sport: str, date: str, markets: str,
    ) -> dict:
        """Try OddsPapi for historical odds (1 credit per call)."""
        try:
            from tools.oddspapi import get_historical_odds, get_usage_status

            usage = get_usage_status()
            if not usage.get("api_key_set"):
                return {"error": "OddsPapi API key not set", "games": []}
            if usage.get("requests_remaining", 0) <= 0:
                return {"error": "OddsPapi monthly limit reached", "games": []}

            result = await get_historical_odds(sport=sport, date=date)
            if result.get("error"):
                logger.debug(f"OddsPapi historical {sport} {date}: {result['error']}")
                return result

            games = result.get("games", [])
            if games:
                logger.info(
                    f"Historical odds via OddsPapi: {sport} {date} → {len(games)} games "
                    f"(remaining: {usage.get('requests_remaining', '?')} req)"
                )
            return {
                "sport": sport,
                "date": date,
                "timestamp": f"{date}T00:00:00Z",
                "games": games,
                "game_count": len(games),
                "source": "oddspapi",
            }
        except Exception as e:
            logger.debug(f"OddsPapi historical fallback failed: {e}")
            return {"error": str(e), "games": []}

    async def get_cached_date_range(self, sport: str) -> tuple[Optional[str], Optional[str]]:
        """Returns (earliest_date, latest_date) in cache for a sport."""
        cursor = await self._db.execute(
            "SELECT MIN(snapshot_date), MAX(snapshot_date) "
            "FROM historical_odds_cache WHERE sport = ?",
            (sport,),
        )
        row = await cursor.fetchone()
        if row and row[0]:
            return row[0], row[1]
        return None, None

    async def get_cached_dates(self, sport: str) -> list[str]:
        """List all cached dates for a sport."""
        cursor = await self._db.execute(
            "SELECT DISTINCT snapshot_date FROM historical_odds_cache "
            "WHERE sport = ? ORDER BY snapshot_date",
            (sport,),
        )
        rows = await cursor.fetchall()
        return [r[0] for r in rows]

    async def get_cache_stats(self) -> dict:
        """Return cache usage statistics."""
        cursor = await self._db.execute(
            "SELECT sport, COUNT(*) as entries, "
            "MIN(snapshot_date) as earliest, MAX(snapshot_date) as latest, "
            "SUM(credits_cost) as total_credits "
            "FROM historical_odds_cache GROUP BY sport"
        )
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        return {
            "sports": [dict(zip(cols, row)) for row in rows],
            "api_credits_remaining": _credits_remaining,
        }

    async def bulk_fetch_date_range(
        self,
        sport: str,
        start_date: str,
        end_date: str,
        markets: str = "h2h,spreads,totals",
        credit_budget: int = 5000,
    ) -> dict:
        """
        Fetch historical data for a date range via odds-api.io Pro.
        Skips dates already in cache. Returns summary.
        """
        from datetime import timedelta

        start = datetime.strptime(start_date, "%Y-%m-%d")
        end = datetime.strptime(end_date, "%Y-%m-%d")

        cached_dates = set(await self.get_cached_dates(sport))
        dates_fetched = []
        dates_skipped = []
        errors = []

        current = start
        while current <= end:
            date_str = current.strftime("%Y-%m-%d")
            current += timedelta(days=1)

            if date_str in cached_dates:
                dates_skipped.append(date_str)
                continue

            result = await self.fetch_historical_odds(
                sport=sport, date=date_str, markets=markets,
            )

            if result.get("error"):
                errors.append({"date": date_str, "error": result["error"]})
            else:
                dates_fetched.append(date_str)

        return {
            "sport": sport,
            "requested_range": f"{start_date} to {end_date}",
            "dates_fetched": len(dates_fetched),
            "dates_cached_already": len(dates_skipped),
            "errors": errors,
        }

    async def _get_cached(
        self, sport: str, date: str, event_id: Optional[str], market_type: str,
    ) -> Optional[dict]:
        """Check SQLite cache for a historical odds response."""
        if event_id:
            cursor = await self._db.execute(
                "SELECT response_json FROM historical_odds_cache "
                "WHERE sport = ? AND snapshot_date = ? AND event_id = ? AND market_type = ?",
                (sport, date, event_id, market_type),
            )
        else:
            cursor = await self._db.execute(
                "SELECT response_json FROM historical_odds_cache "
                "WHERE sport = ? AND snapshot_date = ? AND event_id IS NULL AND market_type = ?",
                (sport, date, market_type),
            )
        row = await cursor.fetchone()
        if row:
            return json.loads(row[0])
        return None

    async def _cache_response(
        self,
        sport: str,
        date: str,
        event_id: Optional[str],
        market_type: str,
        data: dict,
        credits_cost: int,
    ) -> None:
        """Store a response in the cache."""
        await self._db.execute(
            "INSERT OR REPLACE INTO historical_odds_cache "
            "(sport, snapshot_date, event_id, market_type, response_json, credits_cost) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (sport, date, event_id, market_type, json.dumps(data), credits_cost),
        )
        await self._db.commit()

    async def bridge_snapshots_to_cache(self) -> dict:
        """Bridge odds_snapshots into historical_odds_cache.

        The odds_snapshots table has live multi-book odds data collected by
        the monitor. Convert these into historical_odds_cache format so the
        backtest engine can use them.

        Only processes snapshots not already represented in the cache.
        Returns summary of what was bridged.
        """
        if not self._db:
            return {"error": "DB not initialized", "bridged": 0}

        # Find snapshot dates/sports NOT already in historical_odds_cache
        cursor = await self._db.execute(
            """
            SELECT os.sport, os.timestamp, os.snapshot_json, os.game_count
            FROM odds_snapshots os
            WHERE os.game_count > 0
            AND NOT EXISTS (
                SELECT 1 FROM historical_odds_cache hoc
                WHERE hoc.sport = os.sport
                AND hoc.snapshot_date = SUBSTR(os.timestamp, 1, 10)
                AND hoc.market_type = 'h2h,spreads,totals'
                AND hoc.event_id IS NULL
            )
            ORDER BY os.timestamp
            """
        )
        rows = await cursor.fetchall()

        if not rows:
            logger.debug("bridge_snapshots_to_cache: no new snapshots to bridge")
            return {"bridged": 0, "skipped": 0}

        # Group by (sport, date) — take the snapshot with the most games per group
        best_per_day: dict[tuple[str, str], tuple[int, str]] = {}
        for sport, timestamp, snapshot_json, game_count in rows:
            date_str = timestamp[:10]  # "2026-03-22"
            key = (sport, date_str)
            if key not in best_per_day or game_count > best_per_day[key][0]:
                best_per_day[key] = (game_count, snapshot_json)

        bridged = 0
        for (sport, date_str), (game_count, snapshot_json) in best_per_day.items():
            try:
                snapshot = json.loads(snapshot_json)
                games = snapshot.get("games", [])
                if not games:
                    continue

                # Reformat to match historical_odds_cache response_json format
                cache_entry = {
                    "sport": sport,
                    "date": date_str,
                    "timestamp": f"{date_str}T00:00:00Z",
                    "games": games,
                    "game_count": len(games),
                    "source": "bridged_from_odds_snapshots",
                }

                await self._db.execute(
                    "INSERT OR IGNORE INTO historical_odds_cache "
                    "(sport, snapshot_date, event_id, market_type, response_json, credits_cost) "
                    "VALUES (?, ?, NULL, ?, ?, 0)",
                    (sport, date_str, "h2h,spreads,totals", json.dumps(cache_entry)),
                )
                bridged += 1
            except (json.JSONDecodeError, TypeError) as e:
                logger.warning(f"bridge_snapshots_to_cache: failed to parse snapshot for {sport} {date_str}: {e}")
                continue

        await self._db.commit()
        logger.info(f"bridge_snapshots_to_cache: bridged {bridged} snapshot-days into historical_odds_cache")
        return {"bridged": bridged, "total_candidates": len(rows)}
