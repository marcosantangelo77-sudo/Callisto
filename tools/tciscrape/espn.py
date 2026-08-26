"""ESPN API fetchers: rosters, team info, team search."""

import logging
from typing import Optional

from tools.tciscrape.constants import (
    COACHING_TENURE_FALLBACK,
    ESPN_BASE,
    RELIGIOUS_PROGRAMS,
)
from tools.tciscrape.http import _espn_get

logger = logging.getLogger("callisto.tci")

# ESPN team lookup cache — capped to prevent unbounded memory growth.
_team_cache: dict[str, dict] = {}
_TEAM_CACHE_MAX = 500

_espn_teams_cache: dict[str, tuple[list, float]] = {}  # key -> (teams, timestamp)
_ESPN_CACHE_TTL = 3600  # 1 hour
_ESPN_CACHE_MAX = 50    # hard cap — evict oldest on overflow


async def get_team_roster(
    team_id: str,
    sport: str = "basketball",
    league: str = "womens-college-basketball",
    season: int = 2026,
) -> dict:
    """
    Fetch team roster from ESPN API.

    Returns dict with players and their metadata (class year, hometown, etc.)
    """
    url = f"{ESPN_BASE}/{sport}/{league}/teams/{team_id}/roster?season={season}"
    try:
        data = await _espn_get(url)
        # ESPN roster: athletes are flat in the list (not nested in groups)
        athletes = data.get("athletes", [])
        players = []
        for player in athletes:
            bp = player.get("birthPlace", {})
            info = {
                "name": player.get("displayName", ""),
                "position": player.get("position", {}).get("abbreviation", ""),
                "class_year": player.get("experience", {}).get("displayValue", ""),
                "years_exp": player.get("experience", {}).get("years", 0),
                "hometown": bp.get("city", ""),
                "home_state": bp.get("state", ""),
                "home_country": bp.get("country", player.get("birthCountry", {}).get("abbreviation", "USA")),
                "jersey": player.get("jersey", ""),
                "height": player.get("displayHeight", ""),
            }
            players.append(info)
        return {
            "team_id": team_id,
            "team_name": data.get("team", {}).get("displayName", ""),
            "season": season,
            "players": players,
            "player_count": len(players),
        }
    except Exception as e:
        logger.warning(f"Failed to fetch roster for team {team_id}: {e}")
        return {"team_id": team_id, "players": [], "error": str(e)}


async def get_team_info(
    team_id: str,
    sport: str = "basketball",
    league: str = "womens-college-basketball",
) -> dict:
    """Fetch team info including coach from ESPN."""
    url = f"{ESPN_BASE}/{sport}/{league}/teams/{team_id}"
    try:
        data = await _espn_get(url)
        team = data.get("team", {})
        # Coach info from roster endpoint is more reliable
        roster_url = f"{ESPN_BASE}/{sport}/{league}/teams/{team_id}/roster"
        try:
            roster_data = await _espn_get(roster_url)
            coaches = roster_data.get("coach", [])
        except Exception:
            coaches = []
        coach_info = {}
        team_name = team.get("displayName", "")
        if coaches:
            c = coaches[0]  # Head coach is first
            coach_info = {
                "name": f"{c.get('firstName', '')} {c.get('lastName', '')}".strip(),
                "tenure_years": c.get("experience", 0),
            }
        # Fallback: use known coaching tenure when ESPN API is incomplete
        if not coach_info.get("tenure_years") and team_name in COACHING_TENURE_FALLBACK:
            name, tenure = COACHING_TENURE_FALLBACK[team_name]
            coach_info = {"name": name, "tenure_years": tenure}
            logger.info(f"TCI: Using fallback coaching data for {team_name}: {name} ({tenure}yr)")
        return {
            "team_id": team_id,
            "team_name": team_name,
            "abbreviation": team.get("abbreviation", ""),
            "location": team.get("location", ""),
            "conference": team.get("groups", {}).get("id", ""),
            "conference_name": team.get("groups", {}).get("name", ""),
            "head_coach": coach_info,
            "religious_affiliation": RELIGIOUS_PROGRAMS.get(
                team_name, "secular"
            ),
        }
    except Exception as e:
        logger.warning(f"Failed to fetch team info for {team_id}: {e}")
        return {"team_id": team_id, "error": str(e)}


async def _get_all_espn_teams(
    sport: str = "basketball",
    league: str = "womens-college-basketball",
) -> list[dict]:
    """Fetch and cache all ESPN teams for a league (1h TTL)."""
    import time
    cache_key = f"{sport}/{league}"
    if cache_key in _espn_teams_cache:
        teams, ts = _espn_teams_cache[cache_key]
        if time.time() - ts < _ESPN_CACHE_TTL:
            return teams
    url = f"{ESPN_BASE}/{sport}/{league}/teams?limit=400"
    data = await _espn_get(url)
    teams = data.get("sports", [{}])[0].get("leagues", [{}])[0].get("teams", [])
    if len(_espn_teams_cache) >= _ESPN_CACHE_MAX:
        oldest_key = min(_espn_teams_cache, key=lambda k: _espn_teams_cache[k][1])
        del _espn_teams_cache[oldest_key]
    _espn_teams_cache[cache_key] = (teams, time.time())
    return teams


async def _search_espn_team(
    name: str,
    sport: str = "basketball",
    league: str = "womens-college-basketball",
) -> Optional[tuple[str, str]]:
    """Search ESPN for a team ID by display name. Returns (id, displayName) or None."""
    try:
        teams = await _get_all_espn_teams(sport, league)
        for entry in teams:
            team = entry.get("team", {})
            if team.get("displayName", "") == name:
                return (str(team["id"]), team["displayName"])
        # Fallback: partial match
        name_lower = name.lower()
        for entry in teams:
            team = entry.get("team", {})
            if name_lower in team.get("displayName", "").lower():
                return (str(team["id"]), team["displayName"])
    except Exception as e:
        logger.warning(f"ESPN team search failed for '{name}': {e}")
    return None
