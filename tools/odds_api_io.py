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

This module is now a FACADE over tools.odds_io (HTTP / parse / persist
helpers were split into that package). All existing imports keep working:
every public name is re-exported here unchanged.
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from dotenv import load_dotenv

from tools.ingestion_tracking import tracked_ingestion
from tools.odds_io.config import (
    BOOKMAKER_SLUG_MAP as _BOOKMAKER_SLUG_MAP,
    HOURLY_LIMIT as _HOURLY_LIMIT,
    ODDS_API_IO_BASE,
    ODDS_API_IO_KEY,
    SELECTED_BOOKMAKERS as _SELECTED_BOOKMAKERS,
    SPORT_MAP,
    SPORT_TITLES,
    close_client,
    get_client as _get_client,
    resolve_sport,
)
from tools.odds_io.usage import (
    check_budget as _check_budget,
    get_usage_status,
    hourly_remaining,
    increment_usage,
    load_usage,
    save_usage,
)
from tools.odds_io.http_client import (
    BACKOFF_MAX_RETRIES as _BACKOFF_MAX_RETRIES,
    api_get as _api_get,
    compute_backoff as _compute_backoff,
)
from tools.odds_io.normalize import (
    decimal_to_american as _decimal_to_american,
    extract_movement_snapshots as _extract_movement_snapshots,
    find_best_line,
    normalize_event_odds as _normalize_event_odds,
    parse_iso as _parse_iso,
    pick_pre_commence_entry as _pick_pre_commence_entry,
    pick_primary_spread as _pick_primary_spread,
    pick_primary_total as _pick_primary_total,
    safe_float as _safe_float,
    snapshot_to_market_outcomes as _snapshot_to_market_outcomes,
)
from tools.odds_io.persist import build_historical_snapshot

load_dotenv()

logger = logging.getLogger("callisto.odds_api_io")

__all__ = [
    # config
    "SPORT_MAP", "SPORT_TITLES", "ODDS_API_IO_KEY", "ODDS_API_IO_BASE",
    "_HOURLY_LIMIT", "_SELECTED_BOOKMAKERS", "_BOOKMAKER_SLUG_MAP",
    # client
    "close_client",
    # usage
    "get_usage_status", "load_usage", "save_usage", "increment_usage",
    "_check_budget", "hourly_remaining",
    # http
    "_api_get", "_compute_backoff", "_BACKOFF_MAX_RETRIES",
    # normalize
    "_decimal_to_american", "_safe_float", "_normalize_event_odds",
    "_pick_primary_spread", "_pick_primary_total", "find_best_line",
    "_parse_iso", "_extract_movement_snapshots", "_snapshot_to_market_outcomes",
    "_pick_pre_commence_entry",
    # public API
    "get_sports", "get_events", "get_odds", "get_event_odds", "get_scores",
    "get_outrights", "snapshot_all_sports", "get_value_bets",
    "get_arbitrage_bets", "get_odds_multi", "get_odds_updated",
    "get_historical_events", "get_historical_odds", "get_odds_movements",
    "get_historical_snapshot", "get_live_events",
]


def _credits_dict() -> dict:
    """Return the credits/usage dict in standard format."""
    from tools.odds_io.usage import _hourly_requests
    return {
        "remaining_this_hour": max(0, _HOURLY_LIMIT - _hourly_requests),
        "used_this_hour": _hourly_requests,
        "hourly_limit": _HOURLY_LIMIT,
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
            "credits": _credits_dict(),
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
            "credits": _credits_dict(),
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


@tracked_ingestion(
    source=lambda event_id="", bookmaker="DraftKings", market="ML", **_:
        f"odds_api_io.v3.movements.{market}",
    sla_seconds=600,
)
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
    source=lambda event_id="", **_: "odds_api_io.v3.movements.snapshot",
    sla_seconds=600,
)
async def get_historical_snapshot(
    event_id: str | int,
    commence_time: str = "",
    minutes_before_commence: int = 60,
    bookmakers: str = "",
    markets: tuple = ("ML", "Spread", "Totals"),
) -> dict:
    """Fetch a timestamped PRE-COMMENCE odds snapshot for an event.

    Facade over tools.odds_io.persist.build_historical_snapshot — see the
    docstring there for the full lookahead-avoidance rationale.

    Dual-mode fallback:
      - If no pre-commence snapshot exists for a given (book, market)
        combination, fall back to closing odds tagged 'closing_fallback'.
      - If minutes_before_commence == 0, skip movements entirely and return
        closing odds tagged 'closing_mode'.
    """
    return await build_historical_snapshot(
        event_id=event_id,
        commence_time=commence_time,
        minutes_before_commence=minutes_before_commence,
        bookmakers=bookmakers,
        markets=markets,
        movements_fetch=get_odds_movements,
        closing_fetch=get_historical_odds,
    )


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
