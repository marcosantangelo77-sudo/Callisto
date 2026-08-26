"""High-level scraping entry points for Action Network."""

import logging
from typing import Optional

from tools.actionnet.constants import LEAGUE_MAP
from tools.actionnet.http import close_client, rate_limited_get
from tools.actionnet.parser import build_url, extract_public_betting, parse_game

logger = logging.getLogger("callisto.actionnet.scraper")

__all__ = ["close_client", "get_public_betting", "scrape_action_network"]


async def scrape_action_network(sport: str, date_str: Optional[str] = None) -> dict:
    """
    Scrape Action Network pregame odds for a sport.

    Returns data in the same format as tools/odds_api.get_odds() so the
    rest of the system can consume it interchangeably.

    Args:
        sport: Callisto sport key (e.g., "basketball_nba", "americanfootball_nfl")
        date_str: Optional date in YYYYMMDD format. Defaults to today (UTC).

    Returns:
        Standard Callisto odds dict with games, bookmakers, and markets.
    """
    if sport not in LEAGUE_MAP:
        return {
            "sport": sport,
            "game_count": 0,
            "games": [],
            "source": "action_network",
            "error": f"Unsupported sport: {sport}. Supported: {list(LEAGUE_MAP.keys())}",
        }

    try:
        url = build_url(sport, date_str)
        logger.info(f"Scraping Action Network {LEAGUE_MAP[sport].upper()}: {url}")
        data = await rate_limited_get(url)
    except Exception as e:
        logger.error(f"Action Network request failed for {sport}: {e}")
        return {
            "sport": sport,
            "game_count": 0,
            "games": [],
            "source": "action_network",
            "error": str(e),
        }

    # Parse games
    raw_games = data.get("games", [])
    games = []
    for raw_game in raw_games:
        parsed = parse_game(raw_game, sport)
        if parsed:
            games.append(parsed)

    logger.info(
        f"Action Network {sport}: {len(games)} games with odds "
        f"(from {len(raw_games)} total games)"
    )

    return {
        "sport": sport,
        "game_count": len(games),
        "games": games,
        "source": "action_network",
        "credits": {"remaining": None, "used": None, "api_key_set": True},
    }


async def get_public_betting(sport: str, date_str: Optional[str] = None) -> dict:
    """
    Get public betting percentages from Action Network.

    This data shows what percentage of public bets are on each side —
    valuable for contrarian / fading-the-public strategies.

    Args:
        sport: Callisto sport key (e.g., "basketball_nba")
        date_str: Optional date in YYYYMMDD format. Defaults to today (UTC).

    Returns:
        Dict with per-game public betting percentages from all available books.
    """
    if sport not in LEAGUE_MAP:
        return {
            "sport": sport,
            "games": [],
            "error": f"Unsupported sport: {sport}",
        }

    try:
        url = build_url(sport, date_str)
        logger.info(f"Fetching public betting data from Action Network: {LEAGUE_MAP[sport].upper()}")
        data = await rate_limited_get(url)
    except Exception as e:
        logger.error(f"Action Network public betting request failed for {sport}: {e}")
        return {
            "sport": sport,
            "games": [],
            "error": str(e),
        }

    raw_games = data.get("games", [])
    results = []
    for raw_game in raw_games:
        parsed = extract_public_betting(raw_game, sport)
        if parsed:
            results.append(parsed)

    logger.info(f"Action Network public betting {sport}: {len(results)} games with data")

    return {
        "sport": sport,
        "game_count": len(results),
        "games": results,
        "source": "action_network_public_betting",
    }
