"""
Curated thesis-space seed library for Callisto's hypothesis generator.

The autonomous generator was producing junk at a 91% rejection rate — largely
because it kept re-discovering the same shallow theses ("rest advantage",
"back-to-back unders", "weather totals") across every cycle. This file seeds
~50 deliberately *underexplored* thesis spaces, each expressed as a template
that parameterizes over live DB data rather than inventing facts.

Seed schema
-----------
Each seed is a dict:

    {
      "seed_id":            str   # globally unique (e.g. "mlb_umpire_zone_totals")
      "category":           str   # market family: props | totals | spreads | h2h | live | parlay
      "sport":              str   # canonical sport key (baseball_mlb, etc.)
      "market_type":        str   # line-level market (totals, player_strikeouts, ...)
      "thesis_template":    str   # human-readable hypothesis statement, may include {vars}
      "cohort_filter_sql":  str   # SQL WHERE-fragment over game_contexts/player_stats that
                                  # defines which events belong to the cohort. Must be
                                  # specific enough to produce a testable subset.
      "signal_logic":       str   # brief description of the expected market signal
      "min_sample_heuristic": int # rough minimum cohort size needed for an IC estimate
      "ic_prior_estimate":  float # weakly-informed prior on edge magnitude (absolute,
                                  # units of probability, e.g. 0.03 = 3 pp)
      "variance_justification": str  # why this edge is *not* a duplicate of others
      "exploration_status": str   # "unexplored" | "partial" | "exhausted" (runtime updated)
    }

A seed produces at most ONE hypothesis per invocation — the generator will
use a seed as a scaffold, then ask the LLM to specialize it into a concrete
testable hypothesis (concrete umpire, concrete park, concrete lineup
configuration) using DB-observed facts.

The seeds deliberately cover axes the existing template library ignores:
  - Official/referee-specific effects (MLB umpires, NBA refs, NHL officials)
  - Micro-schedule effects (bullpen handoff innings, travel+altitude combos)
  - Identity / cohesion factors in thin women's markets
  - Live / in-game markets (overreaction after leverage swings)
  - Parlay correlation structure (stacks the book doesn't model)
  - Prop/derivative markets that book with sparse data
"""

from __future__ import annotations

from typing import Any, Iterable, Optional


# ───────────────────────────────────────────────────────────────
# SEED LIBRARY
# ───────────────────────────────────────────────────────────────
# Each entry is validated by ``validate_seed()`` at import time.

THESIS_SEEDS: list[dict[str, Any]] = [
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


# ───────────────────────────────────────────────────────────────
# SCHEMA / VALIDATION
# ───────────────────────────────────────────────────────────────

REQUIRED_SEED_KEYS = {
    "seed_id",
    "category",
    "sport",
    "market_type",
    "thesis_template",
    "cohort_filter_sql",
    "signal_logic",
    "min_sample_heuristic",
    "ic_prior_estimate",
    "variance_justification",
    "exploration_status",
}

VALID_CATEGORIES = {
    "props", "totals", "spreads", "h2h", "live", "parlay", "futures",
}

VALID_EXPLORATION = {"unexplored", "partial", "exhausted"}


def validate_seed(seed: dict) -> list[str]:
    """Return a list of validation errors. Empty list = valid."""
    errs: list[str] = []
    if not isinstance(seed, dict):
        return [f"seed is not a dict: {type(seed)}"]
    missing = REQUIRED_SEED_KEYS - set(seed.keys())
    if missing:
        errs.append(f"missing keys: {sorted(missing)}")
    if seed.get("category") not in VALID_CATEGORIES:
        errs.append(f"invalid category: {seed.get('category')}")
    if seed.get("exploration_status") not in VALID_EXPLORATION:
        errs.append(f"invalid exploration_status: {seed.get('exploration_status')}")
    msh = seed.get("min_sample_heuristic")
    if not isinstance(msh, int) or msh <= 0:
        errs.append(f"min_sample_heuristic must be positive int: {msh!r}")
    ic = seed.get("ic_prior_estimate")
    if not isinstance(ic, (int, float)) or not (0.0 <= float(ic) <= 0.5):
        errs.append(f"ic_prior_estimate must be in [0, 0.5]: {ic!r}")
    for k in ("thesis_template", "cohort_filter_sql", "signal_logic",
              "variance_justification", "sport", "market_type"):
        v = seed.get(k, "")
        if not isinstance(v, str) or not v.strip():
            errs.append(f"{k} must be a non-empty string")
    return errs


def _validate_library() -> None:
    """Called at import time — fail fast on malformed seeds."""
    seen: set[str] = set()
    for s in THESIS_SEEDS:
        errs = validate_seed(s)
        if errs:
            raise ValueError(
                f"Invalid thesis seed {s.get('seed_id', '<missing>')}: {errs}"
            )
        if s["seed_id"] in seen:
            raise ValueError(f"Duplicate seed_id: {s['seed_id']}")
        seen.add(s["seed_id"])


_validate_library()


# ───────────────────────────────────────────────────────────────
# RUNTIME QUERIES
# ───────────────────────────────────────────────────────────────

def list_seeds(
    sport: Optional[str] = None,
    category: Optional[str] = None,
    exploration_status: Optional[str] = None,
) -> list[dict]:
    """Filtered view of the seed library."""
    out = list(THESIS_SEEDS)
    if sport:
        out = [s for s in out if s["sport"] == sport]
    if category:
        out = [s for s in out if s["category"] == category]
    if exploration_status:
        out = [s for s in out if s["exploration_status"] == exploration_status]
    return out


def get_seed(seed_id: str) -> Optional[dict]:
    for s in THESIS_SEEDS:
        if s["seed_id"] == seed_id:
            return s
    return None


def seed_category_coverage() -> dict[str, int]:
    """Map category → count for dashboarding."""
    counts: dict[str, int] = {}
    for s in THESIS_SEEDS:
        counts[s["category"]] = counts.get(s["category"], 0) + 1
    return counts


def seed_sport_coverage() -> dict[str, int]:
    counts: dict[str, int] = {}
    for s in THESIS_SEEDS:
        counts[s["sport"]] = counts.get(s["sport"], 0) + 1
    return counts


def pick_unexplored_seeds(
    existing_hypothesis_names: Iterable[str],
    existing_thesis_statements: Iterable[str] = (),
    sport: Optional[str] = None,
    max_seeds: int = 5,
) -> list[dict]:
    """Return up to ``max_seeds`` seeds whose ``seed_id`` does NOT appear
    in any existing hypothesis name or notes — a cheap keyword filter.

    Semantic near-dup check happens later in the generator; this is the
    coarse-grain pass so the LLM doesn't get asked to re-specialize a seed
    that's already been exhausted.
    """
    existing_names_l = [n.lower() for n in existing_hypothesis_names]
    existing_theses_l = [t.lower() for t in existing_thesis_statements]
    pool = list_seeds(sport=sport)
    picked: list[dict] = []
    for s in pool:
        if len(picked) >= max_seeds:
            break
        sid_l = s["seed_id"].lower()
        if any(sid_l in n for n in existing_names_l):
            continue
        # Cheap keyword overlap: skip if a distinctive seed-id token shows
        # up in an existing thesis body verbatim.
        distinctive = [tok for tok in sid_l.split("_") if len(tok) > 4]
        if distinctive and any(
            all(tok in t for tok in distinctive) for t in existing_theses_l
        ):
            continue
        picked.append(s)
    return picked
