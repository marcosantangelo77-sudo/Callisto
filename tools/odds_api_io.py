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

This module is now a pure FACADE over tools.odds_io. All implementation
(code endpoints in tools.odds_io.public_api, Pro-plan endpoints in
tools.odds_io.pro_endpoints, HTTP / parse / persist helpers in the other
package modules) lives there; every public name is re-exported here
unchanged so all existing imports keep working.
"""

from tools.ingestion_tracking import tracked_ingestion
from dotenv import load_dotenv

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
from tools.odds_io.public_api import (
    credits_dict as _credits_dict,
    get_events,
    get_event_odds,
    get_odds,
    get_sports,
    get_scores,
    get_outrights,
    snapshot_all_sports,
)
from tools.odds_io.pro_endpoints import (
    get_arbitrage_bets,
    get_historical_events,
    get_historical_odds,
    get_live_events,
    get_odds_movements,
    get_odds_multi,
    get_odds_updated,
    get_value_bets,
)

load_dotenv()

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

    Kept in the facade so tests can patch ``tools.odds_api_io.get_odds_movements``
    / ``get_historical_odds`` and have the fetchers resolve through this
    module namespace. Delegates assembly to
    tools.odds_io.persist.build_historical_snapshot.
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
