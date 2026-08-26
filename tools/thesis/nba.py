"""
Thesis seed library — NBA / WNBA seeds.

Split out of tools/thesis_seeds.py; re-exported there as part of the public
seed API. Each dict follows the shared seed schema (see tools/thesis/_schema.py).
"""

from __future__ import annotations

from typing import Any

NBA_SEEDS: list[dict[str, Any]] = [
    # ── NBA: refs ─────────────────────────────────────────────
    {
        "seed_id": "nba_ref_crew_foul_rate",
        "category": "props",
        "sport": "basketball_nba",
        "market_type": "player_points",
        "thesis_template": (
            "NBA ref crew {crew_id} calls fouls at {crew_foul_rate} per 48 min, "
            "{pct_above_league}% above league median. For star scorers with "
            "FT-rate > 20%, points-prop OVER is underpriced."
        ),
        "cohort_filter_sql": (
            "game_contexts.ref_crew_id = :crew_id "
            "AND player_stats.ft_rate > 0.20"
        ),
        "signal_logic": "High-whistle crew inflates FT-heavy scorers' points.",
        "min_sample_heuristic": 60,
        "ic_prior_estimate": 0.025,
        "variance_justification": "Crew-specific, player-profile-gated — not generic star prop.",
        "exploration_status": "unexplored",
    },
    {
        "seed_id": "nba_ref_road_favorite_bias",
        "category": "spreads",
        "sport": "basketball_nba",
        "market_type": "spreads",
        "thesis_template": (
            "Ref crews {low_whistle_crew_ids} call {pct_below} % fewer fouls "
            "than league average. Road favorites on their games cover more "
            "often because the home-crowd whistle advantage disappears."
        ),
        "cohort_filter_sql": (
            "game_contexts.ref_crew_id IN (:low_whistle_crew_ids) "
            "AND game_contexts.favorite = game_contexts.away_team"
        ),
        "signal_logic": "Low-whistle refs neutralize home-crowd call bias.",
        "min_sample_heuristic": 50,
        "ic_prior_estimate": 0.022,
        "variance_justification": "Ref-based conditional on spreads market.",
        "exploration_status": "unexplored",
    },
    # ── NBA: schedule spot / travel ───────────────────────────
    {
        "seed_id": "nba_b2b_cross_country_flight",
        "category": "totals",
        "sport": "basketball_nba",
        "market_type": "totals",
        "thesis_template": (
            "Second game of a back-to-back with travel of ≥ 2000 miles AND "
            "≥ 2 timezone shift. Game totals UNDER because pace and shooting "
            "both collapse — not just one or the other."
        ),
        "cohort_filter_sql": (
            "game_contexts.is_b2b_night2 = 1 "
            "AND game_contexts.travel_miles >= 2000 "
            "AND ABS(game_contexts.timezone_shift) >= 2"
        ),
        "signal_logic": "Fatigue compounds on B2B+coast travel; pace and FG% both drop.",
        "min_sample_heuristic": 40,
        "ic_prior_estimate": 0.030,
        "variance_justification": "Compound condition, not a simple B2B filter.",
        "exploration_status": "unexplored",
    },
    {
        "seed_id": "nba_schedule_letdown_spread",
        "category": "spreads",
        "sport": "basketball_nba",
        "market_type": "spreads",
        "thesis_template": (
            "NBA team playing {days_post_marquee} day(s) after a marquee win "
            "over a rival (> 15-point win, prime-time slot) sees ATS DROP "
            "in their next vs non-playoff opponent."
        ),
        "cohort_filter_sql": (
            "game_contexts.days_since_last_game = :days_post_marquee "
            "AND game_contexts.prev_game_result_margin > 15 "
            "AND game_contexts.prev_game_national_tv = 1 "
            "AND game_contexts.opponent_playoff_rank > 10"
        ),
        "signal_logic": "Emotional letdown after statement game, quantitative trigger.",
        "min_sample_heuristic": 30,
        "ic_prior_estimate": 0.025,
        "variance_justification": "Psychological trigger with SQL-specific filter.",
        "exploration_status": "unexplored",
    },
    # ── NBA: live/in-game ─────────────────────────────────────
    {
        "seed_id": "nba_live_q3_closing_run_overreaction",
        "category": "live",
        "sport": "basketball_nba",
        "market_type": "live_spreads",
        "thesis_template": (
            "When a team closes Q3 on a ≥ 8-0 run, live spread moves 2.5+ "
            "points in their favor. Historical Q4 regression implies "
            "the prior-leader covers the live spread at elevated rate."
        ),
        "cohort_filter_sql": (
            "game_contexts.q3_closing_run_points >= 8 "
            "AND game_contexts.q3_closing_run_opp_points = 0"
        ),
        "signal_logic": "Run-recency bias in live market; mean reversion in Q4.",
        "min_sample_heuristic": 60,
        "ic_prior_estimate": 0.030,
        "variance_justification": "Live-market overreaction pattern — distinct surface.",
        "exploration_status": "unexplored",
    },
    # ── WNBA: pace / identity ────────────────────────────────
    {
        "seed_id": "wnba_fibat_pace_overpricing",
        "category": "totals",
        "sport": "basketball_wnba",
        "market_type": "totals",
        "thesis_template": (
            "WNBA matchups where both teams sit in top-5 pace rank produce "
            "totals OVER ~{pct}% above implied — books under-model upper "
            "tail of pace×pace."
        ),
        "cohort_filter_sql": (
            "game_contexts.home_pace_rank <= 5 "
            "AND game_contexts.away_pace_rank <= 5"
        ),
        "signal_logic": "Pace compounds multiplicatively on upper tail.",
        "min_sample_heuristic": 30,
        "ic_prior_estimate": 0.030,
        "variance_justification": "Tail-specific (both teams top-5), not mean pace.",
        "exploration_status": "unexplored",
    },
    {
        "seed_id": "wnba_star_usage_post_injury_prop",
        "category": "props",
        "sport": "basketball_wnba",
        "market_type": "player_points",
        "thesis_template": (
            "First game a WNBA star returns from ≥ 10-day injury, usage lower "
            "than season norm; Under points-prop underpriced due to minutes cap."
        ),
        "cohort_filter_sql": (
            "player_stats.games_missed_last_streak >= 10 "
            "AND player_stats.is_return_game = 1 "
            "AND player_stats.usage_rank <= 10"
        ),
        "signal_logic": "Minutes cap + ramp-up → under.",
        "min_sample_heuristic": 20,
        "ic_prior_estimate": 0.035,
        "variance_justification": "Return-game specific, explicit minutes mechanism.",
        "exploration_status": "unexplored",
    },
    {
        "seed_id": "nba_sgp_star_points_team_total",
        "category": "parlay",
        "sport": "basketball_nba",
        "market_type": "sgp",
        "thesis_template": (
            "SGP: star points-OVER × team-total-OVER. Books decorrelate these "
            "for stars with usage-rate ≥ 32% — joint realization is higher."
        ),
        "cohort_filter_sql": (
            "player_stats.usage_rate >= 0.32"
        ),
        "signal_logic": "High-usage stars drive team total; SGP misprices.",
        "min_sample_heuristic": 60,
        "ic_prior_estimate": 0.030,
        "variance_justification": "High-usage gating + SGP structure.",
        "exploration_status": "unexplored",
    },
    # ── Market microstructure / steam ─────────────────────────
    {
        "seed_id": "nba_pre_tipoff_late_sharp_steam",
        "category": "spreads",
        "sport": "basketball_nba",
        "market_type": "spreads",
        "thesis_template": (
            "Line move ≥ 1.5 pts in last 30 min before tip with reverse line "
            "movement vs. public money — steam side covers ATS at {pct}%."
        ),
        "cohort_filter_sql": (
            "game_contexts.minutes_before_tip <= 30 "
            "AND ABS(game_contexts.line_move_last_30m_pts) >= 1.5 "
            "AND game_contexts.rlm_flag = 1"
        ),
        "signal_logic": "Sharp late steam + RLM → sharp side covers.",
        "min_sample_heuristic": 80,
        "ic_prior_estimate": 0.025,
        "variance_justification": "Market-microstructure trigger, not fundamental.",
        "exploration_status": "unexplored",
    },
    {
        "seed_id": "nba_live_quarter_opening_run",
        "category": "live",
        "sport": "basketball_nba",
        "market_type": "live_totals",
        "thesis_template": (
            "First 90s of Q1 with ≥ 10 combined points triggers live-total "
            "OVER move ≥ 3 pts. Pace settles; UNDER side has edge."
        ),
        "cohort_filter_sql": (
            "game_contexts.q1_first_90s_points >= 10 "
            "AND game_contexts.live_total_move >= 3"
        ),
        "signal_logic": "Early-game pace projects poorly; live total overshoots.",
        "min_sample_heuristic": 60,
        "ic_prior_estimate": 0.025,
        "variance_justification": "Q1-opening-minute microstructure edge.",
        "exploration_status": "unexplored",
    },
    # ── Injury cascade ────────────────────────────────────────
    {
        "seed_id": "nba_star_out_next_best_usage_prop",
        "category": "props",
        "sport": "basketball_nba",
        "market_type": "player_points_rebounds_assists",
        "thesis_template": (
            "When usage leader (team-rank 1) is ruled OUT < 4h to tip, "
            "next-highest-usage player's PRA-OVER is underpriced because "
            "book hasn't propagated cascade."
        ),
        "cohort_filter_sql": (
            "game_contexts.usage_leader_status = 'OUT' "
            "AND game_contexts.hours_to_tipoff < 4 "
            "AND player_stats.team_usage_rank = 2"
        ),
        "signal_logic": "Late injury news lags in prop lines; next man up gains both usage and minutes.",
        "min_sample_heuristic": 30,
        "ic_prior_estimate": 0.035,
        "variance_justification": "Timing-of-news specific, not generic next-man-up.",
        "exploration_status": "unexplored",
    },
    {
        "seed_id": "nba_futures_regular_season_over",
        "category": "futures",
        "sport": "basketball_nba",
        "market_type": "season_wins",
        "thesis_template": (
            "Team with summer-acquired star (trade/FA) on top-15 usage line, "
            "joining a top-10 offense — regular-season-wins OVER underpriced."
        ),
        "cohort_filter_sql": (
            "game_contexts.star_acquired_last_offseason = 1 "
            "AND player_stats.usage_rank <= 15 "
            "AND game_contexts.prior_season_off_rank <= 10"
        ),
        "signal_logic": "Team-building compounds; market underprices synergy.",
        "min_sample_heuristic": 10,
        "ic_prior_estimate": 0.022,
        "variance_justification": "Roster construction axis.",
        "exploration_status": "unexplored",
    },
    # ── Coach/manager specific ────────────────────────────────
    {
        "seed_id": "nba_coach_out_of_timeout_q4",
        "category": "live",
        "sport": "basketball_nba",
        "market_type": "live_next_score",
        "thesis_template": (
            "Specific NBA coaches (tagged by historic OOT offensive rating) "
            "with OOT ORtg top-quartile: live 'next score' on team after "
            "Q4 timeout is underpriced."
        ),
        "cohort_filter_sql": (
            "game_contexts.coach_oot_ortg_rank <= 8 "
            "AND game_contexts.quarter = 4 "
            "AND game_contexts.timeout_just_called = 1"
        ),
        "signal_logic": "OOT playcalling is a repeatable coach-specific skill.",
        "min_sample_heuristic": 40,
        "ic_prior_estimate": 0.025,
        "variance_justification": "Coach-tagged live trigger in Q4.",
        "exploration_status": "unexplored",
    },
    {
        "seed_id": "nba_christmas_day_totals_overreaction",
        "category": "totals",
        "sport": "basketball_nba",
        "market_type": "totals",
        "thesis_template": (
            "Christmas Day games: totals set ~1.5 above season-average pace "
            "due to 'showcase' narrative; UNDER edge historically."
        ),
        "cohort_filter_sql": (
            "game_contexts.is_christmas_day = 1"
        ),
        "signal_logic": "Narrative-based total inflation on showcase days.",
        "min_sample_heuristic": 15,
        "ic_prior_estimate": 0.025,
        "variance_justification": "Slate-specific narrative edge.",
        "exploration_status": "unexplored",
    },
    {
        "seed_id": "nba_player_pts_over_vs_small_ball",
        "category": "props",
        "sport": "basketball_nba",
        "market_type": "player_points",
        "thesis_template": (
            "Primary-scoring guards/wings see point-prop Overs mispriced when "
            "opponent plays small-ball (no rim protector / 5-out). Books "
            "adjust totals for pace but not player-level; the star absorbs "
            "extra possessions at elevated rates. Over is +EV by 1.5%+ when "
            "opp rim-protection-rank is bottom 10."
        ),
        "cohort_filter_sql": (
            "player_stats.usage_rate_rank <= 5 "
            "AND game_contexts.opp_rim_protection_rank >= 20"
        ),
        "signal_logic": "Usage-heavy scorer vs small lineup -> points Over.",
        "min_sample_heuristic": 25,
        "ic_prior_estimate": 0.022,
        "variance_justification": (
            "Player points-prop gated on opponent lineup-size / rim-protection; "
            "orthogonal to ref-crew and B2B travel seeds."
        ),
        "exploration_status": "unexplored",
    },
]

# Validated at package import time by tools/thesis/_schema._validate_library().
