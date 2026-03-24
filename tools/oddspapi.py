"""
OddsPapi API integration — 250 free requests/month with 350+ bookmakers.

OddsPapi provides pre-game odds from 350+ bookmakers including sharp books
(Pinnacle, SBOBet) that most free APIs don't cover. Historical odds are
available at the same cost as live odds — powerful for backtesting.

Free tier: 250 requests/month, unlimited sports & bookmakers.
Tracks request count locally to avoid exceeding the limit.
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

logger = logging.getLogger("callisto.oddspapi")

# Configuration
ODDSPAPI_API_KEY = os.getenv("ODDSPAPI_API_KEY", "")
ODDSPAPI_BASE = "https://api.oddspapi.io/v4"

# Request tracking — persist across restarts
_TRACKER_PATH = Path(os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")).parent / "oddspapi_usage.json"
_monthly_requests: int = 0
_month_key: str = ""
_FREE_LIMIT = 250

# Shared client
_client: Optional[httpx.AsyncClient] = None

# Sport key mapping: odds_api style -> OddsPapi sportId
# These are discovered via the /v4/sports endpoint; common IDs:
SPORT_IDS = {
    "basketball_nba": 18,
    "americanfootball_nfl": 12,
    "icehockey_nhl": 17,
    "basketball_ncaab": 18,  # Same sport, different tournament
    "soccer_epl": 1,
    "soccer_mls": 1,
    "baseball_mlb": 16,
    "tennis_atp": 13,
    "golf_pga": 22,
}

# Tournament mapping for filtering within a sport
TOURNAMENT_IDS = {
    "basketball_nba": 132,
    "americanfootball_nfl": 233,
    "icehockey_nhl": 108,
    "basketball_ncaab": 134,
    "soccer_epl": 7,
    "baseball_mlb": 109,
    "golf_pga": None,  # Varies per tournament — discovered via /v4/sports
    "golf_masters_tournament_winner": None,
    "golf_pga_championship_winner": None,
    "golf_us_open_winner": None,
    "golf_the_open_championship_winner": None,
}

# Market mapping: odds_api style -> OddsPapi market names
MARKET_MAP = {
    "h2h": "1x2",
    "1x2": "1x2",
    "spreads": "handicap",
    "handicap": "handicap",
    "totals": "over_under",
    "over_under": "over_under",
    "outrights": "outrights",
    "winner": "outrights",
}


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


def _load_usage() -> None:
    """Load monthly request count from disk."""
    global _monthly_requests, _month_key
    current_month = datetime.now(timezone.utc).strftime("%Y-%m")

    if _TRACKER_PATH.exists():
        try:
            data = json.loads(_TRACKER_PATH.read_text())
            if data.get("month") == current_month:
                _monthly_requests = data.get("count", 0)
                _month_key = current_month
                return
        except Exception:
            pass

    # New month or no file — reset
    _monthly_requests = 0
    _month_key = current_month
    _save_usage()


def _save_usage() -> None:
    """Persist monthly request count to disk."""
    try:
        _TRACKER_PATH.parent.mkdir(parents=True, exist_ok=True)
        _TRACKER_PATH.write_text(json.dumps({
            "month": _month_key,
            "count": _monthly_requests,
            "updated": datetime.now(timezone.utc).isoformat(),
        }))
    except Exception as e:
        logger.warning(f"Failed to save OddsPapi usage tracker: {e}")


def _increment_usage() -> None:
    """Increment and persist request count."""
    global _monthly_requests
    _monthly_requests += 1
    _save_usage()


def get_usage_status() -> dict:
    """Return current OddsPapi usage status."""
    _load_usage()
    return {
        "month": _month_key,
        "requests_used": _monthly_requests,
        "requests_remaining": max(0, _FREE_LIMIT - _monthly_requests),
        "limit": _FREE_LIMIT,
        "api_key_set": bool(ODDSPAPI_API_KEY),
    }


def _check_budget(cost: int = 1) -> Optional[str]:
    """Check if we have budget for a request. Returns error string or None."""
    _load_usage()
    if not ODDSPAPI_API_KEY:
        return "ODDSPAPI_API_KEY not set in .env"
    if _monthly_requests + cost > _FREE_LIMIT:
        return f"OddsPapi monthly limit reached ({_monthly_requests}/{_FREE_LIMIT})"
    return None


async def _api_get(endpoint: str, params: dict) -> dict:
    """Make an authenticated GET request to OddsPapi."""
    budget_err = _check_budget()
    if budget_err:
        return {"error": budget_err}

    params["apiKey"] = ODDSPAPI_API_KEY
    client = _get_client()

    try:
        resp = await client.get(f"{ODDSPAPI_BASE}{endpoint}", params=params)
        resp.raise_for_status()
        _increment_usage()
        return resp.json()
    except httpx.HTTPStatusError as e:
        logger.error(f"OddsPapi HTTP error: {e.response.status_code} on {endpoint}")
        return {"error": f"HTTP {e.response.status_code}"}
    except httpx.TimeoutException:
        logger.error(f"OddsPapi timeout on {endpoint}")
        return {"error": "Request timeout"}
    except Exception as e:
        logger.error(f"OddsPapi error on {endpoint}: {e}")
        return {"error": str(e)}


async def get_events(sport: str) -> dict:
    """
    List upcoming events/fixtures for a sport.

    Args:
        sport: Sport key ('basketball_nba', 'americanfootball_nfl', etc.)

    Returns:
        Dict with events list, or error.
    """
    sport_id = SPORT_IDS.get(sport)
    tournament_id = TOURNAMENT_IDS.get(sport)

    if sport_id is None:
        return {"error": f"Unknown sport: {sport}", "events": []}

    params = {"sportId": sport_id}
    if tournament_id:
        params["tournamentId"] = tournament_id

    data = await _api_get("/fixtures", params)
    if "error" in data:
        return {"events": [], **data}

    # data is typically a list of fixtures
    fixtures = data if isinstance(data, list) else data.get("data", data.get("fixtures", []))

    events = []
    for fix in fixtures:
        events.append({
            "id": str(fix.get("fixtureId", "")),
            "sport_key": sport,
            "home_team": _extract_team(fix, "home"),
            "away_team": _extract_team(fix, "away"),
            "commence_time": fix.get("startTime", ""),
            "status": fix.get("status", ""),
        })

    return {
        "sport": sport,
        "event_count": len(events),
        "events": events,
        "source": "oddspapi",
        "usage": get_usage_status(),
    }


async def get_odds(
    sport: str,
    markets: str = "1x2,handicap,over_under",
    bookmakers: str = "",
    odds_format: str = "american",
) -> dict:
    """
    Get live/pre-game odds for a sport.

    Normalizes response to match tools/odds_api.get_odds() format so the
    rest of the system can consume it interchangeably.

    Args:
        sport: Sport key ('basketball_nba', etc.)
        markets: Comma-separated market types ('1x2', 'handicap', 'over_under')
        bookmakers: Comma-separated bookmaker slugs (empty = all available)
        odds_format: 'american' or 'decimal'

    Returns:
        Dict with 'sport', 'game_count', 'games' in odds_api format.
    """
    tournament_id = TOURNAMENT_IDS.get(sport)
    if tournament_id is None:
        return {"error": f"Unknown sport: {sport}", "games": []}

    params = {
        "tournamentId": tournament_id,
        "oddsFormat": odds_format,
    }
    if bookmakers:
        params["bookmakers"] = bookmakers

    data = await _api_get("/odds-by-tournaments", params)
    if "error" in data:
        return {"games": [], **data}

    # Normalize to odds_api format
    raw_fixtures = data if isinstance(data, list) else data.get("data", data.get("fixtures", []))
    games = _normalize_odds_response(raw_fixtures, sport, markets)

    logger.info(f"OddsPapi {sport}: {len(games)} games")
    return {
        "sport": sport,
        "game_count": len(games),
        "games": games,
        "source": "oddspapi",
        "credits": {
            "remaining": max(0, _FREE_LIMIT - _monthly_requests),
            "used": _monthly_requests,
            "api_key_set": bool(ODDSPAPI_API_KEY),
        },
    }


async def get_historical_odds(
    sport: str,
    date: str,
    bookmakers: str = "pinnacle",
) -> dict:
    """
    Get historical odds — same cost as live odds.

    This is extremely valuable for backtesting: compare opening vs closing
    lines, track CLV, validate models against historical data.

    Args:
        sport: Sport key
        date: Date string 'YYYY-MM-DD'
        bookmakers: Comma-separated bookmaker slugs (max 3 per OddsPapi docs)

    Returns:
        Dict with historical games and odds.
    """
    tournament_id = TOURNAMENT_IDS.get(sport)
    if tournament_id is None:
        return {"error": f"Unknown sport: {sport}", "games": []}

    params = {
        "tournamentId": tournament_id,
        "fromDate": f"{date}T00:00:00Z",
        "toDate": f"{date}T23:59:59Z",
    }
    if bookmakers:
        params["bookmakers"] = bookmakers

    data = await _api_get("/historical-odds", params)
    if "error" in data:
        return {"games": [], **data}

    raw_fixtures = data if isinstance(data, list) else data.get("data", data.get("fixtures", []))
    games = _normalize_odds_response(raw_fixtures, sport, "1x2,handicap,over_under")

    logger.info(f"OddsPapi historical {sport} {date}: {len(games)} games")
    return {
        "sport": sport,
        "date": date,
        "game_count": len(games),
        "games": games,
        "source": "oddspapi_historical",
        "usage": get_usage_status(),
    }


def _normalize_odds_response(raw_fixtures: list, sport: str, markets_filter: str) -> list[dict]:
    """
    Normalize OddsPapi fixture/odds data to match the odds_api format.

    OddsPapi structure (per fixture):
    {
        "fixtureId": 123,
        "startTime": "...",
        "participants": [...],
        "bookmakerOdds": {
            "pinnacle": { "markets": { ... } },
            "bet365": { "markets": { ... } }
        }
    }

    Target format (odds_api):
    {
        "id": "...",
        "home_team": "...",
        "away_team": "...",
        "bookmakers": [
            {"key": "pinnacle", "markets": [{"key": "h2h", "outcomes": [...]}]}
        ]
    }
    """
    allowed_markets = set(markets_filter.split(","))
    games = []

    for fix in raw_fixtures:
        fixture_id = str(fix.get("fixtureId", fix.get("id", "")))
        home = _extract_team(fix, "home")
        away = _extract_team(fix, "away")
        commence = fix.get("startTime", "")

        bookmakers_data = fix.get("bookmakerOdds", fix.get("bookmakers", {}))
        if isinstance(bookmakers_data, list):
            # Convert list to dict keyed by bookmaker slug
            bookmakers_data = {bm.get("key", bm.get("slug", f"bm_{i}")): bm for i, bm in enumerate(bookmakers_data)}

        normalized_bookmakers = []
        for bm_key, bm_data in bookmakers_data.items():
            raw_markets = bm_data.get("markets", {})
            if isinstance(raw_markets, list):
                # Convert list to dict keyed by market id/name
                raw_markets = {m.get("key", m.get("name", f"m_{i}")): m for i, m in enumerate(raw_markets)}

            norm_markets = []
            for market_id, market_data in raw_markets.items():
                # Map OddsPapi market names to odds_api keys
                odds_api_key = _reverse_market_map(str(market_id), market_data)
                oddspapi_key = MARKET_MAP.get(odds_api_key, odds_api_key)

                # Check if this market is in our filter
                if odds_api_key not in allowed_markets and oddspapi_key not in allowed_markets:
                    continue

                outcomes = _normalize_outcomes(market_data, odds_api_key)
                if outcomes:
                    norm_markets.append({
                        "key": odds_api_key,
                        "last_update": market_data.get("updatedAt", market_data.get("changedAt", "")),
                        "outcomes": outcomes,
                    })

            if norm_markets:
                normalized_bookmakers.append({
                    "key": bm_key,
                    "title": _bookmaker_title(bm_key),
                    "last_update": bm_data.get("updatedAt", ""),
                    "markets": norm_markets,
                })

        if normalized_bookmakers:
            games.append({
                "id": fixture_id,
                "sport_key": sport,
                "sport_title": _sport_title(sport),
                "home_team": home,
                "away_team": away,
                "commence_time": commence,
                "bookmakers": normalized_bookmakers,
            })

    return games


def _reverse_market_map(market_id: str, market_data: dict) -> str:
    """Map OddsPapi market identifiers back to odds_api keys."""
    market_name = market_data.get("name", market_data.get("key", market_id)).lower()

    if any(kw in market_name for kw in ("1x2", "moneyline", "winner", "match result")):
        return "h2h"
    if any(kw in market_name for kw in ("handicap", "spread", "point spread")):
        return "spreads"
    if any(kw in market_name for kw in ("over", "under", "total", "o/u")):
        return "totals"

    # Fallback: try the reverse map
    reverse = {v: k for k, v in MARKET_MAP.items()}
    return reverse.get(market_name, market_name)


def _normalize_outcomes(market_data: dict, market_key: str) -> list[dict]:
    """Normalize OddsPapi outcomes to odds_api format."""
    outcomes = market_data.get("outcomes", market_data.get("selections", []))
    if isinstance(outcomes, dict):
        outcomes = list(outcomes.values())

    normalized = []
    for o in outcomes:
        if isinstance(o, dict):
            price = o.get("price", o.get("odds", 0))
            name = o.get("name", o.get("label", o.get("participant", "")))
            line = o.get("line", o.get("handicap", o.get("point")))

            # Convert decimal to American if needed
            if isinstance(price, float) and 1.0 < price < 100.0:
                price = _decimal_to_american(price)

            entry = {"name": str(name), "price": int(price) if price else 0}
            if line is not None:
                try:
                    entry["point"] = float(line)
                except (ValueError, TypeError):
                    pass
            normalized.append(entry)

    return normalized


def _decimal_to_american(decimal_odds: float) -> int:
    """Convert decimal odds to American format."""
    if decimal_odds >= 2.0:
        return round((decimal_odds - 1) * 100)
    elif decimal_odds > 1.0:
        return round(-100 / (decimal_odds - 1))
    return -10000


def _extract_team(fixture: dict, side: str) -> str:
    """Extract team name from OddsPapi fixture data."""
    participants = fixture.get("participants", [])
    if isinstance(participants, list):
        for p in participants:
            if isinstance(p, dict):
                p_side = (p.get("side", "") or p.get("type", "")).lower()
                if p_side == side:
                    return p.get("name", p.get("participantName", ""))
        # Fallback: first = away, second = home
        if side == "home" and len(participants) >= 2:
            p = participants[1]
            return p.get("name", p.get("participantName", "")) if isinstance(p, dict) else str(p)
        if side == "away" and len(participants) >= 1:
            p = participants[0]
            return p.get("name", p.get("participantName", "")) if isinstance(p, dict) else str(p)

    # Try direct fields
    if side == "home":
        return fixture.get("homeTeam", fixture.get("home_team", ""))
    return fixture.get("awayTeam", fixture.get("away_team", ""))


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
    }
    return titles.get(slug, slug.replace("_", " ").title())


def _sport_title(sport_key: str) -> str:
    """Map sport key to display title."""
    titles = {
        "basketball_nba": "NBA",
        "americanfootball_nfl": "NFL",
        "icehockey_nhl": "NHL",
        "basketball_ncaab": "NCAAB",
        "soccer_epl": "EPL",
        "baseball_mlb": "MLB",
    }
    return titles.get(sport_key, sport_key)
