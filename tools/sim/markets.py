"""Targeted spread / total market simulation against book lines."""

import numpy as np

from tools.odds_api import calculate_implied_probability

from tools.sim.constants import DEFAULT_ITERATIONS, SPORT_DEFAULTS
from tools.sim.game import simulate_game
from tools.sim.edge import _make_edge_result


def _infer_powers(game_odds: dict, sport: str, home_power, away_power):
    """Derive power ratings from consensus book lines if not provided."""
    if home_power is not None and away_power is not None:
        return home_power, away_power

    spreads_found = []
    totals_found = []
    for bm in game_odds.get("bookmakers", []):
        for mkt in bm.get("markets", []):
            if mkt["key"] == "spreads":
                for o in mkt.get("outcomes", []):
                    if o.get("point") is not None:
                        spreads_found.append(o["point"])
            if mkt["key"] == "totals":
                for o in mkt.get("outcomes", []):
                    if o.get("point") is not None:
                        totals_found.append(o["point"])

    consensus_spread = np.median(spreads_found) if spreads_found else 0.0
    consensus_total = np.median(totals_found) if totals_found else (
        SPORT_DEFAULTS.get(sport, {}).get("mean_total", 100)
    )
    # spread = home - away, total = home + away
    # => home = (total + spread) / 2, away = (total - spread) / 2
    inferred_home = (consensus_total + consensus_spread) / 2.0
    inferred_away = (consensus_total - consensus_spread) / 2.0
    home_power = home_power if home_power is not None else inferred_home
    away_power = away_power if away_power is not None else inferred_away
    return home_power, away_power


def simulate_spread(
    game_odds: dict,
    sport: str = "basketball_nba",
    n_sims: int = DEFAULT_ITERATIONS,
    home_power: float = None,
    away_power: float = None,
) -> dict:
    """
    Simulate thousands of outcomes and compare the spread probability
    against what the book is implying.

    Args:
        game_odds: Dict with 'bookmakers' list (Odds API format) plus optional
                   'home_power'/'away_power' overrides.
        sport: Sport key.
        n_sims: Number of simulations.
        home_power: Home team expected score / goals. If None, inferred from
                    the book total and spread.
        away_power: Away team expected score / goals. If None, inferred.

    Returns:
        Dict with simulated_prob, book_prob, edge, confidence_interval for
        every bookmaker spread found.
    """
    home_power, away_power = _infer_powers(game_odds, sport, home_power, away_power)

    sim = simulate_game(home_power, away_power, sport=sport, n_sims=n_sims,
                        home_advantage=0.0)

    results = []
    for bm in game_odds.get("bookmakers", []):
        for mkt in bm.get("markets", []):
            if mkt["key"] != "spreads":
                continue
            for o in mkt.get("outcomes", []):
                point = o.get("point")
                price = o.get("price", -110)
                name = o.get("name", "")
                if point is None:
                    continue

                # Model: P(home margin > -point) for home side
                # For away side: P(home margin < point)
                is_home_side = (point < 0) or (name == sim.home_team)
                if is_home_side:
                    lookup = -point
                    sim_prob = sim.spread_cover_probs.get(lookup, None)
                    if sim_prob is None:
                        sim_prob = float(np.mean(
                            np.array(sim.home_scores) - np.array(sim.away_scores) > lookup
                        ))
                else:
                    lookup = point
                    cover_prob = sim.spread_cover_probs.get(lookup, None)
                    if cover_prob is None:
                        cover_prob = float(np.mean(
                            np.array(sim.home_scores) - np.array(sim.away_scores) > lookup
                        ))
                    sim_prob = 1.0 - cover_prob

                book_prob = calculate_implied_probability(price)
                edge = sim_prob - book_prob
                edge_result = _make_edge_result(sim_prob, book_prob, price, n_sims)

                results.append({
                    "bookmaker": bm.get("title", bm.get("key", "unknown")),
                    "team": name,
                    "line": point,
                    "price": price,
                    "simulated_prob": round(sim_prob, 4),
                    "book_prob": round(book_prob, 4),
                    "edge": round(edge, 4),
                    "edge_pct": round(edge * 100, 2),
                    "confidence_interval": edge_result.confidence_interval,
                    "kelly": round(edge_result.kelly_fraction, 4),
                    "ev_per_100": round(edge_result.ev_per_100, 2),
                    "rating": edge_result.rating,
                })

    return {
        "sport": sport,
        "n_sims": n_sims,
        "fair_spread": round(sim.fair_spread, 2),
        "fair_total": round(sim.fair_total, 2),
        "edges": sorted(results, key=lambda x: abs(x["edge"]), reverse=True),
    }


def simulate_total(
    game_odds: dict,
    sport: str = "basketball_nba",
    n_sims: int = DEFAULT_ITERATIONS,
    home_power: float = None,
    away_power: float = None,
) -> dict:
    """
    Simulate game totals and compare over/under probabilities to book lines.

    Args:
        game_odds: Dict with 'bookmakers' list (Odds API format).
        sport: Sport key.
        n_sims: Number of simulations.
        home_power: Home team expected score / goals.
        away_power: Away team expected score / goals.

    Returns:
        Dict with simulated over/under probabilities vs book for each line.
    """
    home_power, away_power = _infer_powers(game_odds, sport, home_power, away_power)

    sim = simulate_game(home_power, away_power, sport=sport, n_sims=n_sims,
                        home_advantage=0.0)

    sim_totals = np.array(sim.home_scores) + np.array(sim.away_scores)

    results = []
    for bm in game_odds.get("bookmakers", []):
        for mkt in bm.get("markets", []):
            if mkt["key"] != "totals":
                continue
            for o in mkt.get("outcomes", []):
                point = o.get("point")
                price = o.get("price", -110)
                name = o.get("name", "")
                if point is None:
                    continue

                if name == "Over":
                    sim_prob = float(np.mean(sim_totals > point))
                else:
                    sim_prob = float(np.mean(sim_totals < point))

                book_prob = calculate_implied_probability(price)
                edge = sim_prob - book_prob
                edge_result = _make_edge_result(sim_prob, book_prob, price, n_sims)

                results.append({
                    "bookmaker": bm.get("title", bm.get("key", "unknown")),
                    "side": name,
                    "line": point,
                    "price": price,
                    "simulated_prob": round(sim_prob, 4),
                    "book_prob": round(book_prob, 4),
                    "edge": round(edge, 4),
                    "edge_pct": round(edge * 100, 2),
                    "confidence_interval": edge_result.confidence_interval,
                    "kelly": round(edge_result.kelly_fraction, 4),
                    "ev_per_100": round(edge_result.ev_per_100, 2),
                    "rating": edge_result.rating,
                })

    return {
        "sport": sport,
        "n_sims": n_sims,
        "fair_total": round(sim.fair_total, 2),
        "total_std": round(float(np.std(sim_totals, ddof=1)), 2),
        "edges": sorted(results, key=lambda x: abs(x["edge"]), reverse=True),
    }
