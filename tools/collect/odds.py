"""
ESPN core-API odds collector: spreads, totals, moneylines, win probs.
"""

from __future__ import annotations

import difflib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from tools.collect.http import _get_client
from tools.collect.espn import ESPN_SPORTS, ESPN_BASE, get_today_event_ids


logger = logging.getLogger("callisto.data_collector")

# ESPN hidden core API (free, no auth)
ESPN_CORE_BASE = "https://sports.core.api.espn.com/v2/sports"
# Core API uses slightly different league keys than the site API
ESPN_CORE_LEAGUES = {
    "basketball_nba": ("basketball", "nba"),
    "basketball_ncaab": ("basketball", "mens-college-basketball"),
    "americanfootball_nfl": ("football", "nfl"),
    "icehockey_nhl": ("hockey", "nhl"),
    "baseball_mlb": ("baseball", "mlb"),
    "golf_pga": ("golf", "pga"),
}


async def collect_espn_odds(
    dc,
    sport: str,
    event_ids: list[str] = None,
) -> list[dict]:
    """
    Fetch odds data from ESPN's hidden core API.

    Supplementary free data — errors log and return empty, never crash.

    Args:
        sport: Odds API sport key (e.g., 'basketball_nba')
        event_ids: Specific ESPN event IDs. If None, pulls today's scoreboard.

    Returns:
        List of dicts with event_id, teams, odds lines, and win probabilities.
    """
    core_sport = ESPN_CORE_LEAGUES.get(sport)
    if not core_sport:
        logger.warning(f"collect_espn_odds: unsupported sport {sport}")
        return []

    core_category, core_league = core_sport
    client = await _get_client()

    # If no event IDs supplied, get them from today's scoreboard
    if not event_ids:
        event_ids = await get_today_event_ids(sport)
        if not event_ids:
            return []

    results = []
    for eid in event_ids:
        try:
            entry = await _fetch_event_odds(
                client, core_category, core_league, eid,
            )
            if entry:
                results.append(entry)
        except Exception as e:
            logger.warning(f"ESPN odds fetch failed for event {eid}: {e}")
            continue

    logger.info(
        f"Collected ESPN odds for {len(results)}/{len(event_ids)} "
        f"events ({sport})"
    )
    return results

async def _fetch_event_odds(
    dc,
    client: httpx.AsyncClient,
    core_category: str,
    core_league: str,
    event_id: str,
) -> Optional[dict]:
    """Fetch odds and win probabilities for a single ESPN event."""
    base = (
        f"{ESPN_CORE_BASE}/{core_category}/leagues/{core_league}"
        f"/events/{event_id}/competitions/{event_id}"
    )

    # ── Odds (spreads, totals, moneylines from multiple books) ──
    odds_data = []
    try:
        resp = await client.get(f"{base}/odds", params={"limit": 50})
        resp.raise_for_status()
        raw_odds = resp.json()
        for item in raw_odds.get("items", []):
            provider = item.get("provider", {}).get("name", "unknown")
            odds_data.append({
                "provider": provider,
                "provider_id": item.get("provider", {}).get("id"),
                "spread": item.get("spread"),
                "over_under": item.get("overUnder"),
                "home_ml": item.get("homeTeamOdds", {}).get("moneyLine"),
                "away_ml": item.get("awayTeamOdds", {}).get("moneyLine"),
                "home_spread_odds": item.get("homeTeamOdds", {}).get("spreadOdds"),
                "away_spread_odds": item.get("awayTeamOdds", {}).get("spreadOdds"),
                "over_odds": item.get("overOdds"),
                "under_odds": item.get("underOdds"),
                "details": item.get("details"),
            })
    except Exception as e:
        logger.warning(f"ESPN odds endpoint failed for {event_id}: {e}")

    # ── Win probabilities (live or pregame) ──
    probabilities = []
    try:
        resp = await client.get(
            f"{base}/probabilities", params={"limit": 200},
        )
        resp.raise_for_status()
        raw_probs = resp.json()
        for item in raw_probs.get("items", []):
            probabilities.append({
                "home_win_pct": item.get("homeWinPercentage"),
                "away_win_pct": item.get("awayWinPercentage"),
                "tie_pct": item.get("tiePercentage"),
                "sequence": item.get("sequenceNumber"),
            })
    except Exception as e:
        logger.warning(f"ESPN probabilities endpoint failed for {event_id}: {e}")

    if not odds_data and not probabilities:
        return None

    return {
        "event_id": event_id,
        "odds": odds_data,
        "probabilities": probabilities,
    }