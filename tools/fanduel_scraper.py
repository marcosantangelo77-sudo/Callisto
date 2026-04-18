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

# Custom page IDs — the CUSTOM endpoint is the only one currently working.
# The old SPORT/{sport}/{competition} format returns HTTP 400 as of March 2026.
FD_CUSTOM_PAGE_IDS = {
    "basketball_nba": "nba",
    "americanfootball_nfl": "nfl",
    "icehockey_nhl": "nhl",
    "baseball_mlb": "mlb",
    "basketball_ncaab": "ncaab",
    "golf_pga": "golf",
}

# Legacy competition config — kept for reference but no longer used
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
        _client = httpx.AsyncClient(timeout=15.0, headers=_HEADERS, follow_redirects=True, max_redirects=5)
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
    custom_page = FD_CUSTOM_PAGE_IDS.get(sport)
    if not custom_page:
        return {"error": f"Sport '{sport}' not configured for FanDuel scraper", "games": []}

    try:
        # Use the CUSTOM page endpoint (working as of March 2026).
        # The old SPORT/{sport}/{competition} format returns HTTP 400.
        url = f"{FD_API_BASE}/content-managed-page"
        params = {
            "page": "CUSTOM",
            "customPageId": custom_page,
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
        all_markets = attachments.get("markets", {})

        # Group markets by eventId — the CUSTOM endpoint does NOT put a
        # "markets" array inside each event object.  Instead, each market
        # has an "eventId" field that maps back to the event.
        from collections import defaultdict
        markets_by_event: dict[str, dict[str, dict]] = defaultdict(dict)
        for mid, mkt in all_markets.items():
            eid = str(mkt.get("eventId", ""))
            if eid:
                markets_by_event[eid][mid] = mkt

        for event_id, event in events.items():
            # Build a per-event markets dict.
            # Prefer the event's own "markets" list (old format) if present,
            # otherwise use our grouped-by-eventId lookup.
            event_market_ids = event.get("markets", [])
            if event_market_ids:
                event_markets = {
                    str(mid): all_markets[str(mid)]
                    for mid in event_market_ids
                    if str(mid) in all_markets
                }
            else:
                event_markets = markets_by_event.get(str(event.get("eventId", event_id)), {})

            game = _parse_fd_event(event, event_markets, sport)
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


def _parse_fd_event(event: dict, event_markets: dict, sport: str = "") -> Optional[dict]:
    """Parse a FanDuel event into normalized format.

    Args:
        event: Event dict from the attachments/events section.
        event_markets: Dict of {marketId: marketDict} for this event only.
                       Pre-filtered by the caller.
    """
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
            # Not a game event (e.g. "NBA Futures", "NBA Player Awards")
            return None

        commence_time = event.get("openDate", "")

        game = {
            "id": f"fd_{event.get('eventId', '')}",
            "sport_key": sport or event.get("competitionId", ""),
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
        for mid, market in event_markets.items():
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

        # Map FanDuel market types to standard names.
        # The CUSTOM endpoint uses longer names like "MONEY_LINE" and
        # "MATCH_HANDICAP_(2-WAY)" alongside the old short names.
        type_map = {
            "MATCH_ODDS": "h2h",
            "MONEYLINE": "h2h",
            "MONEY_LINE": "h2h",
            "HANDICAP": "spreads",
            "TOTAL_POINTS": "totals",
            "TOTAL_POINTS_(OVER/UNDER)": "totals",
            "MATCH_HANDICAP": "spreads",
            "MATCH_HANDICAP_(2-WAY)": "spreads",
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
            win_odds = runner.get("winRunnerOdds", {})
            # New format: americanDisplayOdds.americanOdds (CUSTOM endpoint)
            price = None
            american_display = win_odds.get("americanDisplayOdds", {})
            if american_display:
                price = american_display.get("americanOdds") or american_display.get("americanOddsInt")
            # Old format: direct americanOdds key
            if price is None:
                price = win_odds.get("americanOdds")
            # Fallback: decimal odds conversion
            if price is None:
                true_odds = win_odds.get("trueOdds", {})
                decimal_odds_obj = true_odds.get("decimalOdds", {})
                decimal_odds = decimal_odds_obj.get("decimalOdds") if isinstance(decimal_odds_obj, dict) else None
                # Also try the old flat key
                if decimal_odds is None:
                    decimal_odds = win_odds.get("decimalOdds")
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
    Specific to golf — uses FanDuel's CUSTOM page endpoint.
    """
    try:
        url = f"{FD_API_BASE}/content-managed-page"
        params = {
            "page": "CUSTOM",
            "customPageId": "golf",
            "_ak": "FhMFpcPWXMeyZxOx",
            "timezone": "America/New_York",
        }

        resp = await _rate_limited_get(url, params)
        resp.raise_for_status()
        data = resp.json()

        attachments = data.get("attachments", {})
        events = attachments.get("events", {})
        all_markets = attachments.get("markets", {})

        # Group markets by eventId (same approach as scrape_fd_odds)
        from collections import defaultdict
        markets_by_event: dict[str, dict[str, dict]] = defaultdict(dict)
        for mid, mkt in all_markets.items():
            eid = str(mkt.get("eventId", ""))
            if eid:
                markets_by_event[eid][mid] = mkt

        results = []
        for event_id, event in events.items():
            eid_str = str(event.get("eventId", event_id))
            event_mkts = markets_by_event.get(eid_str, {})

            parsed_markets = []
            for mid, mkt in event_mkts.items():
                parsed = _parse_fd_market(mkt)
                if parsed:
                    parsed_markets.append(parsed)

            if parsed_markets:
                results.append({
                    "event": event.get("name", ""),
                    "event_id": eid_str,
                    "open_date": event.get("openDate", ""),
                    "markets": parsed_markets,
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
