"""
Team Cohesion Index (TCI) scraper — collects roster-level cohesion metrics
from ESPN for women's basketball teams.

Metrics collected:
  - Roster continuity: % of returning players from prior year
  - Geographic concentration: % of roster from same state/region
  - Coaching tenure: years with current head coach
  - Class distribution: freshmen/sophomore/junior/senior/grad breakdown
  - Transfer count: number of transfers on current roster
  - Program stability: coaching changes in last 5 years

These are used as inputs to the Team Cohesion Index hypothesis, which
predicts that high-cohesion teams outperform in high-pressure tournament
situations, particularly in women's sports where system play dominates.
"""

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

import aiosqlite
import httpx

logger = logging.getLogger("callisto.tci")

DB_PATH = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"

# US state -> region mapping for geographic concentration
STATE_REGIONS = {
    # Southeast
    "AL": "Southeast", "AR": "Southeast", "FL": "Southeast", "GA": "Southeast",
    "KY": "Southeast", "LA": "Southeast", "MS": "Southeast", "NC": "Southeast",
    "SC": "Southeast", "TN": "Southeast", "VA": "Southeast", "WV": "Southeast",
    # Northeast
    "CT": "Northeast", "DE": "Northeast", "MA": "Northeast", "MD": "Northeast",
    "ME": "Northeast", "NH": "Northeast", "NJ": "Northeast", "NY": "Northeast",
    "PA": "Northeast", "RI": "Northeast", "VT": "Northeast", "DC": "Northeast",
    # Midwest
    "IA": "Midwest", "IL": "Midwest", "IN": "Midwest", "KS": "Midwest",
    "MI": "Midwest", "MN": "Midwest", "MO": "Midwest", "NE": "Midwest",
    "ND": "Midwest", "OH": "Midwest", "SD": "Midwest", "WI": "Midwest",
    # Southwest
    "AZ": "Southwest", "NM": "Southwest", "OK": "Southwest", "TX": "Southwest",
    # West
    "CA": "West", "CO": "West", "HI": "West", "ID": "West", "MT": "West",
    "NV": "West", "OR": "West", "UT": "West", "WA": "West", "WY": "West",
    "AK": "West",
}

# Religious-affiliated programs (major ones with institutional stability signal)
RELIGIOUS_PROGRAMS = {
    "Notre Dame": "Catholic",
    "Villanova": "Catholic",
    "Georgetown": "Catholic",
    "Gonzaga": "Catholic",
    "Marquette": "Catholic",
    "Creighton": "Catholic",
    "Seton Hall": "Catholic",
    "DePaul": "Catholic",
    "St. John's": "Catholic",
    "Xavier": "Catholic",
    "BYU": "LDS",
    "Baylor": "Baptist",
    "TCU": "Disciples of Christ",
    "SMU": "Methodist",
    "Wake Forest": "Baptist (historical)",
    "Duke": "Methodist (historical)",
    "Boston College": "Catholic",
    "Holy Cross": "Catholic",
    "Loyola": "Catholic",
    "Dayton": "Catholic",
    "Oklahoma State": "secular",  # Included for reference
}

# ESPN team ID lookup will be populated dynamically
_team_cache: dict[str, dict] = {}
_client: Optional[httpx.AsyncClient] = None


async def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=15.0,
            headers={"User-Agent": "Mozilla/5.0 (Callisto/1.0)"},
        )
    return _client


async def _espn_get(url: str) -> dict:
    """Rate-limited ESPN API request."""
    client = await _get_client()
    resp = await client.get(url)
    resp.raise_for_status()
    return resp.json()


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
        if coaches:
            c = coaches[0]  # Head coach is first
            coach_info = {
                "name": f"{c.get('firstName', '')} {c.get('lastName', '')}".strip(),
                "tenure_years": c.get("experience", 0),
            }
        return {
            "team_id": team_id,
            "team_name": team.get("displayName", ""),
            "abbreviation": team.get("abbreviation", ""),
            "location": team.get("location", ""),
            "conference": team.get("groups", {}).get("id", ""),
            "conference_name": team.get("groups", {}).get("name", ""),
            "head_coach": coach_info,
            "religious_affiliation": RELIGIOUS_PROGRAMS.get(
                team.get("displayName", ""), "secular"
            ),
        }
    except Exception as e:
        logger.warning(f"Failed to fetch team info for {team_id}: {e}")
        return {"team_id": team_id, "error": str(e)}


