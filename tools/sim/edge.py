"""Edge comparison helpers: simulated probability vs book line."""

import logging
import math
from typing import Union

import numpy as np

from tools.odds_api import calculate_implied_probability, calculate_ev
from tools.edge_confidence import score_edge

from tools.sim.models import EdgeResult

logger = logging.getLogger("callisto.simulation")


def _make_edge_result(
    sim_prob: float,
    book_prob: float,
    book_odds: int,
    n_sims: int,
    book_names: list[str] = None,
    market: str = "totals",
) -> EdgeResult:
    """Build an EdgeResult with confidence interval and Kelly sizing."""
    edge = sim_prob - book_prob

    # 95% confidence interval using Wilson score interval for proportions
    z = 1.96
    denom = 1 + z * z / n_sims
    center = (sim_prob + z * z / (2 * n_sims)) / denom
    spread = z * math.sqrt((sim_prob * (1 - sim_prob) + z * z / (4 * n_sims)) / n_sims) / denom
    ci_low = max(0.0, round(center - spread, 4))
    ci_high = min(1.0, round(center + spread, 4))
    confidence_interval = (ci_low, ci_high)

    # Edge confidence interval (CI of sim_prob minus book_prob)
    edge_ci_low = ci_low - book_prob
    edge_ci_high = ci_high - book_prob

    # Kelly criterion
    ev_calc = calculate_ev(probability=max(0.001, min(0.999, sim_prob)), american_odds=book_odds)
    kelly = ev_calc["kelly_fraction"]
    kelly_half = round(kelly / 2, 4)

    # EV per $100
    ev_per_100 = ev_calc["expected_value"]

    # Rating
    if edge >= 0.05 and edge_ci_low > 0:
        rating = "STRONG"
    elif edge >= 0.03 and edge_ci_low > -0.01:
        rating = "MODERATE"
    elif edge >= 0.01:
        rating = "THIN"
    else:
        rating = "NO_EDGE"

    # Optional AGP confidence scoring
    confidence = None
    if book_names:
        try:
            confidence = score_edge(
                edge_pct=abs(edge * 100),
                books_compared=len(book_names),
                book_names=book_names,
                market=market,
                cross_method_confirmed=False,
            )
        except Exception as e:
            logger.warning(f"Could not score edge confidence: {e}")

    return EdgeResult(
        simulated_prob=round(sim_prob, 4),
        book_prob=round(book_prob, 4),
        edge=round(edge, 4),
        edge_pct=round(edge * 100, 2),
        confidence_interval=confidence_interval,
        kelly_fraction=round(kelly, 4),
        kelly_half=kelly_half,
        ev_per_100=round(ev_per_100, 2),
        is_positive_ev=ev_per_100 > 0,
        rating=rating,
        confidence=confidence,
    )


# Public alias
make_edge_result = _make_edge_result


def compare_to_book(
    simulated_dist: Union[np.ndarray, list],
    book_line: float,
    book_odds: int,
    side: str = "over",
    book_names: list[str] = None,
    market: str = "totals",
) -> EdgeResult:
    """
    Compare a simulated distribution against a book's line and odds to
    quantify the edge.

    Args:
        simulated_dist: Array of simulated values (scores, margins, stat totals).
        book_line: The book's line (e.g., 224.5 for total, -3.5 for spread).
        book_odds: American odds offered by the book (e.g., -110).
        side: 'over' or 'under' for totals, 'cover' or 'fade' for spreads.
        book_names: List of book names for confidence scoring.
        market: Market type for confidence scoring.

    Returns:
        EdgeResult with edge, confidence interval, Kelly sizing.
    """
    values = np.asarray(simulated_dist, dtype=float)
    n = len(values)

    # Calculate simulated probability
    if side.lower() in ("over", "cover"):
        sim_prob = float(np.mean(values > book_line))
    else:
        sim_prob = float(np.mean(values < book_line))

    book_prob = calculate_implied_probability(book_odds)

    return _make_edge_result(
        sim_prob, book_prob, book_odds, n,
        book_names=book_names,
        market=market,
    )


