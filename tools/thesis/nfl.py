"""
Thesis seed library — NFL seeds.

Split out of tools/thesis_seeds.py; re-exported there as part of the public
seed API. Each dict follows the shared seed schema (see tools/thesis/_schema.py).
"""

from __future__ import annotations

from typing import Any

NFL_SEEDS: list[dict[str, Any]] = [
    # ── NFL: referees and situational ─────────────────────────
    {
        "seed_id": "nfl_ref_holding_call_rate_totals",
        "category": "totals",
        "sport": "americanfootball_nfl",
        "market_type": "totals",
        "thesis_template": (
            "NFL referee crew {crew} calls holding at top-quartile rate. "
            "Games with two pass-heavy offenses (neutral pass rate > 60%) "
            "under-adjust for drive-extending penalties — total OVER."
        ),
        "cohort_filter_sql": (
            "game_contexts.ref_crew = :crew "
            "AND game_contexts.home_neutral_pass_rate > 0.60 "
            "AND game_contexts.away_neutral_pass_rate > 0.60"
        ),
        "signal_logic": "Holding calls extend drives; pass-heavy amplifies.",
        "min_sample_heuristic": 25,
        "ic_prior_estimate": 0.022,
        "variance_justification": "Ref x offense profile interaction, not generic ref bias.",
        "exploration_status": "unexplored",
    },
    {
        "seed_id": "nfl_short_week_road_underdog",
        "category": "spreads",
        "sport": "americanfootball_nfl",
        "market_type": "spreads",
        "thesis_template": (
            "NFL road underdog on a short week (Thursday after Sunday) "
            "covering spread when the favorite is on a long week. "
            "Injury impact compounds in favor of fresh team."
        ),
        "cohort_filter_sql": (
            "game_contexts.home_days_rest >= 10 "
            "AND game_contexts.away_days_rest = 4 "
            "AND game_contexts.underdog = game_contexts.away_team"
        ),
        "signal_logic": "Asymmetric rest narrative usually attributed — but ATS fade.",
        "min_sample_heuristic": 25,
        "ic_prior_estimate": 0.020,
        "variance_justification": "Contra-narrative rest spot.",
        "exploration_status": "unexplored",
    },
    {
        "seed_id": "nfl_late_season_warm_team_cold_game",
        "category": "totals",
        "sport": "americanfootball_nfl",
        "market_type": "totals",
        "thesis_template": (
            "Dome / warm-climate team visiting cold-weather venue in "
            "Week 13+ with wind ≥ 12 mph or temp ≤ 25°F — totals UNDER."
        ),
        "cohort_filter_sql": (
            "game_contexts.away_home_climate = 'dome_or_warm' "
            "AND game_contexts.season_week >= 13 "
            "AND (game_contexts.wind_mph >= 12 OR game_contexts.temp_f <= 25)"
        ),
        "signal_logic": "QB accuracy + K distance collapse for warm-climate visitors.",
        "min_sample_heuristic": 25,
        "ic_prior_estimate": 0.028,
        "variance_justification": "Climate-origin x venue interaction, not blanket weather.",
        "exploration_status": "unexplored",
    },
    {
        "seed_id": "nfl_sgp_passing_yds_wr_yds",
        "category": "parlay",
        "sport": "americanfootball_nfl",
        "market_type": "sgp",
        "thesis_template": (
            "SGP: QB pass-yards OVER × WR1 receiving-yards OVER when target "
            "share ≥ 28%. Book applies generic correlation coefficient."
        ),
        "cohort_filter_sql": (
            "player_stats.target_share >= 0.28 "
            "AND player_stats.wr_depth_chart_rank = 1"
        ),
        "signal_logic": "Concentration WR means joint realization is higher than book coeff.",
        "min_sample_heuristic": 40,
        "ic_prior_estimate": 0.030,
        "variance_justification": "Target-concentration-gated SGP.",
        "exploration_status": "unexplored",
    },
    # ── Derivative markets ────────────────────────────────────
    {
        "seed_id": "nfl_1h_total_pass_heavy",
        "category": "totals",
        "sport": "americanfootball_nfl",
        "market_type": "h1_totals",
        "thesis_template": (
            "Two top-10 pass-volume offenses — 1H totals OVER because early-game "
            "neutral-script pass is high; book 1H set to proportional share."
        ),
        "cohort_filter_sql": (
            "game_contexts.home_neutral_pass_volume_rank <= 10 "
            "AND game_contexts.away_neutral_pass_volume_rank <= 10"
        ),
        "signal_logic": "Pass-heavy games front-load scoring; 1H OVER.",
        "min_sample_heuristic": 25,
        "ic_prior_estimate": 0.020,
        "variance_justification": "First-half derivative market, team-style gated.",
        "exploration_status": "unexplored",
    },
]

# Validated at package import time by tools/thesis/_schema._validate_library().
