"""Generate multi-book hypotheses derived from live cross-book edge analysis."""
import json
import requests

API = "http://localhost:8420"

hypotheses = [
    {
        "name": "fanduel_spread_overreaction_ncaab",
        "thesis": (
            "FanDuel sets NCAAB spreads 0.5-1.5 points wider than sharp consensus "
            "(LowVig/Pinnacle) in 50%+ of games. When FanDuel spread diverges by 1+ "
            "point from sharp consensus, the sharp side covers at 55%+ rate. This "
            "represents a systematic pricing bias where FanDuel adjusts spreads "
            "reactively to public money rather than true probability."
        ),
        "sport": "basketball_ncaab",
        "market_type": "spreads",
        "hypothesis_model_config": {
            "type": "consensus_devig",
            "devig_method": "power",
            "target_book": "fanduel",
            "consensus_min_books": 5,
            "context_factors": ["sharp_consensus_divergence", "fanduel_spread_offset"],
        },
        "edge_threshold": 0.03,
        "notes": (
            "Derived from live cross-book analysis: FanDuel worst-line on 6/12 "
            "NCAAB spread markets. Targets systematic retail-sharp divergence."
        ),
    },
    {
        "name": "sharp_book_opening_line_fade_nba_totals",
        "thesis": (
            "When Pinnacle/LowVig open a total and it moves 1+ point within 4 hours "
            "while DraftKings/FanDuel lag, the direction of the sharp move predicts "
            "the final closing line 70%+ of the time. Betting the sharp-moved side "
            "on the lagging retail book captures 3%+ CLV on average."
        ),
        "sport": "basketball_nba",
        "market_type": "totals",
        "hypothesis_model_config": {
            "type": "consensus_devig",
            "devig_method": "power",
            "target_book": "draftkings",
            "consensus_min_books": 4,
            "context_factors": [
                "sharp_opening_move",
                "retail_lag_hours",
                "line_movement_magnitude",
            ],
        },
        "edge_threshold": 0.03,
        "notes": "Sharp-retail line lag exploitation. Requires tracking opening lines across books with timestamps.",
    },
    {
        "name": "cross_book_implied_range_outlier_spreads",
        "thesis": (
            "When the implied probability range across 7+ books exceeds 4% "
            "(vs average 3%) on a spread market, there is genuine pricing disagreement. "
            "The sharp consensus side (Pinnacle/LowVig/Circa weighted) wins at 56%+ "
            "rate in these high-disagreement spots because sharp books have better "
            "models for unusual game contexts."
        ),
        "sport": "basketball_nba",
        "market_type": "spreads",
        "hypothesis_model_config": {
            "type": "consensus_devig",
            "devig_method": "power",
            "target_book": "draftkings",
            "consensus_min_books": 7,
            "context_factors": [
                "implied_range_percentile",
                "sharp_weight_ratio",
                "book_count",
            ],
        },
        "edge_threshold": 0.025,
        "notes": "Derived from live data: avg implied range 2.7-3.3%. Games above 4% represent structurally mispriced markets.",
    },
    {
        "name": "retail_book_price_spread_arbitrage_ncaab",
        "thesis": (
            "When the price spread (juice differential) between best and worst book "
            "exceeds 20 cents on an NCAAB spread, the best-priced side covers at "
            "54%+ rate because the wide juice indicates the worst-priced book is "
            "overexposed on one side and the market-clearing price favors the other."
        ),
        "sport": "basketball_ncaab",
        "market_type": "spreads",
        "hypothesis_model_config": {
            "type": "consensus_devig",
            "devig_method": "power",
            "target_book": "draftkings",
            "consensus_min_books": 5,
            "context_factors": [
                "price_spread_cents",
                "book_count",
                "juice_asymmetry",
            ],
        },
        "edge_threshold": 0.02,
        "notes": "Live data shows NCAAB avg price_spread=31 cents. High price spreads correlate with mispricing.",
    },
    {
        "name": "mlb_run_line_sharp_retail_divergence",
        "thesis": (
            "When Pinnacle run line (-1.5) differs from DraftKings/FanDuel by 15+ "
            "cents in juice, the Pinnacle-favored side covers the run line at 55%+ "
            "rate. MLB run lines have thinner markets and higher variance in book "
            "pricing, creating exploitable divergence especially for road favorites."
        ),
        "sport": "baseball_mlb",
        "market_type": "spreads",
        "hypothesis_model_config": {
            "type": "consensus_devig",
            "devig_method": "power",
            "target_book": "draftkings",
            "consensus_min_books": 4,
            "context_factors": [
                "pinnacle_juice_delta",
                "home_away",
                "favorite_status",
            ],
        },
        "edge_threshold": 0.03,
        "notes": "MLB coverage gap - only 6 totals hypotheses exist. Run lines have wider book disagreement than moneylines.",
    },
    {
        "name": "nhl_puckline_sharp_vs_soft_edge",
        "thesis": (
            "NHL puck line (-1.5) pricing diverges 3%+ implied probability between "
            "sharp (Pinnacle/Circa) and soft (DraftKings/FanDuel) books 40%+ of the "
            "time. When divergence exceeds 3%, the sharp side wins at 56%+ rate "
            "because puck line pricing requires accurate goal distribution modeling "
            "that retail books shortcut."
        ),
        "sport": "icehockey_nhl",
        "market_type": "spreads",
        "hypothesis_model_config": {
            "type": "consensus_devig",
            "devig_method": "power",
            "target_book": "draftkings",
            "consensus_min_books": 4,
            "context_factors": [
                "sharp_soft_implied_delta",
                "favorite_magnitude",
            ],
        },
        "edge_threshold": 0.03,
        "notes": "NHL spreads undercovered. Puck lines are structurally mispriced because goal distributions are non-normal.",
    },
    {
        "name": "ncaab_tournament_line_timing_edge",
        "thesis": (
            "During March Madness, lines posted within 30 minutes of game end (for "
            "next round) show 5%+ implied probability divergence across books because "
            "handicapping is rushed. The sharp consensus within 2 hours of posting "
            "predicts the closing line direction 72%+ of the time, creating a "
            "timing-based edge on retail books."
        ),
        "sport": "basketball_ncaab",
        "market_type": "spreads",
        "hypothesis_model_config": {
            "type": "consensus_devig",
            "devig_method": "power",
            "target_book": "draftkings",
            "consensus_min_books": 5,
            "context_factors": [
                "time_since_game_end",
                "tournament_round",
                "sharp_consensus_direction",
            ],
        },
        "edge_threshold": 0.04,
        "notes": "Seasonal/March Madness specific. Captures rushed line-setting during tournament fast turnarounds.",
    },
    {
        "name": "nba_moneyline_offshore_outlier_fade",
        "thesis": (
            "When offshore book moneyline odds diverge 10%+ implied probability from "
            "the sharp consensus (Pinnacle/Circa mean), the sharp side wins at 60%+ "
            "rate. Offshore books price aggressively to attract action and "
            "systematically misprice underdogs by 5-15% implied probability."
        ),
        "sport": "basketball_nba",
        "market_type": "h2h",
        "hypothesis_model_config": {
            "type": "consensus_devig",
            "devig_method": "power",
            "target_book": "mybookie",
            "consensus_min_books": 5,
            "context_factors": [
                "offshore_implied_delta",
                "underdog_status",
            ],
        },
        "edge_threshold": 0.05,
        "notes": "Live EV data shows MyBookie +650 vs sharp consensus ~43% true prob. Massive outlier pricing detected.",
    },
    {
        "name": "tier_weighted_consensus_model_nba",
        "thesis": (
            "A tier-weighted consensus model (sharp books 3x weight, retail 1x) "
            "produces closing line value positive bets at 55%+ rate when applied "
            "against the worst-priced retail book. Pure book-count consensus "
            "understates sharp information; weighting Pinnacle/Circa/LowVig "
            "recovers 2%+ additional edge."
        ),
        "sport": "basketball_nba",
        "market_type": "spreads",
        "hypothesis_model_config": {
            "type": "consensus_devig",
            "devig_method": "power",
            "target_book": "draftkings",
            "consensus_min_books": 6,
            "context_factors": [
                "tier_weighted_consensus",
                "sharp_book_count",
                "retail_book_count",
            ],
        },
        "edge_threshold": 0.02,
        "notes": "Meta-hypothesis: tests whether tier-weighting improves on equal-weight consensus devig. Books table has tier labels.",
    },
    {
        "name": "mlb_total_sharp_steam_capture",
        "thesis": (
            "When 3+ sharp books move an MLB total in the same direction within 2 "
            "hours of opening, the final closing line moves further in that direction "
            "75%+ of the time. Betting the steam direction on the slowest-moving "
            "retail book captures 4%+ CLV because MLB totals markets are less liquid "
            "and slower to adjust."
        ),
        "sport": "baseball_mlb",
        "market_type": "totals",
        "hypothesis_model_config": {
            "type": "consensus_devig",
            "devig_method": "power",
            "target_book": "draftkings",
            "consensus_min_books": 4,
            "context_factors": [
                "sharp_steam_count",
                "hours_since_open",
                "retail_lag",
            ],
        },
        "edge_threshold": 0.03,
        "notes": "MLB totals severely undercovered (6 existing). Steam moves in thin MLB markets have higher signal-to-noise.",
    },
]

submitted = 0
failed = 0
for h in hypotheses:
    try:
        resp = requests.post(f"{API}/hypothesis", json=h, timeout=10)
        if resp.status_code == 200:
            result = resp.json()
            print(f"OK: {h['name']} -> {result.get('hypothesis_id', '?')}")
            submitted += 1
        else:
            print(f"FAIL ({resp.status_code}): {h['name']} -> {resp.text[:200]}")
            failed += 1
    except Exception as e:
        print(f"ERROR: {h['name']} -> {e}")
        failed += 1

print(f"\nSubmitted: {submitted}, Failed: {failed}")
