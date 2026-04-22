"""
The Odds API integration for Callisto — real-time sports betting odds.

Live and pre-match odds from 40+ bookmakers across 70+ sports.
Free tier: 500 credits/month. Each call costs (markets x regions) credits.

This tool enables quantitative betting strategy:
- Real-time line snapshots for movement detection
- Cross-bookmaker comparison for edge identification
- Live in-game odds for overreaction exploitation
"""

import asyncio
import json
import logging
import os
import time
from typing import Optional

import httpx
from dotenv import load_dotenv

from tools.ingestion_tracking import tracked_ingestion

load_dotenv()

logger = logging.getLogger("callisto.odds_api")

# Configuration
ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")
ODDS_API_BASE = "https://api.odds-api.io/v4"

# SECURITY (audit C-1, 2026-04-18): odds-api.io v4 only accepts apiKey as a query
# parameter — there is no Authorization header endpoint. To prevent the key leaking
# to console / rotating log files, logging_config.py downgrades the httpx logger to
# WARNING. Do not log raw URLs from this module — use _redact_url() if you must.


def _redact_url(url: str) -> str:
    """Return a URL with apiKey query value masked. Use for any local logging."""
    import re as _re
    return _re.sub(r"(apiKey=)[^&\s]+", r"\1<redacted>", url)

# Credit tracking
_credits_remaining: Optional[int] = None
_credits_used: Optional[int] = None

# Shared client
_client: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=15.0)
    return _client


async def close_client() -> None:
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
        _client = None


def _update_credits(headers: dict) -> None:
    """Track API credit usage from response headers."""
    global _credits_remaining, _credits_used
    if "x-requests-remaining" in headers:
        _credits_remaining = int(headers["x-requests-remaining"])
    if "x-requests-used" in headers:
        _credits_used = int(headers["x-requests-used"])
    logger.info(f"Odds API credits: {_credits_remaining} remaining, {_credits_used} used")


def get_credit_status() -> dict:
    """Return current credit usage."""
    return {
        "remaining": _credits_remaining,
        "used": _credits_used,
        "api_key_set": bool(ODDS_API_KEY),
    }


@tracked_ingestion(source="odds_api.v4.sports", sla_seconds=3600)
async def get_sports() -> list[dict]:
    """List available sports. FREE — costs 0 credits."""
    if not ODDS_API_KEY:
        return [{"error": "ODDS_API_KEY not set in .env"}]

    client = _get_client()
    try:
        resp = await client.get(
            f"{ODDS_API_BASE}/sports/",
            params={"apiKey": ODDS_API_KEY},
        )
        resp.raise_for_status()
        _update_credits(dict(resp.headers))
        return resp.json()
    except Exception as e:
        logger.error(f"Failed to fetch sports: {e}")
        return [{"error": str(e)}]


@tracked_ingestion(
    source=lambda sport="basketball_ncaab", **_: f"odds_api.v4.odds.{sport}",
    sla_seconds=300,
)
async def get_odds(
    sport: str = "basketball_ncaab",
    regions: str = "us",
    markets: str = "h2h,spreads,totals",
    odds_format: str = "american",
) -> dict:
    """
    Get live and upcoming odds for a sport.

    Credit cost: len(markets.split(',')) * len(regions.split(','))
    Example: markets='h2h,spreads,totals' regions='us' = 3 credits

    Args:
        sport: Sport key (e.g., 'basketball_ncaab', 'americanfootball_nfl')
        regions: Bookmaker regions, comma-separated ('us', 'us,uk', 'eu')
        markets: Market types, comma-separated ('h2h', 'spreads', 'totals')
        odds_format: 'american' or 'decimal'

    Returns:
        Dict with games, odds, and credit info.
    """
    if not ODDS_API_KEY:
        return {"error": "ODDS_API_KEY not set in .env", "games": []}

    client = _get_client()
    try:
        resp = await client.get(
            f"{ODDS_API_BASE}/sports/{sport}/odds/",
            params={
                "apiKey": ODDS_API_KEY,
                "regions": regions,
                "markets": markets,
                "oddsFormat": odds_format,
                "dateFormat": "iso",
            },
        )
        resp.raise_for_status()
        _update_credits(dict(resp.headers))
        games = resp.json()

        return {
            "sport": sport,
            "game_count": len(games),
            "games": games,
            "credits": get_credit_status(),
        }
    except httpx.HTTPStatusError as e:
        # 404 = sport not currently active (off-season), demote to debug
        if e.response.status_code == 404:
            logger.debug(f"Odds API 404 (sport inactive): {sport}")
        else:
            logger.error(f"Odds API HTTP error: {e.response.status_code}")
        return {"error": f"HTTP {e.response.status_code}", "games": []}
    except Exception as e:
        logger.error(f"Odds API error: {e}")
        return {"error": str(e), "games": []}


