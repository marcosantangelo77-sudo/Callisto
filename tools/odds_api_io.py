"""
Odds-API.io integration — Pro plan with 30,000 requests/hour + WebSocket.

https://odds-api.io provides real-time and pre-match odds across 34 sports.
Pro plan: 30,000 req/hr, 15 bookmakers, all markets, historical data,
pre-calculated value bets + arbitrage, and WebSocket streaming.

Key Pro endpoints:
  - GET /v3/odds/multi?eventIds=X,Y,Z  -> odds for up to 10 events (1 request!)
  - GET /v3/odds/updated?since=X       -> incremental odds changes
  - GET /v3/value-bets?bookmaker=X     -> pre-calculated +EV bets (every 5s)
  - GET /v3/arbitrage-bets             -> pre-calculated arb opportunities
  - GET /v3/historical/events          -> historical events (31-day windows)
  - GET /v3/historical/odds            -> historical/closing odds + scores
  - GET /v3/odds/movements             -> opening-to-closing line history
  - WSS /v3/ws                         -> real-time odds streaming

Selected bookmakers (15):
  DraftKings, Fanatics, FanDuel, BetMGM, Caesars, BetRivers, bet365 NJ,
  Hard Rock, Bovada, Circa, BetOnline.ag, WilliamHill NJ,
  Betfair Exchange, Betfair Sportsbook, Sbobet

Base URL: https://api.odds-api.io/v3
Auth: API key via query param (env var ODDS_API_IO_KEY)
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv

from tools.ingestion_tracking import tracked_ingestion

load_dotenv()

logger = logging.getLogger("callisto.odds_api_io")

# Configuration
ODDS_API_IO_KEY = os.getenv("ODDS_API_IO_KEY", "")
ODDS_API_IO_BASE = "https://api.odds-api.io/v3"

# Rate limit: 30,000 requests per hour (Pro plan)
_HOURLY_LIMIT = 30000
_TRACKER_PATH = Path(os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")).parent / "odds_api_io_usage.json"

# Request tracking — sliding window within the current hour
_hourly_requests: int = 0
_hour_key: str = ""
_lifetime_requests: int = 0

# Shared client
_client: Optional[httpx.AsyncClient] = None

# ---------------------------------------------------------------------------
# Sport/league mapping: Callisto canonical keys -> odds-api.io slugs
# ---------------------------------------------------------------------------

SPORT_MAP = {
    "basketball_nba":       {"sport": "basketball",        "league": "usa-nba"},
    "americanfootball_nfl": {"sport": "american-football",  "league": "usa-nfl"},
    "icehockey_nhl":        {"sport": "ice-hockey",         "league": "usa-nhl"},
    "basketball_ncaab":     {"sport": "basketball",         "league": "usa-ncaa-division-i-national-championship"},
    "baseball_mlb":         {"sport": "baseball",           "league": "usa-mlb"},
    "golf_pga":             {"sport": "golf",               "league": None},  # varies per tournament
    # Aliases
    "nba":   {"sport": "basketball",       "league": "usa-nba"},
    "nfl":   {"sport": "american-football", "league": "usa-nfl"},
    "nhl":   {"sport": "ice-hockey",        "league": "usa-nhl"},
    "ncaab": {"sport": "basketball",        "league": "usa-ncaa-division-i-national-championship"},
    "mlb":   {"sport": "baseball",          "league": "usa-mlb"},
}

# Display titles
SPORT_TITLES = {
    "basketball_nba": "NBA",
    "americanfootball_nfl": "NFL",
    "icehockey_nhl": "NHL",
    "basketball_ncaab": "NCAAB",
    "baseball_mlb": "MLB",
    "golf_pga": "PGA Golf",
}

# Pro plan: 15 bookmakers selected via /bookmakers/selected/select
_SELECTED_BOOKMAKERS = (
    "DraftKings,Fanatics,FanDuel,BetMGM,Caesars,BetRivers,bet365 NJ,"
    "Hard Rock,Bovada,Circa,BetOnline.ag,WilliamHill NJ,"
    "Betfair Exchange,Betfair Sportsbook,Sbobet"
)

# Bookmaker name -> normalized slug for output
_BOOKMAKER_SLUG_MAP = {
    "BetMGM": "betmgm",
    "bet365 NJ": "bet365",
    "DraftKings": "draftkings",
    "FanDuel": "fanduel",
    "Fanatics": "fanatics",
    "Caesars": "caesars",
    "BetRivers": "betrivers",
    "Hard Rock": "hardrock",
    "Bovada": "bovada",
    "Circa": "circa",
    "BetOnline.ag": "betonlineag",
    "WilliamHill NJ": "williamhill",
    "Betfair Exchange": "betfair_exchange",
    "Betfair Sportsbook": "betfair",
    "Sbobet": "sbobet",
    "Pinnacle": "pinnacle",
    "FanDuel NJ": "fanduel",
    "BetMGM NJ": "betmgm",
}


# ---------------------------------------------------------------------------
# Client management
# ---------------------------------------------------------------------------

def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=20.0, follow_redirects=True, max_redirects=5)
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
        except Exception as e:
            logger.info(f"Could not load odds-api.io usage tracker (resetting): {e}")

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

# Backoff config — 429 handling. Prior behavior: single call, return
# {"error": "rate limit"} on first 429. Problem: the next five retries in the
# scheduler all tripped the same 429 one second apart and produced 429 storms
# against odds-api.io that counted toward the hourly quota. With exponential
# backoff + Retry-After honoring, a 429 costs one sleep and (typically) one
# retry instead of six wasted calls.
_BACKOFF_BASE_SECONDS = 1.0
_BACKOFF_MAX_SECONDS = 16.0
_BACKOFF_MAX_RETRIES = 3


def _compute_backoff(attempt: int, retry_after: Optional[str]) -> float:
    """Return sleep duration in seconds for this retry attempt."""
    import random
    if retry_after:
        # Retry-After is either an integer-seconds value or an HTTP date.
        try:
            return min(_BACKOFF_MAX_SECONDS, float(retry_after))
        except (TypeError, ValueError):
            pass
    # Exponential with jitter: 1, 2, 4, 8, 16 (cap)
    base = min(_BACKOFF_MAX_SECONDS, _BACKOFF_BASE_SECONDS * (2 ** attempt))
    return base + random.uniform(0, min(1.0, base * 0.1))


async def _api_get(endpoint: str, params: Optional[dict] = None) -> dict | list:
    """
    Make an authenticated GET request to Odds-API.io with 429 backoff.

    429 responses trigger exponential backoff (honoring Retry-After) up to
    _BACKOFF_MAX_RETRIES attempts. After exhausting retries the call returns
    an {"error": "rate limit ..."} sentinel — the @tracked_ingestion
    decorator recognizes this pattern and logs status='rate_limited' so the
    health check can differentiate quota exhaustion from real failures.

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

    for attempt in range(_BACKOFF_MAX_RETRIES + 1):
        try:
            resp = await client.get(url, params=params)
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

            if status == 429 and attempt < _BACKOFF_MAX_RETRIES:
                retry_after = e.response.headers.get("Retry-After")
                sleep_s = _compute_backoff(attempt, retry_after)
                logger.warning(
                    f"Odds-API.io 429 on {endpoint} (attempt {attempt + 1}/"
                    f"{_BACKOFF_MAX_RETRIES + 1}); sleeping {sleep_s:.2f}s"
                )
                await asyncio.sleep(sleep_s)
                continue

            # Non-retryable or retries exhausted
            logger.error(f"Odds-API.io HTTP {status} on {endpoint}: {body}")
            if status == 401:
                return {"error": "Invalid ODDS_API_IO_KEY — check your API key"}
            if status == 429:
                # Retries exhausted — return rate_limited sentinel so
                # @tracked_ingestion tags the run correctly.
                return {"error": f"rate limit: exhausted {_BACKOFF_MAX_RETRIES} retries on {endpoint}"}
            if status == 403:
                return {"error": f"Access denied (bookmaker limit?): {body}"}
            return {"error": f"HTTP {status}: {body or 'Unknown error'}"}
        except httpx.TimeoutException:
            logger.error(f"Odds-API.io timeout on {endpoint}")
            return {"error": "Request timeout — odds-api.io did not respond in 20s"}
        except Exception as e:
            logger.error(f"Odds-API.io error on {endpoint}: {e}")
            return {"error": str(e)}

    # Should be unreachable — loop always returns or retries
    return {"error": "rate limit: backoff loop exited unexpectedly"}


