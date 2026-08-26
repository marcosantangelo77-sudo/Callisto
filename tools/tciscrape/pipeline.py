"""Tournament-wide TCI build pipeline."""

import logging

from tools.tciscrape.constants import (
    DB_PATH,
    ESPN_BASE,
    TOURNAMENT_TEAMS_2026,
)
from tools.tciscrape.compute import compute_tci
from tools.tciscrape.espn import _search_espn_team, get_team_info, get_team_roster
from tools.tciscrape.http import _espn_get
from tools.tciscrape.storage import _store_tci_results

logger = logging.getLogger("callisto.tci")


async def build_tci_for_tournament(
    season: int = 2026,
    sport: str = "basketball",
    league: str = "womens-college-basketball",
    db_path: str = DB_PATH,
) -> list[dict]:
    """
    Build TCI scores for all teams in the current tournament bracket.

    Uses TOURNAMENT_TEAMS_2026 list + ESPN rankings to find all team IDs.
    Fetches roster and coaching data, computes TCI, stores in database.
    """
    team_ids = set()

    # Source 1: ESPN rankings (gets ranked teams with IDs directly)
    url = f"{ESPN_BASE}/{sport}/{league}/rankings"
    try:
        data = await _espn_get(url)
        for ranking in data.get("rankings", []):
            for rank in ranking.get("ranks", []):
                team = rank.get("team", {})
                tid = team.get("id")
                if tid:
                    team_ids.add((str(tid), team.get("displayName", "")))
    except Exception as e:
        logger.warning(f"Rankings fetch failed: {e}")

    # Source 2: look up missing tournament teams by name
    known_names = {name for _, name in team_ids}
    missing = [t for t in TOURNAMENT_TEAMS_2026 if t not in known_names]
    if missing:
        logger.info(f"TCI: {len(missing)} tournament teams not in rankings, searching ESPN...")
        for name in missing:
            result = await _search_espn_team(name, sport, league)
            if result:
                team_ids.add(result)
            else:
                logger.warning(f"TCI: Could not find ESPN ID for '{name}'")

    logger.info(f"TCI: Found {len(team_ids)} tournament teams, building cohesion profiles...")

    results = []
    for team_id, team_name in sorted(team_ids, key=lambda x: x[1]):
        try:
            roster = await get_team_roster(team_id, sport, league, season)
            info = await get_team_info(team_id, sport, league)
            tci = compute_tci(roster, info)
            if tci.get("error"):
                # Failure isolation: a team whose roster could not be built
                # must never leak a partial record into storage/results.
                raise RuntimeError(tci["error"])
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
    await _store_tci_results(results, season, db_path)
    return results
