"""Generate MLB-focused hypotheses for 2026 season opener (March 27)."""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

MLB_HYPOTHESES = [
    # === PITCHER K PROPS ===
    {
        "name": "mlb_pitcher_k_consensus_over",
        "thesis": (
            "Pitcher strikeout (K) props have the highest cross-book variance in MLB. "
            "When the devigged consensus of 4+ books sets a pitcher K Over fair probability "
            "1%+ higher than DraftKings implied, the Over hits at 54%+. Books are slow to "
            "adjust K lines for matchup-specific factors like opponent K rate and lineup handedness."
        ),
        "sport": "baseball_mlb",
        "market_type": "player_strikeouts",
        "model_config": {
            "type": "consensus_devig", "devig_method": "power",
            "target_book": "draftkings", "consensus_min_books": 4,
            "context_factors": ["opponent_k_rate", "lineup_handedness"],
        },
        "edge_threshold": 0.01,
    },
    {
        "name": "mlb_pitcher_k_high_total_over",
        "thesis": (
            "When the game total is set at 9+, starting pitchers face more aggressive swings "
            "in higher-scoring environments. K Overs are underpriced because books anchor K "
            "lines to pitcher averages without adjusting for the pace and aggression implied "
            "by high totals."
        ),
        "sport": "baseball_mlb",
        "market_type": "player_strikeouts",
        "model_config": {
            "type": "consensus_devig", "devig_method": "power",
            "target_book": "draftkings", "consensus_min_books": 3,
            "context_factors": ["game_total", "opponent_k_rate"],
            "side_filter": "Over",
        },
        "edge_threshold": 0.015,
    },
    {
        "name": "mlb_pitcher_k_opener_week1",
        "thesis": (
            "Opening week (first 2 series) pitcher K props are mispriced because books rely "
            "on prior-year baselines while spring training performance, new pitch mixes, and "
            "roster changes create real deviations. Cross-book consensus captures this faster."
        ),
        "sport": "baseball_mlb",
        "market_type": "player_strikeouts",
        "model_config": {
            "type": "consensus_devig", "devig_method": "power",
            "target_book": "draftkings", "consensus_min_books": 4,
            "context_factors": ["season_week", "spring_training_k_rate"],
        },
        "edge_threshold": 0.01,
    },
    # === FIRST 5 INNINGS (F5) ===
    {
        "name": "mlb_f5_total_under_ace_matchup",
        "thesis": (
            "When both starters have an ERA under 3.50, books set F5 totals based on blended "
            "team offense rather than the specific pitcher matchup. F5 Unders in ace-vs-ace "
            "matchups are +EV because the scoring distribution is dominated by the starters."
        ),
        "sport": "baseball_mlb",
        "market_type": "totals",
        "model_config": {
            "type": "consensus_devig", "devig_method": "power",
            "target_book": "draftkings", "consensus_min_books": 4,
            "context_factors": ["starter_era", "f5_line"],
            "side_filter": "Under",
        },
        "edge_threshold": 0.01,
    },
    {
        "name": "mlb_f5_moneyline_home_starter_edge",
        "thesis": (
            "F5 moneylines isolate starting pitcher quality from bullpen depth. Home starters "
            "with a 10%+ K rate advantage over the opposing starter are underpriced on the F5 "
            "ML because books weight team-level metrics too heavily vs starter-specific dominance."
        ),
        "sport": "baseball_mlb",
        "market_type": "h2h",
        "model_config": {
            "type": "consensus_devig", "devig_method": "power",
            "target_book": "draftkings", "consensus_min_books": 4,
            "context_factors": ["starter_k_rate_diff", "home_advantage", "f5_line"],
        },
        "edge_threshold": 0.01,
    },
    {
        "name": "mlb_f5_vs_fullgame_total_divergence",
        "thesis": (
            "When the F5 total implies a different scoring rate than the full-game total, "
            "the market disagrees on bullpen impact. Fading the full-game total toward the "
            "F5-implied rate is +EV because starters dominate early-season scoring patterns."
        ),
        "sport": "baseball_mlb",
        "market_type": "totals",
        "model_config": {
            "type": "consensus_devig", "devig_method": "power",
            "target_book": "draftkings", "consensus_min_books": 3,
            "context_factors": ["f5_total", "fullgame_total", "bullpen_usage"],
        },
        "edge_threshold": 0.01,
    },
    # === OPENING WEEKS TOTALS ===
    {
        "name": "mlb_opening_week_total_under",
        "thesis": (
            "Books set opening week totals based on prior-year team run production, but "
            "early-season offense is suppressed: hitters are timing-adjusting from spring "
            "training, new pitchers face lineups cold, and April weather depresses scoring. "
            "Unders are +EV in the first 2 weeks especially in non-dome parks."
        ),
        "sport": "baseball_mlb",
        "market_type": "totals",
        "model_config": {
            "type": "consensus_devig", "devig_method": "power",
            "target_book": "draftkings", "consensus_min_books": 4,
            "context_factors": ["season_week", "park_type", "temperature"],
            "side_filter": "Under",
        },
        "edge_threshold": 0.01,
    },
    {
        "name": "mlb_opening_week_total_over_dome",
        "thesis": (
            "In dome/retractable-roof stadiums (Tampa, Houston, Toronto, Milwaukee, Arizona, "
            "Miami, Texas), opening week totals should track closer to prior-year averages "
            "since weather is eliminated. If books set dome game totals lower due to general "
            "early-season under bias, Overs in domes are +EV."
        ),
        "sport": "baseball_mlb",
        "market_type": "totals",
        "model_config": {
            "type": "consensus_devig", "devig_method": "power",
            "target_book": "draftkings", "consensus_min_books": 4,
            "context_factors": ["park_type", "season_week", "roof_status"],
            "side_filter": "Over",
        },
        "edge_threshold": 0.01,
    },
    {
        "name": "mlb_april_high_total_fade",
        "thesis": (
            "Games with totals set at 9.5+ in April are overpriced. Books anchor to prior-year "
            "offensive production, but April scoring averages 0.3-0.5 runs/game lower than "
            "season averages due to cold weather, pitcher freshness, and hitter timing. "
            "Unders on high totals in April are systematically +EV."
        ),
        "sport": "baseball_mlb",
        "market_type": "totals",
        "model_config": {
            "type": "consensus_devig", "devig_method": "power",
            "target_book": "draftkings", "consensus_min_books": 4,
            "context_factors": ["game_total_line", "month", "temperature"],
            "side_filter": "Under",
        },
        "edge_threshold": 0.01,
    },
    # === BULLPEN FATIGUE ===
    {
        "name": "mlb_bullpen_fatigue_over_week3plus",
        "thesis": (
            "Starting weeks 3-6, bullpen fatigue accumulates as relievers who threw 2+ "
            "innings in the prior 3 days see significant performance drops. Books are slow "
            "to adjust game totals upward when multiple key relievers are fatigued. Overs "
            "in games where the bullpen has 3+ high-leverage IP in the prior 48h are +EV."
        ),
        "sport": "baseball_mlb",
        "market_type": "totals",
        "model_config": {
            "type": "consensus_devig", "devig_method": "power",
            "target_book": "draftkings", "consensus_min_books": 3,
            "context_factors": ["bullpen_innings_48h", "season_week", "bullpen_era_last_7"],
            "side_filter": "Over",
        },
        "edge_threshold": 0.015,
    },
    {
        "name": "mlb_bullpen_day_game_after_extras",
        "thesis": (
            "After extra-inning games (10+ innings), the next-day bullpen is severely "
            "depleted. If the next game is a day game (less than 18 hours rest), the "
            "bullpen-depleted team gives up significantly more runs. Totals Overs are +EV."
        ),
        "sport": "baseball_mlb",
        "market_type": "totals",
        "model_config": {
            "type": "consensus_devig", "devig_method": "power",
            "target_book": "draftkings", "consensus_min_books": 3,
            "context_factors": ["prev_game_innings", "rest_hours", "bullpen_available"],
            "side_filter": "Over",
        },
        "edge_threshold": 0.015,
    },
    # === COLD WEATHER / PARK FACTORS ===
    {
        "name": "mlb_cold_weather_under_sub55f",
        "thesis": (
            "Games played below 55F see measurably reduced offensive output: ball carries "
            "less, pitchers grip better, hitters have slower bat speed. Books underadjust "
            "totals by 0.3-0.5 runs in cold-weather games. Unders in sub-55F outdoor games "
            "are +EV especially at Wrigley, Fenway, Citi Field, and Yankee Stadium in April."
        ),
        "sport": "baseball_mlb",
        "market_type": "totals",
        "model_config": {
            "type": "consensus_devig", "devig_method": "power",
            "target_book": "draftkings", "consensus_min_books": 4,
            "context_factors": ["temperature", "wind_speed", "park_factor"],
            "side_filter": "Under",
        },
        "edge_threshold": 0.01,
    },
    {
        "name": "mlb_wind_blowing_in_under",
        "thesis": (
            "When wind is blowing in at 10+ mph at an outdoor park, fly ball distance is "
            "reduced and HR probability drops 15-25%. Books adjust totals slightly but not "
            "enough. Unders in high-wind-in games are +EV, strongest at Wrigley Field where "
            "wind direction is a dominant scoring factor."
        ),
        "sport": "baseball_mlb",
        "market_type": "totals",
        "model_config": {
            "type": "consensus_devig", "devig_method": "power",
            "target_book": "draftkings", "consensus_min_books": 3,
            "context_factors": ["wind_speed", "wind_direction", "park_id"],
            "side_filter": "Under",
        },
        "edge_threshold": 0.01,
    },
    {
        "name": "mlb_coors_field_under_public_fade",
        "thesis": (
            "Coors Field totals are consistently set high by books (10.5-12.5), but the "
            "public still hammers Overs creating reverse value. When the consensus of 4+ "
            "books sets the Under fair probability above DraftKings implied, the Under at "
            "Coors is +EV because public money inflates the Over price."
        ),
        "sport": "baseball_mlb",
        "market_type": "totals",
        "model_config": {
            "type": "consensus_devig", "devig_method": "power",
            "target_book": "draftkings", "consensus_min_books": 4,
            "context_factors": ["park_id", "public_betting_pct"],
        },
        "edge_threshold": 0.01,
    },
    # === PUBLIC BETTING FADES ===
    {
        "name": "mlb_public_ml_fade_70pct",
        "thesis": (
            "When 70%+ of public bets are on one MLB moneyline (per Action Network), the "
            "other side is +EV. Public overreaction to team reputation, recent results, and "
            "name-brand pitchers creates systematic mispricing. Fading the heavy public side "
            "on the ML is +EV at 2%+ when public % exceeds 70%."
        ),
        "sport": "baseball_mlb",
        "market_type": "h2h",
        "model_config": {
            "type": "consensus_devig", "devig_method": "power",
            "target_book": "draftkings", "consensus_min_books": 4,
            "context_factors": ["public_bet_pct", "public_money_pct"],
        },
        "edge_threshold": 0.01,
    },
    {
        "name": "mlb_public_total_fade_65pct",
        "thesis": (
            "When 65%+ of public bets are on the Over in an MLB game, the Under is +EV. "
            "The public has a persistent Over bias in baseball driven by offensive highlight "
            "preference. Books shade lines toward the public, creating value on the Under."
        ),
        "sport": "baseball_mlb",
        "market_type": "totals",
        "model_config": {
            "type": "consensus_devig", "devig_method": "power",
            "target_book": "draftkings", "consensus_min_books": 4,
            "context_factors": ["public_bet_pct_over", "public_money_pct"],
            "side_filter": "Under",
        },
        "edge_threshold": 0.01,
    },
    {
        "name": "mlb_big_market_dog_fade",
        "thesis": (
            "Big-market teams (Yankees, Dodgers, Red Sox, Cubs, Mets) as favorites attract "
            "disproportionate public money, inflating their ML price. When these teams are "
            "favored at -150 or higher and public betting exceeds 65%, the underdog ML is "
            "+EV because the line has been pushed past fair value by public action."
        ),
        "sport": "baseball_mlb",
        "market_type": "h2h",
        "model_config": {
            "type": "consensus_devig", "devig_method": "power",
            "target_book": "draftkings", "consensus_min_books": 4,
            "context_factors": ["team_market_size", "public_bet_pct", "favorite_price"],
        },
        "edge_threshold": 0.01,
    },
    # === RUN LINE (SPREADS) ===
    {
        "name": "mlb_runline_heavy_fav_value",
        "thesis": (
            "When the ML favorite is -150 or heavier, the -1.5 run line offers better EV "
            "than the ML because the ML vig is excessive on heavy favorites. Cross-book "
            "consensus identifies when the run line is mispriced relative to the ML-implied "
            "win probability."
        ),
        "sport": "baseball_mlb",
        "market_type": "spreads",
        "model_config": {
            "type": "consensus_devig", "devig_method": "power",
            "target_book": "draftkings", "consensus_min_books": 4,
            "context_factors": ["ml_price", "runline_price", "implied_win_pct"],
        },
        "edge_threshold": 0.01,
    },
    {
        "name": "mlb_runline_dog_plus_1_5_value",
        "thesis": (
            "MLB underdogs on the +1.5 run line cover at a historically high rate (roughly "
            "62-65% of games are decided by 2+ runs). When cross-book consensus sets the +1.5 "
            "dog fair probability 1%+ higher than DraftKings, the dog run line is +EV. "
            "Strongest for good pitching teams with weak offenses."
        ),
        "sport": "baseball_mlb",
        "market_type": "spreads",
        "model_config": {
            "type": "consensus_devig", "devig_method": "power",
            "target_book": "draftkings", "consensus_min_books": 4,
            "context_factors": ["runline_implied", "team_era", "team_ops"],
        },
        "edge_threshold": 0.01,
    },
    # === BATTER/PITCHER PROPS ===
    {
        "name": "mlb_pitcher_hits_allowed_over",
        "thesis": (
            "Pitcher hits-allowed props are less efficient than K props because they depend "
            "more on defense and BABIP luck. Books set hit lines based on season averages, "
            "but opponent contact quality and defensive alignment create exploitable deviations."
        ),
        "sport": "baseball_mlb",
        "market_type": "player_hits_allowed",
        "model_config": {
            "type": "consensus_devig", "devig_method": "power",
            "target_book": "draftkings", "consensus_min_books": 3,
            "context_factors": ["opponent_contact_rate", "defense_quality"],
        },
        "edge_threshold": 0.01,
    },
    {
        "name": "mlb_batter_total_bases_over",
        "thesis": (
            "Batter total bases props have high cross-book variance because they combine "
            "singles, doubles, triples, and HRs into one line. Power hitters in favorable "
            "park/weather conditions (warm, wind out) are underpriced on total bases Overs."
        ),
        "sport": "baseball_mlb",
        "market_type": "player_total_bases",
        "model_config": {
            "type": "consensus_devig", "devig_method": "power",
            "target_book": "draftkings", "consensus_min_books": 3,
            "context_factors": ["park_factor", "temperature", "wind", "batter_iso"],
        },
        "edge_threshold": 0.01,
    },
    # === EARLY SEASON SPECIFIC ===
    {
        "name": "mlb_spring_to_regular_starter_fade",
        "thesis": (
            "Pitchers who dominated spring training (sub-2.00 ERA, 10+ K/9) are overvalued "
            "by books in their first 2-3 regular season starts. Spring training stats are "
            "noisy due to split-squad games, minor leaguers, and reduced effort. Books "
            "overweight recent performance, creating value on the opposing side."
        ),
        "sport": "baseball_mlb",
        "market_type": "h2h",
        "model_config": {
            "type": "consensus_devig", "devig_method": "power",
            "target_book": "draftkings", "consensus_min_books": 4,
            "context_factors": ["spring_training_era", "prior_year_era", "starts_into_season"],
        },
        "edge_threshold": 0.01,
    },
    {
        "name": "mlb_new_team_starter_mispricing",
        "thesis": (
            "Pitchers who changed teams in the offseason are mispriced in their first 5-8 "
            "starts with the new team. Books anchor to prior-year stats without fully adjusting "
            "for new home park, defense, pitch framing, and league familiarity. Cross-book "
            "consensus identifies the correct adjustment faster."
        ),
        "sport": "baseball_mlb",
        "market_type": "h2h",
        "model_config": {
            "type": "consensus_devig", "devig_method": "power",
            "target_book": "draftkings", "consensus_min_books": 4,
            "context_factors": ["pitcher_new_team", "park_factor_change", "league_switch"],
        },
        "edge_threshold": 0.01,
    },
    {
        "name": "mlb_division_opener_dog_value",
        "thesis": (
            "In divisional opening series, underdogs cover the run line and win outright at "
            "a higher rate than non-division games because familiarity compresses talent gaps. "
            "Books overprice division favorites. Division dogs at +130 or longer are +EV."
        ),
        "sport": "baseball_mlb",
        "market_type": "h2h",
        "model_config": {
            "type": "consensus_devig", "devig_method": "power",
            "target_book": "draftkings", "consensus_min_books": 4,
            "context_factors": ["division_matchup", "dog_price", "season_series_game"],
        },
        "edge_threshold": 0.01,
    },
]


async def main():
    from tools.hypothesis import HypothesisManager

    mgr = HypothesisManager()
    await mgr.initialize()

    existing = await mgr.list_hypotheses()
    existing_names = {h["name"] for h in existing}

    created = 0
    for h in MLB_HYPOTHESES:
        if h["name"] in existing_names:
            print(f"  SKIP (exists): {h['name']}")
            continue

        is_prop = h["market_type"].startswith("player_")

        hid = await mgr.create_hypothesis(
            name=h["name"],
            thesis=h["thesis"],
            sport=h["sport"],
            market_type=h["market_type"],
            model_config=h["model_config"],
            edge_threshold=h["edge_threshold"],
            min_sample_size=50,
            notes="MLB season opener focus - generated 2026-03-24",
        )

        if is_prop:
            await mgr.update_status(hid, "paper_trading", "auto:mlb_season_focus")

        created += 1
        tag = "[PAPER]" if is_prop else "[DRAFT]"
        print(f"  {tag} {h['name']}")

    print(f"\nCreated {created} MLB hypotheses")
    await mgr.close()


if __name__ == "__main__":
    asyncio.run(main())