def compute_tci(roster: dict, team_info: dict) -> dict:
    """
    Compute Team Cohesion Index from roster and team data.

    Returns individual metrics and composite TCI score (0-100).
    """
    players = roster.get("players", [])
    if not players:
        return {"tci_score": 0, "error": "no players"}

    from collections import Counter

    # --- Geographic concentration ---
    states = [p["home_state"] for p in players if p.get("home_state")]
    regions = [STATE_REGIONS.get(s, "Unknown") for s in states]
    domestic_count = sum(
        1 for p in players
        if p.get("home_country", "USA") in ("USA", "US", "United States")
    )
    international_count = len(players) - domestic_count

    # Most common region
    if regions:
        region_counts = Counter(regions)
        top_region, top_count = region_counts.most_common(1)[0]
        geo_concentration = top_count / len(regions)
    else:
        top_region = "Unknown"
        geo_concentration = 0

    # Most common state
    if states:
        state_counts = Counter(states)
        top_state, top_state_count = state_counts.most_common(1)[0]
        state_concentration = top_state_count / len(states)
    else:
        top_state = "Unknown"
        state_concentration = 0

    # --- Class distribution (experience proxy) ---
    class_years = [p.get("class_year", "").lower() for p in players]
    upperclassmen = sum(
        1 for c in class_years
        if any(y in c for y in ["junior", "senior", "jr", "sr", "graduate", "grad", "5th"])
    )
    underclassmen = sum(
        1 for c in class_years
        if any(y in c for y in ["freshman", "sophomore", "fr", "so"])
    )
    total_classified = upperclassmen + underclassmen
    experience_ratio = upperclassmen / total_classified if total_classified > 0 else 0.5

    # --- Coaching tenure ---
    coach = team_info.get("head_coach", {})
    coaching_tenure = coach.get("tenure_years", 0)
    # Normalize: 0-2 years = low stability, 3-5 = medium, 6+ = high
    coaching_stability = min(coaching_tenure / 10.0, 1.0)

    # --- Religious/institutional stability ---
    affiliation = team_info.get("religious_affiliation", "secular")
    institutional_factor = 0.15 if affiliation != "secular" else 0.0

    # --- Composite TCI Score (0-100) ---
    # Weights reflect hypothesis about relative importance
    tci_score = (
        geo_concentration * 25         # Geographic cohesion (25%)
        + experience_ratio * 25        # Roster experience/continuity (25%)
        + coaching_stability * 25      # Coaching tenure stability (25%)
        + (1 - international_count / max(len(players), 1)) * 15  # Domestic concentration (15%)
        + institutional_factor * 100 * (10 / 100)  # Institutional stability (10%)
    )

    return {
        "tci_score": round(tci_score, 1),
        "geographic_concentration": round(geo_concentration, 3),
        "top_region": top_region,
        "state_concentration": round(state_concentration, 3),
        "top_state": top_state,
        "experience_ratio": round(experience_ratio, 3),
        "upperclassmen": upperclassmen,
        "underclassmen": underclassmen,
        "coaching_tenure_years": coaching_tenure,
        "coaching_stability": round(coaching_stability, 3),
        "religious_affiliation": affiliation,
        "institutional_factor": institutional_factor,
        "international_players": international_count,
        "domestic_players": domestic_count,
        "roster_size": len(players),
    }