def compare_to_market(
    sim_result,
    market_odds: dict,
) -> list[dict]:
    """
    Compare simulation fair lines against actual market odds.

    This is where the edge lives -- if our model says the fair spread is -5.2
    and the book has -3.5, there's a 1.7-point edge on the favorite.

    Returns a list of identified edges with EV calculations.
    """
    edges = []
    home = sim_result.home_team
    away = sim_result.away_team

    for bm in market_odds.get("bookmakers", []):
        book_name = bm.get("title", bm.get("key", "unknown"))
        for mkt in bm.get("markets", []):
            for o in mkt.get("outcomes", []):
                name = o.get("name", "")
                price = o.get("price", 0)
                point = o.get("point")
                market_implied = calculate_implied_probability(price)

                # Spread comparison
                if mkt["key"] == "spreads" and point is not None:
                    if name == home:
                        model_prob = sim_result.spread_cover_probs.get(-point, 0.5)
                    else:
                        model_prob = 1.0 - sim_result.spread_cover_probs.get(point, 0.5)

                    edge = model_prob - market_implied
                    if abs(edge) >= 0.02:
                        ev = calculate_ev(probability=model_prob, american_odds=price)
                        edge_res = _make_edge_result(model_prob, market_implied, price,
                                                     sim_result.iterations,
                                                     book_names=[book_name],
                                                     market="spreads")
                        edges.append({
                            "market": "spreads",
                            "team": name,
                            "bookmaker": book_name,
                            "line": point,
                            "price": price,
                            "market_implied": round(market_implied, 4),
                            "model_probability": round(model_prob, 4),
                            "edge": round(edge, 4),
                            "edge_pct": round(edge * 100, 2),
                            "ev": ev,
                            "fair_spread": round(sim_result.fair_spread, 1),
                            "confidence_interval": edge_res.confidence_interval,
                            "kelly": round(edge_res.kelly_fraction, 4),
                            "rating": edge_res.rating,
                            "assessment": (
                                f"Model: {model_prob:.1%} | Market: {market_implied:.1%} | "
                                f"Edge: {edge:+.1%} | "
                                f"{'BET' if ev['is_positive_ev'] else 'PASS'}"
                            ),
                        })

                # Total comparison
                elif mkt["key"] == "totals" and point is not None:
                    total_key = int(point)
                    if name == "Over":
                        model_prob = sim_result.over_probs.get(total_key, 0.5)
                    else:
                        model_prob = 1.0 - sim_result.over_probs.get(total_key, 0.5)

                    edge = model_prob - market_implied
                    if abs(edge) >= 0.02:
                        ev = calculate_ev(probability=model_prob, american_odds=price)
                        edge_res = _make_edge_result(model_prob, market_implied, price,
                                                     sim_result.iterations,
                                                     book_names=[book_name],
                                                     market="totals")
                        edges.append({
                            "market": "totals",
                            "team": name,
                            "bookmaker": book_name,
                            "line": point,
                            "price": price,
                            "market_implied": round(market_implied, 4),
                            "model_probability": round(model_prob, 4),
                            "edge": round(edge, 4),
                            "edge_pct": round(edge * 100, 2),
                            "ev": ev,
                            "fair_total": round(sim_result.fair_total, 1),
                            "confidence_interval": edge_res.confidence_interval,
                            "kelly": round(edge_res.kelly_fraction, 4),
                            "rating": edge_res.rating,
                            "assessment": (
                                f"Model: {model_prob:.1%} | Market: {market_implied:.1%} | "
                                f"Edge: {edge:+.1%} | "
                                f"{'BET' if ev['is_positive_ev'] else 'PASS'}"
                            ),
                        })

                # Moneyline comparison
                elif mkt["key"] == "h2h":
                    if name == home:
                        model_prob = sim_result.home_win_pct
                    else:
                        model_prob = sim_result.away_win_pct

                    edge = model_prob - market_implied
                    if abs(edge) >= 0.02:
                        ev = calculate_ev(probability=model_prob, american_odds=price)
                        edge_res = _make_edge_result(model_prob, market_implied, price,
                                                     sim_result.iterations,
                                                     book_names=[book_name],
                                                     market="h2h")
                        edges.append({
                            "market": "h2h",
                            "team": name,
                            "bookmaker": book_name,
                            "line": None,
                            "price": price,
                            "market_implied": round(market_implied, 4),
                            "model_probability": round(model_prob, 4),
                            "edge": round(edge, 4),
                            "edge_pct": round(edge * 100, 2),
                            "ev": ev,
                            "confidence_interval": edge_res.confidence_interval,
                            "kelly": round(edge_res.kelly_fraction, 4),
                            "rating": edge_res.rating,
                            "assessment": (
                                f"Model: {model_prob:.1%} | Market: {market_implied:.1%} | "
                                f"Edge: {edge:+.1%} | "
                                f"{'BET' if ev['is_positive_ev'] else 'PASS'}"
                            ),
                        })

    edges.sort(key=lambda x: abs(x["edge"]), reverse=True)
    return edges


def compare_poisson_to_market(
    poisson_result: dict,
    market_odds: dict,
    home_team: str,
    away_team: str,
) -> list[dict]:
    """Compare Poisson model output to market odds for soccer/hockey/baseball."""
    edges = []

    for bm in market_odds.get("bookmakers", []):
        book_name = bm.get("title", bm.get("key", "unknown"))
        for mkt in bm.get("markets", []):
            for o in mkt.get("outcomes", []):
                name = o.get("name", "")
                price = o.get("price", 0)
                point = o.get("point")
                market_implied = calculate_implied_probability(price)

                if mkt["key"] == "h2h":
                    if name == home_team:
                        model_prob = poisson_result["home_win"]
                    elif name == away_team:
                        model_prob = poisson_result["away_win"]
                    else:
                        model_prob = poisson_result.get("draw", 0)

                    edge = model_prob - market_implied
                    if abs(edge) >= 0.02:
                        ev = calculate_ev(probability=model_prob, american_odds=price)
                        edges.append({
                            "market": "h2h",
                            "team": name,
                            "bookmaker": book_name,
                            "price": price,
                            "market_implied": round(market_implied, 4),
                            "model_probability": round(model_prob, 4),
                            "edge": round(edge, 4),
                            "ev": ev,
                        })

                elif mkt["key"] == "totals" and point is not None:
                    if name == "Over":
                        model_prob = poisson_result["over_probs"].get(point, 0.5)
                    else:
                        model_prob = 1.0 - poisson_result["over_probs"].get(point, 0.5)

                    edge = model_prob - market_implied
                    if abs(edge) >= 0.02:
                        ev = calculate_ev(probability=model_prob, american_odds=price)
                        edges.append({
                            "market": "totals",
                            "team": name,
                            "bookmaker": book_name,
                            "line": point,
                            "price": price,
                            "market_implied": round(market_implied, 4),
                            "model_probability": round(model_prob, 4),
                            "edge": round(edge, 4),
                            "ev": ev,
                        })

    edges.sort(key=lambda x: abs(x["edge"]), reverse=True)
    return edges
