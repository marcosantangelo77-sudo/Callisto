"""
Thesis seed library — Cross-sport and structural seeds (NCAAB/NCAAW, golf, soccer, parlays, market microstructure, boosts).

Split out of tools/thesis_seeds.py; re-exported there as part of the public
seed API. Each dict follows the shared seed schema (see tools/thesis/_schema.py).
"""

from __future__ import annotations

from typing import Any

MISC_SEEDS: list[dict[str, Any]] = [
    # ── NCAAB / NCAAW: schedule density ───────────────────────
    {
        "seed_id": "ncaab_tournament_bid_race_spread",
        "category": "spreads",
        "sport": "basketball_ncaab",
        "market_type": "spreads",
        "thesis_template": (
            "Mid-major team fighting for bubble spot (KenPom 40-75) in final "
            "2 conference games covers ATS vs locked-in opponent — desperation "
            "differential not priced."
        ),
        "cohort_filter_sql": (
            "game_contexts.kenpom_rank BETWEEN 40 AND 75 "
            "AND game_contexts.conference_games_remaining <= 2 "
            "AND game_contexts.opp_bid_status = 'locked'"
        ),
        "signal_logic": "Motivation asymmetry is large but unpriced in spreads.",
        "min_sample_heuristic": 30,
        "ic_prior_estimate": 0.025,
        "variance_justification": "Bubble-specific, not generic rivalry/rest.",
        "exploration_status": "unexplored",
    },
    {
        "seed_id": "ncaaw_coach_tenure_spread",
        "category": "spreads",
        "sport": "basketball_ncaaw",
        "market_type": "spreads",
        "thesis_template": (
            "NCAAW teams with head-coach tenure ≥ 10 years cover ATS more "
            "vs teams in coach year 1-2 — roster-system fit gap in thin market."
        ),
        "cohort_filter_sql": (
            "game_contexts.home_coach_tenure_years >= 10 "
            "AND game_contexts.away_coach_tenure_years <= 2"
        ),
        "signal_logic": "Continuity edge in thin NCAAW market.",
        "min_sample_heuristic": 40,
        "ic_prior_estimate": 0.030,
        "variance_justification": "Coach-tenure asymmetry, specific + SQL-filterable.",
        "exploration_status": "unexplored",
    },
    {
        "seed_id": "ncaaw_post_portal_roster_continuity_total",
        "category": "totals",
        "sport": "basketball_ncaaw",
        "market_type": "totals",
        "thesis_template": (
            "NCAAW teams with < 15% transfer-portal outflow show team-total "
            "OVER in first 8 conference games — roster chemistry is priced "
            "down in thin markets."
        ),
        "cohort_filter_sql": (
            "game_contexts.transfer_outflow_pct < 0.15 "
            "AND game_contexts.conference_game_number <= 8"
        ),
        "signal_logic": "Continuity → efficient offense → higher team total.",
        "min_sample_heuristic": 30,
        "ic_prior_estimate": 0.025,
        "variance_justification": "Portal-era specific, identity/cohesion axis.",
        "exploration_status": "unexplored",
    },
    # ── Golf: course fit x weather ────────────────────────────
    {
        "seed_id": "golf_sg_approach_wind_interaction",
        "category": "props",
        "sport": "golf_pga",
        "market_type": "top_20_finish",
        "thesis_template": (
            "Tournaments with forecast wind ≥ 18 mph on ≥ 2 rounds: SG:Approach "
            "top-15 players finish top-20 at ~{pct}% above implied, because "
            "market weights total distance over approach accuracy."
        ),
        "cohort_filter_sql": (
            "game_contexts.tournament_max_wind_mph >= 18 "
            "AND game_contexts.tournament_high_wind_rounds >= 2 "
            "AND player_stats.sg_approach_rank <= 15"
        ),
        "signal_logic": "Wind amplifies approach-accuracy edge.",
        "min_sample_heuristic": 30,
        "ic_prior_estimate": 0.025,
        "variance_justification": "Skill x weather interaction, not pure weather or pure skill.",
        "exploration_status": "unexplored",
    },
    {
        "seed_id": "golf_tee_time_afternoon_wind_edge",
        "category": "props",
        "sport": "golf_pga",
        "market_type": "round_score",
        "thesis_template": (
            "On courses where forecast wind rises ≥ 8 mph afternoon vs morning, "
            "AM-wave players in round 1 have round-score UNDER edge vs PM wave."
        ),
        "cohort_filter_sql": (
            "game_contexts.am_wave_wind_mph + 8 <= game_contexts.pm_wave_wind_mph "
            "AND player_stats.tee_wave = 'AM'"
        ),
        "signal_logic": "Tee-wave luck isn't fully priced into round-1 matchups.",
        "min_sample_heuristic": 30,
        "ic_prior_estimate": 0.030,
        "variance_justification": "Wave-specific round-1 edge, distinct from tournament-winner.",
        "exploration_status": "unexplored",
    },
    # ── Soccer ────────────────────────────────────────────────
    {
        "seed_id": "soccer_mls_home_xg_travel",
        "category": "totals",
        "sport": "soccer_mls",
        "market_type": "totals",
        "thesis_template": (
            "MLS home team off a ≥ 2000-mile midweek CONCACAF Champions "
            "League trip, within 4 days. Team-total UNDER — travel + compressed "
            "schedule degrades xG."
        ),
        "cohort_filter_sql": (
            "game_contexts.home_midweek_travel_miles >= 2000 "
            "AND game_contexts.home_days_since_midweek <= 4"
        ),
        "signal_logic": "International-travel fatigue on MLS sides is under-modeled.",
        "min_sample_heuristic": 25,
        "ic_prior_estimate": 0.025,
        "variance_justification": "International context; MLS-specific trigger.",
        "exploration_status": "unexplored",
    },
    {
        "seed_id": "soccer_nwsl_derby_cards_prop",
        "category": "props",
        "sport": "soccer_nwsl",
        "market_type": "total_cards",
        "thesis_template": (
            "NWSL derby matches (same-region teams, historical rivalry index > 7) "
            "card-total OVER is underpriced in thin market."
        ),
        "cohort_filter_sql": (
            "game_contexts.rivalry_index >= 7 "
            "AND game_contexts.same_region = 1"
        ),
        "signal_logic": "Rivalry intensity lifts card count; books undersample.",
        "min_sample_heuristic": 20,
        "ic_prior_estimate": 0.030,
        "variance_justification": "Thin-market derby-specific cards prop.",
        "exploration_status": "unexplored",
    },
    # ── Boost / operator promo (structural) ───────────────────
    {
        "seed_id": "boost_nba_7pt_3s_underpriced",
        "category": "props",
        "sport": "basketball_nba",
        "market_type": "player_threes",
        "thesis_template": (
            "DK '7+ threes' profile boost on players averaging 3.5 attempts; "
            "boosted odds on Poisson-weighted attempt rate produce positive EV."
        ),
        "cohort_filter_sql": (
            "player_stats.threes_per_game >= 3.5"
        ),
        "signal_logic": "Operator boost on tail event — Poisson implies true prob > boosted line.",
        "min_sample_heuristic": 40,
        "ic_prior_estimate": 0.030,
        "variance_justification": "Operator-promo structural edge.",
        "exploration_status": "unexplored",
    },
]

# Validated at package import time by tools/thesis/_schema._validate_library().
