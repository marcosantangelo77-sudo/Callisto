"""
Generate comprehensive Masters 2026 betting hypotheses for Callisto.

Augusta National, April 10-13, 2026.

Covers: course fit, strokes gained decomposition, historical/narrative patterns,
weather/conditions, props/matchups, and structural/market inefficiencies.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


MASTERS_HYPOTHESES = [
    # ======================================================================
    # AUGUSTA-SPECIFIC COURSE FIT
    # ======================================================================
    {
        "name": "masters_sg_approach_outperformance",
        "thesis": (
            "Augusta National is a second-shot course where approach accuracy determines "
            "contention more than driving distance. Players ranked top-10 in SG:Approach "
            "over the trailing 24 rounds outperform their outright odds-implied probability "
            "for top-5/top-10 finishes at the Masters. Books overweight driving distance "
            "and total SG:OTT when pricing Masters outrights, creating systematic "
            "underpricing of elite iron players."
        ),
        "sport": "golf_pga_masters",
        "market_type": "top_5_finish",
        "model_config": {
            "type": "strokes_gained_decomposition",
            "key_stat": "sg_approach",
            "lookback_rounds": 24,
            "rank_threshold": 10,
            "training_period_start": "2015-04-01",
            "training_period_end": "2024-04-30",
            "venue": "augusta_national",
            "context_factors": ["sg_approach_rank", "gir_pct", "proximity_to_hole"],
        },
        "edge_threshold": 0.02,
        "confidence": 0.55,
    },
    {
        "name": "masters_par5_scoring_separation",
        "thesis": (
            "Par-5 scoring is the primary separator at Augusta. Holes 2, 8, 13, and 15 "
            "are all reachable in two and produce the widest birdie/eagle differential "
            "in the field. Players in the top quintile of par-5 scoring over their last "
            "12 events are underpriced for top-10 finishes because books anchor to overall "
            "scoring average rather than par-5-specific production. Eagles on 13 and 15 "
            "drive 60%+ of leaderboard moves on the weekend."
        ),
        "sport": "golf_pga_masters",
        "market_type": "top_10_finish",
        "model_config": {
            "type": "scoring_distribution",
            "key_stat": "par5_scoring_avg",
            "lookback_events": 12,
            "training_period_start": "2015-04-01",
            "training_period_end": "2024-04-30",
            "venue": "augusta_national",
            "context_factors": ["par5_eagle_rate", "par5_birdie_rate", "sg_tee_to_green_par5"],
        },
        "edge_threshold": 0.02,
        "confidence": 0.50,
    },
    {
        "name": "masters_amen_corner_scoring_differential",
        "thesis": (
            "Amen Corner (holes 11, 12, 13) produces the highest scoring variance at "
            "Augusta National. Players with historical Amen Corner scoring at or below "
            "par in prior Masters appearances are significantly underpriced in outright "
            "and top-20 markets. The 12th hole alone (par 3, Golden Bell) accounts for "
            "more tournament-ending blow-ups than any other hole on Tour. Course history "
            "on these three holes is more predictive than aggregate form."
        ),
        "sport": "golf_pga_masters",
        "market_type": "top_20_finish",
        "model_config": {
            "type": "hole_level_analysis",
            "key_holes": [11, 12, 13],
            "metric": "amen_corner_scoring_vs_field",
            "training_period_start": "2015-04-01",
            "training_period_end": "2024-04-30",
            "venue": "augusta_national",
            "context_factors": ["amen_corner_history", "masters_appearances", "par3_scoring"],
        },
        "edge_threshold": 0.015,
        "confidence": 0.45,
    },
    {
        "name": "masters_fade_bias_advantage",
        "thesis": (
            "Augusta National's doglegs and green complexes historically favor players who "
            "shape the ball left-to-right (faders). The majority of approach shots require "
            "a controlled fade to hold firm, sloping greens. Players whose predominant "
            "shot shape is a fade and who rank top-20 in SG:Approach have an additional "
            "1-2% edge in top-10 markets beyond what their overall SG predicts. Books do "
            "not adjust for shot-shape compatibility with specific courses."
        ),
        "sport": "golf_pga_masters",
        "market_type": "top_10_finish",
        "model_config": {
            "type": "shot_shape_course_fit",
            "preferred_shape": "fade",
            "key_stat": "sg_approach",
            "training_period_start": "2016-01-01",
            "training_period_end": "2024-04-30",
            "venue": "augusta_national",
            "context_factors": ["shot_shape", "sg_approach", "approach_dispersion"],
        },
        "edge_threshold": 0.015,
        "confidence": 0.40,
    },
    {
        "name": "masters_fast_greens_putting_specialist",
        "thesis": (
            "Augusta's greens run at stimpmeter 13-14, the fastest on Tour. Players who "
            "gain strokes putting on surfaces measured at stimpmeter 12+ (Augusta, TPC "
            "Sawgrass, East Lake) have a putting advantage that books do not isolate. "
            "Overall SG:Putting is a poor proxy because it blends slow-green performance. "
            "Fast-green SG:Putting in the top-15 of the field predicts top-20 finishes "
            "at Augusta better than raw SG:Putting rank."
        ),
        "sport": "golf_pga_masters",
        "market_type": "top_20_finish",
        "model_config": {
            "type": "surface_specific_putting",
            "surface_type": "fast_bentgrass",
            "stimpmeter_threshold": 12,
            "training_period_start": "2016-01-01",
            "training_period_end": "2024-04-30",
            "venue": "augusta_national",
            "context_factors": ["sg_putting_fast_greens", "stimpmeter_avg", "three_putt_avoidance"],
        },
        "edge_threshold": 0.015,
        "confidence": 0.45,
    },
    {
        "name": "masters_iron_precision_over_power",
        "thesis": (
            "Augusta National rewards iron precision over raw power. Players who rank "
            "in the top-20 in proximity to hole from 150-200 yards but outside the top-30 "
            "in driving distance are systematically underpriced because the market anchors "
            "to bomber narratives. At Augusta, shot shaping and distance control on approaches "
            "trump distance off the tee — the course is long enough to reward length but not "
            "so long that short hitters cannot compete with superior iron play."
        ),
        "sport": "golf_pga_masters",
        "market_type": "top_10_finish",
        "model_config": {
            "type": "proximity_vs_distance",
            "proximity_range_yards": [150, 200],
            "proximity_rank_threshold": 20,
            "driving_distance_rank_min": 30,
            "training_period_start": "2016-01-01",
            "training_period_end": "2024-04-30",
            "venue": "augusta_national",
            "context_factors": ["proximity_150_200", "driving_distance_rank", "gir_pct"],
        },
        "edge_threshold": 0.02,
        "confidence": 0.45,
    },

    # ======================================================================
    # HISTORICAL / NARRATIVE
    # ======================================================================
    {
        "name": "masters_first_timer_fade",
        "thesis": (
            "Masters debutants historically underperform their world ranking at Augusta. "
            "From 2010-2024, first-time Masters participants have a top-20 finish rate "
            "15-20% below what their ranking implies. The course demands local knowledge: "
            "green reading, slope awareness, strategy off the tee, and Amen Corner "
            "management. Fading first-timers in top-20 and cut-made markets at Augusta "
            "is +EV. Books price debutants based on form without sufficient course-knowledge "
            "discount."
        ),
        "sport": "golf_pga_masters",
        "market_type": "top_20_finish",
        "model_config": {
            "type": "experience_filter",
            "masters_appearances": 0,
            "direction": "fade",
            "training_period_start": "2010-04-01",
            "training_period_end": "2024-04-30",
            "venue": "augusta_national",
            "context_factors": ["masters_appearances", "world_ranking", "major_experience"],
        },
        "edge_threshold": 0.02,
        "confidence": 0.55,
    },
    {
        "name": "masters_specialist_repeat_top10",
        "thesis": (
            "Players with 3+ top-10 finishes at the Masters outperform their current "
            "world ranking in outright and top-5 markets. Augusta specialists develop "
            "course-specific skills (green reading, strategy, comfort) that compound "
            "over appearances. Books anchor to current form and world ranking without "
            "sufficiently weighting Augusta track record. Historical top-10 performers "
            "at Augusta convert to top-5 finishes at 30%+ in subsequent appearances."
        ),
        "sport": "golf_pga_masters",
        "market_type": "top_5_finish",
        "model_config": {
            "type": "course_history_premium",
            "min_top10s": 3,
            "lookback_years": 10,
            "training_period_start": "2010-04-01",
            "training_period_end": "2024-04-30",
            "venue": "augusta_national",
            "context_factors": ["masters_top10_count", "world_ranking", "recent_form_sg"],
        },
        "edge_threshold": 0.02,
        "confidence": 0.50,
    },
    {
        "name": "masters_course_history_vs_recent_form",
        "thesis": (
            "At the Masters, Augusta course history is more predictive of top-20 finishes "
            "than recent 4-week form. A player with a 2-year Masters average finish of "
            "top-15 who arrives in poor recent form (missed cuts, 40th+ finishes) is still "
            "underpriced because books overreact to recency. The optimal weighting is "
            "roughly 60% course history / 40% recent form for top-20 props. Market pricing "
            "inverts this ratio."
        ),
        "sport": "golf_pga_masters",
        "market_type": "top_20_finish",
        "model_config": {
            "type": "weighted_form_model",
            "course_history_weight": 0.60,
            "recent_form_weight": 0.40,
            "recent_form_events": 4,
            "course_history_years": 3,
            "training_period_start": "2012-04-01",
            "training_period_end": "2024-04-30",
            "venue": "augusta_national",
            "context_factors": ["avg_masters_finish", "last_4_events_finish", "sg_total_l24"],
        },
        "edge_threshold": 0.015,
        "confidence": 0.50,
    },
    {
        "name": "masters_champions_dinner_renaissance",
        "thesis": (
            "Past Masters champions who show a form resurgence (top-10 finish in any "
            "event within 6 weeks of the Masters) are underpriced for top-20 and cut-made "
            "markets. The Champions Dinner and lifetime exemption create psychological "
            "comfort and familiarity that compounds with renewed confidence. Books treat "
            "former champions as aging veterans without accounting for the course-familiarity "
            "moat that reduces variance even for players outside the top-30 in the world."
        ),
        "sport": "golf_pga_masters",
        "market_type": "top_20_finish",
        "model_config": {
            "type": "past_champion_form_filter",
            "past_champion": True,
            "recent_form_window_weeks": 6,
            "form_threshold": "top_10_in_window",
            "training_period_start": "2010-04-01",
            "training_period_end": "2024-04-30",
            "venue": "augusta_national",
            "context_factors": ["past_champion", "recent_top10", "masters_wins", "current_ranking"],
        },
        "edge_threshold": 0.02,
        "confidence": 0.40,
    },
    {
        "name": "masters_southeast_grass_transition",
        "thesis": (
            "Players based in or frequently competing in the Southeast US have an "
            "advantage with the bermuda-to-bent grass transition at Augusta. The fairways "
            "are overseeded rye on bermuda base, and the greens are bentgrass — a surface "
            "combination unique to Augusta. Players who primarily compete on bermuda/bent "
            "courses (Florida Swing, Southeast events) in March have better turf feel. "
            "This advantage is unpriced in markets because it is not captured by standard stats."
        ),
        "sport": "golf_pga_masters",
        "market_type": "make_cut",
        "model_config": {
            "type": "geographic_turf_advantage",
            "preferred_regions": ["southeast_us", "florida"],
            "turf_type": "bermuda_bent_transition",
            "training_period_start": "2015-04-01",
            "training_period_end": "2024-04-30",
            "venue": "augusta_national",
            "context_factors": ["home_region", "march_events_played", "bent_grass_sg_putting"],
        },
        "edge_threshold": 0.015,
        "confidence": 0.35,
    },

    # ======================================================================
    # STROKES GAINED DECOMPOSITION
    # ======================================================================
    {
        "name": "masters_sg_around_green_premium",
        "thesis": (
            "SG:Around-the-Green matters more at Augusta than the tour average due to "
            "severe pin positions, undulating greenside slopes, and the premium on creative "
            "short-game shots. Players ranked top-15 in SG:ATG over the trailing 24 rounds "
            "outperform their top-20 finish probability by 8-12%. The market underweights "
            "scrambling ability at Augusta because overall SG:ATG is considered lower-impact "
            "than approach or putting on Tour."
        ),
        "sport": "golf_pga_masters",
        "market_type": "top_20_finish",
        "model_config": {
            "type": "strokes_gained_decomposition",
            "key_stat": "sg_around_the_green",
            "lookback_rounds": 24,
            "rank_threshold": 15,
            "training_period_start": "2015-04-01",
            "training_period_end": "2024-04-30",
            "venue": "augusta_national",
            "context_factors": ["sg_atg_rank", "scrambling_pct", "up_and_down_pct"],
        },
        "edge_threshold": 0.015,
        "confidence": 0.45,
    },
    {
        "name": "masters_sg_tee_to_green_par5",
        "thesis": (
            "SG:Tee-to-Green on par 5s is the single highest-leverage stat at Augusta. "
            "The four par 5s (2, 8, 13, 15) yield 2-4 strokes of separation between "
            "contenders and the field over 72 holes. Players in the top decile of "
            "SG:T2G on par 5s over their last 12 events are underpriced for top-5 "
            "finishes because books weight total SG equally across par types rather "
            "than decomposing by par-5 performance specifically."
        ),
        "sport": "golf_pga_masters",
        "market_type": "top_5_finish",
        "model_config": {
            "type": "strokes_gained_decomposition",
            "key_stat": "sg_tee_to_green_par5",
            "lookback_events": 12,
            "percentile_threshold": 10,
            "training_period_start": "2015-04-01",
            "training_period_end": "2024-04-30",
            "venue": "augusta_national",
            "context_factors": ["sg_t2g_par5", "par5_eagle_rate", "driving_distance"],
        },
        "edge_threshold": 0.02,
        "confidence": 0.50,
    },
    {
        "name": "masters_sg_putting_bentgrass_specific",
        "thesis": (
            "SG:Putting on bentgrass greens specifically (not overall SG:Putting) is the "
            "correct putting metric for Masters pricing. Augusta's bentgrass greens play "
            "differently from bermuda or poa annua surfaces. Players who gain 0.5+ strokes "
            "putting on bentgrass surfaces over their last 12 events but rank average in "
            "overall SG:Putting are underpriced — the market uses aggregate putting which "
            "dilutes bentgrass-specific skill."
        ),
        "sport": "golf_pga_masters",
        "market_type": "top_20_finish",
        "model_config": {
            "type": "surface_specific_sg",
            "key_stat": "sg_putting_bentgrass",
            "surface_filter": "bentgrass",
            "lookback_events": 12,
            "sg_threshold": 0.5,
            "training_period_start": "2016-01-01",
            "training_period_end": "2024-04-30",
            "venue": "augusta_national",
            "context_factors": ["sg_putting_bentgrass", "sg_putting_overall", "bentgrass_rounds"],
        },
        "edge_threshold": 0.015,
        "confidence": 0.45,
    },

    # ======================================================================
    # WEATHER / CONDITIONS
    # ======================================================================
    {
        "name": "masters_thursday_wave_advantage",
        "thesis": (
            "At the Masters, the Thursday morning wave has historically scored 0.3-0.5 "
            "strokes better than the afternoon wave due to calmer morning conditions and "
            "firmer afternoon greens. First-round leader props and R1 top-5 markets do "
            "not fully adjust for wave assignment. Players in the favorable early wave "
            "are underpriced for R1 top-5 and first-round leader markets by 5-8%."
        ),
        "sport": "golf_pga_masters",
        "market_type": "first_round_leader",
        "model_config": {
            "type": "wave_advantage",
            "favorable_wave": "AM",
            "day": "thursday",
            "scoring_advantage_strokes": 0.4,
            "training_period_start": "2012-04-01",
            "training_period_end": "2024-04-30",
            "venue": "augusta_national",
            "context_factors": ["tee_time_wave", "wind_forecast", "temperature_am_vs_pm"],
        },
        "edge_threshold": 0.02,
        "confidence": 0.45,
    },
    {
        "name": "masters_east_wind_hole12_blowup",
        "thesis": (
            "When the wind shifts to an easterly direction at Augusta, the 12th hole "
            "(Golden Bell, 155-yard par 3 over Rae's Creek) becomes dramatically more "
            "difficult. The swirling wind in the valley makes club selection almost "
            "random. Under east wind conditions, the field bogey rate on 12 doubles. "
            "This increases overall scoring variance, making longshot outrights and "
            "top-20 underdogs more likely to cash as favorites make bogeys/doubles."
        ),
        "sport": "golf_pga_masters",
        "market_type": "tournament_winner",
        "model_config": {
            "type": "weather_condition_filter",
            "wind_direction": "east",
            "key_hole": 12,
            "training_period_start": "2012-04-01",
            "training_period_end": "2024-04-30",
            "venue": "augusta_national",
            "context_factors": ["wind_direction_forecast", "wind_speed", "hole12_history"],
        },
        "edge_threshold": 0.01,
        "confidence": 0.40,
    },
    {
        "name": "masters_rain_softened_bomber_advantage",
        "thesis": (
            "When rain softens Augusta National, the course plays significantly longer "
            "because the ball does not roll on the fairways. Par 5s become harder to "
            "reach in two for mid-length hitters, and approach shots hold greens more "
            "easily (reducing the Augusta green-reading advantage of veterans). Bombers "
            "(top-20 in driving distance) are underpriced in rain-softened conditions "
            "because they can still reach par 5s in two while shorter hitters cannot."
        ),
        "sport": "golf_pga_masters",
        "market_type": "top_10_finish",
        "model_config": {
            "type": "weather_condition_filter",
            "condition": "rain_softened",
            "advantage_player_type": "bomber",
            "driving_distance_rank_threshold": 20,
            "training_period_start": "2012-04-01",
            "training_period_end": "2024-04-30",
            "venue": "augusta_national",
            "context_factors": ["precipitation_forecast", "driving_distance", "par5_eagle_rate"],
        },
        "edge_threshold": 0.02,
        "confidence": 0.40,
    },
    {
        "name": "masters_cold_morning_course_management",
        "thesis": (
            "Cold mornings at Augusta (sub-55F at tee time, common in early April) "
            "reduce ball flight distance by 5-8 yards and make greens firmer. Players "
            "who excel in course management — low bogey rate, high fairways hit — "
            "outperform in cold conditions because the course punishes aggression more. "
            "Books do not adjust matchup pricing for temperature-driven course-management "
            "advantages."
        ),
        "sport": "golf_pga_masters",
        "market_type": "matchups",
        "model_config": {
            "type": "weather_condition_filter",
            "condition": "cold_morning",
            "temperature_threshold_f": 55,
            "advantage_stat": "bogey_avoidance",
            "training_period_start": "2012-04-01",
            "training_period_end": "2024-04-30",
            "venue": "augusta_national",
            "context_factors": ["temperature_forecast", "bogey_avoidance_rank", "fairways_hit_pct"],
        },
        "edge_threshold": 0.015,
        "confidence": 0.35,
    },

    # ======================================================================
    # PROPS / MATCHUPS
    # ======================================================================
    {
        "name": "masters_frl_fade_top5_favorites",
        "thesis": (
            "The first-round leader market at the Masters is historically volatile. "
            "Top-5 pre-tournament favorites have won R1 leader only 15% of the time "
            "over the last decade. Players ranked 40-80 in the field offer better "
            "expected value as FRL selections because the market overconcentrates "
            "probability on top names. FRL is a single-round market with enormous "
            "variance — the correct pricing spreads probability much more evenly than "
            "books reflect."
        ),
        "sport": "golf_pga_masters",
        "market_type": "first_round_leader",
        "model_config": {
            "type": "frl_distribution_model",
            "favorites_to_fade": "top_5_odds",
            "value_zone_rank": [40, 80],
            "training_period_start": "2010-04-01",
            "training_period_end": "2024-04-30",
            "venue": "augusta_national",
            "context_factors": ["pre_tournament_odds_rank", "r1_history_masters", "sg_total_l12"],
        },
        "edge_threshold": 0.02,
        "confidence": 0.45,
    },
    {
        "name": "masters_top10_value_ranked_15_30",
        "thesis": (
            "Players ranked 15-30 in the Masters field by pre-tournament odds offer "
            "the best risk-adjusted value in top-10 finish markets. The market overweights "
            "the top-5 ranked players, compressing their top-10 probability too high and "
            "leaving value in the 15-30 range. These players are good enough to contend "
            "but priced at 5:1 to 10:1 for top-10, when the true probability is 15-20% "
            "(fair price 4:1 to 5.5:1)."
        ),
        "sport": "golf_pga_masters",
        "market_type": "top_10_finish",
        "model_config": {
            "type": "field_stratification",
            "odds_rank_range": [15, 30],
            "training_period_start": "2010-04-01",
            "training_period_end": "2024-04-30",
            "venue": "augusta_national",
            "context_factors": ["pre_tournament_odds_rank", "top10_probability_implied", "sg_total"],
        },
        "edge_threshold": 0.02,
        "confidence": 0.50,
    },
    {
        "name": "masters_european_ryder_cup_motivation",
        "thesis": (
            "European players in Ryder Cup years show elevated performance at Augusta. "
            "The Masters is the first major of the year and European players use it as "
            "a Ryder Cup qualification statement. In Ryder Cup years (even years), European "
            "players ranked 10-40 in the world outperform their odds-implied probability "
            "for top-20 finishes by 5-8%. This narrative is too diffuse for books to price."
        ),
        "sport": "golf_pga_masters",
        "market_type": "top_20_finish",
        "model_config": {
            "type": "narrative_filter",
            "narrative": "european_ryder_cup_year",
            "ryder_cup_year": True,
            "nationality_filter": "european",
            "world_ranking_range": [10, 40],
            "training_period_start": "2010-04-01",
            "training_period_end": "2024-04-30",
            "venue": "augusta_national",
            "context_factors": ["nationality", "ryder_cup_year", "world_ranking"],
        },
        "edge_threshold": 0.015,
        "confidence": 0.35,
    },
    {
        "name": "masters_cut_made_fade_injured_stars",
        "thesis": (
            "The Masters cut is the harshest in golf — top-50 and ties after 36 holes. "
            "2-3 big-name players miss the cut every year, often due to undisclosed "
            "injuries, rust from layoffs, or mental fatigue. Players who have played "
            "fewer than 6 events in the prior 10 weeks (indicating possible injury or "
            "reduced schedule) are overpriced for cut-made props. Fading low-activity "
            "big names for missed-cut value is +EV."
        ),
        "sport": "golf_pga_masters",
        "market_type": "make_cut",
        "model_config": {
            "type": "activity_filter",
            "min_events_threshold": 6,
            "lookback_weeks": 10,
            "direction": "fade_under_threshold",
            "training_period_start": "2012-04-01",
            "training_period_end": "2024-04-30",
            "venue": "augusta_national",
            "context_factors": ["events_played_10wk", "world_ranking", "withdrawal_history"],
        },
        "edge_threshold": 0.02,
        "confidence": 0.45,
    },
    {
        "name": "masters_round_improvement_pattern",
        "thesis": (
            "Players who historically improve round-over-round at Augusta (lower R4 score "
            "than R1) tend to repeat this pattern. Augusta rewards learning — players who "
            "figure out the greens and pin positions through the week gain 1-2 strokes "
            "by Sunday. In live betting, these 'Sunday closers' are underpriced for "
            "top-10 finishes after a mediocre R1 because the market overreacts to early "
            "scores without accounting for progressive Augusta adaptation."
        ),
        "sport": "golf_pga_masters",
        "market_type": "top_10_finish",
        "model_config": {
            "type": "round_progression",
            "pattern": "r1_to_r4_improvement",
            "metric": "avg_round_improvement",
            "training_period_start": "2012-04-01",
            "training_period_end": "2024-04-30",
            "venue": "augusta_national",
            "context_factors": ["r1_vs_r4_differential", "masters_appearances", "sunday_scoring_avg"],
        },
        "edge_threshold": 0.02,
        "confidence": 0.45,
    },

    # ======================================================================
    # STRUCTURAL / MARKET
    # ======================================================================
    {
        "name": "masters_outright_american_overpricing",
        "thesis": (
            "Books systematically overprice popular American players in Masters outright "
            "markets because the public disproportionately bets American names. DraftKings "
            "in particular caters to US bettors who overweight names like Scheffler, Spieth, "
            "and DeChambeau relative to their true win probability. International players at "
            "equivalent world rankings offer 10-15% longer outright odds. Fading the "
            "American premium and backing international equivalents is +EV."
        ),
        "sport": "golf_pga_masters",
        "market_type": "tournament_winner",
        "model_config": {
            "type": "nationality_market_bias",
            "overpriced_nationality": "american",
            "comparison_metric": "odds_vs_world_ranking_implied",
            "training_period_start": "2015-04-01",
            "training_period_end": "2024-04-30",
            "venue": "augusta_national",
            "context_factors": ["nationality", "world_ranking", "outright_odds", "public_bet_pct"],
        },
        "edge_threshold": 0.02,
        "confidence": 0.40,
    },
    {
        "name": "masters_dead_heat_top5_edge",
        "thesis": (
            "Top-5 and top-10 finish bets at the Masters are subject to dead-heat rules "
            "when multiple players tie for the boundary position. Augusta produces 2-3 way "
            "ties at the top-5 and top-10 cutoff roughly 60% of the time. The mathematical "
            "impact of dead-heat deductions means the implied probability of a full payout "
            "is lower than the headline odds suggest. Books that do NOT apply dead-heat "
            "rules (or apply them less aggressively) offer structural edge."
        ),
        "sport": "golf_pga_masters",
        "market_type": "top_5_finish",
        "model_config": {
            "type": "dead_heat_adjustment",
            "market": "top_5",
            "historical_tie_rate": 0.60,
            "training_period_start": "2010-04-01",
            "training_period_end": "2024-04-30",
            "venue": "augusta_national",
            "context_factors": ["dead_heat_rules_by_book", "tie_probability", "field_density"],
        },
        "edge_threshold": 0.01,
        "confidence": 0.50,
    },
    {
        "name": "masters_sunday_back9_live_closer",
        "thesis": (
            "The Sunday back nine at Augusta produces the most dramatic leaderboard swings "
            "in professional golf. Holes 12-15 create an average of 3-5 lead changes per "
            "Masters Sunday. Players within 4 strokes of the lead entering the back nine "
            "on Sunday who have strong par-5 scoring and low Amen Corner bogey rates are "
            "underpriced in live outright and top-5 markets. Books overanchor to the "
            "current leaderboard position without pricing the volatility of the back nine."
        ),
        "sport": "golf_pga_masters",
        "market_type": "tournament_winner",
        "model_config": {
            "type": "live_betting_model",
            "window": "sunday_back_9",
            "strokes_back_threshold": 4,
            "training_period_start": "2010-04-01",
            "training_period_end": "2024-04-30",
            "venue": "augusta_national",
            "context_factors": ["strokes_back_entering_back9", "par5_scoring", "amen_corner_bogey_rate"],
        },
        "edge_threshold": 0.03,
        "confidence": 0.45,
    },
    {
        "name": "masters_r1_info_revelation_odds_shift",
        "thesis": (
            "Pre-tournament outright odds vs Thursday evening odds reveal systematic "
            "market overreaction to R1 scores. Players who shoot 2-under or better in R1 "
            "see their outright odds compress by 30-50%, but the actual probability increase "
            "is only 15-25% because Augusta is a 4-round test with enormous back-nine "
            "variance. Pre-tournament selections at longer odds who have a good R1 offer "
            "better value than buying Thursday evening odds for the same player."
        ),
        "sport": "golf_pga_masters",
        "market_type": "tournament_winner",
        "model_config": {
            "type": "odds_movement_analysis",
            "window": "pre_tournament_vs_r1_close",
            "r1_score_threshold": -2,
            "training_period_start": "2012-04-01",
            "training_period_end": "2024-04-30",
            "venue": "augusta_national",
            "context_factors": ["pre_tournament_odds", "post_r1_odds", "r1_score", "implied_prob_shift"],
        },
        "edge_threshold": 0.02,
        "confidence": 0.50,
    },
    {
        "name": "masters_outright_longshot_portfolio",
        "thesis": (
            "A portfolio of 5-8 Masters longshots (80:1 to 200:1) with strong Augusta "
            "course fit metrics (SG:Approach top-20, par-5 scoring top-25, 3+ prior "
            "Masters starts) has positive expected value because the outright winner market "
            "is inherently overround and the tail probability of longshot winners at Augusta "
            "is underestimated. From 2010-2024, 4 of 15 Masters winners went off at 40:1 "
            "or longer. A systematic longshot portfolio captures this tail value."
        ),
        "sport": "golf_pga_masters",
        "market_type": "tournament_winner",
        "model_config": {
            "type": "portfolio_longshot",
            "odds_range": [8000, 20000],
            "portfolio_size": [5, 8],
            "required_metrics": ["sg_approach_top20", "par5_scoring_top25", "masters_starts_3plus"],
            "training_period_start": "2010-04-01",
            "training_period_end": "2024-04-30",
            "venue": "augusta_national",
            "context_factors": ["outright_odds", "sg_approach", "par5_scoring", "masters_starts"],
        },
        "edge_threshold": 0.01,
        "confidence": 0.40,
    },
    {
        "name": "masters_top20_high_gir_low_putting",
        "thesis": (
            "Players with elite GIR% (top-10 on Tour) but average putting (ranked 40-80) "
            "are underpriced for top-20 finishes at Augusta. The conventional wisdom is "
            "that putting wins at Augusta, but the data shows GIR% is more predictive of "
            "top-20 finishes because Augusta's greens are so difficult that even good "
            "putters struggle — the advantage goes to those who give themselves the most "
            "birdie looks through iron play, not those who convert at the highest rate."
        ),
        "sport": "golf_pga_masters",
        "market_type": "top_20_finish",
        "model_config": {
            "type": "stat_combination_filter",
            "gir_rank_threshold": 10,
            "putting_rank_range": [40, 80],
            "training_period_start": "2015-04-01",
            "training_period_end": "2024-04-30",
            "venue": "augusta_national",
            "context_factors": ["gir_pct_rank", "sg_putting_rank", "proximity_to_hole"],
        },
        "edge_threshold": 0.015,
        "confidence": 0.40,
    },
    {
        "name": "masters_matchup_experience_premium",
        "thesis": (
            "In head-to-head matchup props at the Masters, the player with more Masters "
            "experience (starts) has a structural edge that books underweight. When two "
            "similarly ranked players are matched up and one has 5+ more Masters starts, "
            "the experienced player wins the matchup 55-58% of the time. Books price "
            "matchups primarily on current form and world ranking, underweighting the "
            "Augusta-specific course knowledge accumulated over multiple appearances."
        ),
        "sport": "golf_pga_masters",
        "market_type": "matchups",
        "model_config": {
            "type": "h2h_experience_edge",
            "experience_gap_threshold": 5,
            "metric": "masters_starts",
            "training_period_start": "2012-04-01",
            "training_period_end": "2024-04-30",
            "venue": "augusta_national",
            "context_factors": ["player_a_masters_starts", "player_b_masters_starts", "ranking_diff"],
        },
        "edge_threshold": 0.02,
        "confidence": 0.45,
    },
    {
        "name": "masters_make_cut_debutant_miss_rate",
        "thesis": (
            "Masters debutants miss the cut at a rate 25-30% higher than their world "
            "ranking implies. The top-50-and-ties cut at Augusta is the toughest in golf, "
            "and first-timers face course-knowledge gaps that are not captured by any "
            "standard stat. Laying against debutants in make-cut markets (especially "
            "those ranked 30-60 in the world who are priced at -200 to -300 to make the "
            "cut) is systematically +EV."
        ),
        "sport": "golf_pga_masters",
        "market_type": "make_cut",
        "model_config": {
            "type": "experience_filter",
            "masters_appearances": 0,
            "direction": "fade",
            "world_ranking_range": [30, 60],
            "training_period_start": "2010-04-01",
            "training_period_end": "2024-04-30",
            "venue": "augusta_national",
            "context_factors": ["masters_appearances", "world_ranking", "cut_made_rate_tour"],
        },
        "edge_threshold": 0.02,
        "confidence": 0.50,
    },
    {
        "name": "masters_r1_low_round_value",
        "thesis": (
            "The lowest round of the tournament at the Masters is underpriced for "
            "players with strong Thursday scoring histories at Augusta. Certain players "
            "consistently fire low opening rounds — partly due to tee time advantage, "
            "partly due to lack of weekend pressure, and partly due to course familiarity "
            "on fresh greens. Players with 2+ sub-68 Thursday rounds at Augusta in the "
            "last 5 years offer value in R1 scoring props and R1 top-5 markets."
        ),
        "sport": "golf_pga_masters",
        "market_type": "round_scoring",
        "model_config": {
            "type": "round_specific_history",
            "round": 1,
            "score_threshold": 68,
            "min_occurrences": 2,
            "lookback_years": 5,
            "training_period_start": "2012-04-01",
            "training_period_end": "2024-04-30",
            "venue": "augusta_national",
            "context_factors": ["r1_scoring_history_augusta", "tee_time_wave", "course_conditions"],
        },
        "edge_threshold": 0.02,
        "confidence": 0.40,
    },
    {
        "name": "masters_bogey_free_stretch_value",
        "thesis": (
            "Players with the lowest bogey rates on Tour over the trailing 24 rounds are "
            "underpriced for cut-made and top-20 markets at Augusta. Augusta punishes "
            "bogeys more than it rewards birdies — a double bogey on 12 or a three-putt "
            "from above the hole on any green can end a tournament. Bogey avoidance is "
            "a more stable and predictive metric than birdie rate for Augusta performance "
            "but receives less market attention."
        ),
        "sport": "golf_pga_masters",
        "market_type": "make_cut",
        "model_config": {
            "type": "stability_metric",
            "key_stat": "bogey_avoidance_rate",
            "lookback_rounds": 24,
            "rank_threshold": 15,
            "training_period_start": "2015-04-01",
            "training_period_end": "2024-04-30",
            "venue": "augusta_national",
            "context_factors": ["bogey_rate", "double_bogey_rate", "scoring_consistency"],
        },
        "edge_threshold": 0.015,
        "confidence": 0.50,
    },
    {
        "name": "masters_weekend_scoring_momentum",
        "thesis": (
            "Players who make the cut at the Masters and shoot below-par in R3 have "
            "a historically elevated probability of top-10 finishes in R4. The Moving Day "
            "momentum at Augusta is stronger than at other majors because the course rewards "
            "confidence — players who figure out the pins and greens carry that knowledge "
            "into Sunday. Live odds after R3 undervalue this momentum factor."
        ),
        "sport": "golf_pga_masters",
        "market_type": "top_10_finish",
        "model_config": {
            "type": "momentum_model",
            "trigger": "r3_under_par",
            "lookback_metric": "r3_to_r4_conversion_rate",
            "training_period_start": "2012-04-01",
            "training_period_end": "2024-04-30",
            "venue": "augusta_national",
            "context_factors": ["r3_score", "r3_position", "strokes_back_after_r3"],
        },
        "edge_threshold": 0.02,
        "confidence": 0.40,
    },
]


async def main():
    from tools.hypothesis import HypothesisManager

    mgr = HypothesisManager()
    await mgr.initialize()

    existing = await mgr.list_hypotheses()
    existing_names = {h["name"] for h in existing}

    created = 0
    skipped = 0
    for h in MASTERS_HYPOTHESES:
        if h["name"] in existing_names:
            print(f"  SKIP (exists): {h['name']}")
            skipped += 1
            continue

        confidence = h.get("confidence", 0.45)
        notes = (
            f"Masters 2026 hypothesis — confidence {confidence:.0%} — "
            f"generated 2026-03-24 — Augusta National course fit analysis"
        )

        hid = await mgr.create_hypothesis(
            name=h["name"],
            thesis=h["thesis"],
            sport=h["sport"],
            market_type=h["market_type"],
            model_config=h["model_config"],
            edge_threshold=h.get("edge_threshold", 0.015),
            min_sample_size=30,  # Golf has smaller sample sizes than team sports
            significance_level=0.10,  # Relax for golf (fewer events per year)
            notes=notes,
        )

        created += 1
        print(f"  [{h['market_type']:20s}] {h['name']} -> {hid}")

    print(f"\n{'='*60}")
    print(f"Masters 2026 Hypotheses: {created} created, {skipped} skipped")
    print(f"Total in database: {len(existing_names) + created}")

    # Summary by market type
    market_counts = {}
    for h in MASTERS_HYPOTHESES:
        mt = h["market_type"]
        market_counts[mt] = market_counts.get(mt, 0) + 1
    print(f"\nBy market type:")
    for mt, count in sorted(market_counts.items(), key=lambda x: -x[1]):
        print(f"  {mt:25s}: {count}")

    await mgr.close()


if __name__ == "__main__":
    asyncio.run(main())
