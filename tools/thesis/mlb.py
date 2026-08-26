"""
Thesis seed library — MLB seeds.

Split out of tools/thesis_seeds.py; re-exported there as part of the public
seed API. Each dict follows the shared seed schema (see tools/thesis/_schema.py).
"""

from __future__ import annotations

from typing import Any

MLB_SEEDS: list[dict[str, Any]] = [
    # ── MLB: umpires ──────────────────────────────────────────
    {
        "seed_id": "mlb_umpire_zone_totals_bias",
        "category": "totals",
        "sport": "baseball_mlb",
        "market_type": "totals",
        "thesis_template": (
            "HP umpire {ump_name} has a {zone_adj} called-strike zone "
            "(top/bottom quartile by zone area over last 24 months). "
            "Games he calls run {direction} the book total more often than "
            "chance; books price the lineup matchup but not the umpire."
        ),
        "cohort_filter_sql": (
            "game_contexts.home_plate_umpire = :ump_name "
            "AND game_contexts.game_date >= :train_start"
        ),
        "signal_logic": (
            "Expanded zone → more called strikes → fewer walks → total UNDER. "
            "Tight zone → opposite. Book lines do not adjust for umpire."
        ),
        "min_sample_heuristic": 60,
        "ic_prior_estimate": 0.025,
        "variance_justification": (
            "Orthogonal to pitcher/lineup/weather: same two teams with a "
            "different umpire produce a different expected total."
        ),
        "exploration_status": "unexplored",
    },
    {
        "seed_id": "mlb_umpire_k_prop_bias",
        "category": "props",
        "sport": "baseball_mlb",
        "market_type": "player_strikeouts",
        "thesis_template": (
            "HP umpire {ump_name} grants extra called strikes. Starting-pitcher "
            "K props set against league-wide priors are soft on his games."
        ),
        "cohort_filter_sql": (
            "game_contexts.home_plate_umpire = :ump_name "
            "AND player_stats.position = 'SP'"
        ),
        "signal_logic": "Umpire zone expansion lifts K-props OVER.",
        "min_sample_heuristic": 40,
        "ic_prior_estimate": 0.030,
        "variance_justification": "Per-prop, not per-game — different market surface than totals.",
        "exploration_status": "unexplored",
    },
    {
        "seed_id": "mlb_umpire_walk_prop_bias",
        "category": "props",
        "sport": "baseball_mlb",
        "market_type": "player_walks",
        "thesis_template": (
            "Tight-zone umpire {ump_name} inflates BB-prop OVERs, especially "
            "for starters with career BB/9 above league median."
        ),
        "cohort_filter_sql": (
            "game_contexts.home_plate_umpire = :ump_name "
            "AND player_stats.career_bb9 > 3.2"
        ),
        "signal_logic": "Tight zone + nibbler → walks spike.",
        "min_sample_heuristic": 30,
        "ic_prior_estimate": 0.035,
        "variance_justification": "Interaction between umpire and pitcher command profile.",
        "exploration_status": "unexplored",
    },
    # ── MLB: bullpen / handoff ─────────────────────────────────
    {
        "seed_id": "mlb_pitcher_bullpen_handoff_f5",
        "category": "totals",
        "sport": "baseball_mlb",
        "market_type": "totals_f5",
        "thesis_template": (
            "When starter projected pitch count is near the manager's historical "
            "hook (at or above 85% of average exit pitch count), F5 totals UNDER "
            "is underpriced because the market prices the bullpen being in-game."
        ),
        "cohort_filter_sql": (
            "game_contexts.projected_starter_pc >= 0.85 * game_contexts.manager_avg_exit_pc"
        ),
        "signal_logic": "Manager's pattern of leaving starter in through F5 is stable; books lag.",
        "min_sample_heuristic": 50,
        "ic_prior_estimate": 0.020,
        "variance_justification": "First-5 inning market, not full game — distinct surface.",
        "exploration_status": "unexplored",
    },
    {
        "seed_id": "mlb_opener_bullpen_f3",
        "category": "totals",
        "sport": "baseball_mlb",
        "market_type": "totals_first_3",
        "thesis_template": (
            "Teams using an opener (listed SP throws <3 innings historically) "
            "produce first-3-inning UNDERs — the opener tends to dominate one "
            "time through before the bulk guy enters cold."
        ),
        "cohort_filter_sql": (
            "game_contexts.starter_is_opener = 1 "
            "AND player_stats.career_innings_per_start < 3.0"
        ),
        "signal_logic": "Opener + one-TTO gives F3 UNDER a structural edge.",
        "min_sample_heuristic": 30,
        "ic_prior_estimate": 0.028,
        "variance_justification": "Opener-specific, not generic F5 under.",
        "exploration_status": "unexplored",
    },
    {
        "seed_id": "mlb_3rd_time_through_over",
        "category": "live",
        "sport": "baseball_mlb",
        "market_type": "live_totals",
        "thesis_template": (
            "Live over edges open when the starting pitcher is about to face "
            "the heart of the order for the 3rd time (lineup slots 2-5). "
            "Live totals lag the TTO-3 penalty."
        ),
        "cohort_filter_sql": (
            "game_contexts.current_batter_tto = 3 "
            "AND game_contexts.batter_lineup_slot BETWEEN 2 AND 5"
        ),
        "signal_logic": "TTO-3 wOBA bump vs fatigued starter; live total adjusts slowly.",
        "min_sample_heuristic": 80,
        "ic_prior_estimate": 0.035,
        "variance_justification": "Live (in-game), with specific lineup-slot trigger.",
        "exploration_status": "unexplored",
    },
    # ── MLB: lineup ──────────────────────────────────────────
    {
        "seed_id": "mlb_lineup_vs_lhp_stack",
        "category": "totals",
        "sport": "baseball_mlb",
        "market_type": "team_totals",
        "thesis_template": (
            "Team {team} starts {n_rhb}+ RHB against LHP {lhp_name}, whose "
            "career wOBA allowed to RHB is top-quartile. Team-total OVER "
            "undervalues the platoon stack."
        ),
        "cohort_filter_sql": (
            "game_contexts.opposing_starter_throws = 'L' "
            "AND game_contexts.rhb_in_lineup >= :n_rhb"
        ),
        "signal_logic": "Platoon advantage on stacked day; team total under-reacts.",
        "min_sample_heuristic": 40,
        "ic_prior_estimate": 0.022,
        "variance_justification": "Team total (not game total), with platoon trigger.",
        "exploration_status": "unexplored",
    },
    {
        "seed_id": "mlb_new_leadoff_hitter_runs_prop",
        "category": "props",
        "sport": "baseball_mlb",
        "market_type": "player_runs",
        "thesis_template": (
            "First {games_at_top}-game stretch of a player batting leadoff "
            "carries under-adjusted runs-scored prop due to limited sample "
            "at the new slot; books keep prior-slot baseline."
        ),
        "cohort_filter_sql": (
            "player_stats.lineup_slot = 1 "
            "AND player_stats.games_at_current_slot <= :games_at_top"
        ),
        "signal_logic": "Slot-change batters see more PA; prop line slow to adjust.",
        "min_sample_heuristic": 25,
        "ic_prior_estimate": 0.025,
        "variance_justification": "Slot-change timing — a novel trigger, not rest/matchup.",
        "exploration_status": "unexplored",
    },
    # ── MLB: park / weather interactions ───────────────────────
    {
        "seed_id": "mlb_wind_out_hr_prop_bandbox",
        "category": "props",
        "sport": "baseball_mlb",
        "market_type": "player_home_runs",
        "thesis_template": (
            "At {park} with wind out to {direction} ≥ {mph} mph, HR-prop OVER "
            "for pull-side power hitters is underpriced."
        ),
        "cohort_filter_sql": (
            "game_contexts.park = :park AND game_contexts.wind_direction = :direction "
            "AND game_contexts.wind_mph >= :mph AND player_stats.pull_hr_rate >= 0.55"
        ),
        "signal_logic": "Wind vector + pull-side profile compounds HR-prop edge.",
        "min_sample_heuristic": 40,
        "ic_prior_estimate": 0.030,
        "variance_justification": "Triple interaction — park x wind x hitter profile.",
        "exploration_status": "unexplored",
    },
    {
        "seed_id": "mlb_humidor_game_totals",
        "category": "totals",
        "sport": "baseball_mlb",
        "market_type": "totals",
        "thesis_template": (
            "First {n_games} games of season at humidor parks (Coors, Chase, "
            "Fenway after storage change) see under-reaction as the new "
            "ball-storage humidity stabilizes."
        ),
        "cohort_filter_sql": (
            "game_contexts.park IN ('COL','ARI','BOS') "
            "AND game_contexts.season_game_number <= :n_games"
        ),
        "signal_logic": "Humidor effect takes weeks to manifest in market.",
        "min_sample_heuristic": 25,
        "ic_prior_estimate": 0.020,
        "variance_justification": "Season-phase x park interaction, not blanket park factor.",
        "exploration_status": "unexplored",
    },
    # ── Parlay / correlation ──────────────────────────────────
    {
        "seed_id": "mlb_sgp_leadoff_runs_team_total",
        "category": "parlay",
        "sport": "baseball_mlb",
        "market_type": "sgp",
        "thesis_template": (
            "SGP correlation: leadoff hitter runs-OVER × team-total-OVER. "
            "Book prices them ~independent; empirical joint prob is higher."
        ),
        "cohort_filter_sql": (
            "player_stats.lineup_slot = 1"
        ),
        "signal_logic": "Conditional on team scoring, leadoff scores at elevated rate.",
        "min_sample_heuristic": 60,
        "ic_prior_estimate": 0.035,
        "variance_justification": "Correlation-structure edge, not individual prop.",
        "exploration_status": "unexplored",
    },
    {
        "seed_id": "mlb_closing_line_vs_draftkings_edge",
        "category": "h2h",
        "sport": "baseball_mlb",
        "market_type": "h2h",
        "thesis_template": (
            "When DraftKings ML is 5%+ off the devigged consensus of Pinnacle + "
            "Circa at T-15min to first pitch, the consensus side wins vs DK "
            "implied at {pct}%+ above chance."
        ),
        "cohort_filter_sql": (
            "game_contexts.minutes_to_start <= 15"
        ),
        "signal_logic": "Sharp-book consensus is closer to truth than DK pricing.",
        "min_sample_heuristic": 100,
        "ic_prior_estimate": 0.020,
        "variance_justification": "Cross-book arbitrage-adjacent, book-specific.",
        "exploration_status": "unexplored",
    },
    # ── Live overreactions ────────────────────────────────────
    {
        "seed_id": "mlb_live_2out_rally_spread",
        "category": "live",
        "sport": "baseball_mlb",
        "market_type": "live_spreads",
        "thesis_template": (
            "After a 2-out, bases-loaded rally scoring ≥ 2 runs in innings 4-6, "
            "live spread moves ≥ 1.5. Fade side is underpriced — cluster luck "
            "mean-reverts."
        ),
        "cohort_filter_sql": (
            "game_contexts.inning BETWEEN 4 AND 6 "
            "AND game_contexts.two_out_loaded_run_event = 1 "
            "AND ABS(game_contexts.live_line_move) >= 1.5"
        ),
        "signal_logic": "Cluster luck in 2-out bases-loaded situations mean-reverts.",
        "min_sample_heuristic": 40,
        "ic_prior_estimate": 0.030,
        "variance_justification": "In-game overreaction, distinct trigger.",
        "exploration_status": "unexplored",
    },
    # ── Rule / ballpark era changes ───────────────────────────
    {
        "seed_id": "mlb_shift_ban_pull_hitter_hits_prop",
        "category": "props",
        "sport": "baseball_mlb",
        "market_type": "player_hits",
        "thesis_template": (
            "Since the shift ban, extreme-pull LHB (pull% > 55%) have hits-prop "
            "OVER edge not yet fully absorbed by pricing."
        ),
        "cohort_filter_sql": (
            "player_stats.bats = 'L' "
            "AND player_stats.pull_pct > 0.55 "
            "AND game_contexts.season >= 2023"
        ),
        "signal_logic": "Rule-change BABIP lift persists longer than book adjustment.",
        "min_sample_heuristic": 50,
        "ic_prior_estimate": 0.025,
        "variance_justification": "Rule-change era-specific.",
        "exploration_status": "unexplored",
    },
    {
        "seed_id": "mlb_pitch_clock_late_inning_prop",
        "category": "props",
        "sport": "baseball_mlb",
        "market_type": "player_strikeouts",
        "thesis_template": (
            "Relief pitchers facing 4+ batters (multi-inning) under pitch-clock "
            "regime have K/9 edge in innings 7-9 not fully priced in multi-out "
            "K props."
        ),
        "cohort_filter_sql": (
            "player_stats.role = 'RP' "
            "AND game_contexts.inning >= 7 "
            "AND game_contexts.season >= 2023"
        ),
        "signal_logic": "Pitch-clock rhythm favors RPs' repeatable deliveries.",
        "min_sample_heuristic": 40,
        "ic_prior_estimate": 0.022,
        "variance_justification": "Rule-regime x role x inning compound.",
        "exploration_status": "unexplored",
    },
    # ── Futures / derivative ──────────────────────────────────
    {
        "seed_id": "mlb_futures_pythag_div_win",
        "category": "futures",
        "sport": "baseball_mlb",
        "market_type": "division_winner",
        "thesis_template": (
            "At All-Star break, team with Pythag record ≥ 6 games better than "
            "actual record AND within 5 games of division lead: futures "
            "price has not priced regression in team's favor."
        ),
        "cohort_filter_sql": (
            "game_contexts.pythag_wins_minus_actual_wins >= 6 "
            "AND game_contexts.gb_from_division_lead <= 5 "
            "AND game_contexts.season_pct_complete >= 0.55"
        ),
        "signal_logic": "Run-differential-based projection beats W-L perception.",
        "min_sample_heuristic": 15,
        "ic_prior_estimate": 0.020,
        "variance_justification": "Futures market, Pythag-specific.",
        "exploration_status": "unexplored",
    },
    # ── Pitcher-specific micro edges ──────────────────────────
    {
        "seed_id": "mlb_starter_first_inning_scoreless",
        "category": "props",
        "sport": "baseball_mlb",
        "market_type": "first_inning_scoreless",
        "thesis_template": (
            "Starter with career 1st-inning ERA < 2.50 and 2+ MLB seasons: "
            "'Scoreless 1st' prop consistently underpriced."
        ),
        "cohort_filter_sql": (
            "player_stats.career_first_inning_era < 2.50 "
            "AND player_stats.seasons >= 2"
        ),
        "signal_logic": "Repeatable 1st-inning skill; books set to league baseline.",
        "min_sample_heuristic": 50,
        "ic_prior_estimate": 0.020,
        "variance_justification": "Micro-derivative market (1st inning), pitcher-specific.",
        "exploration_status": "unexplored",
    },
    {
        "seed_id": "mlb_pitcher_catcher_battery_framing",
        "category": "props",
        "sport": "baseball_mlb",
        "market_type": "player_strikeouts",
        "thesis_template": (
            "Starter paired with elite framer catcher (CSAA top-10) has "
            "K-prop edge not priced on per-start basis — book uses "
            "season-long pitcher K-rate, ignores battery effect."
        ),
        "cohort_filter_sql": (
            "game_contexts.catcher_csaa_rank <= 10 "
            "AND player_stats.position = 'SP'"
        ),
        "signal_logic": "Framing lifts CSW → lifts K-rate for the paired start.",
        "min_sample_heuristic": 30,
        "ic_prior_estimate": 0.022,
        "variance_justification": "Battery pairing — unique interaction.",
        "exploration_status": "unexplored",
    },
    {
        "seed_id": "mlb_manager_ipg_hook_tendency",
        "category": "totals",
        "sport": "baseball_mlb",
        "market_type": "totals_f7",
        "thesis_template": (
            "Managers with quick-hook tendency (avg starter exit < 5.0 IP "
            "on non-ace starters): F7 totals UNDER is underpriced vs full-game."
        ),
        "cohort_filter_sql": (
            "game_contexts.manager_avg_exit_ip < 5.0 "
            "AND player_stats.is_ace = 0"
        ),
        "signal_logic": "Quick-hook pattern is stable; F7 line set too high.",
        "min_sample_heuristic": 40,
        "ic_prior_estimate": 0.022,
        "variance_justification": "Manager tendency x derivative market.",
        "exploration_status": "unexplored",
    },
    # ── Day-of-week / slate effects ───────────────────────────
    {
        "seed_id": "mlb_sunday_day_game_travel_total",
        "category": "totals",
        "sport": "baseball_mlb",
        "market_type": "totals",
        "thesis_template": (
            "Sunday day-game preceded by Saturday night game with 2+ timezone "
            "shift away-team travel: team-total UNDER for the traveling club."
        ),
        "cohort_filter_sql": (
            "game_contexts.day_of_week = 'Sun' "
            "AND game_contexts.is_day_game = 1 "
            "AND ABS(game_contexts.prev_game_tz_shift) >= 2"
        ),
        "signal_logic": "Compound travel + day-game fatigue compresses offense.",
        "min_sample_heuristic": 30,
        "ic_prior_estimate": 0.025,
        "variance_justification": "Day-slot + travel interaction.",
        "exploration_status": "unexplored",
    },
    # ── Ported from feat/mlb-nhl-props-and-alts HYPOTHESIS_TEMPLATES ──
    # These 6 prop seeds originally landed in hypothesis_generator.py's
    # HYPOTHESIS_TEMPLATES list (the pre-wiki template-expansion path).
    # They are duplicated here so the wiki-grounded generator's
    # pick_unexplored_seeds() loop can surface them alongside the rest of
    # the 53-seed library. Both paths (template + wiki-grounded) remain
    # usable; the seeds are deliberately represented in both to preserve
    # semantics from both branches.
    {
        "seed_id": "mlb_pitcher_k_over_vs_low_k_team",
        "category": "props",
        "sport": "baseball_mlb",
        "market_type": "pitcher_strikeouts",
        "thesis_template": (
            "When a starting pitcher with above-median K/9 faces a team in the "
            "bottom quartile/tercile of team K-rate, books set the pitcher K "
            "Over too conservatively. Contact-team aggression becomes a "
            "weakness against high-K stuff; fair Over exceeds implied by 1.5%+."
        ),
        "cohort_filter_sql": (
            "player_stats.pitcher_k9_season >= player_stats.pitcher_k9_league_median "
            "AND game_contexts.opp_team_k_rate_rank >= 22"
        ),
        "signal_logic": "K9 vs team K-rate quartile mismatch -> K prop Over.",
        "min_sample_heuristic": 30,
        "ic_prior_estimate": 0.025,
        "variance_justification": (
            "Pitcher-K props gated on opponent team K-rate tier; distinct axis "
            "from park-factor and bullpen-handoff seeds."
        ),
        "exploration_status": "unexplored",
    },
    {
        "seed_id": "mlb_batter_hits_over_at_hitter_parks",
        "category": "props",
        "sport": "baseball_mlb",
        "market_type": "batter_hits",
        "thesis_template": (
            "Batters with rolling AVG in the top quartile/tercile at extreme "
            "hitter parks (Coors, Great American, Fenway) see hit-prop Overs "
            "set too low. Books apply park factor to totals but lag on "
            "individual-player prop lines; fair exceeds implied by 1.5%+."
        ),
        "cohort_filter_sql": (
            "player_stats.batter_avg_rolling_rank <= 25 "
            "AND game_contexts.park_hitter_factor_rank <= 5"
        ),
        "signal_logic": "Hot-bat + hitter-park stacking underpriced by book.",
        "min_sample_heuristic": 30,
        "ic_prior_estimate": 0.020,
        "variance_justification": (
            "Batter hit-prop cross of hot-streak and extreme-park factor; "
            "not covered by generic totals or HR prop seeds."
        ),
        "exploration_status": "unexplored",
    },
    {
        "seed_id": "mlb_nrfi_both_aces_first_inning",
        "category": "totals",
        "sport": "baseball_mlb",
        "market_type": "first_inning_nrfi_yrfi",
        "thesis_template": (
            "When both starting pitchers rank top quartile/tercile in first-"
            "inning wOBA-allowed, the NRFI (No Runs First Inning) line is "
            "mispriced. Books aggregate across starter quality; top-tier aces "
            "strike out the top of the order at elevated rates in frame 1. "
            "NRFI fair probability exceeds implied by 2%+."
        ),
        "cohort_filter_sql": (
            "player_stats.home_sp_first_inning_woba_rank <= 7 "
            "AND player_stats.away_sp_first_inning_woba_rank <= 7"
        ),
        "signal_logic": "Both SP top-tier 1st-inning wOBA -> NRFI Yes.",
        "min_sample_heuristic": 30,
        "ic_prior_estimate": 0.025,
        "variance_justification": (
            "NRFI derivative gated on BOTH starters' 1st-inning splits; "
            "distinct from starter-first-scoreless and opener-bullpen seeds."
        ),
        "exploration_status": "unexplored",
    },
]

# Validated at package import time by tools/thesis/_schema._validate_library().
