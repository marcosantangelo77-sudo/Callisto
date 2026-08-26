"""
Seed/template data and variable expansion for the hypothesis generator.

Extracted from tools/hypothesis_generator.py as part of the hypgen split.
Pure data + pure functions only — no I/O, no LLM calls, no DB access.
"""

# ──────────────────────────────────────────────────
# Wiki-grounded variance-enforced generator constants
# ──────────────────────────────────────────────────
# Candidate sim >= CANDIDATE_DEDUP_SIM  ⇒ drop the weaker of the two
CANDIDATE_DEDUP_SIM: float = 0.85
# Candidate sim >= PRIOR_CORPUS_SIM to any wiki/existing-hyp  ⇒ drop (already covered)
PRIOR_CORPUS_SIM: float = 0.80
# How many wiki articles to prime the LLM with
WIKI_CONTEXT_TOP_K: int = 8
# How many recent rejected hypotheses to show as negative examples
NEGATIVE_EXAMPLES_N: int = 4


def expand_variables(variables: dict) -> list[dict]:
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
