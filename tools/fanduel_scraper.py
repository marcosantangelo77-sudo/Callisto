"""
FanDuel live odds scraper — free, unlimited pregame odds.

FanDuel exposes public JSON endpoints for their sportsbook.
This scraper pulls pregame odds and normalizes them to match the format
returned by tools/odds_api.py so the rest of the system can consume them
interchangeably with DraftKings and The Odds API data.

Zero API cost. Rate-limited to 1 request per 2 seconds to be polite.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger("callisto.fanduel_scraper")

# FanDuel API base — their public sportsbook API
FD_API_BASE = "https://sbapi.nj.sportsbook.fanduel.com/api"

# Event group / competition IDs (discovered via FanDuel API exploration)
FD_COMPETITIONS = {
    "basketball_nba": {"sport": "basketball", "competition": "7522"},
    "americanfootball_nfl": {"sport": "american-football", "competition": "54"},
    "icehockey_nhl": {"sport": "ice-hockey", "competition": "7524"},
    "basketball_ncaab": {"sport": "basketball", "competition": "10547"},
    "baseball_mlb": {"sport": "baseball", "competition": "10336"},
    "golf_pga": {"sport": "golf", "competition": "13163"},
}

# Rate limiting
_last_request_time: float = 0.0
_RATE_LIMIT_SECONDS = 2.0

# Shared client
_client: Optional[httpx.AsyncClient] = None

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=15.0, headers=_HEADERS, follow_redirects=True)
    return _client


async def close_client() -> None:
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
        _client = None


async def _rate_limited_get(url: str, params: dict = None) -> httpx.Response:
    """GET with rate limiting — 1 request per 2 seconds."""
    global _last_request_time
    now = time.monotonic()
    wait = _RATE_LIMIT_SECONDS - (now - _last_request_time)
    if wait > 0:
        await asyncio.sleep(wait)

    client = _get_client()
    resp = await client.get(url, params=params)
    _last_request_time = time.monotonic()
    return resp


async def scrape_fd_odds(sport: str) -> dict:
    """
    Scrape FanDuel pregame odds for a sport.

    Returns normalized format matching tools/odds_api.py output:
    {
        "games": [...],
        "game_count": N,
        "source": "fanduel_scraper",
        "credits_used": 0,
    }
    """
    config = FD_COMPETITIONS.get(sport)
    if not config:
        return {"error": f"Sport '{sport}' not configured for FanDuel scraper", "games": []}

    try:
        # FanDuel competition events endpoint
        url = f"{FD_API_BASE}/content-managed-page"
        params = {
            "page": f"SPORT/{config['sport']}/{config['competition']}",
            "pbHorizontal": "false",
            "_ak": "FhMFpcPWXMeyZxOx",
            "timezone": "America/New_York",
        }

        resp = await _rate_limited_get(url, params)
        resp.raise_for_status()
        data = resp.json()

        games = []
        # Parse FanDuel's response structure
        attachments = data.get("attachments", {})
        events = attachments.get("events", {})
        markets = attachments.get("markets", {})

        for event_id, event in events.items():
            game = _parse_fd_event(event, markets)
            if game:
                games.append(game)

        logger.info(f"FanDuel scraper: {len(games)} games for {sport}")
        return {
            "sport": sport,
            "games": games,
            "game_count": len(games),
            "source": "fanduel_scraper",
            "credits_used": 0,
            "credits": {"remaining": None, "used": None, "api_key_set": True},
        }

    except httpx.HTTPStatusError as e:
        logger.warning(f"FanDuel HTTP error for {sport}: {e.response.status_code}")
        return {"error": f"HTTP {e.response.status_code}", "games": []}
    except Exception as e:
        logger.warning(f"FanDuel scraper error for {sport}: {e}")
        return {"error": str(e), "games": []}


def _parse_fd_event(event: dict, markets: dict) -> Optional[dict]:
    """Parse a FanDuel event into normalized format."""
    try:
        name = event.get("name", "")
        if " @ " in name:
            parts = name.split(" @ ")
            away_team = parts[0].strip()
            home_team = parts[1].strip()
        elif " v " in name:
            parts = name.split(" v ")
            away_team = parts[0].strip()
            home_team = parts[1].strip()
        else:
            away_team = name
            home_team = ""

        commence_time = event.get("openDate", "")

        # Get market IDs for this event
        event_market_ids = event.get("markets", [])

        game = {
            "id": f"fd_{event.get('eventId', '')}",
            "sport_key": event.get("competitionId", ""),
            "commence_time": commence_time,
            "home_team": home_team,
            "away_team": away_team,
            "bookmakers": [{
                "key": "fanduel",
                "title": "FanDuel",
                "last_update": datetime.now(timezone.utc).isoformat(),
                "markets": [],
            }],
        }

        # Parse each market for this event
        for market_id in event_market_ids:
            mid = str(market_id)
            if mid not in markets:
                continue
            market = markets[mid]
            parsed = _parse_fd_market(market)
            if parsed:
                game["bookmakers"][0]["markets"].append(parsed)

        # Only return games that have at least one parsed market
        if not game["bookmakers"][0]["markets"]:
            return None

        return game

    except Exception as e:
        logger.warning(f"Failed to parse FanDuel event: {e}")
        return None


def _parse_fd_market(market: dict) -> Optional[dict]:
    """Parse a FanDuel market into normalized format."""
    try:
        market_type = market.get("marketType", "")
        runners = market.get("runners", [])

        # Map FanDuel market types to standard names
        type_map = {
            "MATCH_ODDS": "h2h",
            "MONEYLINE": "h2h",
            "HANDICAP": "spreads",
            "TOTAL_POINTS": "totals",
            "MATCH_HANDICAP": "spreads",
            "ALTERNATE_TOTAL_POINTS": "totals",
            "WINNER": "outrights",
            "OUTRIGHT": "outrights",
            "TOP_5": "top_5_finish",
            "TOP_10": "top_10_finish",
            "TOP_20": "top_20_finish",
        }

        key = type_map.get(market_type)
        if not key:
            return None

        outcomes = []
        for runner in runners:
            price = runner.get("winRunnerOdds", {}).get("americanOdds")
            if price is None:
                decimal_odds = runner.get("winRunnerOdds", {}).get("decimalOdds")
                if decimal_odds and decimal_odds > 1:
                    # Convert decimal to american
                    if decimal_odds >= 2.0:
                        price = round((decimal_odds - 1) * 100)
                    else:
                        price = round(-100 / (decimal_odds - 1))

            if price is not None:
                outcome = {
                    "name": runner.get("runnerName", ""),
                    "price": int(price),
                }
                handicap = runner.get("handicap")
                if handicap is not None:
                    outcome["point"] = float(handicap)
                outcomes.append(outcome)

        if not outcomes:
            return None

        return {
            "key": key,
            "last_update": datetime.now(timezone.utc).isoformat(),
            "outcomes": outcomes,
        }

    except Exception:
        return None


async def scrape_fd_golf_outrights() -> dict:
    """
    Scrape FanDuel golf tournament outright and placement odds.

    Returns tournament winner, top-5, top-10, top-20, and make-cut markets.
    Specific to golf — uses FanDuel's golf-specific endpoints.
    """
    try:
        url = f"{FD_API_BASE}/content-managed-page"
        params = {
            "page": "SPORT/golf/13163",
            "pbHorizontal": "false",
            "_ak": "FhMFpcPWXMeyZxOx",
            "timezone": "America/New_York",
        }

        resp = await _rate_limited_get(url, params)
        resp.raise_for_status()
        data = resp.json()

        attachments = data.get("attachments", {})
        events = attachments.get("events", {})
        markets = attachments.get("markets", {})

        results = []
        for event_id, event in events.items():
            event_markets = []
            for mid in event.get("markets", []):
                mid_str = str(mid)
                if mid_str in markets:
                    parsed = _parse_fd_market(markets[mid_str])
                    if parsed:
                        event_markets.append(parsed)

            if event_markets:
                results.append({
                    "event": event.get("name", ""),
                    "event_id": str(event.get("eventId", "")),
                    "open_date": event.get("openDate", ""),
                    "markets": event_markets,
                })

        return {
            "tournaments": results,
            "count": len(results),
            "source": "fanduel_scraper",
            "credits_used": 0,
        }

    except Exception as e:
        logger.warning(f"FanDuel golf scraper error: {e}")
        return {"error": str(e), "tournaments": []}
