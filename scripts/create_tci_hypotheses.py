"""Generate Team Cohesion Index (TCI) hypotheses for women's sports betting."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TCI_HYPOTHESES = [
    # === CORE TCI THESIS ===
    {
        "name": "wcbb_tci_high_cohesion_ats",
        "thesis": (
            "In the NCAA Women's Tournament, teams with a Team Cohesion Index (TCI) score "
            "in the top quartile cover the spread at 55%+ against low-TCI opponents. "
            "Cohesion — measured by roster continuity, geographic concentration, coaching "
            "tenure, and program stability — predicts performance under tournament pressure "
            "better than efficiency metrics. Books price off BPI/NET rankings, not cohesion."
        ),
        "sport": "basketball_ncaaw",
        "market_type": "spreads",
        "model_config": {
            "type": "consensus_devig", "devig_method": "power",
            "target_book": "draftkings", "consensus_min_books": 3,
            "context_factors": ["tci_score", "tci_differential", "tournament_round"],
        },
        "edge_threshold": 0.01,
    },
    {
        "name": "wcbb_tci_high_cohesion_ml",
        "thesis": (
            "High-TCI women's teams win outright in tournament games at a rate exceeding "
            "their moneyline-implied probability. The cohesion advantage compounds in "
            "single-elimination: communication under pressure, resiliency after opponent "
            "runs, and composure in hostile environments. Books underweight these factors."
        ),
        "sport": "basketball_ncaaw",
        "market_type": "h2h",
        "model_config": {
            "type": "consensus_devig", "devig_method": "power",
            "target_book": "draftkings", "consensus_min_books": 3,
            "context_factors": ["tci_score", "tci_differential"],
        },
        "edge_threshold": 0.01,
    },
    # === ROSTER CONTINUITY ===
    {
        "name": "wcbb_roster_continuity_tournament_edge",
        "thesis": (
            "Teams returning 65%+ of prior-year minutes outperform their seed in the NCAA "
            "Women's Tournament. Roster continuity means players know each other's tendencies, "
            "reducing communication breakdowns under pressure. Transfer-heavy rosters "
            "underperform their talent level in March. ATS win rate is 54%+ for high-continuity "
            "vs low-continuity matchups."
        ),
        "sport": "basketball_ncaaw",
        "market_type": "spreads",
        "model_config": {
            "type": "consensus_devig", "devig_method": "power",
            "target_book": "draftkings", "consensus_min_books": 3,
            "context_factors": ["experience_ratio", "transfer_count", "returning_minutes_pct"],
        },
        "edge_threshold": 0.01,
    },
    {
        "name": "wcbb_transfer_heavy_fade",
        "thesis": (
            "Women's teams with 4+ transfer portal additions underperform their talent "
            "rating in tournament play. The integration friction — new plays, new chemistry, "
            "new roles — manifests as turnovers and miscommunication in high-pressure possessions. "
            "Fading transfer-heavy teams ATS in the tournament is +EV."
        ),
        "sport": "basketball_ncaaw",
        "market_type": "spreads",
        "model_config": {
            "type": "consensus_devig", "devig_method": "power",
            "target_book": "draftkings", "consensus_min_books": 3,
            "context_factors": ["transfer_count", "roster_turnover_pct"],
        },
        "edge_threshold": 0.01,
    },
    # === COACHING STABILITY ===
    {
        "name": "wcbb_coaching_tenure_ats",
        "thesis": (
            "Head coaches with 6+ years at their current program have teams that cover "
            "the spread at a higher rate in tournament games. Long coaching tenure creates "
            "systematic advantages: players recruited into the system, consistent culture, "
            "and institutional knowledge of tournament preparation. Books price coaching "
            "quality via win%, not tenure stability."
        ),
        "sport": "basketball_ncaaw",
        "market_type": "spreads",
        "model_config": {
            "type": "consensus_devig", "devig_method": "power",
            "target_book": "draftkings", "consensus_min_books": 3,
            "context_factors": ["coaching_tenure_years", "coaching_stability"],
        },
        "edge_threshold": 0.01,
    },
    {
        "name": "wcbb_new_coach_tournament_fade",
        "thesis": (
            "Women's teams with a first or second-year head coach underperform ATS in the "
            "NCAA Tournament. New coaches haven't yet imprinted their system, and the stress "
            "of March exposes the lack of shared language and trust. This effect is stronger "
            "in women's basketball than men's because system play matters more."
        ),
        "sport": "basketball_ncaaw",
        "market_type": "spreads",
        "model_config": {
            "type": "consensus_devig", "devig_method": "power",
            "target_book": "draftkings", "consensus_min_books": 3,
            "context_factors": ["coaching_tenure_years", "first_tournament"],
        },
        "edge_threshold": 0.01,
    },
    # === GEOGRAPHIC/CULTURAL COHESION ===
    {
        "name": "wcbb_geographic_concentration_ats",
        "thesis": (
            "Teams where 50%+ of the roster comes from the same geographic region cover "
            "the spread at a higher rate. Shared regional basketball culture creates implicit "
            "communication — players who grew up in the same AAU circuits and high school "
            "conferences have pre-existing chemistry that transfers to college play."
        ),
        "sport": "basketball_ncaaw",
        "market_type": "spreads",
        "model_config": {
            "type": "consensus_devig", "devig_method": "power",
            "target_book": "draftkings", "consensus_min_books": 3,
            "context_factors": ["geographic_concentration", "top_region"],
        },
        "edge_threshold": 0.01,
    },
    {
        "name": "wcbb_institutional_stability_ats",
        "thesis": (
            "Programs with strong institutional identity (religious-affiliated schools like "
            "Notre Dame, BYU, Gonzaga, Villanova) have lower roster turnover and higher "
            "program stability. This translates to tournament spread coverage because the "
            "culture produces 'composure under pressure' — players don't emotionally collapse "
            "during opponent runs. Books don't price institutional identity."
        ),
        "sport": "basketball_ncaaw",
        "market_type": "spreads",
        "model_config": {
            "type": "consensus_devig", "devig_method": "power",
            "target_book": "draftkings", "consensus_min_books": 3,
            "context_factors": ["religious_affiliation", "institutional_factor", "tci_score"],
        },
        "edge_threshold": 0.01,
    },
    # === IN-GAME COMPOSURE ===
    {
        "name": "wcbb_tci_run_resistance",
        "thesis": (
            "High-TCI teams recover from opponent 10-0 runs faster than low-TCI teams. "
            "In tournament games, the ability to stop the bleeding during an opponent run "
            "is the difference between a 5-point loss and a 25-point loss. This composure "
            "is driven by communication and trust (cohesion), not talent. High-TCI teams "
            "cover in games where they trail by 8+ at any point."
        ),
        "sport": "basketball_ncaaw",
        "market_type": "spreads",
        "model_config": {
            "type": "consensus_devig", "devig_method": "power",
            "target_book": "draftkings", "consensus_min_books": 3,
            "context_factors": ["tci_score", "in_game_run_recovery", "turnovers_per_game"],
        },
        "edge_threshold": 0.01,
    },
    {
        "name": "wcbb_tci_road_composure",
        "thesis": (
            "High-TCI teams perform better ATS in hostile road/neutral-site tournament "
            "environments. Cohesive teams maintain communication when the crowd is against "
            "them because their trust and shared language don't depend on external validation. "
            "Low-TCI teams — especially those reliant on home crowd energy — underperform "
            "ATS in away/neutral tournament games."
        ),
        "sport": "basketball_ncaaw",
        "market_type": "spreads",
        "model_config": {
            "type": "consensus_devig", "devig_method": "power",
            "target_book": "draftkings", "consensus_min_books": 3,
            "context_factors": ["tci_score", "venue_type", "home_away_ats_split"],
        },
        "edge_threshold": 0.01,
    },
    # === TOTALS ===
    {
        "name": "wcbb_low_tci_matchup_over",
        "thesis": (
            "When two low-TCI teams meet in tournament play, the Over is +EV. Low-cohesion "
            "teams produce more turnovers, transition points, and chaotic possessions under "
            "tournament pressure. The pace increases and defensive communication breaks down, "
            "pushing scoring above the total."
        ),
        "sport": "basketball_ncaaw",
        "market_type": "totals",
        "model_config": {
            "type": "consensus_devig", "devig_method": "power",
            "target_book": "draftkings", "consensus_min_books": 3,
            "context_factors": ["tci_score_home", "tci_score_away", "combined_tci"],
            "side_filter": "Over",
        },
        "edge_threshold": 0.01,
    },
    {
        "name": "wcbb_high_tci_matchup_under",
        "thesis": (
            "When two high-TCI teams meet, the Under is +EV. Both teams execute their "
            "systems effectively, leading to longer possessions, fewer turnovers, and more "
            "half-court play. The pace slows and defensive execution is high. Books set "
            "totals based on season averages, not matchup-level cohesion dynamics."
        ),
        "sport": "basketball_ncaaw",
        "market_type": "totals",
        "model_config": {
            "type": "consensus_devig", "devig_method": "power",
            "target_book": "draftkings", "consensus_min_books": 3,
            "context_factors": ["tci_score_home", "tci_score_away", "combined_tci"],
            "side_filter": "Under",
        },
        "edge_threshold": 0.01,
    },
    # === CROSS-SPORT EXTENSION (WNBA) ===
    {
        "name": "wnba_roster_continuity_early_season",
        "thesis": (
            "WNBA teams with high roster continuity (70%+ returning players) outperform "
            "ATS in the first 4 weeks of the season while new-look rosters are still "
            "integrating. The WNBA's short preseason (3 weeks) means chemistry-dependent "
            "teams have a systematic advantage early. Books anchor to talent rankings, "
            "not continuity."
        ),
        "sport": "basketball_wnba",
        "market_type": "spreads",
        "model_config": {
            "type": "consensus_devig", "devig_method": "power",
            "target_book": "draftkings", "consensus_min_books": 3,
            "context_factors": ["roster_continuity", "weeks_into_season"],
        },
        "edge_threshold": 0.01,
    },
]


async def main():
    from tools.hypothesis import HypothesisManager

    mgr = HypothesisManager()
    await mgr.initialize()

    existing = await mgr.list_hypotheses()
    existing_names = {h["name"] for h in existing}

    created = 0
    for h in TCI_HYPOTHESES:
        if h["name"] in existing_names:
            print(f"  SKIP (exists): {h['name']}")
            continue

        hid = await mgr.create_hypothesis(
            name=h["name"],
            thesis=h["thesis"],
            sport=h["sport"],
            market_type=h["market_type"],
            model_config=h["model_config"],
            edge_threshold=h["edge_threshold"],
            min_sample_size=30,  # Lower for tournament-only data
            notes="TCI (Team Cohesion Index) hypothesis - women's sports edge",
        )

        created += 1
        print(f"  [DRAFT] {h['name']}")

    print(f"\nCreated {created} TCI hypotheses")
    await mgr.close()


if __name__ == "__main__":
    asyncio.run(main())
