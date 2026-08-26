"""Team Cohesion Index (TCI) scraper package.

Collects roster-level cohesion metrics from ESPN for women's basketball teams.

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

Module layout:
  constants — lookup tables and thresholds (no I/O)
  http      — shared async HTTP client + rate-limited ESPN GET
  espn      — ESPN API fetchers (rosters, team info, team search)
  compute   — pure TCI computation from roster/team dicts
  signals   — decomposed matchup signals (experience ratio, stability)
  storage   — SQLite persistence + matchup queries
  pipeline  — tournament-wide TCI build orchestration
"""

from tools.tciscrape.constants import (
    COACHING_TENURE_FALLBACK,
    EXP_RATIO_MIN_DIFF,
    EXP_RATIO_STRONG_DIFF,
    RELIGIOUS_PROGRAMS,
    STAB_SCORE_MIN_DIFF,
    STATE_REGIONS,
    TOURNAMENT_TEAMS_2026,
)
from tools.tciscrape.compute import compute_tci
from tools.tciscrape.espn import (
    _get_all_espn_teams,
    _search_espn_team,
    get_team_info,
    get_team_roster,
)
from tools.tciscrape.http import _espn_get, _get_client, close_client
from tools.tciscrape.pipeline import build_tci_for_tournament
from tools.tciscrape.signals import get_experience_signal, get_stability_signal
from tools.tciscrape.storage import _store_tci_results, get_tci_matchup

__all__ = [
    "COACHING_TENURE_FALLBACK",
    "EXP_RATIO_MIN_DIFF",
    "EXP_RATIO_STRONG_DIFF",
    "RELIGIOUS_PROGRAMS",
    "STAB_SCORE_MIN_DIFF",
    "STATE_REGIONS",
    "TOURNAMENT_TEAMS_2026",
    "_espn_get",
    "_get_all_espn_teams",
    "_get_client",
    "_search_espn_team",
    "_store_tci_results",
    "build_tci_for_tournament",
    "close_client",
    "compute_tci",
    "get_experience_signal",
    "get_stability_signal",
    "get_team_info",
    "get_team_roster",
    "get_tci_matchup",
]
