"""
Thesis seed library — NHL seeds.

Split out of tools/thesis_seeds.py; re-exported there as part of the public
seed API. Each dict follows the shared seed schema (see tools/thesis/_schema.py).
"""

from __future__ import annotations

from typing import Any

NHL_SEEDS: list[dict[str, Any]] = [
    # ── NHL: goalies / rest ───────────────────────────────────
    {
        "seed_id": "nhl_backup_goalie_b2b",
        "category": "totals",
        "sport": "icehockey_nhl",
        "market_type": "totals",
        "thesis_template": (
            "Second night of a B2B with starter playing Game 1, backup goalie "
            "confirmed in Game 2 (SV% < 0.905 career). Over is underpriced "
            "when the backup faces an above-average offense."
        ),
        "cohort_filter_sql": (
            "game_contexts.is_b2b_night2 = 1 "
            "AND player_stats.goalie_role = 'backup' "
            "AND player_stats.career_svpct < 0.905 "
            "AND game_contexts.opp_xga_rank <= 15"
        ),
        "signal_logic": "Backup quality gap + opponent offense combines for over.",
        "min_sample_heuristic": 40,
        "ic_prior_estimate": 0.028,
        "variance_justification": "Goalie identity, not just rest day.",
        "exploration_status": "unexplored",
    },
    {
        "seed_id": "nhl_goalie_rest_7plus",
        "category": "h2h",
        "sport": "icehockey_nhl",
        "market_type": "h2h",
        "thesis_template": (
            "Starter on 7+ days rest (injury return or extended rest) is "
            "overpriced in ML market — rust offsets rest at this horizon."
        ),
        "cohort_filter_sql": (
            "player_stats.goalie_role = 'starter' "
            "AND player_stats.days_since_last_start >= 7"
        ),
        "signal_logic": "Contra-narrative: extended rest is net-negative for goalies.",
        "min_sample_heuristic": 40,
        "ic_prior_estimate": 0.022,
        "variance_justification": "Inverts the usual 'rest = good' prior.",
        "exploration_status": "unexplored",
    },
    {
        "seed_id": "nhl_empty_net_live_total",
        "category": "live",
        "sport": "icehockey_nhl",
        "market_type": "live_totals",
        "thesis_template": (
            "When trailing team pulls goalie with ≥ 90s remaining and down "
            "1-2 goals, live total OVER closes ~{pct}% too cheap; ENG rate "
            "conditional on pull time is stable."
        ),
        "cohort_filter_sql": (
            "game_contexts.goalie_pulled = 1 "
            "AND game_contexts.pull_time_remaining_s >= 90 "
            "AND ABS(game_contexts.score_diff) <= 2"
        ),
        "signal_logic": "ENG-conditional goal rate ≈ 25-30%; live line lags.",
        "min_sample_heuristic": 80,
        "ic_prior_estimate": 0.040,
        "variance_justification": "In-game mechanical edge, not pre-game thesis.",
        "exploration_status": "unexplored",
    },
    # ── NHL: special teams ────────────────────────────────────
    {
        "seed_id": "nhl_pp_mismatch_team_total",
        "category": "totals",
        "sport": "icehockey_nhl",
        "market_type": "team_totals",
        "thesis_template": (
            "Top-5 PP unit vs bottom-10 PK. Team-total OVER is underpriced "
            "when projected PIM rate from ref assignment is top-quartile."
        ),
        "cohort_filter_sql": (
            "game_contexts.team_pp_rank <= 5 "
            "AND game_contexts.opp_pk_rank >= 22 "
            "AND game_contexts.ref_pim_per_game_rank <= 8"
        ),
        "signal_logic": "Special-teams mismatch x ref-PIM rate compounds.",
        "min_sample_heuristic": 30,
        "ic_prior_estimate": 0.025,
        "variance_justification": "Ref-conditional special-teams edge.",
        "exploration_status": "unexplored",
    },
    {
        "seed_id": "nhl_d_pair_injury_shot_share",
        "category": "props",
        "sport": "icehockey_nhl",
        "market_type": "player_shots",
        "thesis_template": (
            "When top-D pair is split by injury, the remaining top-D "
            "sees TOI spike to 26+ min — shots-on-goal prop OVER underpriced."
        ),
        "cohort_filter_sql": (
            "game_contexts.top_d_pair_injury_flag = 1 "
            "AND player_stats.position = 'D' "
            "AND player_stats.depth_chart_rank = 1"
        ),
        "signal_logic": "TOI cascade elevates shots prop baseline.",
        "min_sample_heuristic": 25,
        "ic_prior_estimate": 0.028,
        "variance_justification": "Defenseman-specific injury cascade.",
        "exploration_status": "unexplored",
    },
    {
        "seed_id": "nhl_2nd_period_goals_over",
        "category": "totals",
        "sport": "icehockey_nhl",
        "market_type": "period_totals",
        "thesis_template": (
            "Top-10 shot-volume offenses: 2nd-period goals OVER 1.5 is "
            "underpriced — the 'long change' amplifies scoring in P2."
        ),
        "cohort_filter_sql": (
            "game_contexts.home_shot_rank <= 10 "
            "AND game_contexts.away_shot_rank <= 10"
        ),
        "signal_logic": "Long-change second period structurally highest-scoring.",
        "min_sample_heuristic": 40,
        "ic_prior_estimate": 0.025,
        "variance_justification": "Period derivative + rule mechanic.",
        "exploration_status": "unexplored",
    },
    {
        "seed_id": "nhl_goalie_saves_over_vs_b2b_road",
        "category": "props",
        "sport": "icehockey_nhl",
        "market_type": "goalie_saves",
        "thesis_template": (
            "Goalies facing a team on the second night of a back-to-back with "
            "travel see elevated shot volume. Tired skaters take more low-"
            "percentage perimeter shots that inflate Saves without changing "
            "Goals Against much. Over Saves is +EV by 1.5%+."
        ),
        "cohort_filter_sql": (
            "game_contexts.opp_back_to_back = 1 "
            "AND game_contexts.opp_travel_km >= 1000"
        ),
        "signal_logic": "B2B+travel opponent -> low-pct shot volume -> Saves Over.",
        "min_sample_heuristic": 20,
        "ic_prior_estimate": 0.020,
        "variance_justification": (
            "Goalie-saves prop keyed on OPPONENT fatigue rather than goalie "
            "rest; complements nhl_backup_goalie_b2b and nhl_goalie_rest_7plus."
        ),
        "exploration_status": "unexplored",
    },
    {
        "seed_id": "nhl_skater_sog_over_after_trailing_3rd",
        "category": "props",
        "sport": "icehockey_nhl",
        "market_type": "skater_shots_on_goal",
        "thesis_template": (
            "Volume shooters who logged 3rd-period comeback minutes in the "
            "previous game see elevated SOG in the next outing — line combos "
            "stabilize and shot rate reverts high. Books set SOG Over using "
            "season averages rather than momentum-adjusted rate. Over +EV 1.5%+."
        ),
        "cohort_filter_sql": (
            "player_stats.prev_game_trailing_toi_rank <= 10 "
            "AND player_stats.season_shot_volume_rank <= 15"
        ),
        "signal_logic": "Prev-game leverage TOI -> next-game SOG Over.",
        "min_sample_heuristic": 25,
        "ic_prior_estimate": 0.018,
        "variance_justification": (
            "SOG prop gated on previous-game leverage minutes; distinct from "
            "d-pair-injury and empty-net total seeds."
        ),
        "exploration_status": "unexplored",
    },
]

# Validated at package import time by tools/thesis/_schema._validate_library().
