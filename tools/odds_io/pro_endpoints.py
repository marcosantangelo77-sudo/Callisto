"""Pro-plan endpoints extracted from tools/odds_api_io.py.

Split out of tools/odds_api_io.py — see tools/odds_io package docstring.
Holds the odds-api.io Pro plan surface: value bets, arbitrage, multi-event
odds, incremental updates, historical events/odds/movements, pre-commence
snapshots, and live event listing.

The public surface remains importable from tools.odds_api_io (facade).
"""

import logging

from tools.ingestion_tracking import tracked_ingestion
from tools.odds_io.config import (
    SELECTED_BOOKMAKERS as _SELECTED_BOOKMAKERS,
    SPORT_MAP,
)
from tools.odds_io.http_client import api_get as _api_get
from tools.odds_io.normalize import (
    decimal_to_american as _decimal_to_american,
    safe_float as _safe_float,
)
from tools.odds_io.persist import build_historical_snapshot
from tools.odds_io.public_api import credits_dict
from tools.odds_io.usage import check_budget as _check_budget

logger = logging.getLogger("callisto.odds_api_io")


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
        "credits": credits_dict(),
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
        "credits": credits_dict(),
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
