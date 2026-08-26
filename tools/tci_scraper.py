"""
Team Cohesion Index (TCI) scraper — facade over :mod:`tools.tciscrape`.

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

This module is a backwards-compatible facade; the implementation lives in
the ``tools.tciscrape`` package. Import from either path.
"""

import asyncio

# Re-export the full public (and historically-imported private) surface so
# existing callers of ``tools.tci_scraper`` keep working unchanged.
from tools.tciscrape import (  # noqa: F401
    COACHING_TENURE_FALLBACK,
    EXP_RATIO_MIN_DIFF,
    EXP_RATIO_STRONG_DIFF,
    RELIGIOUS_PROGRAMS,
    STAB_SCORE_MIN_DIFF,
    STATE_REGIONS,
    TOURNAMENT_TEAMS_2026,
    _espn_get,
    _get_all_espn_teams,
    _get_client,
    _search_espn_team,
    _store_tci_results,
    build_tci_for_tournament,
    close_client,
    compute_tci,
    get_experience_signal,
    get_stability_signal,
    get_team_info,
    get_team_roster,
    get_tci_matchup,
)
from tools.tciscrape.constants import DB_PATH, ESPN_BASE  # noqa: F401
from tools.tciscrape.espn import _team_cache, _TEAM_CACHE_MAX  # noqa: F401
from tools.tciscrape.http import _client  # noqa: F401


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