# ---------------------------------------------------------------------------
# Odds helpers: decimal -> American conversion
# ---------------------------------------------------------------------------

def _decimal_to_american(dec: float) -> int:
    """Convert decimal odds to American format."""
    if dec >= 2.0:
        return round((dec - 1) * 100)
    elif dec > 1.0:
        return round(-100 / (dec - 1))
    return -10000


# ---------------------------------------------------------------------------
# Public API functions
# ---------------------------------------------------------------------------

@tracked_ingestion(source="odds_api_io.v3.sports", sla_seconds=3600)
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


@tracked_ingestion(
    source=lambda sport, **_: f"odds_api_io.v3.events.{sport}",
    sla_seconds=600,
)
async def get_events(sport: str) -> dict:
    """
    List upcoming events/games for a sport.

    Args:
        sport: Sport key ('basketball_nba', 'nba', 'americanfootball_nfl', etc.)

    Returns:
        Dict with 'events' list in normalized format, plus usage info.
    """
    mapping = SPORT_MAP.get(sport, SPORT_MAP.get(sport.lower().strip()))
    if not mapping:
        return {"events": [], "error": f"Unknown sport: {sport}"}

    params = {"sport": mapping["sport"]}
    if mapping.get("league"):
        params["league"] = mapping["league"]

    data = await _api_get("/events", params)
    if isinstance(data, dict) and "error" in data:
        return {"events": [], **data}

    events_raw = data if isinstance(data, list) else data.get("data", [])

    # Filter to pending/live only (exclude settled and cancelled)
    events = []
    for ev in events_raw:
        status = ev.get("status", "")
        if status in ("settled", "cancelled", "postponed"):
            continue
        events.append({
            "id": str(ev.get("id", "")),
            "sport_key": sport,
            "sport_title": SPORT_TITLES.get(sport, sport),
            "home_team": ev.get("home", ""),
            "away_team": ev.get("away", ""),
            "commence_time": ev.get("date", ""),
            "status": status,
        })

    return {
        "sport": sport,
        "event_count": len(events),
        "events": events,
        "source": "odds_api_io",
        "usage": get_usage_status(),
    }


