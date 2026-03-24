"""
Historical odds fetcher — pull past odds data for backtesting.

The Odds API provides historical snapshots via /v4/historical/sports/{sport}/odds.
Each call costs credits (markets x regions). We cache aggressively in SQLite
so a date range only costs credits the first time.

Credit budget strategy:
  - 500 credits/month total (shared with live monitoring)
  - Each historical call costs markets x regions (typically 3)
  - bulk_fetch respects a per-run credit budget and stops when exhausted
  - Cached data costs 0 credits on re-run
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import aiosqlite
import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("callisto.historical_odds")

ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")
ODDS_API_BASE = "https://api.the-odds-api.com/v4"
DB_PATH = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")

_client: Optional[httpx.AsyncClient] = None
_credits_remaining: Optional[int] = None
_credits_used: Optional[int] = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=30.0)
    return _client


async def close_client() -> None:
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
        _client = None


def _update_credits(headers: dict) -> None:
    global _credits_remaining, _credits_used
    if "x-requests-remaining" in headers:
        _credits_remaining = int(headers["x-requests-remaining"])
    if "x-requests-used" in headers:
        _credits_used = int(headers["x-requests-used"])
    logger.info(f"Historical API credits: {_credits_remaining} remaining, {_credits_used} used")


class HistoricalOddsFetcher:
    """Fetch and cache historical odds from The Odds API."""

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

        Source cascade (cheapest first):
          1. SQLite cache (free)
          2. OddsPapi (1 req per call, 250/month free, includes Pinnacle)
          3. The Odds API (10 credits per call, 500/month free)

        Args:
            sport: Sport key (e.g., 'basketball_nba')
            date: ISO date string (e.g., '2025-12-15')
            regions: Bookmaker regions
            markets: Market types

        Returns:
            Dict with games and odds data.
        """
        # Check cache first
        cached = await self._get_cached(sport, date, None, markets)
        if cached:
            logger.debug(f"Cache hit: {sport} {date}")
            return cached

        # Source 1: OddsPapi (1 credit, includes sharp books)
        result = await self._fetch_via_oddspapi(sport, date, markets)
        if result and result.get("games"):
            credits_cost = 1  # OddsPapi costs 1 request regardless of markets
            await self._cache_response(sport, date, None, markets, result, credits_cost)
            return result

        # Source 2: The Odds API (10 credits per call — expensive)
        result = await self._fetch_via_odds_api(sport, date, regions, markets, odds_format)
        if result and result.get("games"):
            credits_cost = len(markets.split(",")) * len(regions.split(","))
            await self._cache_response(sport, date, None, markets, result, credits_cost)
            return result

        # Both sources failed
        source_errors = []
        if result and result.get("error"):
            source_errors.append(f"Odds API: {result['error']}")
        logger.warning(f"Historical odds {sport} {date}: all sources failed — {source_errors}")
        return result or {"error": "All historical odds sources failed", "games": []}

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

    async def _fetch_via_odds_api(
        self, sport: str, date: str, regions: str, markets: str, odds_format: str,
    ) -> dict:
        """Try The Odds API for historical odds (10 credits per call)."""
        if not ODDS_API_KEY:
            return {"error": "ODDS_API_KEY not set", "games": []}

        date_iso = f"{date}T00:00:00Z"
        client = _get_client()
        try:
            resp = await client.get(
                f"{ODDS_API_BASE}/historical/sports/{sport}/odds/",
                params={
                    "apiKey": ODDS_API_KEY,
                    "regions": regions,
                    "markets": markets,
                    "oddsFormat": odds_format,
                    "dateFormat": "iso",
                    "date": date_iso,
                },
            )
            resp.raise_for_status()
            _update_credits(dict(resp.headers))
            data = resp.json()

            return {
                "sport": sport,
                "date": date,
                "timestamp": data.get("timestamp", date_iso),
                "previous_timestamp": data.get("previous_timestamp"),
                "next_timestamp": data.get("next_timestamp"),
                "games": data.get("data", []),
                "game_count": len(data.get("data", [])),
                "source": "odds_api",
            }
        except httpx.HTTPStatusError as e:
            logger.error(f"Historical Odds API HTTP error: {e.response.status_code}")
            return {"error": f"HTTP {e.response.status_code}", "games": []}
        except Exception as e:
            logger.error(f"Historical Odds API error: {e}")
            return {"error": str(e), "games": []}

    async def fetch_historical_event_odds(
        self,
        sport: str,
        event_id: str,
        date: str,
        regions: str = "us",
        markets: str = "player_points,player_rebounds,player_assists,player_threes",
        odds_format: str = "american",
    ) -> dict:
        """Fetch historical odds for a specific event (e.g., player props)."""
        cached = await self._get_cached(sport, date, event_id, markets)
        if cached:
            logger.debug(f"Cache hit: {sport} {date} {event_id}")
            return cached

        if not ODDS_API_KEY:
            return {"error": "ODDS_API_KEY not set"}

        date_iso = f"{date}T00:00:00Z"
        client = _get_client()

        try:
            resp = await client.get(
                f"{ODDS_API_BASE}/historical/sports/{sport}/events/{event_id}/odds",
                params={
                    "apiKey": ODDS_API_KEY,
                    "regions": regions,
                    "markets": markets,
                    "oddsFormat": odds_format,
                    "dateFormat": "iso",
                    "date": date_iso,
                },
            )
            resp.raise_for_status()
            _update_credits(dict(resp.headers))
            data = resp.json()

            result = {
                "sport": sport,
                "event_id": event_id,
                "date": date,
                "timestamp": data.get("timestamp", date_iso),
                "data": data.get("data", {}),
            }

            credits_cost = len(markets.split(",")) * len(regions.split(","))
            await self._cache_response(sport, date, event_id, markets, result, credits_cost)

            return result

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 422:
                return {"error": "Event not found or props not available", "data": {}}
            return {"error": f"HTTP {e.response.status_code}"}
        except Exception as e:
            logger.error(f"Historical event odds error: {e}")
            return {"error": str(e)}

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
        credit_budget: int = 50,
    ) -> dict:
        """
        Fetch historical data for a date range, respecting credit budget.
        Skips dates already in cache. Returns summary.
        """
        from datetime import timedelta

        start = datetime.strptime(start_date, "%Y-%m-%d")
        end = datetime.strptime(end_date, "%Y-%m-%d")

        cached_dates = set(await self.get_cached_dates(sport))
        credits_per_call = len(markets.split(","))
        credits_spent = 0
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

            if credits_spent + credits_per_call > credit_budget:
                logger.warning(
                    f"Credit budget exhausted ({credits_spent}/{credit_budget}). "
                    f"Stopping at {date_str}."
                )
                break

            result = await self.fetch_historical_odds(
                sport=sport, date=date_str, markets=markets,
            )

            if result.get("error"):
                errors.append({"date": date_str, "error": result["error"]})
            else:
                dates_fetched.append(date_str)
                credits_spent += credits_per_call

        return {
            "sport": sport,
            "requested_range": f"{start_date} to {end_date}",
            "dates_fetched": len(dates_fetched),
            "dates_cached_already": len(dates_skipped),
            "credits_spent": credits_spent,
            "credit_budget": credit_budget,
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