@tracked_ingestion(
    source=lambda sport="basketball_ncaab", **_: f"odds_api.v4.scores.{sport}",
    sla_seconds=600,
)
async def get_scores(
    sport: str = "basketball_ncaab",
    days_from: int = 1,
) -> dict:
    """
    Get live scores and recently completed games.
    Costs 0 credits for in-season sports.

    Args:
        sport: Sport key
        days_from: Number of days back to include completed games (1-3)
    """
    if not ODDS_API_KEY:
        return {"error": "ODDS_API_KEY not set in .env", "games": []}

    client = _get_client()
    try:
        resp = await client.get(
            f"{ODDS_API_BASE}/sports/{sport}/scores/",
            params={
                "apiKey": ODDS_API_KEY,
                "daysFrom": days_from,
                "dateFormat": "iso",
            },
        )
        resp.raise_for_status()
        _update_credits(dict(resp.headers))
        games = resp.json()

        return {
            "sport": sport,
            "game_count": len(games),
            "games": games,
        }
    except Exception as e:
        logger.error(f"Scores API error: {e}")
        return {"error": str(e), "games": []}


@tracked_ingestion(
    source=lambda sport, event_id, **_: f"odds_api.v4.event_odds.{sport}",
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
    Get odds for a single event. Useful for tracking line movement on a specific game.

    Args:
        sport: Sport key
        event_id: The event ID from get_odds()
        regions: Bookmaker regions
        markets: Market types
        odds_format: 'american' or 'decimal'
    """
    if not ODDS_API_KEY:
        return {"error": "ODDS_API_KEY not set in .env"}

    client = _get_client()
    try:
        resp = await client.get(
            f"{ODDS_API_BASE}/sports/{sport}/events/{event_id}/odds",
            params={
                "apiKey": ODDS_API_KEY,
                "regions": regions,
                "markets": markets,
                "oddsFormat": odds_format,
                "dateFormat": "iso",
            },
        )
        resp.raise_for_status()
        _update_credits(dict(resp.headers))
        return resp.json()
    except Exception as e:
        logger.error(f"Event odds error: {e}")
        return {"error": str(e)}


@tracked_ingestion(
    source=lambda sport, event_id, **_: f"odds_api.v4.alt_lines.{sport}",
    sla_seconds=900,
)
async def get_alternate_lines(
    sport: str,
    event_id: str,
    regions: str = "us",
    odds_format: str = "american",
) -> dict:
    """
    Get alternate spreads and totals for a specific event.

    Alternate lines are the foundation of parlay construction —
    each alternate spread/total is a different risk/reward profile.
    Cross-book pricing on alternates diverges more than standard lines.

    Also pulls player props when available (NBA, NFL game days).
    """
    if not ODDS_API_KEY:
        return {"error": "ODDS_API_KEY not set in .env"}

    client = _get_client()
    markets = "alternate_spreads,alternate_totals"

    try:
        resp = await client.get(
            f"{ODDS_API_BASE}/sports/{sport}/events/{event_id}/odds",
            params={
                "apiKey": ODDS_API_KEY,
                "regions": regions,
                "markets": markets,
                "oddsFormat": odds_format,
                "dateFormat": "iso",
            },
        )
        resp.raise_for_status()
        _update_credits(dict(resp.headers))
        data = resp.json()

        # Structure: separate alternate spreads and totals by bookmaker
        result = {
            "event_id": event_id,
            "sport": sport,
            "home_team": data.get("home_team", ""),
            "away_team": data.get("away_team", ""),
            "bookmakers": data.get("bookmakers", []),
            "credits": get_credit_status(),
        }
        return result
    except Exception as e:
        logger.error(f"Alternate lines error: {e}")
        return {"error": str(e)}


@tracked_ingestion(
    source=lambda sport, event_id, **_: f"odds_api.v4.player_props.{sport}",
    sla_seconds=900,
)
async def get_player_props(
    sport: str,
    event_id: str,
    prop_markets: str = "player_points,player_rebounds,player_assists",
    regions: str = "us",
    odds_format: str = "american",
) -> dict:
    """
    Get player prop lines for a specific event.

    Player props are where the biggest edges live — books price based on
    season averages but context (injuries, matchups, role changes) creates
    systematic mispricings.

    Available markets: player_points, player_rebounds, player_assists,
    player_threes, player_points_rebounds_assists, player_points_rebounds,
    player_points_assists, player_rebounds_assists
    """
    if not ODDS_API_KEY:
        return {"error": "ODDS_API_KEY not set in .env"}

    client = _get_client()
    try:
        resp = await client.get(
            f"{ODDS_API_BASE}/sports/{sport}/events/{event_id}/odds",
            params={
                "apiKey": ODDS_API_KEY,
                "regions": regions,
                "markets": prop_markets,
                "oddsFormat": odds_format,
                "dateFormat": "iso",
            },
        )
        resp.raise_for_status()
        _update_credits(dict(resp.headers))
        data = resp.json()

        # Flatten props by player for easy analysis
        players = {}
        for bm in data.get("bookmakers", []):
            for mkt in bm.get("markets", []):
                for outcome in mkt.get("outcomes", []):
                    player = outcome.get("description", "")
                    if not player:
                        continue
                    if player not in players:
                        players[player] = []
                    players[player].append({
                        "bookmaker": bm["title"],
                        "market": mkt["key"],
                        "name": outcome.get("name", ""),  # Over/Under
                        "price": outcome.get("price", 0),
                        "point": outcome.get("point"),
                    })

        return {
            "event_id": event_id,
            "sport": sport,
            "players": players,
            "player_count": len(players),
            "bookmakers": data.get("bookmakers", []),
            "credits": get_credit_status(),
        }
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 422:
            return {"error": "Player props not available for this sport/event", "players": {}}
        logger.error(f"Player props error: {e.response.status_code}")
        return {"error": f"HTTP {e.response.status_code}", "players": {}}
    except Exception as e:
        logger.error(f"Player props error: {e}")
        return {"error": str(e), "players": {}}


def find_best_line(game: dict, market: str = "spreads", team: str = "") -> dict:
    """
    Compare lines across bookmakers for a game and find the best available.

    This is where edges live — different books price differently.

    Args:
        game: A game dict from get_odds()
        market: 'h2h', 'spreads', or 'totals'
        team: Team name to find best line for (for spreads/h2h)

    Returns:
        Dict with best line, bookmaker, and comparison across books.
    """
    bookmaker_lines = []
    for bm in game.get("bookmakers", []):
        for mkt in bm.get("markets", []):
            if mkt["key"] != market:
                continue
            for outcome in mkt.get("outcomes", []):
                entry = {
                    "bookmaker": bm["title"],
                    "name": outcome.get("name", ""),
                    "price": outcome.get("price", 0),
                    "point": outcome.get("point"),
                    "last_update": bm.get("last_update", ""),
                    # Our own ingest timestamp — set by
                    # line_monitor._stamp_snapshot_fetched_at. When present
                    # this is what edge_scanner uses for freshness decay
                    # (strictly more meaningful than the book's
                    # self-reported last_update). Falls back to bm.last_update
                    # on legacy snapshots.
                    "fetched_at": (
                        outcome.get("fetched_at")
                        or bm.get("fetched_at")
                        or bm.get("last_update", "")
                    ),
                }
                if not team or team.lower() in outcome.get("name", "").lower():
                    bookmaker_lines.append(entry)

    if not bookmaker_lines:
        return {"error": "No lines found", "lines": []}

    # H2H contamination guard: if a team's lines contain BOTH large positive
    # AND large negative prices, two sides of the market leaked into one
    # team's line set (e.g. correct underdog +500 mixed with opponent's
    # favorite -700 due to a scraper home/away swap).  Keep only the
    # majority sign to purge the contaminated entries.
    if market == "h2h" and len(bookmaker_lines) >= 3:
        prices = [l["price"] for l in bookmaker_lines]
        has_big_pos = any(p > 150 for p in prices)
        has_big_neg = any(p < -150 for p in prices)
        if has_big_pos and has_big_neg:
            n_pos = sum(1 for p in prices if p > 0)
            n_neg = sum(1 for p in prices if p < 0)
            if n_pos >= n_neg:
                # Team is an underdog — keep positive lines only
                bookmaker_lines = [l for l in bookmaker_lines if l["price"] > 0]
            else:
                # Team is a favorite — keep negative lines only
                bookmaker_lines = [l for l in bookmaker_lines if l["price"] < 0]
            logger.warning(
                f"H2H contamination filtered for {team}: kept "
                f"{len(bookmaker_lines)} lines (pos={n_pos}, neg={n_neg})"
            )

    if not bookmaker_lines:
        return {"error": "No lines found after contamination filter", "lines": []}

    # Sort by best price (highest for positive odds, highest for negative = closest to 0)
    bookmaker_lines.sort(key=lambda x: x["price"], reverse=True)

    return {
        "best": bookmaker_lines[0],
        "worst": bookmaker_lines[-1],
        "spread_across_books": bookmaker_lines[0]["price"] - bookmaker_lines[-1]["price"],
        "all_lines": bookmaker_lines,
    }


def detect_line_movement(snapshot_old: dict, snapshot_new: dict) -> list[dict]:
    """
    Compare two odds snapshots and detect significant line movements.

    This is the core of the overreaction strategy:
    - Large movement after observable event = potential +EV opportunity
    - Movement direction vs event impact = gauge market efficiency

    Args:
        snapshot_old: Previous odds snapshot (from get_odds or get_event_odds)
        snapshot_new: Current odds snapshot

    Returns:
        List of movements with direction, magnitude, and bookmaker.
    """
    movements = []

    old_lines = {}
    new_lines = {}

    for snapshot, store in [(snapshot_old, old_lines), (snapshot_new, new_lines)]:
        games = snapshot.get("games", [snapshot]) if isinstance(snapshot, dict) else [snapshot]
        for game in games:
            game_id = game.get("id", "unknown")
            for bm in game.get("bookmakers", []):
                for mkt in bm.get("markets", []):
                    for outcome in mkt.get("outcomes", []):
                        key = (game_id, bm["key"], mkt["key"], outcome.get("name", ""))
                        store[key] = {
                            "price": outcome.get("price", 0),
                            "point": outcome.get("point"),
                            "bookmaker": bm["title"],
                            "market": mkt["key"],
                            "team": outcome.get("name", ""),
                        }

    # Find movements
    for key in new_lines:
        if key not in old_lines:
            continue
        old = old_lines[key]
        new = new_lines[key]

        price_diff = new["price"] - old["price"]
        point_diff = (new["point"] or 0) - (old["point"] or 0)

        if abs(price_diff) >= 5 or abs(point_diff) >= 0.5:
            movements.append({
                "team": new["team"],
                "market": new["market"],
                "bookmaker": new["bookmaker"],
                "old_price": old["price"],
                "new_price": new["price"],
                "price_movement": price_diff,
                "old_point": old["point"],
                "new_point": new["point"],
                "point_movement": point_diff,
                "direction": "favorable" if price_diff > 0 else "unfavorable",
            })

    movements.sort(key=lambda x: abs(x["price_movement"]), reverse=True)
    return movements


def calculate_implied_probability(american_odds: int) -> float:
    """Convert American odds to implied probability."""
    if american_odds > 0:
        return 100 / (american_odds + 100)
    else:
        return abs(american_odds) / (abs(american_odds) + 100)


def calculate_ev(
    probability: float,
    american_odds: int,
    stake: float = 100,
) -> dict:
    """
    Calculate expected value of a bet.

    +EV = edge exists. This is the only reason to bet.

    Args:
        probability: Your estimated true probability (0.0-1.0)
        american_odds: The line being offered
        stake: Bet amount

    Returns:
        Dict with EV, implied prob, edge, and Kelly criterion sizing.
    """
    implied = calculate_implied_probability(american_odds)

    if american_odds > 0:
        payout = stake * (american_odds / 100)
    else:
        payout = stake * (100 / abs(american_odds))

    ev = (probability * payout) - ((1 - probability) * stake)
    edge = probability - implied

    # Kelly criterion: optimal bet sizing
    # f* = (bp - q) / b where b=payout ratio, p=true prob, q=1-p
    b = payout / stake
    kelly = (b * probability - (1 - probability)) / b if b > 0 else 0
    kelly = max(0, kelly)  # Never negative (don't bet if -EV)

    return {
        "expected_value": round(ev, 2),
        "implied_probability": round(implied, 4),
        "your_probability": round(probability, 4),
        "edge": round(edge, 4),
        "kelly_fraction": round(kelly, 4),
        "recommended_stake_pct": round(kelly * 100, 2),
        "is_positive_ev": ev > 0,
    }
