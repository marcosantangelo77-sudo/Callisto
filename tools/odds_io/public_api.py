"""Core public API endpoints extracted from tools/odds_api_io.py.

Split out of tools/odds_api_io.py — see tools/odds_io package docstring.
Holds the pre-match fetch surface: sport discovery, event listing, per-event
and full-slate odds, scores, outrights, and the multi-sport snapshot batch.

The public surface remains importable from tools.odds_api_io (facade).
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from tools.ingestion_tracking import tracked_ingestion
from tools.odds_io.config import (
    ODDS_API_IO_KEY,
    SELECTED_BOOKMAKERS as _SELECTED_BOOKMAKERS,
    SPORT_TITLES,
    resolve_sport,
)
from tools.odds_io.http_client import api_get as _api_get
from tools.odds_io.normalize import normalize_event_odds as _normalize_event_odds
from tools.odds_io.usage import (
    check_budget as _check_budget,
    get_usage_status,
    hourly_remaining,
)

logger = logging.getLogger("callisto.odds_api_io")


def credits_dict() -> dict:
    """Return the credits/usage dict in standard format."""
    from tools.odds_io.usage import _hourly_requests
    from tools.odds_io.config import HOURLY_LIMIT
    return {
        "remaining_this_hour": max(0, HOURLY_LIMIT - _hourly_requests),
        "used_this_hour": _hourly_requests,
        "hourly_limit": HOURLY_LIMIT,
        "api_key_set": bool(ODDS_API_IO_KEY),
    }


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
    mapping = resolve_sport(sport)
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
    mapping = resolve_sport(sport)
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
            "credits": credits_dict(),
        }

    # Filter to today's and tomorrow's games only (not 2+ weeks out).
    # This is critical: NBA has 150+ pending events spanning weeks. We only
    # want the immediate slate to conserve the 100 req/hr budget.
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
            "credits": credits_dict(),
        }

    # Budget check: we need 1 request per event
    budget_err = _check_budget(cost=len(pending_events))
    if budget_err:
        # Try to fetch as many as we can afford
        remaining = hourly_remaining()
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
        "credits": credits_dict(),
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
    mapping = resolve_sport(sport)
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
