"""Sports tool dispatcher (extracted verbatim from orchestrator.py)."""

from tools.odds_api import (
    get_odds as odds_get_odds,
    get_scores as odds_get_scores,
    get_event_odds as odds_get_event_odds,
    get_alternate_lines as odds_get_alternate_lines,
    get_player_props as odds_get_player_props,
    calculate_ev,
)
from tools.edge_scanner import full_edge_scan
from tools.contextual_data import get_injuries, get_scoreboard, get_team_roster
from tools.line_gaps import scan_line_gaps, scan_prop_gaps
from tools.prop_scanner import scan_props_ev
from tools.clv_tracker import CLVTracker
from tools.edge_confidence import score_edge
from tools.boost_evaluator import (
    devig_multiplicative,
    evaluate_fixed_boost,
    evaluate_percentage_boost,
    evaluate_purchased_boost,
    evaluate_free_bet,
)
from tools.cache_manager import get_cache_manager
from tools.devig import devig_american
from tools.sim import sim_from_odds, player_prop_sim
from tools.ev import evaluate_edge
from tools.sizing import bet_size_american, best_price
from tools.sgp import evaluate_sgp


async def _sports_tool_dispatch(name: str, arguments: dict):
    """Sports tool implementations (moved verbatim from the old
    Orchestrator._execute_tool chain). Module-level so the sports
    DomainPlugin can own them without importing Orchestrator."""
    if name == "get_odds":
        return await odds_get_odds(
            sport=arguments.get("sport", "basketball_ncaab"),
            regions=arguments.get("regions", "us"),
            markets=arguments.get("markets", "h2h,spreads,totals"),
            odds_format=arguments.get("odds_format", "american"),
        )
    if name == "get_scores":
        return await odds_get_scores(
            sport=arguments.get("sport", "basketball_ncaab"),
            days_from=arguments.get("days_from", 1),
        )
    if name == "get_event_odds":
        return await odds_get_event_odds(
            sport=arguments.get("sport", ""),
            event_id=arguments.get("event_id", ""),
            regions=arguments.get("regions", "us"),
            markets=arguments.get("markets", "h2h,spreads,totals"),
            odds_format=arguments.get("odds_format", "american"),
        )
    if name == "calculate_ev":
        return calculate_ev(
            probability=float(arguments.get("probability", 0.5)),
            american_odds=int(arguments.get("american_odds", -110)),
            stake=float(arguments.get("stake", 100)),
        )
    if name == "get_alternate_lines":
        return await odds_get_alternate_lines(
            sport=arguments.get("sport", ""),
            event_id=arguments.get("event_id", ""),
            regions=arguments.get("regions", "us"),
        )
    if name == "get_player_props":
        return await odds_get_player_props(
            sport=arguments.get("sport", ""),
            event_id=arguments.get("event_id", ""),
            prop_markets=arguments.get("prop_markets", "player_points,player_rebounds,player_assists"),
        )
    if name == "edge_scan":
        sport = arguments.get("sport", "basketball_ncaab")
        snapshot = await odds_get_odds(sport=sport)
        report = full_edge_scan(snapshot)
        # Attach AGP confidence scores to each detected edge
        for market_key in ["cross_book_spreads", "cross_book_h2h", "cross_book_totals"]:
            for edge in report.get(market_key, []):
                conf = score_edge(
                    edge_pct=round(edge.get("implied_range", 0) * 100, 2),
                    books_compared=edge.get("book_count", edge.get("num_bookmakers", 1)),
                    book_names=[edge.get("best_line", {}).get("bookmaker", "")],
                    market=market_key.replace("cross_book_", ""),
                    has_sharp_book=edge.get("sharp_consensus") is not None,
                )
                edge["confidence"] = {
                    "score": conf.score, "tier": conf.tier,
                    "source_class": conf.source_class, "reasoning": conf.reasoning,
                }
        return report
    if name == "get_injuries":
        return await get_injuries(sport=arguments.get("sport", "basketball_ncaab"))
    if name == "get_team_roster":
        return await get_team_roster(
            sport=arguments.get("sport", "basketball_nba"),
            team_id=arguments.get("team_id", ""),
        )
    if name == "get_scoreboard":
        return await get_scoreboard(sport=arguments.get("sport", "basketball_ncaab"))
    if name == "scan_line_gaps":
        sport = arguments.get("sport", "basketball_ncaab")
        event_id = arguments.get("event_id", "")
        alt_data = await odds_get_alternate_lines(sport=sport, event_id=event_id)
        if alt_data.get("error"):
            return alt_data
        gaps = scan_line_gaps(
            alt_data.get("bookmakers", []),
            market_key="alternate_spreads",
        )
        prop_data = await odds_get_player_props(sport=sport, event_id=event_id)
        prop_gaps = []
        if not prop_data.get("error"):
            prop_gaps = scan_prop_gaps(prop_data)
        return {"line_gaps": gaps, "prop_gaps": prop_gaps}
    if name == "evaluate_boost":
        bt = arguments.get("boost_type", "fixed")
        fair_prob = arguments.get("fair_probability")
        # Auto-devig if fair_probability not given but odds_for/against provided
        if fair_prob is None:
            odds_for = arguments.get("odds_for", -110)
            odds_against = arguments.get("odds_against", -110)
            fair_prob, _ = devig_multiplicative(odds_for, odds_against)
        if bt == "fixed":
            return evaluate_fixed_boost(
                boosted_odds=int(arguments.get("boosted_odds", -110)),
                fair_probability=float(fair_prob),
                max_stake=float(arguments.get("max_stake", 100)),
                description=arguments.get("description", ""),
            )
        elif bt == "percentage":
            return evaluate_percentage_boost(
                boost_pct=float(arguments.get("boost_pct", 20)),
                base_odds=int(arguments.get("base_odds", -110)),
                fair_probability=float(fair_prob),
                max_stake=float(arguments.get("max_stake", 100)),
                description=arguments.get("description", ""),
            )
        elif bt == "free_bet":
            return evaluate_free_bet(
                free_bet_amount=float(arguments.get("max_stake", 100)),
                bet_odds=int(arguments.get("boosted_odds", arguments.get("base_odds", 200))),
                fair_probability=float(fair_prob),
                description=arguments.get("description", ""),
            )
        elif bt == "purchased":
            return evaluate_purchased_boost(
                boost_cost=float(arguments.get("boost_cost", 0)),
                boost_pct=float(arguments.get("boost_pct", 20)),
                base_odds=int(arguments.get("base_odds", -110)),
                fair_probability=float(fair_prob),
                max_stake=float(arguments.get("max_stake", 100)),
                description=arguments.get("description", ""),
                book=arguments.get("book", "Fanatics"),
            )
        return {"error": f"Unknown boost type: {bt}"}
    if name == "scan_props_ev":
        return await scan_props_ev(
            sport=arguments.get("sport", "basketball_nba"),
            event_id=arguments.get("event_id", ""),
            target_book=arguments.get("target_book", "draftkings"),
            edge_threshold=float(arguments.get("edge_threshold", 0.015)),
        )
    # ── New framework tools ──
    if name == "devig_market":
        return devig_american(
            side_a_american=int(arguments.get("side_a_american", -110)),
            side_b_american=int(arguments.get("side_b_american", -110)),
            method=arguments.get("method", "auto"),
        )
    if name == "simulate_game":
        return sim_from_odds(
            spread=float(arguments.get("spread", 0)),
            total=float(arguments.get("total", 0)),
            sport=arguments.get("sport", "nba"),
        )
    if name == "simulate_prop":
        return player_prop_sim(
            stat_per_min=float(arguments.get("stat_per_min", 0.5)),
            stat_per_min_std=float(arguments.get("stat_per_min_std", 0.15)),
            projected_minutes=float(arguments.get("projected_minutes", 30)),
            minutes_std=float(arguments.get("minutes_std", 4.0)),
            pace_factor=float(arguments.get("pace_factor", 1.0)),
            defense_factor=float(arguments.get("defense_factor", 1.0)),
            usage_factor=float(arguments.get("usage_factor", 1.0)),
            stat_name=arguments.get("stat_name", "points"),
        )
    if name == "evaluate_edge":
        return evaluate_edge(
            fair_prob=float(arguments.get("fair_prob", 0.5)),
            book_odds_american=int(arguments.get("book_odds_american", -110)),
            confidence=arguments.get("confidence", "medium"),
            p_push=float(arguments.get("p_push", 0.0)),
        )
    if name == "bet_size":
        return bet_size_american(
            bankroll=float(arguments.get("bankroll", 1000)),
            fair_prob=float(arguments.get("fair_prob", 0.5)),
            book_odds_american=int(arguments.get("book_odds_american", -110)),
            confidence=arguments.get("confidence", "medium"),
            max_wager=arguments.get("max_wager"),
            p_push=float(arguments.get("p_push", 0.0)),
        )
    if name == "best_price":
        return best_price(
            dk_odds_american=int(arguments.get("dk_odds_american", -110)),
            fan_odds_american=int(arguments.get("fan_odds_american", -110)),
        )
    if name == "evaluate_sgp":
        return evaluate_sgp(
            legs=arguments.get("legs", []),
            sport=arguments.get("sport", "nba"),
            book_sgp_decimal=float(arguments.get("book_sgp_decimal", 3.0)),
        )
    if name == "query_warm_cache":
        cm = get_cache_manager()
        kwargs = {}
        for k in ["days_back", "sport", "book", "n"]:
            if k in arguments and arguments[k] is not None:
                kwargs[k] = arguments[k]
        return await cm.get_warm_data(
            query_type=arguments.get("query_type", "clv_summary"),
            **kwargs,
        )
    if name == "record_bet":
        tracker = CLVTracker()
        await tracker.initialize()
        try:
            bet_id = await tracker.record_bet(
                sport=arguments.get("sport", ""),
                game_description=arguments.get("game_description", ""),
                team=arguments.get("team", ""),
                market=arguments.get("market", ""),
                bookmaker=arguments.get("bookmaker", ""),
                placement_odds=int(arguments.get("placement_odds", -110)),
                placement_point=arguments.get("placement_point"),
                stake=float(arguments.get("stake", 100)),
                event_id=arguments.get("event_id", ""),
                edge_estimate=arguments.get("edge_estimate"),
                notes=arguments.get("notes", ""),
            )
            return {"bet_id": bet_id, "status": "recorded", "message": f"Bet #{bet_id} recorded for CLV tracking"}
        finally:
            await tracker.close()