@tracked_ingestion(
    source=lambda sport="basketball_nba", **_: f"odds_api_io.v3.odds.{sport}",
    sla_seconds=300,
)
async def get_odds(
    sport: str = "basketball_nba",
    regions: str = "us",
    markets: str = "h2h,spreads,totals",
    odds_format: str = "american",
) -> dict:
    """
    Get live and upcoming odds for a sport.

    Fetches pending events, then fetches odds for each one.
    The free tier is limited to 2 bookmakers (currently BetMGM + bet365 NJ).

    Costs: 1 request for events + 1 per game with odds.
    Typical daily NBA slate (6-10 games) = 7-11 requests = well within 100/hr.

    Output format matches tools/odds_api.get_odds() exactly.
    """
    mapping = SPORT_MAP.get(sport, SPORT_MAP.get(sport.lower().strip()))
    if not mapping:
        return {"games": [], "error": f"Unknown sport: {sport}"}

    # Step 1: Get pending events (1 request)
    events_result = await get_events(sport)
    if events_result.get("error"):
        return {"games": [], **events_result}

    pending_events = events_result.get("events", [])
    if not pending_events:
        logger.info(f"Odds-API.io {sport}: no pending events")
        return {
            "sport": sport,
            "game_count": 0,
            "games": [],
            "source": "odds_api_io",
            "credits": _credits_dict(),
        }

    # Filter to today's and tomorrow's games only (not 2+ weeks out).
    # This is critical: NBA has 150+ pending events spanning weeks. We only
    # want the immediate slate to conserve the 100 req/hr budget.
    from datetime import timedelta
    now_utc = datetime.now(timezone.utc)
    cutoff = now_utc + timedelta(hours=36)
    today_events = []
    for ev in pending_events:
        try:
            ev_date = datetime.fromisoformat(ev.get("commence_time", "").replace("Z", "+00:00"))
            if ev_date <= cutoff:
                today_events.append(ev)
        except (ValueError, TypeError):
            # If we can't parse the date, include it (safe default)
            today_events.append(ev)

    pending_events = today_events
    if not pending_events:
        logger.info(f"Odds-API.io {sport}: no games within 36h window")
        return {
            "sport": sport,
            "game_count": 0,
            "games": [],
            "source": "odds_api_io",
            "credits": _credits_dict(),
        }

    # Budget check: we need 1 request per event
    budget_err = _check_budget(cost=len(pending_events))
    if budget_err:
        # Try to fetch as many as we can afford
        remaining = max(0, _HOURLY_LIMIT - _hourly_requests)
        if remaining == 0:
            return {"games": [], "error": budget_err}
        pending_events = pending_events[:remaining]
        logger.warning(
            f"Odds-API.io budget tight — fetching only {len(pending_events)} "
            f"of {events_result['event_count']} events"
        )

    # Step 2: Fetch odds for each event concurrently (N requests)
    tasks = [
        _fetch_event_odds(ev["id"], ev, sport)
        for ev in pending_events
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    games = []
    for result in results:
        if isinstance(result, Exception):
            logger.warning(f"Odds-API.io event fetch error: {result}")
            continue
        if result and "error" not in result:
            games.append(result)

    logger.info(f"Odds-API.io {sport}: {len(games)} games with odds")
    return {
        "sport": sport,
        "game_count": len(games),
        "games": games,
        "source": "odds_api_io",
        "credits": _credits_dict(),
    }


async def _fetch_event_odds(event_id: str, event_info: dict, sport: str) -> Optional[dict]:
    """Fetch and normalize odds for a single event."""
    params = {
        "eventId": event_id,
        "bookmakers": _SELECTED_BOOKMAKERS,
    }

    data = await _api_get("/odds", params)
    if isinstance(data, dict) and "error" in data:
        logger.debug(f"Odds-API.io no odds for event {event_id}: {data.get('error')}")
        return None

    return _normalize_event_odds(data, event_info, sport)


@tracked_ingestion(
    source=lambda sport, event_id, **_: f"odds_api_io.v3.event_odds.{sport}",
    sla_seconds=600,
)
async def get_event_odds(
    sport: str,
    event_id: str,
    regions: str = "us",
    markets: str = "h2h,spreads,totals",
    odds_format: str = "american",
) -> dict:
    """
    Get odds for a single event by ID.

    Args:
        sport: Sport key
        event_id: Event ID from get_events()
    """
    params = {
        "eventId": event_id,
        "bookmakers": _SELECTED_BOOKMAKERS,
    }

    data = await _api_get("/odds", params)
    if isinstance(data, dict) and "error" in data:
        return data

    event_info = {
        "id": event_id,
        "sport_key": sport,
        "home_team": "",
        "away_team": "",
        "commence_time": "",
    }

    result = _normalize_event_odds(data, event_info, sport)
    return result if result else {"error": f"No odds for event {event_id}"}


@tracked_ingestion(
    source=lambda sport="basketball_nba", **_: f"odds_api_io.v3.scores.{sport}",
    sla_seconds=600,
)
async def get_scores(
    sport: str = "basketball_nba",
    days_from: int = 1,
) -> dict:
    """
    Get live scores and recently completed games.

    Uses the events endpoint and filters for settled games with scores.
    """
    mapping = SPORT_MAP.get(sport, SPORT_MAP.get(sport.lower().strip()))
    if not mapping:
        return {"games": [], "error": f"Unknown sport: {sport}"}

    params = {"sport": mapping["sport"]}
    if mapping.get("league"):
        params["league"] = mapping["league"]

    data = await _api_get("/events", params)
    if isinstance(data, dict) and "error" in data:
        return {"games": [], **data}

    events_raw = data if isinstance(data, list) else []

    games = []
    for g in events_raw:
        scores = g.get("scores")
        if scores is None:
            continue
        games.append({
            "id": str(g.get("id", "")),
            "sport_key": sport,
            "home_team": g.get("home", ""),
            "away_team": g.get("away", ""),
            "commence_time": g.get("date", ""),
            "completed": g.get("status") == "settled",
            "scores": scores,
            "last_update": "",
        })

    return {
        "sport": sport,
        "game_count": len(games),
        "games": games,
        "source": "odds_api_io",
    }


async def get_outrights(
    sport: str = "golf_pga",
    regions: str = "us",
    odds_format: str = "american",
) -> dict:
    """Get outright/futures odds."""
    return await get_odds(sport=sport, regions=regions, markets="outrights", odds_format=odds_format)


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

    Budget: ~1 events call + N odds calls per sport. For a typical day
    (NBA 10, NHL 8, MLB 15 = 33 games + 3 event calls = ~36 requests).
    Well within 100/hr limit.
    """
    sports = [
        "basketball_nba",
        "icehockey_nhl",
        "baseball_mlb",
    ]

    budget_err = _check_budget(cost=len(sports) * 5)
    if budget_err:
        return {"error": budget_err}

    # Run sequentially to avoid hammering the API
    snapshot = {}
    total_games = 0
    for s in sports:
        try:
            result = await get_odds(sport=s, regions=regions, markets=markets, odds_format=odds_format)
            snapshot[s] = result
            total_games += result.get("game_count", 0)
        except Exception as e:
            snapshot[s] = {"error": str(e), "games": []}

    return {
        "total_games": total_games,
        "sports": snapshot,
        "source": "odds_api_io",
        "usage": get_usage_status(),
    }


# ---------------------------------------------------------------------------
# Normalization: odds-api.io format -> Callisto standard format
# ---------------------------------------------------------------------------

def _normalize_event_odds(raw: dict, event_info: dict, sport: str) -> Optional[dict]:
    """
    Normalize a single event's odds response from odds-api.io to the
    standard Callisto format.

    odds-api.io response structure:
    {
        "id": 62924773,
        "home": "Phoenix Suns",
        "away": "Denver Nuggets",
        "date": "2026-03-25T03:00:00Z",
        "status": "pending",
        "bookmakers": {
            "BetMGM": [
                {"name": "ML", "updatedAt": "...", "odds": [{"home": "2.95", "away": "1.43"}]},
                {"name": "Spread", "updatedAt": "...", "odds": [{"hdp": 6.5, "home": "1.91", "away": "1.91"}]},
                {"name": "Totals", "updatedAt": "...", "odds": [{"hdp": 226.5, "over": "1.87", "under": "1.95"}]}
            ]
        }
    }
    """
    if not raw or not isinstance(raw, dict):
        return None

    home_team = raw.get("home", event_info.get("home_team", ""))
    away_team = raw.get("away", event_info.get("away_team", ""))
    commence_time = raw.get("date", event_info.get("commence_time", ""))

    game = {
        "id": str(raw.get("id", event_info.get("id", ""))),
        "sport_key": sport,
        "sport_title": SPORT_TITLES.get(sport, sport),
        "home_team": home_team,
        "away_team": away_team,
        "commence_time": commence_time,
        "bookmakers": [],
    }

    raw_bookmakers = raw.get("bookmakers", {})
    if not isinstance(raw_bookmakers, dict):
        return None

    for bm_name, bm_markets in raw_bookmakers.items():
        bm_slug = _BOOKMAKER_SLUG_MAP.get(bm_name, bm_name.lower().replace(" ", "_"))
        normalized_markets = []
        last_update = ""

        if not isinstance(bm_markets, list):
            continue

        for mkt in bm_markets:
            mkt_name = mkt.get("name", "").lower().strip()
            updated_at = mkt.get("updatedAt", "")
            if updated_at:
                last_update = updated_at

            odds_list = mkt.get("odds", [])
            if not odds_list:
                continue

            # Classify market type
            if mkt_name in ("ml", "moneyline", "1x2", "winner"):
                # Moneyline — pick the primary line (first entry)
                odds_entry = odds_list[0]
                home_dec = _safe_float(odds_entry.get("home"))
                away_dec = _safe_float(odds_entry.get("away"))
                if home_dec and away_dec:
                    normalized_markets.append({
                        "key": "h2h",
                        "last_update": updated_at,
                        "outcomes": [
                            {"name": home_team, "price": _decimal_to_american(home_dec)},
                            {"name": away_team, "price": _decimal_to_american(away_dec)},
                        ],
                    })

            elif mkt_name in ("spread", "spreads", "handicap", "point spread"):
                # Spreads — find the primary spread (closest to the main line)
                # Pick the entry with the tightest odds or the middle index
                best = _pick_primary_spread(odds_list, home_team, away_team)
                if best:
                    normalized_markets.append({
                        "key": "spreads",
                        "last_update": updated_at,
                        "outcomes": best,
                    })

            elif mkt_name in ("totals", "total", "over/under", "total points"):
                # Totals — find the primary total
                best = _pick_primary_total(odds_list)
                if best:
                    normalized_markets.append({
                        "key": "totals",
                        "last_update": updated_at,
                        "outcomes": best,
                    })

        if normalized_markets:
            game["bookmakers"].append({
                "key": bm_slug,
                "title": bm_name,
                "last_update": last_update,
                "markets": normalized_markets,
            })

    if not game["bookmakers"]:
        return None

    return game


def _pick_primary_spread(odds_list: list, home: str, away: str) -> Optional[list]:
    """
    From a list of spread entries, pick the primary (main) spread.
    The primary spread is typically the one closest to -110/-110 (even odds).
    """
    best_entry = None
    best_score = float("inf")

    for entry in odds_list:
        hdp = entry.get("hdp")
        home_dec = _safe_float(entry.get("home"))
        away_dec = _safe_float(entry.get("away"))
        if hdp is None or not home_dec or not away_dec:
            continue
        # Score: how close to even (1.91 is -110 in decimal)
        score = abs(home_dec - 1.91) + abs(away_dec - 1.91)
        if score < best_score:
            best_score = score
            best_entry = entry

    if not best_entry:
        return None

    hdp = float(best_entry["hdp"])
    home_dec = _safe_float(best_entry["home"])
    away_dec = _safe_float(best_entry["away"])

    return [
        {"name": home, "price": _decimal_to_american(home_dec), "point": hdp},
        {"name": away, "price": _decimal_to_american(away_dec), "point": -hdp},
    ]


def _pick_primary_total(odds_list: list) -> Optional[list]:
    """
    From a list of total entries, pick the primary (main) total.
    Same logic: closest to -110/-110.
    """
    best_entry = None
    best_score = float("inf")

    for entry in odds_list:
        hdp = entry.get("hdp")
        over_dec = _safe_float(entry.get("over"))
        under_dec = _safe_float(entry.get("under"))
        if hdp is None or not over_dec or not under_dec:
            continue
        score = abs(over_dec - 1.91) + abs(under_dec - 1.91)
        if score < best_score:
            best_score = score
            best_entry = entry

    if not best_entry:
        return None

    hdp = float(best_entry["hdp"])
    over_dec = _safe_float(best_entry["over"])
    under_dec = _safe_float(best_entry["under"])

    return [
        {"name": "Over", "price": _decimal_to_american(over_dec), "point": hdp},
        {"name": "Under", "price": _decimal_to_american(under_dec), "point": hdp},
    ]


def _safe_float(val) -> Optional[float]:
    """Safely convert a value to float."""
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _credits_dict() -> dict:
    """Return the credits/usage dict in standard format."""
    return {
        "remaining_this_hour": max(0, _HOURLY_LIMIT - _hourly_requests),
        "used_this_hour": _hourly_requests,
        "hourly_limit": _HOURLY_LIMIT,
        "api_key_set": bool(ODDS_API_IO_KEY),
    }


# ---------------------------------------------------------------------------
# Pro plan endpoints: value bets, arbitrage, multi-odds, historical
# ---------------------------------------------------------------------------


async def get_value_bets(bookmaker: str = "DraftKings") -> dict:
    """
    Get pre-calculated +EV bets from odds-api.io (updated every 5 seconds).

    Returns bets where the bookmaker's odds exceed the consensus fair value
    derived from all selected bookmakers. Pro plan only.
    """
    budget_err = _check_budget(1)
    if budget_err:
        return {"error": budget_err, "bets": []}

    data = await _api_get("/value-bets", {"bookmaker": bookmaker})
    if isinstance(data, dict) and data.get("error"):
        return data

    bets = data if isinstance(data, list) else []
    normalized = []
    for vb in bets:
        market = vb.get("market", {})
        bk_odds = vb.get("bookmakerOdds", {})
        ev_raw = vb.get("expectedValue", 100)
        ev_pct = (ev_raw - 100) / 100 if ev_raw > 0 else 0

        normalized.append({
            "event_id": str(vb.get("eventId", "")),
            "bookmaker": vb.get("bookmaker", bookmaker),
            "side": vb.get("betSide", ""),
            "market": market.get("name", ""),
            "line": market.get("hdp"),
            "ev_pct": round(ev_pct, 4),
            "ev_raw": ev_raw,
            "consensus_odds_home": _safe_float(market.get("home")),
            "consensus_odds_away": _safe_float(market.get("away")),
            "book_odds_home": _safe_float(bk_odds.get("home")),
            "book_odds_away": _safe_float(bk_odds.get("away")),
            "book_line": bk_odds.get("hdp"),
            "bet_url": bk_odds.get("href", ""),
            "updated_at": vb.get("expectedValueUpdatedAt", ""),
        })

    return {
        "bookmaker": bookmaker,
        "count": len(normalized),
        "bets": normalized,
        "source": "odds_api_io_pro",
        "credits": _credits_dict(),
    }


async def get_arbitrage_bets() -> dict:
    """
    Get pre-calculated arbitrage opportunities across selected bookmakers.

    Returns guaranteed-profit opportunities with optimal stake calculations.
    """
    budget_err = _check_budget(1)
    if budget_err:
        return {"error": budget_err, "arbs": []}

    data = await _api_get("/arbitrage-bets", {"bookmakers": _SELECTED_BOOKMAKERS})
    if isinstance(data, dict) and data.get("error"):
        return data

    arbs = data if isinstance(data, list) else []
    normalized = []
    for arb in arbs:
        legs = []
        for leg in arb.get("legs", []):
            legs.append({
                "bookmaker": leg.get("bookmaker", ""),
                "side": leg.get("side", ""),
                "odds_decimal": _safe_float(leg.get("odds")),
                "odds_american": _decimal_to_american(_safe_float(leg.get("odds")) or 2.0),
                "url": leg.get("directLink", ""),
            })
        normalized.append({
            "event_id": str(arb.get("eventId", "")),
            "market": arb.get("market", {}).get("name", ""),
            "profit_margin": arb.get("profitMargin", 0),
            "implied_probability": arb.get("impliedProbability", 0),
            "legs": legs,
            "optimal_stakes": arb.get("optimalStakes", []),
        })

    return {
        "count": len(normalized),
        "arbs": normalized,
        "source": "odds_api_io_pro",
        "credits": _credits_dict(),
    }


async def get_odds_multi(event_ids: list[str | int], bookmakers: str = "") -> list[dict]:
    """
    Get odds for up to 10 events in a single request (Pro plan efficiency).

    This is the key throughput multiplier: 10 events per API call.
    """
    if not event_ids:
        return []

    budget_err = _check_budget(1)
    if budget_err:
        return []

    bm = bookmakers or _SELECTED_BOOKMAKERS
    ids_str = ",".join(str(eid) for eid in event_ids[:10])
    data = await _api_get("/odds/multi", {"eventIds": ids_str, "bookmakers": bm})
    if isinstance(data, dict) and data.get("error"):
        return []

    # data should be a list of event-odds objects
    results = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
    return results


@tracked_ingestion(source="odds_api_io.v3.odds.updated", sla_seconds=300)
async def get_odds_updated(since_unix: int, sport: str = "", bookmaker: str = "") -> dict:
    """
    Get incremental odds changes since a unix timestamp (max 60s ago).

    Only returns odds that changed, not full snapshots. Efficient for
    high-frequency polling without wasting requests.
    """
    budget_err = _check_budget(1)
    if budget_err:
        return {"error": budget_err, "updates": []}

    params: dict = {"since": since_unix}
    if sport:
        mapping = SPORT_MAP.get(sport, {})
        params["sport"] = mapping.get("sport", sport)
    if bookmaker:
        params["bookmaker"] = bookmaker

    data = await _api_get("/odds/updated", params)
    if isinstance(data, dict) and data.get("error"):
        return data

    updates = data if isinstance(data, list) else []
    return {
        "count": len(updates),
        "updates": updates,
        "since": since_unix,
        "source": "odds_api_io_pro",
    }


async def get_historical_events(
    sport: str,
    from_date: str,
    to_date: str,
) -> dict:
    """
    Get historical events for a sport within a date range (max 31 days).

    Useful for backtesting: returns completed events with scores.
    """
    budget_err = _check_budget(1)
    if budget_err:
        return {"error": budget_err, "events": []}

    mapping = SPORT_MAP.get(sport, {})
    sport_slug = mapping.get("sport", sport)
    league_slug = mapping.get("league", "")

    # API requires RFC3339 format (e.g., 2026-03-20T00:00:00Z)
    if from_date and "T" not in from_date:
        from_date = f"{from_date}T00:00:00Z"
    if to_date and "T" not in to_date:
        to_date = f"{to_date}T23:59:59Z"

    params: dict = {"sport": sport_slug, "from": from_date, "to": to_date}
    if league_slug:
        params["league"] = league_slug

    data = await _api_get("/historical/events", params)
    if isinstance(data, dict) and data.get("error"):
        return data

    events = data if isinstance(data, list) else []
    return {
        "sport": sport,
        "count": len(events),
        "events": events,
        "from": from_date,
        "to": to_date,
        "source": "odds_api_io_pro",
    }


async def get_historical_odds(event_id: str | int, bookmakers: str = "") -> dict:
    """
    Get historical/closing odds + scores for a specific event.

    Returns opening odds, closing odds, and final scores. Critical for
    backtesting and closing line value (CLV) analysis.
    """
    budget_err = _check_budget(1)
    if budget_err:
        return {"error": budget_err}

    bm = bookmakers or _SELECTED_BOOKMAKERS
    data = await _api_get("/historical/odds", {
        "eventId": str(event_id),
        "bookmakers": bm,
    })
    return data if isinstance(data, dict) else {"data": data}


async def get_odds_movements(
    event_id: str | int,
    bookmaker: str = "DraftKings",
    market: str = "ML",
) -> dict:
    """
    Get full line movement history for an event (opening to current/closing).

    Shows every price change for the specified bookmaker+market combination.
    """
    budget_err = _check_budget(1)
    if budget_err:
        return {"error": budget_err}

    data = await _api_get("/odds/movements", {
        "eventId": str(event_id),
        "bookmaker": bookmaker,
        "market": market,
    })
    return data if isinstance(data, dict) else {"data": data}


@tracked_ingestion(
    source=lambda sport="", **_: f"odds_api_io.v3.live_events.{sport or 'all'}",
    sla_seconds=300,
)
async def get_live_events(sport: str = "") -> dict:
    """Get currently live (in-play) events."""
    budget_err = _check_budget(1)
    if budget_err:
        return {"error": budget_err, "events": []}

    params: dict = {}
    if sport:
        mapping = SPORT_MAP.get(sport, {})
        params["sport"] = mapping.get("sport", sport)

    data = await _api_get("/events/live", params)
    events = data if isinstance(data, list) else []
    return {
        "count": len(events),
        "events": events,
        "source": "odds_api_io_pro",
    }


# ---------------------------------------------------------------------------
# Utility: compare with other sources
# ---------------------------------------------------------------------------

def find_best_line(game: dict, market: str = "spreads", team: str = "") -> dict:
    """
    Compare lines across bookmakers for a game and find the best available.

    Same interface as odds_api.find_best_line() — works with any game dict
    in the standard format regardless of source.
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
