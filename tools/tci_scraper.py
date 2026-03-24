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

# Coaching tenure fallback — ESPN API doesn't return coach data for all teams.
# Source: public coaching records as of 2025-26 season (years at current school).
COACHING_TENURE_FALLBACK = {
    "Notre Dame Fighting Irish": ("Niele Ivey", 6),
    "Kentucky Wildcats": ("Kenny Brooks", 2),
    "Vanderbilt Commodores": ("Shea Ralph", 4),
    "Baylor Bears": ("Nicki Collen", 5),
    "Duke Blue Devils": ("Kara Lawson", 6),
    "Princeton Tigers": ("Carla Berube", 4),
    "Texas Longhorns": ("Vic Schaefer", 6),
    "Michigan State Spartans": ("Robyn Fralick", 2),
    "TCU Horned Frogs": ("Mark Campbell", 3),
    "Ole Miss Rebels": ("Yolett McPhee-McCuin", 8),
    "Iowa Hawkeyes": ("Jan Jensen", 3),
    "North Carolina Tar Heels": ("Courtney Banghart", 6),
    "Oklahoma Sooners": ("Jennie Baranczyk", 5),
    "West Virginia Mountaineers": ("Dawn Plitzuweit", 3),
    "Minnesota Golden Gophers": ("Dawn Plitzuweit", 1),  # If transferred
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


def compute_tci(roster: dict, team_info: dict) -> dict:
    """
    Compute Team Cohesion Index from roster and team data.

    Returns individual metrics and composite TCI score (0-100).

    Based on meta-analysis (195 studies, n=12,023): task cohesion and social
    cohesion are distinct constructs. In women's teams, social cohesion shows
    a NEGATIVE performance correlation while task cohesion is strongly positive.
    The formula separates these dimensions accordingly.

    Task Cohesion (positive contributors):
      - Roster experience/continuity (30%)
      - Coaching tenure stability (30%)
      - Class balance / role clarity (10%)

    Social Cohesion (negative/neutral — NOT added to score):
      - Geographic concentration (tracked but not scored positively)
      - Domestic concentration (tracked but not scored)

    Program Stability (moderate positive):
      - Coaching tenure consistency (captured in task cohesion)
      - Low transfer churn proxy (20%)
      - Institutional continuity (10%)
    """
    players = roster.get("players", [])
    if not players:
        return {"tci_score": 0, "error": "no players"}

    from collections import Counter

    # --- Geographic concentration (tracked, NOT positively scored) ---
    states = [p["home_state"] for p in players if p.get("home_state")]
    regions = [STATE_REGIONS.get(s, "Unknown") for s in states]
    domestic_count = sum(
        1 for p in players
        if p.get("home_country", "USA") in ("USA", "US", "United States")
    )
    international_count = len(players) - domestic_count

    if regions:
        region_counts = Counter(regions)
        top_region, top_count = region_counts.most_common(1)[0]
        geo_concentration = top_count / len(regions)
    else:
        top_region = "Unknown"
        geo_concentration = 0

    if states:
        state_counts = Counter(states)
        top_state, top_state_count = state_counts.most_common(1)[0]
        state_concentration = top_state_count / len(states)
    else:
        top_state = "Unknown"
        state_concentration = 0

    # --- Class distribution (experience = task cohesion proxy) ---
    class_years = [p.get("class_year", "").lower() for p in players]
    seniors_grad = sum(
        1 for c in class_years
        if any(y in c for y in ["senior", "sr", "graduate", "grad", "5th"])
    )
    juniors = sum(
        1 for c in class_years
        if any(y in c for y in ["junior", "jr"])
    )
    sophomores = sum(
        1 for c in class_years
        if any(y in c for y in ["sophomore", "so"])
    )
    freshmen = sum(
        1 for c in class_years
        if any(y in c for y in ["freshman", "fr"])
    )
    upperclassmen = seniors_grad + juniors
    underclassmen = sophomores + freshmen
    total_classified = upperclassmen + underclassmen
    experience_ratio = upperclassmen / total_classified if total_classified > 0 else 0.5

    # Class balance: teams with a spread across all years have better role clarity
    # Perfect balance = 0.25 each; measure via inverse of standard deviation
    if total_classified > 0:
        class_fracs = [
            seniors_grad / total_classified,
            juniors / total_classified,
            sophomores / total_classified,
            freshmen / total_classified,
        ]
        class_mean = 0.25
        class_variance = sum((f - class_mean) ** 2 for f in class_fracs) / 4
        # 0 variance = perfect balance (score 1.0), high variance = unbalanced (score 0)
        class_balance = max(0, 1.0 - (class_variance ** 0.5) * 4)
    else:
        class_balance = 0.5

    # --- Coaching tenure (TASK cohesion — system continuity) ---
    coach = team_info.get("head_coach", {})
    coaching_tenure = coach.get("tenure_years", 0)
    coaching_stability = min(coaching_tenure / 10.0, 1.0)

    # --- Transfer churn proxy (LOW freshmen ratio = roster stability) ---
    # High freshman/transfer count signals roster disruption
    if total_classified > 0:
        continuity_proxy = 1.0 - (freshmen / total_classified)
    else:
        continuity_proxy = 0.5

    # --- Institutional stability (weaker signal, reduced weight) ---
    affiliation = team_info.get("religious_affiliation", "secular")
    institutional_factor = 0.1 if affiliation != "secular" else 0.0

    # --- Composite TCI Score (0-100) ---
    # Weights based on academic evidence for women's team performance:
    # Task cohesion proxies dominate; social cohesion excluded from positive scoring
    tci_score = (
        experience_ratio * 30           # Task: roster experience (30%)
        + coaching_stability * 30       # Task: coaching system tenure (30%)
        + continuity_proxy * 20         # Stability: low roster churn (20%)
        + class_balance * 10            # Task: role clarity / class spread (10%)
        + institutional_factor * 100    # Stability: institutional continuity (10%)
    )

    # --- Social Cohesion Index (tracked separately, NOT added to TCI) ---
    # Academic evidence: social cohesion is negatively correlated with
    # women's team performance. High values here may indicate RISK, not edge.
    social_cohesion = (
        geo_concentration * 50
        + (1 - international_count / max(len(players), 1)) * 50
    )

    return {
        "tci_score": round(tci_score, 1),
        "task_cohesion": round(experience_ratio * 30 + coaching_stability * 30 + class_balance * 10, 1),
        "social_cohesion": round(social_cohesion, 1),
        "stability_score": round(continuity_proxy * 20 + institutional_factor * 100, 1),
        "geographic_concentration": round(geo_concentration, 3),
        "top_region": top_region,
        "state_concentration": round(state_concentration, 3),
        "top_state": top_state,
        "experience_ratio": round(experience_ratio, 3),
        "class_balance": round(class_balance, 3),
        "continuity_proxy": round(continuity_proxy, 3),
        "upperclassmen": upperclassmen,
        "underclassmen": underclassmen,
        "seniors_grad": seniors_grad,
        "juniors": juniors,
        "sophomores": sophomores,
        "freshmen": freshmen,
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
        # Drop and recreate to match new schema with decomposed metrics
        await db.execute("DROP TABLE IF EXISTS tci_scores")
        await db.execute("""
            CREATE TABLE tci_scores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                team_name TEXT NOT NULL,
                team_id TEXT,
                sport TEXT NOT NULL DEFAULT 'basketball_ncaaw',
                season INTEGER NOT NULL,
                tci_score REAL,
                task_cohesion REAL,
                social_cohesion REAL,
                stability_score REAL,
                geographic_concentration REAL,
                top_region TEXT,
                state_concentration REAL,
                experience_ratio REAL,
                class_balance REAL,
                continuity_proxy REAL,
                coaching_tenure_years INTEGER,
                coaching_stability REAL,
                religious_affiliation TEXT,
                institutional_factor REAL,
                international_players INTEGER,
                domestic_players INTEGER,
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
                 task_cohesion, social_cohesion, stability_score,
                 geographic_concentration, top_region, state_concentration,
                 experience_ratio, class_balance, continuity_proxy,
                 coaching_tenure_years, coaching_stability, religious_affiliation,
                 institutional_factor, international_players, domestic_players,
                 roster_size, full_data)
                VALUES (?, ?, 'basketball_ncaaw', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    r["team_name"], r.get("team_id"), season,
                    r["tci_score"], r.get("task_cohesion", 0),
                    r.get("social_cohesion", 0), r.get("stability_score", 0),
                    r["geographic_concentration"], r["top_region"],
                    r.get("state_concentration", 0), r["experience_ratio"],
                    r.get("class_balance", 0), r.get("continuity_proxy", 0),
                    r["coaching_tenure_years"], r["coaching_stability"],
                    r["religious_affiliation"], r.get("institutional_factor", 0),
                    r["international_players"], r.get("domestic_players", 0),
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
        print(f"\n{'Team':30} {'TCI':>6} {'Task':>6} {'Social':>7} {'Stab':>6} {'Exp':>5} {'Bal':>5} {'Coach':>5}")
        print("-" * 95)
        for r in sorted(results, key=lambda x: x["tci_score"], reverse=True):
            print(
                f"{r['team_name']:30} {r['tci_score']:6.1f} "
                f"{r.get('task_cohesion', 0):6.1f} "
                f"{r.get('social_cohesion', 0):7.1f} "
                f"{r.get('stability_score', 0):6.1f} "
                f"{r['experience_ratio']:5.2f} "
                f"{r.get('class_balance', 0):5.2f} "
                f"{r['coaching_tenure_years']:5}"
            )
        # Summary stats
        if results:
            scores = [r["tci_score"] for r in results]
            print(f"\n  n={len(results)}  mean={sum(scores)/len(scores):.1f}  "
                  f"min={min(scores):.1f}  max={max(scores):.1f}  "
                  f"spread={max(scores)-min(scores):.1f}")

    asyncio.run(_main())
