"""
Contextual data ingestion — injuries, referee tendencies, weather.

This is the information layer that feeds into models and edge detection.
Books partially account for all of this, but:
- They update slower than we can detect
- Retail books use cruder adjustments than sharp books
- Public data + speed = edge

Data sources (free):
- ESPN API: injuries, rosters, schedules, basic stats
- Weather: OpenMeteo (free, no key needed) for NFL/MLB venues
- Referee data: structured from public box scores

Stale line exploitation = being faster than the book's adjustment.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

from tools.ingestion_tracking import tracked_ingestion

logger = logging.getLogger("callisto.contextual_data")

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


# ---- ESPN API (free, no key) ----

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"

# Sport key mapping: Odds API keys → ESPN path segments
SPORT_MAP = {
    "basketball_nba": ("basketball", "nba"),
    "basketball_ncaab": ("basketball", "mens-college-basketball"),
    "basketball_ncaaw": ("basketball", "womens-college-basketball"),
    "americanfootball_nfl": ("football", "nfl"),
    "americanfootball_ncaaf": ("football", "college-football"),
    "baseball_mlb": ("baseball", "mlb"),
    "icehockey_nhl": ("hockey", "nhl"),
    "soccer_epl": ("soccer", "eng.1"),
    "soccer_usa_mls": ("soccer", "usa.1"),
}


@tracked_ingestion(
    source=lambda sport="basketball_ncaab", **_: f"espn.injuries.{sport}",
    sla_seconds=1800,
)
async def get_injuries(sport: str = "basketball_ncaab") -> dict:
    """
    Get current injury report from ESPN.

    Injuries create the fastest-decaying edges:
    - Starter ruled out 30 min before game → lines haven't fully adjusted
    - The SPECIFIC replacement player's profile changes team dynamics
    - Books adjust team line but may not fully reprice player props

    Returns structured injury data for edge exploitation.
    """
    espn = SPORT_MAP.get(sport)
    if not espn:
        return {"error": f"Sport {sport} not mapped to ESPN", "injuries": []}

    sport_path, league_path = espn
    client = _get_client()

    try:
        # ESPN injuries endpoint
        resp = await client.get(
            f"{ESPN_BASE}/{sport_path}/{league_path}/injuries"
        )

        if resp.status_code == 200:
            data = resp.json()
            injuries = []

            for team_data in data.get("items", []):
                team_name = team_data.get("team", {}).get("displayName", "")
                team_abbr = team_data.get("team", {}).get("abbreviation", "")

                for injury in team_data.get("injuries", []):
                    athlete = injury.get("athlete", {})
                    injuries.append({
                        "team": team_name,
                        "team_abbr": team_abbr,
                        "player": athlete.get("displayName", ""),
                        "position": athlete.get("position", {}).get("abbreviation", ""),
                        "status": injury.get("status", ""),
                        "type": injury.get("type", {}).get("description", ""),
                        "detail": injury.get("details", {}).get("detail", ""),
                        "side": injury.get("details", {}).get("side", ""),
                        "return_date": injury.get("details", {}).get("returnDate", ""),
                    })

            return {
                "sport": sport,
                "injury_count": len(injuries),
                "injuries": injuries,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        else:
            # Fallback: try scoreboard for injury info embedded in game data
            return await _get_injuries_from_scoreboard(sport)

    except Exception as e:
        logger.error(f"ESPN injury fetch failed: {e}")
        return {"error": str(e), "injuries": []}


async def _get_injuries_from_scoreboard(sport: str) -> dict:
    """Fallback: extract injury info from ESPN scoreboard."""
    espn = SPORT_MAP.get(sport)
    if not espn:
        return {"error": "Sport not mapped", "injuries": []}

    sport_path, league_path = espn
    client = _get_client()

    try:
        resp = await client.get(
            f"{ESPN_BASE}/{sport_path}/{league_path}/scoreboard"
        )
        if resp.status_code != 200:
            return {"error": f"ESPN returned {resp.status_code}", "injuries": []}

        data = resp.json()
        injuries = []

        for event in data.get("events", []):
            for competition in event.get("competitions", []):
                for competitor in competition.get("competitors", []):
                    team_name = competitor.get("team", {}).get("displayName", "")
                    for player in competitor.get("injuries", []):
                        injuries.append({
                            "team": team_name,
                            "player": player.get("athlete", {}).get("displayName", ""),
                            "position": player.get("athlete", {}).get("position", {}).get("abbreviation", ""),
                            "status": player.get("status", ""),
                            "type": player.get("type", {}).get("description", ""),
                        })

        return {
            "sport": sport,
            "source": "scoreboard_fallback",
            "injury_count": len(injuries),
            "injuries": injuries,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        logger.error(f"ESPN scoreboard fallback failed: {e}")
        return {"error": str(e), "injuries": []}


@tracked_ingestion(
    source=lambda sport, team_id, **_: f"espn.roster.{sport}",
    sla_seconds=3600,
)
async def get_team_roster(sport: str, team_id: str) -> dict:
    """Get team roster with player stats from ESPN."""
    espn = SPORT_MAP.get(sport)
    if not espn:
        return {"error": f"Sport {sport} not mapped"}

    sport_path, league_path = espn
    client = _get_client()

    try:
        resp = await client.get(
            f"{ESPN_BASE}/{sport_path}/{league_path}/teams/{team_id}/roster"
        )
        if resp.status_code != 200:
            return {"error": f"ESPN returned {resp.status_code}"}

        data = resp.json()
        players = []
        for entry in data.get("athletes", []):
            # ESPN uses two formats:
            # - NHL/MLB: athletes[].items[] (grouped by position)
            # - NBA/NFL: athletes[] directly (flat list)
            if "items" in entry:
                # Grouped format (NHL, MLB)
                athlete_list = entry["items"]
            else:
                # Flat format (NBA, NFL) — entry IS the athlete
                athlete_list = [entry]

            for athlete in athlete_list:
                pos = athlete.get("position", {})
                if isinstance(pos, dict):
                    pos_abbr = pos.get("abbreviation", "")
                else:
                    pos_abbr = str(pos)
                status = athlete.get("status", {})
                if isinstance(status, dict):
                    status_type = status.get("type", "")
                else:
                    status_type = str(status)
                exp = athlete.get("experience", {})
                if isinstance(exp, dict):
                    exp_years = exp.get("years", 0)
                else:
                    exp_years = exp if isinstance(exp, (int, float)) else 0
                players.append({
                    "name": athlete.get("displayName", athlete.get("fullName", "")),
                    "position": pos_abbr,
                    "jersey": athlete.get("jersey", ""),
                    "status": status_type,
                    "experience": exp_years,
                })

        return {
            "team": data.get("team", {}).get("displayName", ""),
            "player_count": len(players),
            "players": players,
        }
    except Exception as e:
        logger.warning(f"get_team_roster failed for {sport}/{team_id}: {e}")
        return {"error": str(e)}


@tracked_ingestion(
    source=lambda sport="basketball_ncaab", **_: f"espn.scoreboard_light.{sport}",
    sla_seconds=600,
)
async def get_scoreboard(sport: str = "basketball_ncaab") -> dict:
    """
    Get today's scoreboard — live scores, game status, and basic stats.

    Useful for:
    - Detecting which games are live (for live betting opportunities)
    - Getting current score differentials (for overreaction detection)
    - Checking game status before placing bets
    """
    espn = SPORT_MAP.get(sport)
    if not espn:
        return {"error": f"Sport {sport} not mapped"}

    sport_path, league_path = espn
    client = _get_client()

    try:
        resp = await client.get(
            f"{ESPN_BASE}/{sport_path}/{league_path}/scoreboard"
        )
        if resp.status_code != 200:
            return {"error": f"ESPN returned {resp.status_code}"}

        data = resp.json()
        games = []

        for event in data.get("events", []):
            competition = event.get("competitions", [{}])[0]
            competitors = competition.get("competitors", [])
            status = competition.get("status", {})

            home = next((c for c in competitors if c.get("homeAway") == "home"), {})
            away = next((c for c in competitors if c.get("homeAway") == "away"), {})

            games.append({
                "id": event.get("id", ""),
                "name": event.get("name", ""),
                "home_team": home.get("team", {}).get("displayName", ""),
                "away_team": away.get("team", {}).get("displayName", ""),
                "home_score": home.get("score", "0"),
                "away_score": away.get("score", "0"),
                "status": status.get("type", {}).get("name", ""),
                "status_detail": status.get("type", {}).get("detail", ""),
                "period": status.get("period", 0),
                "clock": status.get("displayClock", ""),
                "start_time": event.get("date", ""),
                "broadcast": event.get("competitions", [{}])[0].get("broadcasts", [{}]),
            })

        return {
            "sport": sport,
            "game_count": len(games),
            "games": games,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        logger.warning(f"get_scoreboard failed for {sport}: {e}")
        return {"error": str(e)}


# ---- Weather (OpenMeteo — free, no key) ----

OPENMETEO_BASE = "https://api.open-meteo.com/v1/forecast"

# NFL/MLB venue coordinates (expand as needed)
VENUE_COORDS = {
    # NFL outdoor stadiums where weather matters
    "lambeau_field": (44.5013, -88.0622),
    "soldier_field": (41.8623, -87.6167),
    "arrowhead": (39.0489, -94.4839),
    "metlife": (40.8128, -74.0742),
    "heinz_field": (40.4468, -80.0158),
    "gillette": (42.0909, -71.2643),
    "mile_high": (39.7439, -105.0201),  # Denver — altitude matters
    # MLB
    "wrigley": (41.9484, -87.6553),
    "fenway": (42.3467, -71.0972),
    "coors_field": (39.7559, -104.9942),  # Denver altitude + thin air
    "yankee_stadium": (40.8296, -73.9262),
}


@tracked_ingestion(
    source=lambda latitude, longitude, venue_name="", **_: "openmeteo.weather",
    sla_seconds=3600,
)
async def get_weather(
    latitude: float,
    longitude: float,
    venue_name: str = "",
) -> dict:
    """
    Get weather forecast for a venue. Free via OpenMeteo.

    Weather impacts:
    - Wind speed/direction: NFL passing game, MLB ball flight
    - Temperature: affects ball grip, player performance
    - Precipitation: reduces scoring in outdoor sports
    - Altitude: Denver is real — thinner air = more offense (NBA, NFL, MLB)

    Books partially model this but retail books use cruder adjustments.
    """
    client = _get_client()

    try:
        resp = await client.get(
            OPENMETEO_BASE,
            params={
                "latitude": latitude,
                "longitude": longitude,
                "hourly": "temperature_2m,precipitation,wind_speed_10m,wind_direction_10m,wind_gusts_10m,relative_humidity_2m",
                "forecast_days": 2,
                "temperature_unit": "fahrenheit",
                "wind_speed_unit": "mph",
            },
        )
        if resp.status_code != 200:
            return {"error": f"OpenMeteo returned {resp.status_code}"}

        data = resp.json()
        hourly = data.get("hourly", {})
        times = hourly.get("time", [])

        # Get current/upcoming conditions
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:00")
        idx = 0
        for i, t in enumerate(times):
            if t >= now:
                idx = i
                break

        # Get next 6 hours of weather
        forecast = []
        for i in range(idx, min(idx + 6, len(times))):
            forecast.append({
                "time": times[i],
                "temp_f": hourly.get("temperature_2m", [0])[i],
                "precipitation_mm": hourly.get("precipitation", [0])[i],
                "wind_mph": hourly.get("wind_speed_10m", [0])[i],
                "wind_direction": hourly.get("wind_direction_10m", [0])[i],
                "wind_gusts_mph": hourly.get("wind_gusts_10m", [0])[i],
                "humidity_pct": hourly.get("relative_humidity_2m", [0])[i],
            })

        # Impact assessment
        if forecast:
            avg_wind = sum(f["wind_mph"] for f in forecast) / len(forecast)
            avg_temp = sum(f["temp_f"] for f in forecast) / len(forecast)
            total_precip = sum(f["precipitation_mm"] for f in forecast)

            impact = _assess_weather_impact(avg_wind, avg_temp, total_precip)
        else:
            impact = "UNKNOWN"

        return {
            "venue": venue_name,
            "latitude": latitude,
            "longitude": longitude,
            "forecast": forecast,
            "impact_assessment": impact,
            "elevation": data.get("elevation", 0),
        }

    except Exception as e:
        # BUGFIX (2026-04-21): previously referenced `venue` which is not
        # defined in this scope (the parameter is `venue_name`). Hitting the
        # except branch therefore raised NameError from INSIDE the handler,
        # masking the real error and appearing as a cryptic unhandled
        # exception upstream. Fixed to use venue_name.
        logger.warning(f"get_weather failed for venue={venue_name!r}: {e}")
        return {"error": str(e)}


def _assess_weather_impact(wind_mph: float, temp_f: float, precip_mm: float) -> str:
    """Assess weather impact on scoring."""
    impacts = []

    if wind_mph > 20:
        impacts.append(f"HIGH WIND ({wind_mph:.0f} mph) — significantly reduces passing/kicking accuracy, favors under")
    elif wind_mph > 15:
        impacts.append(f"Moderate wind ({wind_mph:.0f} mph) — may affect long passes and field goals")

    if temp_f < 25:
        impacts.append(f"EXTREME COLD ({temp_f:.0f}F) — reduces grip, favors lower scoring")
    elif temp_f < 40:
        impacts.append(f"Cold ({temp_f:.0f}F) — slight impact on ball handling")
    elif temp_f > 95:
        impacts.append(f"Extreme heat ({temp_f:.0f}F) — fatigue factor, check substitution patterns")

    if precip_mm > 5:
        impacts.append(f"RAIN/SNOW ({precip_mm:.1f}mm) — slippery conditions, strongly favors under")
    elif precip_mm > 1:
        impacts.append(f"Light precipitation ({precip_mm:.1f}mm) — moderate impact on footing")

    if not impacts:
        return "NEUTRAL — no significant weather factors"

    return " | ".join(impacts)


# ---- Referee/Umpire Tendencies ----

# This would ideally pull from a database of historical referee data.
# For now, provide the framework — data can be populated from box scores.

# Known NBA referee tendencies (public data, frequently cited)
NBA_REF_PROFILES = {
    "Scott Foster": {
        "foul_rate": "high",
        "pace_impact": "slower",
        "over_tendency": -1.5,  # Points adjustment to total
        "notes": "Highest foul rate in NBA, games tend to go under",
    },
    "Tony Brothers": {
        "foul_rate": "high",
        "pace_impact": "slower",
        "over_tendency": -2.0,
        "notes": "Technical foul leader, games significantly under",
    },
    "Ed Malloy": {
        "foul_rate": "average",
        "pace_impact": "neutral",
        "over_tendency": 0.5,
        "notes": "Slightly over-leaning, average foul rate",
    },
}


def get_referee_adjustment(
    referee_names: list[str],
    sport: str = "basketball_nba",
) -> dict:
    """
    Get referee tendency adjustments for total/pace modeling.

    In NBA: referee crew assignments affect foul rates, pace, and totals.
    In MLB: home plate umpire's strike zone directly impacts run scoring.

    The data is public but most retail bettors ignore it.
    Books partially account for refs but the adjustment is often incomplete.

    Args:
        referee_names: List of assigned referees
        sport: Sport key
    """
    adjustments = []
    total_adjustment = 0.0

    if sport in ("basketball_nba", "basketball_ncaab"):
        for ref in referee_names:
            profile = NBA_REF_PROFILES.get(ref)
            if profile:
                adj = profile.get("over_tendency", 0)
                total_adjustment += adj
                adjustments.append({
                    "referee": ref,
                    "foul_rate": profile["foul_rate"],
                    "pace_impact": profile["pace_impact"],
                    "total_adjustment": adj,
                    "notes": profile["notes"],
                })
            else:
                adjustments.append({
                    "referee": ref,
                    "foul_rate": "unknown",
                    "pace_impact": "unknown",
                    "total_adjustment": 0,
                    "notes": "No profile data available",
                })

    return {
        "referees": adjustments,
        "net_total_adjustment": round(total_adjustment, 1),
        "recommendation": (
            f"Adjust total by {total_adjustment:+.1f} points based on ref tendencies"
            if total_adjustment != 0
            else "No significant referee adjustment"
        ),
    }
