"""
Aggregated thesis-seed library for Callisto's hypothesis generator.

This package is the split of the former monolithic tools/thesis_seeds.py.
Seed dicts are grouped by sport/category:

- ``mlb``   — MLB seeds (umpires, bullpen, props, futures, ...)
- ``nba``   — NBA and WNBA seeds (refs, schedule, live, pace/identity)
- ``nhl``   — NHL seeds (goalies, special teams, period derivatives)
- ``nfl``   — NFL seeds (referees, situational, 1H totals)
- ``misc``  — NCAAB/NCAAW, golf, soccer, parlays, market microstructure,
              boosts, day-of-week effects

The full library (``THESIS_SEEDS``), schema constants, validation, and
runtime query helpers are re-assembled here and re-exported from
``tools/thesis_seeds.py``, which remains the public import path.
"""

from __future__ import annotations

from typing import Any

from ._schema import (
    REQUIRED_SEED_KEYS,
    VALID_CATEGORIES,
    VALID_EXPLORATION,
    _validate_library,
    validate_seed,
)
from .mlb import MLB_SEEDS
from .nba import NBA_SEEDS
from .nhl import NHL_SEEDS
from .nfl import NFL_SEEDS
from .misc import MISC_SEEDS

THESIS_SEEDS: list[dict[str, Any]] = (
    MLB_SEEDS + NBA_SEEDS + NHL_SEEDS + NFL_SEEDS + MISC_SEEDS
)

# Preserve the original (pre-split) library order. ``pick_unexplored_seeds``
# and other consumers take the first N matches, so ordering is behavior.
_ORIG_ORDER = ["mlb_umpire_zone_totals_bias", "mlb_umpire_k_prop_bias", "mlb_umpire_walk_prop_bias", "mlb_pitcher_bullpen_handoff_f5", "mlb_opener_bullpen_f3", "mlb_3rd_time_through_over", "mlb_lineup_vs_lhp_stack", "mlb_new_leadoff_hitter_runs_prop", "mlb_wind_out_hr_prop_bandbox", "mlb_humidor_game_totals", "nba_ref_crew_foul_rate", "nba_ref_road_favorite_bias", "nba_b2b_cross_country_flight", "nba_schedule_letdown_spread", "nba_live_q3_closing_run_overreaction", "nhl_backup_goalie_b2b", "nhl_goalie_rest_7plus", "nhl_empty_net_live_total", "nhl_pp_mismatch_team_total", "nfl_ref_holding_call_rate_totals", "nfl_short_week_road_underdog", "nfl_late_season_warm_team_cold_game", "ncaab_tournament_bid_race_spread", "ncaaw_coach_tenure_spread", "ncaaw_post_portal_roster_continuity_total", "wnba_fibat_pace_overpricing", "wnba_star_usage_post_injury_prop", "golf_sg_approach_wind_interaction", "golf_tee_time_afternoon_wind_edge", "mlb_sgp_leadoff_runs_team_total", "nba_sgp_star_points_team_total", "nfl_sgp_passing_yds_wr_yds", "nba_pre_tipoff_late_sharp_steam", "mlb_closing_line_vs_draftkings_edge", "soccer_mls_home_xg_travel", "soccer_nwsl_derby_cards_prop", "mlb_live_2out_rally_spread", "nba_live_quarter_opening_run", "mlb_shift_ban_pull_hitter_hits_prop", "mlb_pitch_clock_late_inning_prop", "nba_star_out_next_best_usage_prop", "nhl_d_pair_injury_shot_share", "mlb_futures_pythag_div_win", "nba_futures_regular_season_over", "mlb_starter_first_inning_scoreless", "mlb_pitcher_catcher_battery_framing", "nba_coach_out_of_timeout_q4", "mlb_manager_ipg_hook_tendency", "boost_nba_7pt_3s_underpriced", "mlb_sunday_day_game_travel_total", "nba_christmas_day_totals_overreaction", "nfl_1h_total_pass_heavy", "nhl_2nd_period_goals_over", "mlb_pitcher_k_over_vs_low_k_team", "mlb_batter_hits_over_at_hitter_parks", "nba_player_pts_over_vs_small_ball", "nhl_goalie_saves_over_vs_b2b_road", "nhl_skater_sog_over_after_trailing_3rd", "mlb_nrfi_both_aces_first_inning"]

_by_id = {s["seed_id"]: s for s in THESIS_SEEDS}
assert set(_by_id) == set(_ORIG_ORDER), "seed-id drift vs original library"
THESIS_SEEDS[:] = [_by_id[sid] for sid in _ORIG_ORDER]


_validate_library()

from .runtime import (  # noqa: E402
    get_seed,
    list_seeds,
    pick_unexplored_seeds,
    seed_category_coverage,
    seed_sport_coverage,
)

__all__ = [
    "THESIS_SEEDS",
    "MLB_SEEDS",
    "NBA_SEEDS",
    "NHL_SEEDS",
    "NFL_SEEDS",
    "MISC_SEEDS",
    "REQUIRED_SEED_KEYS",
    "VALID_CATEGORIES",
    "VALID_EXPLORATION",
    "validate_seed",
    "list_seeds",
    "get_seed",
    "seed_category_coverage",
    "seed_sport_coverage",
    "pick_unexplored_seeds",
]
