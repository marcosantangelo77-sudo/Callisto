"""
Autonomous hypothesis generator — turns embedded data into testable betting theses.

This is Callisto's creative engine. It:
  1. Analyzes clusters of similar game/prop contexts from the vector store
  2. Detects statistical anomalies within clusters (hit rates, edge persistence)
  3. Generates testable hypotheses with specific model configs
  4. Creates them as drafts in the HypothesisManager for backtesting

Hypothesis templates encode domain knowledge about WHERE edges exist:
  - Props: situational mispricing (rest, pace, matchup, minutes changes)
  - Lines: key number value, stale line detection, reverse movement
  - Boosts: structural +EV from operator promotions

The local models (Architect/Manager) drive this autonomously. Claude Code
escalation handles the heavy statistical analysis when needed.
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiosqlite
from dotenv import load_dotenv

from tools.embeddings import VectorStore, embed_text, cosine_similarity
from tools.hypothesis import HypothesisManager

load_dotenv()

logger = logging.getLogger("callisto.hypothesis_generator")

DB_PATH = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")


# ──────────────────────────────────────────────────
# HYPOTHESIS TEMPLATES
# ──────────────────────────────────────────────────
# Each template defines a class of edge to test.
# The generator fills in sport-specific and context-specific parameters.

HYPOTHESIS_TEMPLATES = [
    {
        "id": "rest_advantage_props",
        "name": "Rest advantage {prop_type} mispricing",
        "thesis": (
            "Players on {rest_days}+ days rest have {prop_type} lines set too low "
            "by books that don't fully account for rest effects on {stat_category}. "
            "Fair probability of Over is higher than book implied by {min_edge}%+."
        ),
        "sport_filter": ["basketball_nba", "basketball_ncaab"],
        "market_type": "player_{prop_type}",
        "model_config": {
            "type": "consensus_devig",
            "devig_method": "power",
            "target_book": "draftkings",
            "consensus_min_books": 3,
            "context_factors": ["rest_days"],
        },
        "variables": {
            "rest_days": [2, 3, 4],
            "prop_type": ["points", "rebounds", "assists", "threes"],
            "stat_category": ["scoring", "rebounding", "passing", "three-point shooting"],
            "min_edge": [1, 2, 3],
        },
    },
    {
        "id": "back_to_back_unders",
        "name": "Back-to-back {prop_type} unders",
        "thesis": (
            "Players on the second night of a back-to-back have reduced {stat_category} "
            "output. Books adjust lines but not enough — Under is +EV at {min_edge}%+."
        ),
        "sport_filter": ["basketball_nba"],
        "market_type": "player_{prop_type}",
        "model_config": {
            "type": "consensus_devig",
            "devig_method": "power",
            "target_book": "draftkings",
            "consensus_min_books": 3,
            "context_factors": ["back_to_back"],
            "side_filter": "Under",
        },
        "variables": {
            "prop_type": ["points", "rebounds", "assists", "points_rebounds_assists"],
            "stat_category": ["scoring", "rebounding", "passing", "combined stats"],
            "min_edge": [1, 2, 3],
        },
    },
    {
        "id": "pace_mismatch_overs",
        "name": "Pace mismatch {prop_type} overs",
        "thesis": (
            "When a slow-pace team faces a fast-pace team, books underestimate the "
            "pace-up effect on player {stat_category}. {prop_type} Overs are +EV "
            "when pace differential exceeds {pace_diff} possessions."
        ),
        "sport_filter": ["basketball_nba"],
        "market_type": "player_{prop_type}",
        "model_config": {
            "type": "consensus_devig",
            "devig_method": "power",
            "target_book": "draftkings",
            "consensus_min_books": 3,
            "context_factors": ["pace_differential"],
            "side_filter": "Over",
        },
        "variables": {
            "prop_type": ["points", "assists", "points_rebounds_assists"],
            "stat_category": ["scoring", "passing", "combined stats"],
            "pace_diff": [4, 6, 8],
            "min_edge": [1, 2, 3],
        },
    },
    {
        "id": "injury_role_boost",
        "name": "Teammate injury {prop_type} boost",
        "thesis": (
            "When a team's top {role} is injured, the backup/next-man-up sees increased "
            "{stat_category}. Books are slow to adjust {prop_type} lines upward, "
            "creating Over edges of {min_edge}%+."
        ),
        "sport_filter": ["basketball_nba", "basketball_ncaab"],
        "market_type": "player_{prop_type}",
        "model_config": {
            "type": "consensus_devig",
            "devig_method": "power",
            "target_book": "draftkings",
            "consensus_min_books": 2,
            "context_factors": ["teammate_injury"],
            "side_filter": "Over",
        },
        "variables": {
            "prop_type": ["points", "rebounds", "assists", "threes"],
            "stat_category": ["scoring", "rebounding", "passing", "three-point shooting"],
            "role": ["scorer", "rebounder", "playmaker"],
            "min_edge": [1.5, 3],
        },
    },
    {
        "id": "home_underdog_spread",
        "name": "Home underdog spread value",
        "thesis": (
            "Home underdogs of {spread_range} points receive insufficient home-court "
            "adjustment from books. ATS win rate exceeds implied probability by {min_edge}%+."
        ),
        "sport_filter": ["basketball_nba", "basketball_ncaab"],
        "market_type": "spreads",
        "model_config": {
            "type": "consensus_devig",
            "devig_method": "power",
            "target_book": "draftkings",
            "consensus_min_books": 3,
            "context_factors": ["home_underdog"],
        },
        "variables": {
            "spread_range": ["1-4", "4-7", "7-10"],
            "min_edge": [0.5, 1, 1.5],
        },
    },
    {
        "id": "total_weather_impact",
        "name": "Weather impact on {sport} totals",
        "thesis": (
            "Games played in {weather_condition} conditions see reduced scoring. "
            "Books don't fully adjust totals for weather — Under is +EV when "
            "{weather_metric} exceeds {threshold}."
        ),
        "sport_filter": ["americanfootball_nfl", "baseball_mlb"],
        "market_type": "totals",
        "model_config": {
            "type": "consensus_devig",
            "devig_method": "power",
            "target_book": "draftkings",
            "consensus_min_books": 3,
            "context_factors": ["weather"],
            "side_filter": "Under",
        },
        "variables": {
            "sport": ["NFL", "MLB"],
            "weather_condition": ["high wind", "heavy rain", "extreme cold"],
            "weather_metric": ["wind_mph", "precipitation_mm", "temp_f"],
            "threshold": [15, 5, 32],
            "min_edge": [0.5, 1, 1.5],
        },
    },
    {
        "id": "golf_course_horse",
        "name": "Course horse {finish_type} mispricing at {tournament}",
        "thesis": (
            "Players with {min_top_finishes}+ top-{finish_rank} finishes at {tournament} "
            "in the last {lookback_years} years have {finish_type} lines set too long. "
            "Course-specific institutional knowledge compounds at venues played annually "
            "(especially Augusta). Fair probability of {finish_type} exceeds book implied "
            "by {min_edge}%+."
        ),
        "sport_filter": ["golf_pga"],
        "market_type": "{finish_type}",
        "model_config": {
            "type": "consensus_devig",
            "devig_method": "multiplicative",
            "target_book": "draftkings",
            "consensus_min_books": 3,
            "context_factors": ["course_history", "recent_form"],
        },
        "variables": {
            "tournament": ["Masters", "US_Open", "Open_Championship", "PGA_Championship"],
            "finish_type": ["tournament_winner", "top_5_finish", "top_10_finish"],
            "finish_rank": [5, 10, 20],
            "min_top_finishes": [2, 3],
            "lookback_years": [5, 10],
            "min_edge": [1, 2, 3],
        },
    },
    {
        "id": "golf_age_discount",
        "name": "Age discount on {finish_type} for veterans at {tournament}",
        "thesis": (
            "Players aged {min_age}+ with strong course history are over-discounted "
            "by books due to age bias. At {tournament}, course knowledge degrades slower "
            "than raw athleticism — especially at Augusta where putting from memory and "
            "shot-shaping matter more than distance. {finish_type} odds are too long."
        ),
        "sport_filter": ["golf_pga"],
        "market_type": "{finish_type}",
        "model_config": {
            "type": "consensus_devig",
            "devig_method": "multiplicative",
            "target_book": "draftkings",
            "consensus_min_books": 3,
            "context_factors": ["player_age", "course_history", "sg_approach"],
        },
        "variables": {
            "tournament": ["Masters", "Open_Championship"],
            "finish_type": ["tournament_winner", "top_5_finish", "top_10_finish", "top_20_finish"],
            "min_age": [40, 43, 45],
            "min_edge": [1, 2, 3],
        },
    },
    {
        "id": "golf_recent_form_lag",
        "name": "Recent winner {finish_type} odds lag at majors",
        "thesis": (
            "Players who won a PGA Tour event within {weeks_since_win} weeks before a major "
            "have {finish_type} odds that don't fully reflect the form spike. Books adjust "
            "slowly for recency — the confidence and momentum carry forward. "
            "Fair probability exceeds book implied by {min_edge}%+."
        ),
        "sport_filter": ["golf_pga"],
        "market_type": "{finish_type}",
        "model_config": {
            "type": "consensus_devig",
            "devig_method": "multiplicative",
            "target_book": "draftkings",
            "consensus_min_books": 3,
            "context_factors": ["recent_win", "sg_total"],
        },
        "variables": {
            "finish_type": ["tournament_winner", "top_5_finish", "top_10_finish"],
            "weeks_since_win": [2, 4, 6, 8],
            "min_edge": [1, 2, 3],
        },
    },
    {
        "id": "golf_sg_approach_mispricing",
        "name": "SG:Approach elite players underpriced at approach-dominant courses",
        "thesis": (
            "Players ranked top-{sg_rank} in Strokes Gained: Approach over the last "
            "{lookback_events} events are underpriced at courses where approach play "
            "is the dominant success factor (Augusta, Pebble Beach, Muirfield Village). "
            "SG:Approach correlates most strongly with major wins — books weight "
            "overall rank too heavily vs. skill-specific fit."
        ),
        "sport_filter": ["golf_pga"],
        "market_type": "{finish_type}",
        "model_config": {
            "type": "consensus_devig",
            "devig_method": "multiplicative",
            "target_book": "draftkings",
            "consensus_min_books": 3,
            "context_factors": ["sg_approach_rank", "course_sg_correlation"],
        },
        "variables": {
            "finish_type": ["tournament_winner", "top_5_finish", "top_10_finish"],
            "sg_rank": [5, 10, 15],
            "lookback_events": [5, 10, 16],
            "min_edge": [1, 2, 3],
        },
    },
    {
        "id": "golf_first_round_leader",
        "name": "First-round leader tendency mispricing",
        "thesis": (
            "Players who have led after Round 1 at a specific venue {min_times}+ times "
            "in the last {lookback_years} years have first-round leader / top-5 R1 odds "
            "set too long. Early-round course comfort is a repeatable skill, not randomness. "
            "Fair probability exceeds book implied by {min_edge}%+."
        ),
        "sport_filter": ["golf_pga"],
        "market_type": "first_round_leader",
        "model_config": {
            "type": "consensus_devig",
            "devig_method": "multiplicative",
            "target_book": "draftkings",
            "consensus_min_books": 2,
            "context_factors": ["r1_history", "course_familiarity"],
        },
        "variables": {
            "min_times": [2, 3],
            "lookback_years": [5, 10],
            "min_edge": [2, 3, 5],
        },
    },
    {
        "id": "golf_weather_round_scoring",
        "name": "Weather impact on tournament round scoring",
        "thesis": (
            "When {weather_condition} conditions are forecast for a tournament round, "
            "books underadjust round scoring props and matchup odds. Players with "
            "experience in adverse conditions gain a relative edge. "
            "Affected markets are mispriced by {min_edge}%+."
        ),
        "sport_filter": ["golf_pga"],
        "market_type": "round_score",
        "model_config": {
            "type": "consensus_devig",
            "devig_method": "multiplicative",
            "target_book": "draftkings",
            "consensus_min_books": 3,
            "context_factors": ["weather_forecast", "player_weather_history"],
        },
        "variables": {
            "weather_condition": ["high wind (15+ mph)", "rain", "cold (<55F)"],
            "min_edge": [1, 2, 3],
        },
    },
    # ── MLB-specific templates ──
    {
        "id": "mlb_pitcher_prop_rest",
        "name": "Starting pitcher {prop_type} on {rest_days}+ days rest",
        "thesis": (
            "Starting pitchers on {rest_days}+ days rest have {prop_type} lines "
            "that don't fully account for the rest advantage. Extended rest improves "
            "velocity retention, spin rate, and command through later innings. "
            "Books set Over strikeout / Under earned run lines too conservatively. "
            "Fair probability of the favorable side exceeds book implied by {min_edge}%+."
        ),
        "sport_filter": ["baseball_mlb"],
        "market_type": "player_{prop_type}",
        "model_config": {
            "type": "consensus_devig",
            "devig_method": "power",
            "target_book": "draftkings",
            "consensus_min_books": 3,
            "context_factors": ["pitcher_rest_days", "pitch_count_recent"],
        },
        "variables": {
            "prop_type": ["strikeouts", "earned_runs", "hits_allowed", "outs_recorded"],
            "rest_days": [5, 6, 7],
            "min_edge": [1, 2, 3],
        },
    },
    {
        "id": "mlb_opening_week_totals",
        "name": "MLB opening week {weather_factor} total mispricing",
        "thesis": (
            "Early-season MLB games (first 2 weeks) in {weather_factor} conditions "
            "see inflated or deflated run totals that books don't fully adjust for. "
            "Pitchers are not fully stretched, bullpens are fresh, cold-weather parks "
            "suppress offense. Under is +EV when {weather_factor} is present."
        ),
        "sport_filter": ["baseball_mlb"],
        "market_type": "totals",
        "model_config": {
            "type": "consensus_devig",
            "devig_method": "power",
            "target_book": "draftkings",
            "consensus_min_books": 3,
            "context_factors": ["season_week", "weather", "park_factor"],
        },
        "variables": {
            "weather_factor": ["cold (<55F)", "wind (15+ mph)", "rain/drizzle"],
            "min_edge": [1, 1.5, 2],
        },
    },
    {
        "id": "mlb_schedule_spot",
        "name": "MLB schedule spot {spot_type} spread value",
        "thesis": (
            "Teams in {spot_type} schedule situations show ATS performance that "
            "diverges from book implied probability. Books underweight travel fatigue, "
            "timezone shifts, and letdown/lookahead dynamics in MLB where the 162-game "
            "schedule creates persistent schedule spot edges. ATS win rate exceeds "
            "implied by {min_edge}%+."
        ),
        "sport_filter": ["baseball_mlb"],
        "market_type": "spreads",
        "model_config": {
            "type": "consensus_devig",
            "devig_method": "power",
            "target_book": "draftkings",
            "consensus_min_books": 3,
            "context_factors": ["schedule_spot", "travel_distance", "timezone_shift"],
        },
        "variables": {
            "spot_type": [
                "3+ game road trip finale", "home after 7+ road games",
                "day game after night game", "cross-country travel (3+ timezone shift)",
            ],
            "min_edge": [1, 1.5, 2],
        },
    },
    {
        "id": "mlb_park_factor_totals",
        "name": "MLB park factor mispricing on totals at {park_type} parks",
        "thesis": (
            "Games at {park_type} parks have totals that don't fully reflect "
            "park-specific run environment. Books adjust but lag behind the "
            "true park factor, especially early season when lines are calibrated "
            "to league-wide trends. Fair total probability diverges by {min_edge}%+."
        ),
        "sport_filter": ["baseball_mlb"],
        "market_type": "totals",
        "model_config": {
            "type": "consensus_devig",
            "devig_method": "power",
            "target_book": "draftkings",
            "consensus_min_books": 3,
            "context_factors": ["park_factor", "altitude", "dimensions"],
        },
        "variables": {
            "park_type": ["extreme hitter (Coors, Great American)", "extreme pitcher (Oracle, Petco)", "bandbox (Fenway, Yankee)"],
            "min_edge": [1, 1.5, 2],
        },
    },
    # ── NCAAW/WNBA identity/cohesion templates ──
    {
        "id": "ncaaw_cohesion_spread",
        "name": "NCAAW {cohesion_factor} cohesion spread advantage",
        "thesis": (
            "Teams with strong {cohesion_factor} cohesion outperform their spread "
            "implied probability. Thin NCAAW markets don't price intangible cohesion "
            "factors — regional identity, institutional values, coaching tenure, and "
            "roster stability create systematic edges. ATS win rate exceeds book "
            "implied by {min_edge}%+."
        ),
        "sport_filter": ["basketball_ncaaw"],
        "market_type": "spreads",
        "model_config": {
            "type": "consensus_devig",
            "devig_method": "power",
            "target_book": "draftkings",
            "consensus_min_books": 2,
            "context_factors": ["team_cohesion", "coaching_tenure", "roster_stability"],
        },
        "variables": {
            "cohesion_factor": ["regional identity", "coaching stability (10+ years)", "roster continuity (low transfer portal)", "institutional values alignment"],
            "min_edge": [1.5, 2, 3],
        },
    },
    {
        "id": "wnba_demographic_totals",
        "name": "WNBA {factor} demographic composition total mispricing",
        "thesis": (
            "WNBA teams with {factor} demographic composition have game totals "
            "that diverge from book expectations. Social cohesion drives pace, "
            "defensive intensity, and chemistry in ways that thin WNBA markets "
            "don't price. Fair total probability exceeds implied by {min_edge}%+."
        ),
        "sport_filter": ["basketball_wnba"],
        "market_type": "totals",
        "model_config": {
            "type": "consensus_devig",
            "devig_method": "power",
            "target_book": "draftkings",
            "consensus_min_books": 2,
            "context_factors": ["demographic_composition", "team_cohesion", "pace"],
        },
        "variables": {
            "factor": ["high regional identity", "strong institutional alignment", "veteran-heavy roster"],
            "min_edge": [1.5, 2, 3],
        },
    },
    # ── Prop-market seed hypotheses (MLB / NBA / NHL) ──
    # Template roots for the prop-market expansion. The generator
    # instantiates them on game-day once the live slate is known.
    {
        "id": "mlb_pitcher_k_over_when_facing_low_k_team",
        "name": "MLB pitcher {prop_type} Over vs low-K opponents",
        "thesis": (
            "When a starting pitcher with above-median K/9 faces a team in the "
            "bottom {opp_k_tier} of team K-rate, books set the pitcher K Over "
            "too conservatively. The strike-zone mismatch compounds — a contact "
            "team's aggressive approach becomes a weakness against high-K stuff. "
            "Fair probability of Over exceeds book implied by {min_edge}%+."
        ),
        "sport_filter": ["baseball_mlb"],
        "market_type": "pitcher_strikeouts",
        "model_config": {
            "type": "prop_fair_value+consensus",
            "fair_value_fn": "project_mlb_pitcher_strikeouts",
            "devig_method": "power",
            "target_book": "draftkings",
            "consensus_min_books": 2,
            "context_factors": ["pitcher_k9", "opponent_team_k_rate", "park_factor"],
            "side_filter": "Over",
        },
        "variables": {
            "prop_type": ["strikeouts"],
            "opp_k_tier": ["quartile", "tercile"],
            "min_edge": [1.5, 2, 3],
        },
    },
    {
        "id": "mlb_batter_hits_over_at_hitter_parks",
        "name": "MLB batter {prop_type} Over at extreme hitter parks",
        "thesis": (
            "Batters with rolling AVG in the top {batter_tier} at extreme "
            "hitter parks (Coors, Great American, Fenway) see hit-prop Overs "
            "set too low. Books apply park factor to totals but lag on "
            "individual-player prop lines. Fair exceeds implied by {min_edge}%+."
        ),
        "sport_filter": ["baseball_mlb"],
        "market_type": "batter_hits",
        "model_config": {
            "type": "prop_fair_value+consensus",
            "fair_value_fn": "project_mlb_batter_hits",
            "devig_method": "power",
            "target_book": "draftkings",
            "consensus_min_books": 2,
            "context_factors": ["batter_avg", "park_factor", "opp_sp_quality"],
            "side_filter": "Over",
        },
        "variables": {
            "prop_type": ["hits", "total_bases"],
            "batter_tier": ["quartile", "tercile"],
            "min_edge": [1.5, 2, 3],
        },
    },
    {
        "id": "nba_player_pts_over_when_opp_plays_small_ball",
        "name": "NBA {prop_type} Over vs small-ball opponents",
        "thesis": (
            "Primary-scoring guards and wings see point-prop Overs mispriced "
            "when the opponent plays small-ball (no rim protector / 5-out). "
            "Books adjust totals for pace but not player-level; the star "
            "absorbs the extra possessions at elevated rates. Over is +EV "
            "by {min_edge}%+ when opp rim-protection-rank is bottom 10."
        ),
        "sport_filter": ["basketball_nba"],
        "market_type": "player_{prop_type}",
        "model_config": {
            "type": "consensus_devig",
            "devig_method": "power",
            "target_book": "draftkings",
            "consensus_min_books": 3,
            "context_factors": ["opp_rim_protection", "opp_lineup_size", "pace_differential"],
            "side_filter": "Over",
        },
        "variables": {
            "prop_type": ["points", "points_rebounds_assists"],
            "min_edge": [1.5, 2, 3],
        },
    },
    {
        "id": "nhl_goalie_saves_over_when_playing_b2b_road_team",
        "name": "NHL goalie {prop_type} Over vs B2B road opponents",
        "thesis": (
            "Goalies facing a team on the second night of a back-to-back with "
            "travel see elevated shot volume. Tired skaters take more low-"
            "percentage perimeter shots that inflate Saves without changing "
            "Goals Against much. Over Saves is +EV by {min_edge}%+."
        ),
        "sport_filter": ["icehockey_nhl"],
        "market_type": "goalie_saves",
        "model_config": {
            "type": "prop_fair_value+consensus",
            "fair_value_fn": "project_nhl_skater_shots_on_goal",
            "devig_method": "power",
            "target_book": "draftkings",
            "consensus_min_books": 2,
            "context_factors": ["opp_back_to_back", "opp_travel_km", "opp_shot_rate"],
            "side_filter": "Over",
        },
        "variables": {
            "prop_type": ["saves"],
            "min_edge": [1.5, 2, 3],
        },
    },
    {
        "id": "nhl_skater_sog_over_when_trailing_last_game",
        "name": "NHL {prop_type} Over after a trailing-third-period game",
        "thesis": (
            "Volume shooters who logged 3rd-period comeback minutes in the "
            "previous game see elevated SOG in the next outing — line combos "
            "stabilize and shot rate reverts high. Books set SOG Over using "
            "season averages rather than momentum-adjusted rate. Over is "
            "+EV by {min_edge}%+."
        ),
        "sport_filter": ["icehockey_nhl"],
        "market_type": "skater_shots_on_goal",
        "model_config": {
            "type": "prop_fair_value+consensus",
            "fair_value_fn": "project_nhl_skater_shots_on_goal",
            "devig_method": "power",
            "target_book": "draftkings",
            "consensus_min_books": 2,
            "context_factors": ["prev_game_trailing_toi", "shot_volume_rank"],
            "side_filter": "Over",
        },
        "variables": {
            "prop_type": ["shots_on_goal"],
            "min_edge": [1.5, 2, 3],
        },
    },
    {
        "id": "mlb_first_inning_nrfi_sharp_starter",
        "name": "MLB NRFI when both starters are top-tier",
        "thesis": (
            "When both starting pitchers rank top {sp_tier} in first-inning "
            "wOBA-allowed, the NRFI (No Runs First Inning) line is mispriced. "
            "Books aggregate across starter quality; top-tier aces strike out "
            "the top of the order at elevated rates in the first frame. "
            "NRFI fair probability exceeds implied by {min_edge}%+."
        ),
        "sport_filter": ["baseball_mlb"],
        "market_type": "first_inning_nrfi_yrfi",
        "model_config": {
            "type": "consensus_devig",
            "devig_method": "power",
            "target_book": "draftkings",
            "consensus_min_books": 2,
            "context_factors": ["home_sp_first_inning_woba", "away_sp_first_inning_woba"],
            "side_filter": "NRFI",
        },
        "variables": {
            "sp_tier": ["quartile", "tercile"],
            "min_edge": [2, 3, 4],
        },
    },
    {
        "id": "consensus_divergence",
        "name": "Cross-book consensus divergence on {market_type}",
        "thesis": (
            "When the devigged consensus fair probability from {min_books}+ books "
            "diverges from the target book's implied by {min_edge}%+, the consensus "
            "is correct more often than the target book. This is the core model."
        ),
        "sport_filter": ["basketball_nba", "basketball_ncaab", "americanfootball_nfl",
                         "icehockey_nhl", "baseball_mlb", "golf_pga"],
        "market_type": "{market_type}",
        "model_config": {
            "type": "consensus_devig",
            "devig_method": "power",
            "target_book": "draftkings",
            "consensus_min_books": "{min_books}",
        },
        "variables": {
            "market_type": ["spreads", "totals", "h2h",
                           "player_points", "player_rebounds", "player_assists",
                           "pitcher_strikeouts", "batter_hits", "batter_total_bases",
                           "skater_shots_on_goal", "goalie_saves"],
            "min_books": [3, 4, 5],
            "min_edge": [0.5, 1, 2],
        },
    },
]


class HypothesisGenerator:
    """Generates testable hypotheses from data patterns and templates."""

    def __init__(
        self,
        hypothesis_manager: HypothesisManager,
        vector_store: VectorStore,
        db_path: str = DB_PATH,
    ):
        self.hypothesis_manager = hypothesis_manager
        self.vector_store = vector_store
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def initialize(self) -> None:
        self._db = await aiosqlite.connect(self.db_path)
        await self._db.execute("PRAGMA busy_timeout = 60000")
        logger.info("Hypothesis generator initialized")

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    async def generate_from_templates(
        self,
        sport: str,
        max_hypotheses: int = 50,
        training_cutoff_date: Optional[str] = None,
    ) -> list[dict]:
        """
        Generate hypotheses from templates for a given sport.
        Expands variable combinations and creates draft hypotheses.
        Skips combinations that already exist.

        Args:
            sport: Sport key (e.g., "basketball_nba")
            max_hypotheses: Max hypotheses to create this call
            training_cutoff_date: ISO date string (YYYY-MM-DD). Data up to this
                date is the training set; backtests will use data after this date.
                Defaults to 30 days before today.

        Returns list of created hypothesis summaries.
        """
        existing_names = await self.hypothesis_manager.get_all_names()

        # Compute temporal metadata
        today = datetime.now(timezone.utc).date()
        if training_cutoff_date:
            try:
                cutoff = datetime.strptime(training_cutoff_date, "%Y-%m-%d").date()
            except ValueError:
                cutoff = today - timedelta(days=30)
        else:
            cutoff = today - timedelta(days=30)

        training_period_start = "2023-01-01"
        training_period_end = str(cutoff)
        forward_test_start = str(cutoff + timedelta(days=1))

        created = []

        for template in HYPOTHESIS_TEMPLATES:
            if sport not in template["sport_filter"]:
                continue

            # Player prop templates now supported — prop_snapshots provides
            # multi-book data and BacktestEngine._process_prop_snapshots handles devig.

            # Generate all variable combinations
            combos = self._expand_variables(template["variables"])

            for combo in combos:
                if len(created) >= max_hypotheses:
                    break

                # Fill template
                name = template["name"].format(**combo)
                if name in existing_names:
                    continue

                thesis = template["thesis"].format(**combo)
                market_type = template["market_type"].format(**combo)
                edge_threshold = combo.get("min_edge", 2) / 100.0

                # Build model config with temporal metadata
                model_config = {}
                for k, v in template["model_config"].items():
                    if isinstance(v, str) and "{" in v:
                        model_config[k] = v.format(**combo)
                    else:
                        model_config[k] = v

                # Convert string numbers to int
                if "consensus_min_books" in model_config:
                    try:
                        model_config["consensus_min_books"] = int(
                            model_config["consensus_min_books"]
                        )
                    except (ValueError, TypeError):
                        pass

                # Attach temporal isolation metadata
                model_config["training_period_start"] = training_period_start
                model_config["training_period_end"] = training_period_end
                model_config["forward_test_start"] = forward_test_start

                try:
                    hid = await self.hypothesis_manager.create_hypothesis(
                        name=name,
                        thesis=thesis,
                        sport=sport,
                        market_type=market_type,
                        model_config=model_config,
                        edge_threshold=edge_threshold,
                        notes=(
                            f"Auto-generated from template '{template['id']}'. "
                            f"Train: [{training_period_start}..{training_period_end}], "
                            f"forward-test from {forward_test_start}."
                        ),
                    )
                    created.append({
                        "hypothesis_id": hid,
                        "name": name,
                        "template": template["id"],
                        "variables": combo,
                        "training_period_end": training_period_end,
                        "forward_test_start": forward_test_start,
                    })
                    existing_names.add(name)
                except Exception as e:
                    logger.warning(f"Failed to create hypothesis '{name}': {e}")

        logger.info(
            f"Generated {len(created)} hypotheses for {sport} "
            f"from {len(HYPOTHESIS_TEMPLATES)} templates "
            f"(training cutoff: {training_period_end})"
        )
        return created

    async def generate_from_clusters(
        self,
        collection: str = "prop_outcomes",
        similarity_threshold: float = 0.85,
        min_cluster_size: int = 10,
        min_hit_rate_delta: float = 0.05,
        data_period: str | None = None,
    ) -> list[dict]:
        """
        Analyze embedding clusters to discover data-driven hypotheses.

        For each cluster of similar prop outcomes:
          1. Check if the cluster has a statistically interesting hit rate
          2. If hit rate diverges from expected, generate a hypothesis
          3. Extract common features from the cluster as context factors

        Args:
            collection: which embedding collection to cluster
            similarity_threshold: min cosine similarity for clustering
            min_cluster_size: ignore clusters smaller than this
            min_hit_rate_delta: min deviation from expected to generate hypothesis
            data_period: 'historical' = cluster only on historical data (for backtesting),
                         'recent' = only recent data, None = all data (for live trading)

        Returns list of created hypothesis summaries.
        """
        clusters = await self.vector_store.cluster_by_similarity(
            collection, threshold=similarity_threshold, data_period=data_period
        )

        created = []
        for cluster in clusters:
            if len(cluster) < min_cluster_size:
                continue

            # Analyze cluster
            analysis = self._analyze_cluster(cluster)
            if not analysis:
                continue

            hit_rate = analysis["hit_rate"]
            expected_rate = analysis["expected_rate"]
            delta = hit_rate - expected_rate

            if abs(delta) < min_hit_rate_delta:
                continue

            # Generate hypothesis from cluster pattern
            side = "Over" if delta > 0 else "Under"
            common = analysis["common_features"]
            sport = common.get("sport", "basketball_nba")
            market = common.get("market", "player_points")

            name = (
                f"Cluster-discovered: {market.replace('player_', '')} "
                f"{side} edge ({common.get('pattern_desc', 'unknown pattern')})"
            )

            thesis = (
                f"In situations matching this cluster pattern "
                f"(N={len(cluster)}, hit_rate={hit_rate:.1%} vs "
                f"expected {expected_rate:.1%}), {side} bets on "
                f"{market} show a {abs(delta)*100:.1f}% edge. "
                f"Pattern features: {common.get('pattern_desc', 'see metadata')}."
            )

            # Tag which embedding data the hypothesis was derived from
            period_label = data_period or "all"

            # Compute temporal isolation metadata for cluster-derived hypotheses
            today = datetime.now(timezone.utc).date()
            training_cutoff = today - timedelta(days=30)
            training_period_start = "2023-01-01"
            training_period_end = str(training_cutoff)
            forward_test_start = str(training_cutoff + timedelta(days=1))

            try:
                hid = await self.hypothesis_manager.create_hypothesis(
                    name=name,
                    thesis=thesis,
                    sport=sport,
                    market_type=market,
                    model_config={
                        "type": "cluster_derived",
                        "devig_method": "power",
                        "target_book": "draftkings",
                        "consensus_min_books": 3,
                        "cluster_features": common,
                        "source_cluster_size": len(cluster),
                        "source_data_period": period_label,
                        "training_period_start": training_period_start,
                        "training_period_end": training_period_end,
                        "forward_test_start": forward_test_start,
                    },
                    edge_threshold=abs(delta),
                    notes=(
                        f"Auto-discovered from {collection} cluster "
                        f"(N={len(cluster)}, data_period={period_label}). "
                        f"Train: [{training_period_start}..{training_period_end}], "
                        f"forward-test from {forward_test_start}."
                    ),
                )
                created.append({
                    "hypothesis_id": hid,
                    "name": name,
                    "cluster_size": len(cluster),
                    "hit_rate": round(hit_rate, 4),
                    "expected_rate": round(expected_rate, 4),
                    "delta": round(delta, 4),
                    "data_period": period_label,
                    "training_period_end": training_period_end,
                    "forward_test_start": forward_test_start,
                })
            except Exception as e:
                logger.warning(f"Failed to create cluster hypothesis: {e}")

        logger.info(
            f"Generated {len(created)} hypotheses from {len(clusters)} clusters "
            f"in '{collection}'"
        )
        return created

    async def generate_from_claude(
        self,
        sport: str,
        data_summary: str,
    ) -> list[dict]:
        """
        Ask the hypothesis_gen ladder (qwen36 primary, Claude last) to
        generate novel hypotheses from a data summary.

        The function keeps its historical name for call-site compatibility,
        but the ladder picks the best available model per task_type and
        respects CALLISTO_LOCAL_ONLY + Claude Max hours demotion.
        """
        from inference import escalate_with_ladder

        prompt = (
            f"You are Callisto's hypothesis engine. Given the following data summary "
            f"for {sport}, generate 3-5 novel, testable betting hypotheses.\n\n"
            f"DATA SUMMARY:\n{data_summary}\n\n"
            f"For each hypothesis, return JSON with:\n"
            f"- name: short descriptive name\n"
            f"- thesis: detailed testable claim\n"
            f"- market_type: one of (spreads, totals, h2h, player_points, "
            f"player_rebounds, player_assists, player_threes, "
            f"player_points_rebounds_assists)\n"
            f"- edge_threshold: minimum edge to flag (decimal, e.g., 0.03)\n"
            f"- model_config: dict with devig_method, target_book, "
            f"consensus_min_books, and any context_factors\n\n"
            f"Return ONLY a JSON array. No explanation text."
        )

        result = await escalate_with_ladder(
            prompt=prompt,
            system_context="Callisto hypothesis generation — return structured JSON only.",
            task_type="hypothesis_gen",
            timeout=120,
            hermes_caller="hypothesis_gen",
        )

        if result.get("error"):
            logger.error(f"Hypothesis generation ladder failed: {result['error']}")
            return []

        # Parse response
        content = result.get("content", "")
        try:
            # Try to extract JSON from response
            start = content.find("[")
            end = content.rfind("]") + 1
            if start >= 0 and end > start:
                hypotheses_raw = json.loads(content[start:end])
            else:
                logger.warning("Could not find JSON array in Claude response")
                return []
        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse Claude hypotheses: {e}")
            return []

        # Temporal metadata for Claude-generated hypotheses
        today = datetime.now(timezone.utc).date()
        training_cutoff = today - timedelta(days=30)
        training_period_start = "2023-01-01"
        training_period_end = str(training_cutoff)
        forward_test_start = str(training_cutoff + timedelta(days=1))

        created = []
        for h_raw in hypotheses_raw:
            try:
                mc = h_raw.get("model_config", {
                    "type": "consensus_devig",
                    "devig_method": "power",
                    "target_book": "draftkings",
                    "consensus_min_books": 3,
                })
                # Inject temporal isolation metadata
                mc["training_period_start"] = training_period_start
                mc["training_period_end"] = training_period_end
                mc["forward_test_start"] = forward_test_start

                hid = await self.hypothesis_manager.create_hypothesis(
                    name=h_raw.get("name", "Unnamed"),
                    thesis=h_raw.get("thesis", ""),
                    sport=sport,
                    market_type=h_raw.get("market_type", "spreads"),
                    model_config=mc,
                    edge_threshold=float(h_raw.get("edge_threshold", 0.02)),
                    notes=(
                        f"Auto-generated by Claude Code hypothesis engine. "
                        f"Train: [{training_period_start}..{training_period_end}], "
                        f"forward-test from {forward_test_start}."
                    ),
                )
                created.append({
                    "hypothesis_id": hid,
                    "name": h_raw.get("name"),
                    "source": "claude_code",
                    "training_period_end": training_period_end,
                    "forward_test_start": forward_test_start,
                })
            except Exception as e:
                logger.warning(f"Failed to create Claude hypothesis: {e}")

        logger.info(f"Claude Code generated {len(created)} hypotheses for {sport}")
        return created

    def _expand_variables(self, variables: dict) -> list[dict]:
        """Expand variable dict into list of all combinations."""
        if not variables:
            return [{}]

        keys = list(variables.keys())
        values = list(variables.values())

        combos = [{}]
        for key, vals in zip(keys, values):
            new_combos = []
            for combo in combos:
                if isinstance(vals, list):
                    for v in vals:
                        new_combo = combo.copy()
                        new_combo[key] = v
                        new_combos.append(new_combo)
                else:
                    combo[key] = vals
                    new_combos.append(combo)
            combos = new_combos

        return combos

    def _analyze_cluster(self, cluster: list[dict]) -> Optional[dict]:
        """
        Analyze a cluster of prop outcomes to find patterns.
        Returns analysis dict with hit_rate, expected_rate, common_features.
        """
        hits = 0
        total = 0
        edges = []
        sports = []
        markets = []
        players = []

        for item in cluster:
            meta = item.get("metadata") or {}
            if meta.get("hit") is not None:
                total += 1
                if meta["hit"]:
                    hits += 1
            if meta.get("edge") is not None:
                edges.append(meta["edge"])
            if meta.get("sport"):
                sports.append(meta["sport"])
            if meta.get("market"):
                markets.append(meta["market"])
            if meta.get("player"):
                players.append(meta["player"])

        if total < 5:
            return None

        hit_rate = hits / total
        # Expected rate from book implied probabilities
        expected_probs = [
            item.get("metadata", {}).get("book_implied_over", 0.5)
            for item in cluster
            if item.get("metadata", {}).get("book_implied_over") is not None
        ]
        expected_rate = (
            sum(expected_probs) / len(expected_probs)
            if expected_probs
            else 0.5
        )

        # Find most common features
        def mode(lst):
            if not lst:
                return None
            return max(set(lst), key=lst.count)

        common_sport = mode(sports)
        common_market = mode(markets)

        # Build pattern description
        pattern_parts = []
        if common_sport:
            pattern_parts.append(common_sport.replace("basketball_", ""))
        if common_market:
            pattern_parts.append(common_market.replace("player_", ""))
        avg_edge = sum(edges) / len(edges) if edges else 0
        if avg_edge:
            pattern_parts.append(f"avg_edge={avg_edge:.1%}")
        pattern_desc = " ".join(pattern_parts) if pattern_parts else "mixed"

        return {
            "hit_rate": hit_rate,
            "expected_rate": expected_rate,
            "total_resolved": total,
            "avg_edge": avg_edge,
            "common_features": {
                "sport": common_sport,
                "market": common_market,
                "pattern_desc": pattern_desc,
                "unique_players": len(set(players)),
            },
        }
