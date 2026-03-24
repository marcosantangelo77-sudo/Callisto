"""
Odds-API.io integration — free odds API with 100 requests/hour (72K/month).

https://odds-api.io provides real-time and pre-match odds across major US sports.
Free tier: 100 requests per hour, no monthly cap — 144x more than our paid OddsPapi
subscription and the single highest-volume free odds source in the stack.

This module normalizes output to match the odds_api.py format so the rest of
the system can consume it interchangeably with The Odds API, OddsPapi, and DK scraper.

Base URL: https://api.odds-api.io/v3
Auth: API key via query param (env var ODDS_API_IO_KEY)
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("callisto.odds_api_io")

# Configuration
ODDS_API_IO_KEY = os.getenv("ODDS_API_IO_KEY", "")
ODDS_API_IO_BASE = "https://api.odds-api.io/v3"

# Rate limit: 100 requests per hour
_HOURLY_LIMIT = 100
_TRACKER_PATH = Path(os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")).parent / "odds_api_io_usage.json"

# Request tracking — sliding window within the current hour
_hourly_requests: int = 0
_hour_key: str = ""
_lifetime_requests: int = 0

# Shared client
_client: Optional[httpx.AsyncClient] = None

# Sport key mapping: canonical odds_api keys -> odds-api.io sport slugs
# Modeled after The Odds API v4 sport keys (odds-api.io uses the same convention)
SPORT_KEYS = {
    "basketball_nba": "basketball_nba",
    "americanfootball_nfl": "americanfootball_nfl",
    "icehockey_nhl": "icehockey_nhl",
    "basketball_ncaab": "basketball_ncaab",
    "baseball_mlb": "baseball_mlb",
    "golf_pga": "golf_pga_championship_winner",
    # Aliases — accept shorthand
    "nba": "basketball_nba",
    "nfl": "americanfootball_nfl",
    "nhl": "icehockey_nhl",
    "ncaab": "basketball_ncaab",
    "mlb": "baseball_mlb",
    "pga": "golf_pga_championship_winner",
}

# Display titles
SPORT_TITLES = {
    "basketball_nba": "NBA",
    "americanfootball_nfl": "NFL",
    "icehockey_nhl": "NHL",
    "basketball_ncaab": "NCAAB",
    "baseball_mlb": "MLB",
    "golf_pga_championship_winner": "PGA Golf",
}

# Supported markets
SUPPORTED_MARKETS = {"h2h", "spreads", "totals", "outrights"}


# ---------------------------------------------------------------------------
# Client management
# ---------------------------------------------------------------------------

def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=20.0, follow_redirects=True)
    return _client


async def close_client() -> None:
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
        _client = None


# ---------------------------------------------------------------------------
# Usage tracking — persisted hourly window
# ---------------------------------------------------------------------------

def _current_hour_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")


def _load_usage() -> None:
    """Load hourly request count from disk."""
    global _hourly_requests, _hour_key, _lifetime_requests
    current_hour = _current_hour_key()

    if _TRACKER_PATH.exists():
        try:
            data = json.loads(_TRACKER_PATH.read_text())
            _lifetime_requests = data.get("lifetime", 0)
            if data.get("hour") == current_hour:
                _hourly_requests = data.get("count", 0)
                _hour_key = current_hour
                return
        except Exception:
            pass

    # New hour or no file — reset hourly counter
    _hourly_requests = 0
    _hour_key = current_hour
    _save_usage()


def _save_usage() -> None:
    """Persist usage tracker to disk."""
    try:
        _TRACKER_PATH.parent.mkdir(parents=True, exist_ok=True)
        _TRACKER_PATH.write_text(json.dumps({
            "hour": _hour_key,
            "count": _hourly_requests,
            "lifetime": _lifetime_requests,
            "updated": datetime.now(timezone.utc).isoformat(),
        }))
    except Exception as e:
        logger.warning(f"Failed to save odds-api.io usage tracker: {e}")


def _increment_usage() -> None:
    """Increment and persist request count."""
    global _hourly_requests, _lifetime_requests
    _hourly_requests += 1
    _lifetime_requests += 1
    _save_usage()


def get_usage_status() -> dict:
    """Return current Odds-API.io usage status."""
    _load_usage()
    return {
        "hour": _hour_key,
        "requests_used_this_hour": _hourly_requests,
        "requests_remaining_this_hour": max(0, _HOURLY_LIMIT - _hourly_requests),
        "hourly_limit": _HOURLY_LIMIT,
        "lifetime_requests": _lifetime_requests,
        "api_key_set": bool(ODDS_API_IO_KEY),
    }


def _check_budget(cost: int = 1) -> Optional[str]:
    """Check if we have budget for a request. Returns error string or None."""
    _load_usage()
    if not ODDS_API_IO_KEY:
        return "ODDS_API_IO_KEY not set in .env — get a free key at https://odds-api.io"
    if _hourly_requests + cost > _HOURLY_LIMIT:
        return (
            f"Odds-API.io hourly limit reached ({_hourly_requests}/{_HOURLY_LIMIT}). "
            f"Resets at the top of the next UTC hour."
        )
    return None


# ---------------------------------------------------------------------------
# Core API helper
# ---------------------------------------------------------------------------

async def _api_get(endpoint: str, params: Optional[dict] = None) -> dict | list:
    """
    Make an authenticated GET request to Odds-API.io.

    Returns parsed JSON (dict or list) on success.
    Returns {"error": "..."} on failure.
    """
    budget_err = _check_budget()
    if budget_err:
        return {"error": budget_err}

    params = params or {}
    params["apiKey"] = ODDS_API_IO_KEY
    client = _get_client()

    url = f"{ODDS_API_IO_BASE}{endpoint}"
    try:
        resp = await client.get(url, params=params)
        _update_headers(dict(resp.headers))
        resp.raise_for_status()
        _increment_usage()
        return resp.json()
    except httpx.HTTPStatusError as e:
        status = e.response.status_code
        body = ""
        try:
            body = e.response.text[:300]
        except Exception:
            pass
        logger.error(f"Odds-API.io HTTP {status} on {endpoint}: {body}")
        if status == 401:
            return {"error": "Invalid ODDS_API_IO_KEY — check your API key"}
        if status == 429:
            return {"error": "Odds-API.io rate limit hit (100/hr). Wait and retry."}
        return {"error": f"HTTP {status}: {body or 'Unknown error'}"}
    except httpx.TimeoutException:
        logger.error(f"Odds-API.io timeout on {endpoint}")
        return {"error": "Request timeout — odds-api.io did not respond in 20s"}
    except Exception as e:
        logger.error(f"Odds-API.io error on {endpoint}: {e}")
        return {"error": str(e)}


def _update_headers(headers: dict) -> None:
    """Extract any rate-limit info from response headers (if provided)."""
    # Some v3 APIs echo remaining quota in headers — capture if present
    for key in ("x-requests-remaining", "x-ratelimit-remaining"):
        val = headers.get(key)
        if val is not None:
            logger.info(f"Odds-API.io header {key}: {val}")


# ---------------------------------------------------------------------------
# Sport key resolution
# ---------------------------------------------------------------------------

def _resolve_sport(sport: str) -> Optional[str]:
    """Resolve a user-supplied sport key to the odds-api.io sport slug."""
    # Exact match first
    if sport in SPORT_KEYS:
        return SPORT_KEYS[sport]
    # Case-insensitive fallback
    lower = sport.lower().strip()
    if lower in SPORT_KEYS:
        return SPORT_KEYS[lower]
    # Already a valid slug (pass through)
    return sport


# ---------------------------------------------------------------------------
# Public API functions
# ---------------------------------------------------------------------------

async def get_sports() -> list[dict]:
    """
    List all available sports on Odds-API.io.

    Costs 1 request. Useful for discovering sport keys.
    """
    data = await _api_get("/sports")
    if isinstance(data, dict) and "error" in data:
        return [data]
    if isinstance(data, list):
        return data
    return [data]


async def get_events(sport: str) -> dict:
    """
    List upcoming events/games for a sport.

    Args:
        sport: Sport key ('basketball_nba', 'nba', 'americanfootball_nfl', etc.)

    Returns:
        Dict with 'events' list in normalized format, plus usage info.
    """
    sport_slug = _resolve_sport(sport)

    data = await _api_get(f"/sports/{sport_slug}/events")
    if isinstance(data, dict) and "error" in data:
        return {"events": [], **data}

    events_raw = data if isinstance(data, list) else data.get("data", [])

    events = []
    for ev in events_raw:
        events.append({
            "id": ev.get("id", ""),
            "sport_key": ev.get("sport_key", sport_slug),
            "sport_title": ev.get("sport_title", SPORT_TITLES.get(sport_slug, sport)),
            "home_team": ev.get("home_team", ""),
            "away_team": ev.get("away_team", ""),
            "commence_time": ev.get("commence_time", ""),
        })

    return {
        "sport": sport_slug,
        "event_count": len(events),
        "events": events,
        "source": "odds_api_io",
        "usage": get_usage_status(),
    }


async def get_odds(
    sport: str = "basketball_nba",
    regions: str = "us",
    markets: str = "h2h,spreads,totals",
    odds_format: str = "american",
) -> dict:
    """
    Get live and upcoming odds for a sport.

    Output format matches tools/odds_api.get_odds() exactly so the rest of
    the system can consume it interchangeably.

    Args:
        sport: Sport key ('basketball_nba', 'americanfootball_nfl', etc.)
        regions: Bookmaker regions, comma-separated ('us', 'us,uk', 'eu')
        markets: Market types, comma-separated ('h2h', 'spreads', 'totals', 'outrights')
        odds_format: 'american' or 'decimal'

    Returns:
        Dict with 'sport', 'game_count', 'games' list, and 'credits' info.
    """
    sport_slug = _resolve_sport(sport)

    params = {
        "regions": regions,
        "markets": markets,
        "oddsFormat": odds_format,
        "dateFormat": "iso",
    }

    data = await _api_get(f"/sports/{sport_slug}/odds", params)
    if isinstance(data, dict) and "error" in data:
        return {"games": [], **data}

    games_raw = data if isinstance(data, list) else data.get("data", [])
    games = _normalize_games(games_raw, sport_slug)

    logger.info(f"Odds-API.io {sport_slug}: {len(games)} games, {markets}")
    return {
        "sport": sport_slug,
        "game_count": len(games),
        "games": games,
        "source": "odds_api_io",
        "credits": {
            "remaining_this_hour": max(0, _HOURLY_LIMIT - _hourly_requests),
            "used_this_hour": _hourly_requests,
            "hourly_limit": _HOURLY_LIMIT,
            "api_key_set": bool(ODDS_API_IO_KEY),
        },
    }


async def get_event_odds(
    sport: str,
    event_id: str,
    regions: str = "us",
    markets: str = "h2h,spreads,totals",
    odds_format: str = "american",
) -> dict:
    """
    Get odds for a single event. Useful for tracking line movement on a specific game.

    Args:
        sport: Sport key
        event_id: Event ID (from get_events or get_odds)
        regions: Bookmaker regions
        markets: Market types
        odds_format: 'american' or 'decimal'

    Returns:
        Single game dict in the standard format, or error dict.
    """
    sport_slug = _resolve_sport(sport)

    params = {
        "regions": regions,
        "markets": markets,
        "oddsFormat": odds_format,
        "dateFormat": "iso",
    }

    data = await _api_get(f"/sports/{sport_slug}/events/{event_id}/odds", params)
    if isinstance(data, dict) and "error" in data:
        return data

    # Single event response — normalize it
    if isinstance(data, dict):
        game = _normalize_single_game(data, sport_slug)
        return game

    # If API returns a list, find our event
    if isinstance(data, list):
        for item in data:
            if item.get("id") == event_id:
                return _normalize_single_game(item, sport_slug)
        # Return first if only one result
        if len(data) == 1:
            return _normalize_single_game(data[0], sport_slug)

    return {"error": f"Event {event_id} not found in response"}


async def get_scores(
    sport: str = "basketball_nba",
    days_from: int = 1,
) -> dict:
    """
    Get live scores and recently completed games.

    Args:
        sport: Sport key
        days_from: Number of days back to include completed games (1-3)

    Returns:
        Dict with 'games' list containing score data.
    """
    sport_slug = _resolve_sport(sport)

    params = {
        "daysFrom": days_from,
        "dateFormat": "iso",
    }

    data = await _api_get(f"/sports/{sport_slug}/scores", params)
    if isinstance(data, dict) and "error" in data:
        return {"games": [], **data}

    games_raw = data if isinstance(data, list) else data.get("data", [])

    games = []
    for g in games_raw:
        games.append({
            "id": g.get("id", ""),
            "sport_key": g.get("sport_key", sport_slug),
            "home_team": g.get("home_team", ""),
            "away_team": g.get("away_team", ""),
            "commence_time": g.get("commence_time", ""),
            "completed": g.get("completed", False),
            "scores": g.get("scores"),
            "last_update": g.get("last_update", ""),
        })

    return {
        "sport": sport_slug,
        "game_count": len(games),
        "games": games,
        "source": "odds_api_io",
    }


async def get_outrights(
    sport: str = "golf_pga",
    regions: str = "us",
    odds_format: str = "american",
) -> dict:
    """
    Get outright/futures odds (e.g., PGA tournament winner, conference winner).

    Args:
        sport: Sport key — most useful for golf_pga
        regions: Bookmaker regions
        odds_format: 'american' or 'decimal'

    Returns:
        Dict with games/events and outright odds.
    """
    return await get_odds(
        sport=sport,
        regions=regions,
        markets="outrights",
        odds_format=odds_format,
    )


# ---------------------------------------------------------------------------
# Convenience: multi-sport snapshot
# ---------------------------------------------------------------------------

async def snapshot_all_sports(
    regions: str = "us",
    markets: str = "h2h,spreads,totals",
    odds_format: str = "american",
) -> dict:
    """
    Pull odds for all supported major sports in one call batch.

    Uses 5-6 of the 100 hourly requests. Returns a dict keyed by sport.
    """
    sports = [
        "basketball_nba",
        "americanfootball_nfl",
        "icehockey_nhl",
        "basketball_ncaab",
        "baseball_mlb",
    ]

    budget_err = _check_budget(cost=len(sports))
    if budget_err:
        return {"error": budget_err}

    tasks = [
        get_odds(sport=s, regions=regions, markets=markets, odds_format=odds_format)
        for s in sports
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    snapshot = {}
    total_games = 0
    for sport, result in zip(sports, results):
        if isinstance(result, Exception):
            snapshot[sport] = {"error": str(result), "games": []}
        else:
            snapshot[sport] = result
            total_games += result.get("game_count", 0)

    return {
        "total_games": total_games,
        "sports": snapshot,
        "source": "odds_api_io",
        "usage": get_usage_status(),
    }


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------

def _normalize_games(games_raw: list, sport_slug: str) -> list[dict]:
    """
    Normalize a list of games from the API response to the standard format.

    Target format (matching tools/odds_api.py output):
    {
        "id": "...",
        "sport_key": "basketball_nba",
        "sport_title": "NBA",
        "home_team": "...",
        "away_team": "...",
        "commence_time": "...",
        "bookmakers": [
            {
                "key": "draftkings",
                "title": "DraftKings",
                "last_update": "...",
                "markets": [
                    {
                        "key": "h2h",
                        "last_update": "...",
                        "outcomes": [
                            {"name": "Team A", "price": -110},
                            {"name": "Team B", "price": +105}
                        ]
                    }
                ]
            }
        ]
    }
    """
    games = []
    for g in games_raw:
        game = _normalize_single_game(g, sport_slug)
        if game and "error" not in game:
            games.append(game)
    return games


def _normalize_single_game(raw: dict, sport_slug: str) -> dict:
    """Normalize a single game/event from the API response."""
    game = {
        "id": raw.get("id", ""),
        "sport_key": raw.get("sport_key", sport_slug),
        "sport_title": raw.get("sport_title", SPORT_TITLES.get(sport_slug, sport_slug)),
        "home_team": raw.get("home_team", ""),
        "away_team": raw.get("away_team", ""),
        "commence_time": raw.get("commence_time", ""),
        "bookmakers": [],
    }

    for bm in raw.get("bookmakers", []):
        normalized_bm = {
            "key": bm.get("key", ""),
            "title": bm.get("title", _bookmaker_title(bm.get("key", ""))),
            "last_update": bm.get("last_update", ""),
            "markets": [],
        }

        for mkt in bm.get("markets", []):
            market_key = mkt.get("key", "")
            outcomes = []
            for o in mkt.get("outcomes", []):
                entry = {
                    "name": o.get("name", ""),
                    "price": o.get("price", 0),
                }
                if "point" in o and o["point"] is not None:
                    entry["point"] = o["point"]
                if "description" in o and o["description"]:
                    entry["description"] = o["description"]
                outcomes.append(entry)

            if outcomes:
                normalized_bm["markets"].append({
                    "key": market_key,
                    "last_update": mkt.get("last_update", ""),
                    "outcomes": outcomes,
                })

        if normalized_bm["markets"]:
            game["bookmakers"].append(normalized_bm)

    return game


def _bookmaker_title(slug: str) -> str:
    """Convert bookmaker slug to display title."""
    titles = {
        "pinnacle": "Pinnacle",
        "bet365": "Bet365",
        "draftkings": "DraftKings",
        "fanduel": "FanDuel",
        "betmgm": "BetMGM",
        "caesars": "Caesars",
        "bovada": "Bovada",
        "betrivers": "BetRivers",
        "unibet": "Unibet",
        "williamhill": "William Hill",
        "sbobet": "SBOBet",
        "betfair": "Betfair",
        "mybookie": "MyBookie",
        "barstool": "Barstool",
        "betonlineag": "BetOnline.ag",
        "lowvig": "LowVig.ag",
        "betus": "BetUS",
        "superbook": "SuperBook",
        "wynnbet": "WynnBET",
        "pointsbetus": "PointsBet US",
        "betparx": "betPARX",
        "hardrock": "Hard Rock Bet",
        "espnbet": "ESPN BET",
        "fliff": "Fliff",
        "fanatics": "Fanatics",
    }
    return titles.get(slug, slug.replace("_", " ").title())


# ---------------------------------------------------------------------------
# Utility: compare with other sources
# ---------------------------------------------------------------------------

def find_best_line(game: dict, market: str = "spreads", team: str = "") -> dict:
    """
    Compare lines across bookmakers for a game and find the best available.

    Same interface as odds_api.find_best_line() — works with any game dict
    in the standard format regardless of source.

    Args:
        game: A game dict from get_odds()
        market: 'h2h', 'spreads', or 'totals'
        team: Team name to find best line for (for spreads/h2h)

    Returns:
        Dict with best line, worst line, spread across books, and all lines.
    """
    bookmaker_lines = []
    for bm in game.get("bookmakers", []):
        for mkt in bm.get("markets", []):
            if mkt["key"] != market:
                continue
            for outcome in mkt.get("outcomes", []):
                entry = {
                    "bookmaker": bm.get("title", bm.get("key", "")),
                    "name": outcome.get("name", ""),
                    "price": outcome.get("price", 0),
                    "point": outcome.get("point"),
                    "last_update": bm.get("last_update", ""),
                }
                if not team or team.lower() in outcome.get("name", "").lower():
                    bookmaker_lines.append(entry)

    if not bookmaker_lines:
        return {"error": "No lines found", "lines": []}

    bookmaker_lines.sort(key=lambda x: x["price"], reverse=True)

    return {
        "best": bookmaker_lines[0],
        "worst": bookmaker_lines[-1],
        "spread_across_books": bookmaker_lines[0]["price"] - bookmaker_lines[-1]["price"],
        "all_lines": bookmaker_lines,
    }
