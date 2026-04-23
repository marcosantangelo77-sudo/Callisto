"""
Generate comprehensive MLB 2026 season hypotheses — second wave.

Covers angles not addressed by create_mlb_hypotheses.py:
  - Pitcher-specific (regression, handedness, pitch count, openers, weather/breaking ball)
  - Hitting / batter props (leadoff OBP, stolen bases, multi-hit, platoon)
  - Game-level situational (circadian, rubber match, umpire, interleague DH)
  - Structural / market (opening line value, cross-book divergence, weather moves, BP availability)
  - Additional early season angles

All hypotheses use temporal isolation with training 2023-01-01 to 2025-12-31.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Shared temporal config for all hypotheses
TEMPORAL = {
    "training_period_start": "2023-01-01",
    "training_period_end": "2025-12-31",
    "forward_test_start": "2026-01-01",
    "backtest_period_start": "2026-01-01",
    "backtest_period_end": "2026-12-31",
    "temporal_isolation": True,
}


def mc(type_: str, **kwargs) -> dict:
    """Build model_config with temporal metadata baked in."""
    cfg = {"type": type_, "source": "claude_mlb_wave2", **TEMPORAL, **kwargs}
    return cfg


MLB_HYPOTHESES_WAVE2 = [

    # ═══════════════════════════════════════════════════
    # PITCHER-SPECIFIC
    # ═══════════════════════════════════════════════════

    {
        "name": "mlb_ace_day_after_loss_k_over",
        "thesis": (
            "Top-tier starters (sub-3.00 ERA, top-20 K/9) who lost their previous start "
            "show elevated K rates in their next outing as they pitch more aggressively to "
            "avoid consecutive losses. K prop Overs are +EV the start after a loss for aces."
        ),
        "sport": "baseball_mlb",
        "market_type": "player_strikeouts",
        "model_config": mc(
            "consensus_devig", devig_method="power", target_book="draftkings",
            consensus_min_books=3,
            context_factors=["prev_start_result", "pitcher_k9", "pitcher_era"],
            side_filter="Over",
        ),
        "edge_threshold": 0.015,
        "confidence": 0.45,
    },
    {
        "name": "mlb_pitcher_lr_platoon_k_over",
        "thesis": (
            "When a pitcher faces a lineup stacked with same-side hitters (e.g., RHP vs "
            "RHB-heavy lineup), K rates increase because same-side hitters have worse "
            "visibility on breaking pitches. K Overs are underpriced when platoon "
            "disadvantage exceeds 60% of the opposing lineup."
        ),
        "sport": "baseball_mlb",
        "market_type": "player_strikeouts",
        "model_config": mc(
            "consensus_devig", devig_method="power", target_book="draftkings",
            consensus_min_books=3,
            context_factors=["pitcher_hand", "lineup_handedness_pct", "pitcher_k_rate_vs_same"],
            side_filter="Over",
        ),
        "edge_threshold": 0.012,
        "confidence": 0.50,
    },
    {
        "name": "mlb_high_pitch_count_next_start_under",
        "thesis": (
            "Starters who threw 110+ pitches in their previous start show degraded "
            "velocity and command in their next outing, leading to shorter outings and "
            "lower K totals. K prop Unders and F5 Unders are +EV when the starter's "
            "prior pitch count exceeded 110."
        ),
        "sport": "baseball_mlb",
        "market_type": "player_strikeouts",
        "model_config": mc(
            "consensus_devig", devig_method="power", target_book="draftkings",
            consensus_min_books=3,
            context_factors=["prev_pitch_count", "days_rest", "pitcher_age"],
            side_filter="Under",
        ),
        "edge_threshold": 0.015,
        "confidence": 0.45,
    },
    {
        "name": "mlb_opener_piggyback_total_over",
        "thesis": (
            "Games using an opener/piggyback strategy (reliever starts, bulk pitcher "
            "follows) produce higher scoring than traditional starts because the transition "
            "between arms creates a vulnerability window. Books set totals based on average "
            "team run production, underweighting the opener effect by 0.3-0.5 runs."
        ),
        "sport": "baseball_mlb",
        "market_type": "totals",
        "model_config": mc(
            "consensus_devig", devig_method="power", target_book="draftkings",
            consensus_min_books=3,
            context_factors=["starter_type", "bulk_pitcher_era", "bullpen_usage_pattern"],
            side_filter="Over",
        ),
        "edge_threshold": 0.012,
        "confidence": 0.40,
    },
    {
        "name": "mlb_cold_breaking_ball_pitcher_under",
        "thesis": (
            "Pitchers who rely heavily on breaking balls (slider/curveball usage >40%) "
            "lose 2-3 inches of movement in sub-55F conditions. Their K rate drops and "
            "contact quality rises. But books set their K props based on season averages, "
            "making Unders +EV for breaking-ball-heavy pitchers in cold weather."
        ),
        "sport": "baseball_mlb",
        "market_type": "player_strikeouts",
        "model_config": mc(
            "consensus_devig", devig_method="power", target_book="draftkings",
            consensus_min_books=3,
            context_factors=["temperature", "pitcher_breaking_pct", "pitcher_k9"],
            side_filter="Under",
        ),
        "edge_threshold": 0.015,
        "confidence": 0.45,
    },
    {
        "name": "mlb_pitcher_first_inning_run_over",
        "thesis": (
            "Starting pitchers with a career first-inning ERA 0.5+ runs higher than their "
            "overall ERA consistently give up early runs. Books set first-inning run props "
            "based on overall pitcher quality. First-inning 'Yes' run props are +EV for "
            "starters with historically bad first innings (top quartile first-inning ERA)."
        ),
        "sport": "baseball_mlb",
        "market_type": "player_props",
        "model_config": mc(
            "consensus_devig", devig_method="power", target_book="draftkings",
            consensus_min_books=3,
            context_factors=["pitcher_first_inning_era", "pitcher_overall_era", "opponent_ops_first_inning"],
        ),
        "edge_threshold": 0.015,
        "confidence": 0.40,
    },
    {
        "name": "mlb_pitcher_short_rest_f5_under",
        "thesis": (
            "Starters on 4 days rest (instead of standard 5) show measurably lower "
            "velocity and higher walk rates. F5 totals don't fully account for this. "
            "F5 Unders are +EV when either starter is on short rest, as shorter outings "
            "and lower-quality stuff suppress early scoring."
        ),
        "sport": "baseball_mlb",
        "market_type": "totals",
        "model_config": mc(
            "consensus_devig", devig_method="power", target_book="draftkings",
            consensus_min_books=3,
            context_factors=["days_rest", "starter_velocity_trend", "f5_total"],
            side_filter="Under",
        ),
        "edge_threshold": 0.012,
        "confidence": 0.40,
    },

    # ═══════════════════════════════════════════════════
    # HITTING / BATTER PROPS
    # ═══════════════════════════════════════════════════

    {
        "name": "mlb_leadoff_obp_high_total_over",
        "thesis": (
            "Leadoff hitters get 1-2 extra plate appearances per game. In games with "
            "totals set at 9+, high-OBP leadoff men (.360+) have more opportunities to "
            "reach base, making their hits/reached-base props underpriced. Hits Over props "
            "for elite leadoff hitters in high-total games are +EV."
        ),
        "sport": "baseball_mlb",
        "market_type": "player_hits",
        "model_config": mc(
            "consensus_devig", devig_method="power", target_book="draftkings",
            consensus_min_books=3,
            context_factors=["batting_order", "player_obp", "game_total"],
            side_filter="Over",
        ),
        "edge_threshold": 0.012,
        "confidence": 0.45,
    },
    {
        "name": "mlb_power_hitter_tb_warm_wind_out",
        "thesis": (
            "Power hitters (ISO .220+) see a 10-15% increase in XBH probability when "
            "temperature is above 75F and wind is blowing out at 8+ mph. Books set total "
            "bases lines using season averages without granular weather adjustment. "
            "Total bases Overs for power hitters in warm wind-out conditions are +EV."
        ),
        "sport": "baseball_mlb",
        "market_type": "player_total_bases",
        "model_config": mc(
            "consensus_devig", devig_method="power", target_book="draftkings",
            consensus_min_books=3,
            context_factors=["temperature", "wind_speed", "wind_direction", "batter_iso", "park_factor"],
            side_filter="Over",
        ),
        "edge_threshold": 0.015,
        "confidence": 0.50,
    },
    {
        "name": "mlb_rhb_vs_lhp_hits_over",
        "thesis": (
            "Right-handed batters with a .300+ career BA against left-handed pitching "
            "are underpriced on hits props when facing a LHP starter. Books set hit lines "
            "based on overall BA, not platoon splits. Hits Overs for strong platoon-split "
            "RHBs vs LHP starters are +EV."
        ),
        "sport": "baseball_mlb",
        "market_type": "player_hits",
        "model_config": mc(
            "consensus_devig", devig_method="power", target_book="draftkings",
            consensus_min_books=3,
            context_factors=["batter_hand", "pitcher_hand", "batter_ba_vs_opp_hand", "batter_ops_vs_opp_hand"],
            side_filter="Over",
        ),
        "edge_threshold": 0.012,
        "confidence": 0.50,
    },
    {
        "name": "mlb_lhb_vs_rhp_tb_over",
        "thesis": (
            "Left-handed power hitters facing RHP starters with a reverse-platoon "
            "weakness (higher BABIP allowed to LHB) produce elevated extra-base hit "
            "rates. Total bases Overs for LHB power bats vs susceptible RHP are +EV "
            "when the platoon split gap exceeds 30 OPS points."
        ),
        "sport": "baseball_mlb",
        "market_type": "player_total_bases",
        "model_config": mc(
            "consensus_devig", devig_method="power", target_book="draftkings",
            consensus_min_books=3,
            context_factors=["batter_hand", "pitcher_hand", "pitcher_ops_allowed_vs_lhb", "batter_iso"],
            side_filter="Over",
        ),
        "edge_threshold": 0.012,
        "confidence": 0.45,
    },
    {
        "name": "mlb_stolen_base_early_season_over",
        "thesis": (
            "In the first 3 weeks of the season, stolen base props are underpriced "
            "because catchers haven't established pop times against new basestealers, "
            "pitchers haven't refined pickoff moves with new batterymates, and aggressive "
            "baserunning is higher early. SB Overs for players with 25+ SB potential are +EV."
        ),
        "sport": "baseball_mlb",
        "market_type": "player_stolen_bases",
        "model_config": mc(
            "consensus_devig", devig_method="power", target_book="draftkings",
            consensus_min_books=3,
            context_factors=["season_week", "player_sb_pace", "catcher_cs_rate", "pitcher_sb_allowed"],
            side_filter="Over",
        ),
        "edge_threshold": 0.015,
        "confidence": 0.40,
    },
    {
        "name": "mlb_multi_hit_high_ba_vs_high_whip",
        "thesis": (
            "Batters with a .290+ BA facing starters with a 1.35+ WHIP have elevated "
            "multi-hit game probability. Books set hit props using overall averages, "
            "not matchup-specific quality. The 2+ hits prop for high-BA hitters vs "
            "high-WHIP pitchers is underpriced by 3-5%."
        ),
        "sport": "baseball_mlb",
        "market_type": "player_hits",
        "model_config": mc(
            "consensus_devig", devig_method="power", target_book="draftkings",
            consensus_min_books=3,
            context_factors=["batter_ba", "pitcher_whip", "pitcher_babip_allowed"],
            side_filter="Over",
        ),
        "edge_threshold": 0.015,
        "confidence": 0.45,
    },
    {
        "name": "mlb_batter_hr_park_wind_over",
        "thesis": (
            "Home run props for power hitters in the top-5 HR park factors (Coors, "
            "Great American, Yankee Stadium, Wrigley wind-out, Minute Maid) are "
            "underpriced by 5-8% when temperature exceeds 70F. Books adjust park "
            "factors insufficiently for the compounding effect of warm air + hitter-friendly parks."
        ),
        "sport": "baseball_mlb",
        "market_type": "player_home_runs",
        "model_config": mc(
            "consensus_devig", devig_method="power", target_book="draftkings",
            consensus_min_books=3,
            context_factors=["park_hr_factor", "temperature", "batter_hr_rate", "wind_direction"],
            side_filter="Over",
        ),
        "edge_threshold": 0.02,
        "confidence": 0.40,
    },
    {
        "name": "mlb_batter_rbi_high_total_over",
        "thesis": (
            "RBI props for 3-4-5 hitters in games with totals set at 9.5+ are "
            "underpriced because books use season RBI averages. Higher-total games "
            "imply more baserunners, directly increasing RBI opportunities for "
            "middle-of-order bats. RBI Overs for cleanup hitters in high-total games are +EV."
        ),
        "sport": "baseball_mlb",
        "market_type": "player_rbis",
        "model_config": mc(
            "consensus_devig", devig_method="power", target_book="draftkings",
            consensus_min_books=3,
            context_factors=["batting_order", "game_total", "player_rbi_rate", "team_obp"],
            side_filter="Over",
        ),
        "edge_threshold": 0.015,
        "confidence": 0.40,
    },

    # ═══════════════════════════════════════════════════
    # GAME-LEVEL SITUATIONAL
    # ═══════════════════════════════════════════════════

    {
        "name": "mlb_west_coast_early_et_under",
        "thesis": (
            "West Coast teams (LAD, LAA, SF, OAK, SD, SEA) playing road games with "
            "first pitch before 1:30pm ET (10:30am body clock) show suppressed offensive "
            "output due to circadian mismatch. Totals Unders are +EV for early-start "
            "road games involving West Coast teams, strongest for day games after night games."
        ),
        "sport": "baseball_mlb",
        "market_type": "totals",
        "model_config": mc(
            "consensus_devig", devig_method="power", target_book="draftkings",
            consensus_min_books=3,
            context_factors=["team_timezone", "game_start_et", "travel_direction", "prev_game_end_time"],
            side_filter="Under",
        ),
        "edge_threshold": 0.012,
        "confidence": 0.45,
    },
    {
        "name": "mlb_rubber_match_home_ml",
        "thesis": (
            "In 3-game series rubber matches (series tied 1-1), the home team wins at "
            "a rate 2-3% above implied probability. Home field advantage intensifies in "
            "decisive games due to lineup optimization, crowd energy, and bullpen management "
            "advantages. Home ML in rubber matches is +EV when cross-book consensus confirms."
        ),
        "sport": "baseball_mlb",
        "market_type": "h2h",
        "model_config": mc(
            "consensus_devig", devig_method="power", target_book="draftkings",
            consensus_min_books=4,
            context_factors=["series_game_number", "series_score", "home_away"],
        ),
        "edge_threshold": 0.01,
        "confidence": 0.45,
    },
    {
        "name": "mlb_umpire_tight_zone_under",
        "thesis": (
            "Home plate umpires in the top quartile of called strike rate expand the "
            "zone by 1-2 inches, suppressing offense and increasing K rates. Games with "
            "tight-zone umpires see 0.4-0.7 fewer runs on average. Totals Unders are +EV "
            "when the assigned HP umpire has a top-quartile called strike rate."
        ),
        "sport": "baseball_mlb",
        "market_type": "totals",
        "model_config": mc(
            "consensus_devig", devig_method="power", target_book="draftkings",
            consensus_min_books=3,
            context_factors=["umpire_called_strike_rate", "umpire_runs_per_game", "starter_k_rates"],
            side_filter="Under",
        ),
        "edge_threshold": 0.012,
        "confidence": 0.50,
    },
    {
        "name": "mlb_umpire_wide_zone_pitcher_k_over",
        "thesis": (
            "When the assigned HP umpire has a top-quartile called strike rate (wide zone), "
            "starting pitchers with strong command (BB/9 < 2.5) see inflated K rates "
            "because borderline pitches are called strikes. K prop Overs for low-walk "
            "pitchers paired with wide-zone umps are +EV."
        ),
        "sport": "baseball_mlb",
        "market_type": "player_strikeouts",
        "model_config": mc(
            "consensus_devig", devig_method="power", target_book="draftkings",
            consensus_min_books=3,
            context_factors=["umpire_called_strike_rate", "pitcher_bb9", "pitcher_k9"],
            side_filter="Over",
        ),
        "edge_threshold": 0.015,
        "confidence": 0.50,
    },
    {
        "name": "mlb_series_sweep_game3_dog_ml",
        "thesis": (
            "Teams facing a 3-game series sweep (down 0-2) show elevated motivation and "
            "lineup optimization to avoid the sweep. The team down 0-2 as underdog is "
            "underpriced on the ML because books overweight momentum. Dogs at +130 or "
            "longer in sweep-avoidance spots are +EV."
        ),
        "sport": "baseball_mlb",
        "market_type": "h2h",
        "model_config": mc(
            "consensus_devig", devig_method="power", target_book="draftkings",
            consensus_min_books=4,
            context_factors=["series_score", "dog_price", "team_win_pct_last_30"],
        ),
        "edge_threshold": 0.012,
        "confidence": 0.40,
    },
    {
        "name": "mlb_day_game_road_team_total_under",
        "thesis": (
            "Road teams playing day games after traveling to a new city the previous "
            "night show 8-12% lower wOBA and 15% higher K rate due to travel fatigue "
            "and circadian disruption. Team totals for the road team in these spots "
            "trend under. Full-game unders are +EV when the road team traveled 500+ miles."
        ),
        "sport": "baseball_mlb",
        "market_type": "totals",
        "model_config": mc(
            "consensus_devig", devig_method="power", target_book="draftkings",
            consensus_min_books=3,
            context_factors=["travel_miles", "game_time", "prev_game_end_time", "rest_hours"],
            side_filter="Under",
        ),
        "edge_threshold": 0.012,
        "confidence": 0.45,
    },
    {
        "name": "mlb_four_game_series_game4_over",
        "thesis": (
            "The 4th game of a 4-game series sees depleted bullpens on both sides after "
            "3 consecutive days of usage. Teams typically carry 13 pitchers, and by game 4, "
            "3-4 relievers are unavailable. Totals Overs in game 4 of 4-game series are +EV "
            "as late-inning scoring spikes."
        ),
        "sport": "baseball_mlb",
        "market_type": "totals",
        "model_config": mc(
            "consensus_devig", devig_method="power", target_book="draftkings",
            consensus_min_books=3,
            context_factors=["series_game_number", "series_length", "bullpen_ip_last_3"],
            side_filter="Over",
        ),
        "edge_threshold": 0.015,
        "confidence": 0.45,
    },

    # ═══════════════════════════════════════════════════
    # STRUCTURAL / MARKET EFFICIENCY
    # ═══════════════════════════════════════════════════

    {
        "name": "mlb_opening_line_first_hour_value",
        "thesis": (
            "MLB lines move most in the first 60 minutes after posting as sharp money "
            "attacks soft openers. Capturing the opening line within 15 minutes of posting "
            "provides 1.5-3% edge on closing line value. Signals that match opening line "
            "direction have higher CLV than signals captured at close."
        ),
        "sport": "baseball_mlb",
        "market_type": "h2h",
        "model_config": mc(
            "line_timing", target_book="draftkings",
            context_factors=["line_age_minutes", "opening_line", "current_line", "clv_at_capture"],
        ),
        "edge_threshold": 0.01,
        "confidence": 0.55,
    },
    {
        "name": "mlb_cross_book_runline_divergence",
        "thesis": (
            "When the -1.5 run line juice diverges by 20+ cents between Pinnacle/sharp "
            "books and DraftKings/retail books, the sharp-book side covers at 55%+. "
            "This divergence signals retail money inflating one side. Taking the "
            "Pinnacle-favored run line side is +EV when divergence exceeds 20 cents."
        ),
        "sport": "baseball_mlb",
        "market_type": "spreads",
        "model_config": mc(
            "cross_book_divergence", devig_method="power",
            target_book="draftkings", reference_book="pinnacle",
            context_factors=["runline_juice_diff", "ml_implied_diff", "public_pct"],
        ),
        "edge_threshold": 0.012,
        "confidence": 0.55,
    },
    {
        "name": "mlb_weather_driven_total_move",
        "thesis": (
            "When weather forecast changes (wind direction shift, temperature swing 10+F) "
            "cause the total to move 0.5+ runs after initial posting, the market overreacts. "
            "Fading weather-driven total moves of 0.5+ runs is +EV because books and bettors "
            "overcorrect for updated forecasts."
        ),
        "sport": "baseball_mlb",
        "market_type": "totals",
        "model_config": mc(
            "line_movement_fade", target_book="draftkings",
            context_factors=["total_open", "total_current", "total_move_size", "weather_change_trigger"],
        ),
        "edge_threshold": 0.012,
        "confidence": 0.40,
    },
    {
        "name": "mlb_bullpen_availability_late_spread",
        "thesis": (
            "Teams with 3+ key relievers unavailable (pitched 2+ days in a row or "
            "pitched 30+ pitches previous day) are vulnerable in late innings. When "
            "the opposing team has a fully rested bullpen, the live spread shifts 0.5+ "
            "runs. Pre-game run line value exists betting against the depleted bullpen."
        ),
        "sport": "baseball_mlb",
        "market_type": "spreads",
        "model_config": mc(
            "consensus_devig", devig_method="power", target_book="draftkings",
            consensus_min_books=3,
            context_factors=["bullpen_availability_score", "closer_available", "key_relievers_rested"],
        ),
        "edge_threshold": 0.015,
        "confidence": 0.45,
    },
    {
        "name": "mlb_alt_runline_plus_2_5_dog_value",
        "thesis": (
            "Alt run line +2.5 for underdogs covers at approximately 78-82% of the time "
            "in MLB. When books price +2.5 alt lines at -200 or better for dogs with a "
            "starter ERA under 4.50, the implied cover rate is lower than actual. "
            "Parlay-ready alt lines offer hidden EV for good-pitching underdogs."
        ),
        "sport": "baseball_mlb",
        "market_type": "spreads",
        "model_config": mc(
            "consensus_devig", devig_method="power", target_book="draftkings",
            consensus_min_books=3,
            context_factors=["alt_runline", "dog_starter_era", "dog_price"],
        ),
        "edge_threshold": 0.01,
        "confidence": 0.40,
    },
    {
        "name": "mlb_sharp_money_reverse_line_move",
        "thesis": (
            "When 65%+ of public bets are on one ML side but the line moves against the "
            "public (reverse line move), sharp money is driving the move. The anti-public "
            "side in MLB reverse line moves is +EV at 2-3%, as sharps are typically better "
            "calibrated than retail for MLB game outcomes."
        ),
        "sport": "baseball_mlb",
        "market_type": "h2h",
        "model_config": mc(
            "reverse_line_move", target_book="draftkings",
            context_factors=["public_bet_pct", "line_move_direction", "line_move_size", "sharp_money_pct"],
        ),
        "edge_threshold": 0.012,
        "confidence": 0.55,
    },

    # ═══════════════════════════════════════════════════
    # EARLY SEASON / OPENING WEEK (IMMEDIATE)
    # ═══════════════════════════════════════════════════

    {
        "name": "mlb_opening_week_k_prop_over",
        "thesis": (
            "In the first week of the regular season, hitters are adjusting from spring "
            "training velocity and pitch mix. K rates spike 8-12% league-wide in week 1 "
            "compared to season averages. Pitcher K prop Overs are systematically "
            "underpriced during opening week across the board."
        ),
        "sport": "baseball_mlb",
        "market_type": "player_strikeouts",
        "model_config": mc(
            "consensus_devig", devig_method="power", target_book="draftkings",
            consensus_min_books=3,
            context_factors=["season_week", "league_k_rate_week1_vs_season"],
            side_filter="Over",
        ),
        "edge_threshold": 0.012,
        "confidence": 0.45,
    },
    {
        "name": "mlb_new_pitcher_team_k_under",
        "thesis": (
            "Pitchers who changed teams in the offseason have unknown pitch mixes and "
            "sequences to new opponents in their first 3 starts. But by starts 4-6, "
            "opposing hitters have video and scouting reports. K Unders for pitchers "
            "in starts 4-6 with a new team are +EV as hitters have adjusted."
        ),
        "sport": "baseball_mlb",
        "market_type": "player_strikeouts",
        "model_config": mc(
            "consensus_devig", devig_method="power", target_book="draftkings",
            consensus_min_books=3,
            context_factors=["pitcher_new_team", "starts_with_new_team", "opponent_familiarity"],
            side_filter="Under",
        ),
        "edge_threshold": 0.015,
        "confidence": 0.35,
    },
    {
        "name": "mlb_early_season_stolen_base_aggression",
        "thesis": (
            "Stolen base attempt rates are 15-20% higher in April than season averages "
            "as teams test catchers/pitchers, new pitch clock encourages running, and "
            "scouting data is sparse. Over 0.5 SB team props and individual SB props "
            "for speedsters are underpriced in weeks 1-4."
        ),
        "sport": "baseball_mlb",
        "market_type": "player_stolen_bases",
        "model_config": mc(
            "consensus_devig", devig_method="power", target_book="draftkings",
            consensus_min_books=3,
            context_factors=["month", "team_sb_rate", "catcher_pop_time", "pitcher_hold_time"],
            side_filter="Over",
        ),
        "edge_threshold": 0.015,
        "confidence": 0.40,
    },
    {
        "name": "mlb_traded_hitter_props_fade_week1",
        "thesis": (
            "Hitters who changed teams in the offseason have inflated props in their "
            "first 2 weeks based on prior-year numbers in their old park/lineup. New "
            "lineup position, protection, and park factors create a 2-3 week adjustment "
            "period. Unders on hits/TB props for traded hitters in weeks 1-2 are +EV."
        ),
        "sport": "baseball_mlb",
        "market_type": "player_hits",
        "model_config": mc(
            "consensus_devig", devig_method="power", target_book="draftkings",
            consensus_min_books=3,
            context_factors=["player_new_team", "games_with_new_team", "park_factor_change"],
            side_filter="Under",
        ),
        "edge_threshold": 0.015,
        "confidence": 0.35,
    },
    {
        "name": "mlb_f5_under_cold_weather_april",
        "thesis": (
            "First-5-innings Unders are especially profitable in April cold-weather "
            "games because starters are at peak freshness (low pitch counts, sharp stuff) "
            "and cold conditions compound the offensive suppression. F5 Unders in outdoor "
            "parks with temps below 60F are +EV at 3-5% above market-implied rate."
        ),
        "sport": "baseball_mlb",
        "market_type": "totals",
        "model_config": mc(
            "consensus_devig", devig_method="power", target_book="draftkings",
            consensus_min_books=3,
            context_factors=["f5_total", "temperature", "month", "park_type"],
            side_filter="Under",
        ),
        "edge_threshold": 0.012,
        "confidence": 0.50,
    },
    {
        "name": "mlb_division_first_series_under",
        "thesis": (
            "The first divisional series of the season (teams meeting for the first time) "
            "produces lower-scoring games because pitchers have fresher arsenals, familiarity "
            "advantages accrue more to pitching staffs who know opponent weaknesses, and "
            "both teams prioritize quality starts. Unders are +EV in first-meeting division series."
        ),
        "sport": "baseball_mlb",
        "market_type": "totals",
        "model_config": mc(
            "consensus_devig", devig_method="power", target_book="draftkings",
            consensus_min_books=3,
            context_factors=["division_matchup", "season_series_game", "starter_vs_team_career"],
            side_filter="Under",
        ),
        "edge_threshold": 0.012,
        "confidence": 0.40,
    },

    # ═══════════════════════════════════════════════════
    # ADVANCED STRUCTURAL
    # ═══════════════════════════════════════════════════

    {
        "name": "mlb_interleague_pitcher_unfamiliarity_k_over",
        "thesis": (
            "In interleague games, starting pitchers face lineups they rarely see "
            "(different league, limited career matchup data). This unfamiliarity "
            "increases K rates by 5-8% as hitters have no recent at-bats against "
            "the pitcher's stuff. K Overs for starters in interleague starts are +EV."
        ),
        "sport": "baseball_mlb",
        "market_type": "player_strikeouts",
        "model_config": mc(
            "consensus_devig", devig_method="power", target_book="draftkings",
            consensus_min_books=3,
            context_factors=["interleague", "pitcher_career_pa_vs_opponent", "pitcher_k9"],
            side_filter="Over",
        ),
        "edge_threshold": 0.012,
        "confidence": 0.45,
    },
    {
        "name": "mlb_getaway_day_under",
        "thesis": (
            "The final game of a road series ('getaway day') before a team travels "
            "trends under because managers rest regulars, shorten lineups, and pull "
            "starters earlier to manage travel logistics. Totals Unders on getaway-day "
            "games are +EV, especially for cross-country travel."
        ),
        "sport": "baseball_mlb",
        "market_type": "totals",
        "model_config": mc(
            "consensus_devig", devig_method="power", target_book="draftkings",
            consensus_min_books=3,
            context_factors=["series_game_number", "travel_next", "travel_distance_next"],
            side_filter="Under",
        ),
        "edge_threshold": 0.012,
        "confidence": 0.40,
    },
    {
        "name": "mlb_doubleheader_game2_over",
        "thesis": (
            "Game 2 of doubleheaders features bullpen pitching, fatigued arms, and "
            "7-inning format remnants in scheduling-affected games. Even with 9-inning "
            "format, game 2 totals are set too low because books anchor to full-rest "
            "assumptions. Game 2 Overs are +EV due to depleted pitching on both sides."
        ),
        "sport": "baseball_mlb",
        "market_type": "totals",
        "model_config": mc(
            "consensus_devig", devig_method="power", target_book="draftkings",
            consensus_min_books=3,
            context_factors=["doubleheader_game", "bullpen_usage_game1", "starter_quality_game2"],
            side_filter="Over",
        ),
        "edge_threshold": 0.015,
        "confidence": 0.50,
    },
    {
        "name": "mlb_9th_inning_save_opp_over_0_5",
        "thesis": (
            "Closers entering save situations (leading by 1-3 runs in the 9th) allow "
            "at least 1 run approximately 28-32% of the time. Live/pre-game 'will there "
            "be a run in the 9th inning' Yes props at +110 or better are +EV for games "
            "where the projected closer has a save conversion rate under 85%."
        ),
        "sport": "baseball_mlb",
        "market_type": "player_props",
        "model_config": mc(
            "consensus_devig", devig_method="power", target_book="draftkings",
            consensus_min_books=3,
            context_factors=["closer_save_pct", "closer_era", "closer_whip", "lead_size"],
        ),
        "edge_threshold": 0.015,
        "confidence": 0.35,
    },
    {
        "name": "mlb_night_game_total_over_hot_weather",
        "thesis": (
            "Night games in summer (June-August) at outdoor parks when daytime high "
            "exceeded 90F see elevated scoring because the ball still carries in warm "
            "air, pitchers fatigue faster in residual heat, and fan energy amplifies "
            "offensive aggression. Totals Overs in hot-weather night games are +EV."
        ),
        "sport": "baseball_mlb",
        "market_type": "totals",
        "model_config": mc(
            "consensus_devig", devig_method="power", target_book="draftkings",
            consensus_min_books=3,
            context_factors=["game_time", "temperature_high", "temperature_gametime", "park_type"],
            side_filter="Over",
        ),
        "edge_threshold": 0.012,
        "confidence": 0.40,
    },
    {
        "name": "mlb_pitcher_3rd_time_through_order_over",
        "thesis": (
            "Starters who are left in to face the lineup a 3rd time show a 20-30% "
            "spike in OPS allowed as hitters adjust to pitch sequences. When a starter "
            "is projected to go 6+ innings (high pitch efficiency), the 6th-7th inning "
            "scoring spike makes late F5/full-game Overs +EV. Books underweight the "
            "3rd-time-through penalty."
        ),
        "sport": "baseball_mlb",
        "market_type": "totals",
        "model_config": mc(
            "consensus_devig", devig_method="power", target_book="draftkings",
            consensus_min_books=3,
            context_factors=["starter_ip_projected", "starter_3rd_time_ops", "starter_pitch_efficiency"],
            side_filter="Over",
        ),
        "edge_threshold": 0.012,
        "confidence": 0.50,
    },
    {
        "name": "mlb_pitcher_era_vs_xera_regression",
        "thesis": (
            "Pitchers whose ERA is 1.0+ runs lower than their xERA (expected ERA based "
            "on quality of contact, K rate, BB rate) are due for regression. Books anchor "
            "to actual ERA for pricing. Overs/Unders on these pitchers' starts should "
            "be priced closer to xERA, creating value on Overs for lucky pitchers."
        ),
        "sport": "baseball_mlb",
        "market_type": "totals",
        "model_config": mc(
            "regression", target_book="draftkings",
            context_factors=["pitcher_era", "pitcher_xera", "pitcher_era_minus_xera", "pitcher_fip"],
            side_filter="Over",
        ),
        "edge_threshold": 0.012,
        "confidence": 0.55,
    },
    {
        "name": "mlb_lineup_confirm_total_move",
        "thesis": (
            "When MLB lineups are confirmed (typically 2-3 hours before first pitch) "
            "and a key hitter is absent (rest day, minor injury), totals drop 0.3-0.5 "
            "runs. If the consensus total before lineup confirmation already priced in "
            "the full lineup, the Under after lineup news is +EV as the market adjusts."
        ),
        "sport": "baseball_mlb",
        "market_type": "totals",
        "model_config": mc(
            "line_timing", target_book="draftkings",
            context_factors=["lineup_confirmed", "key_hitter_absent", "total_pre_lineup", "total_post_lineup"],
        ),
        "edge_threshold": 0.012,
        "confidence": 0.45,
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
    for h in MLB_HYPOTHESES_WAVE2:
        if h["name"] in existing_names:
            print(f"  SKIP (exists): {h['name']}")
            skipped += 1
            continue

        hid = await mgr.create_hypothesis(
            name=h["name"],
            thesis=h["thesis"],
            sport=h["sport"],
            market_type=h["market_type"],
            model_config=h["model_config"],
            edge_threshold=h["edge_threshold"],
            min_sample_size=50,
            notes=f"MLB 2026 wave2 — confidence {h.get('confidence', 0.4)}",
        )
        created += 1
        print(f"  CREATED [{hid}]: {h['name']}")

    print(f"\n{'='*60}")
    print(f"Created: {created} | Skipped (existing): {skipped} | Total in batch: {len(MLB_HYPOTHESES_WAVE2)}")
    await mgr.close()


if __name__ == "__main__":
    asyncio.run(main())