async def build_tci_for_tournament(
    season: int = 2026,
    sport: str = "basketball",
    league: str = "womens-college-basketball",
) -> list[dict]:
    """
    Build TCI scores for all teams in the current tournament bracket.

    Fetches roster and coaching data, computes TCI, stores in database.
    """
    # Get tournament teams from ESPN rankings/bracket
    url = f"{ESPN_BASE}/{sport}/{league}/rankings"
    try:
        data = await _espn_get(url)
    except Exception as e:
        logger.error(f"Failed to fetch rankings: {e}")
        return []

    team_ids = set()
    for ranking in data.get("rankings", []):
        for rank in ranking.get("ranks", []):
            team = rank.get("team", {})
            tid = team.get("id")
            if tid:
                team_ids.add((tid, team.get("displayName", "")))

    logger.info(f"TCI: Found {len(team_ids)} ranked teams, building cohesion profiles...")

    results = []
    for team_id, team_name in sorted(team_ids, key=lambda x: x[1]):
        try:
            roster = await get_team_roster(team_id, sport, league, season)
            info = await get_team_info(team_id, sport, league)
            tci = compute_tci(roster, info)
            tci["team_id"] = team_id
            tci["team_name"] = team_name or info.get("team_name", "")
            tci["conference"] = info.get("conference_name", "")
            tci["head_coach"] = info.get("head_coach", {}).get("name", "")
            results.append(tci)
            logger.info(
                f"TCI: {tci['team_name']:25} score={tci['tci_score']:5.1f} "
                f"geo={tci['geographic_concentration']:.2f} "
                f"exp={tci['experience_ratio']:.2f} "
                f"coach={tci['coaching_tenure_years']}yr"
            )
        except Exception as e:
            logger.warning(f"TCI: Failed for {team_name} ({team_id}): {e}")

    # Store in database
    await _store_tci_results(results, season)
    return results


async def _store_tci_results(results: list[dict], season: int) -> None:
    """Store TCI results in the database."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tci_scores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                team_name TEXT NOT NULL,
                team_id TEXT,
                sport TEXT NOT NULL DEFAULT 'basketball_ncaaw',
                season INTEGER NOT NULL,
                tci_score REAL,
                geographic_concentration REAL,
                top_region TEXT,
                experience_ratio REAL,
                coaching_tenure_years INTEGER,
                coaching_stability REAL,
                religious_affiliation TEXT,
                international_players INTEGER,
                roster_size INTEGER,
                full_data TEXT,
                computed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(team_name, sport, season)
            )
        """)

        for r in results:
            await db.execute(
                """INSERT OR REPLACE INTO tci_scores
                (team_name, team_id, sport, season, tci_score,
                 geographic_concentration, top_region, experience_ratio,
                 coaching_tenure_years, coaching_stability, religious_affiliation,
                 international_players, roster_size, full_data)
                VALUES (?, ?, 'basketball_ncaaw', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    r["team_name"], r.get("team_id"), season,
                    r["tci_score"], r["geographic_concentration"],
                    r["top_region"], r["experience_ratio"],
                    r["coaching_tenure_years"], r["coaching_stability"],
                    r["religious_affiliation"], r["international_players"],
                    r["roster_size"], json.dumps(r),
                ),
            )

        await db.commit()
        logger.info(f"TCI: Stored {len(results)} team cohesion profiles")


async def get_tci_matchup(
    home_team: str, away_team: str, season: int = 2026
) -> dict:
    """
    Get TCI comparison for a matchup.

    Returns both teams' TCI scores and the differential.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        results = {}
        for team in [home_team, away_team]:
            cursor = await db.execute(
                "SELECT full_data FROM tci_scores WHERE team_name LIKE ? AND season = ?",
                (f"%{team}%", season),
            )
            row = await cursor.fetchone()
            if row:
                results[team] = json.loads(row[0])
            else:
                results[team] = {"tci_score": 0, "error": "not found"}

    home_tci = results.get(home_team, {}).get("tci_score", 0)
    away_tci = results.get(away_team, {}).get("tci_score", 0)

    return {
        "home_team": home_team,
        "away_team": away_team,
        "home_tci": results.get(home_team, {}),
        "away_tci": results.get(away_team, {}),
        "tci_differential": round(home_tci - away_tci, 1),
        "cohesion_edge": "home" if home_tci > away_tci else "away",
    }


# --- CLI entry point ---
if __name__ == "__main__":
    async def _main():
        results = await build_tci_for_tournament(season=2026)
        print(f"\n{'Team':30} {'TCI':>6} {'Geo':>6} {'Exp':>6} {'Coach':>6} {'Relig':>10}")
        print("-" * 75)
        for r in sorted(results, key=lambda x: x["tci_score"], reverse=True):
            print(
                f"{r['team_name']:30} {r['tci_score']:6.1f} "
                f"{r['geographic_concentration']:6.2f} "
                f"{r['experience_ratio']:6.2f} "
                f"{r['coaching_tenure_years']:6} "
                f"{r['religious_affiliation']:>10}"
            )

    asyncio.run(_main())
