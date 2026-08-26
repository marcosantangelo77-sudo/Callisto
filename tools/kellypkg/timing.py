"""
Optimal bet timing value via line movement EV estimation
(split from tools/kelly.py).
"""

import math

from tools.kellypkg.constants import (
    LINE_MOVEMENT_PROFILES,
    MARKET_CLV_DECAY,
    _DEFAULT_MOVEMENT_PROFILE,
)


def timing_value(
    current_edge: float,
    hours_to_game: float,
    sport: str = "basketball_nba",
    market: str = "spreads",
) -> dict:
    """
    Estimate whether to bet now or wait for a better number.

    The tradeoff:
    - Betting NOW locks in the current edge.
    - WAITING risks the line moving against you (edge shrinks or disappears)
      but also has a chance the line moves in your favor (edge grows).

    We model this as:
        EV(bet_now)  = current_edge
        EV(wait)     = E[edge_at_close | current_edge, time, sport, market]
                     = current_edge * decay + line_improvement_prob * improvement_size
                       - line_worsening_prob * worsening_size

    Key insight: in efficient markets, lines tend to move TOWARD the true price.
    If you have an edge, the expected direction of movement is AGAINST you
    (the market will correct).  But in inefficient sub-markets (props, alts),
    or when sharp action hasn't arrived yet, there can be +EV in waiting.

    Args:
        current_edge:  Current edge as a decimal (e.g., 0.03 for 3%).
        hours_to_game: Hours until game starts.
        sport:         Sport key for line movement profile.
        market:        Market type for CLV decay rate.

    Returns:
        Dict with bet_now_ev, wait_ev, and recommendation.
    """
    profile = LINE_MOVEMENT_PROFILES.get(sport, _DEFAULT_MOVEMENT_PROFILE)
    decay_rate = MARKET_CLV_DECAY.get(market, 1.0)

    # Select volatility regime based on time to game
    if hours_to_game > 24:
        vol = profile["early_vol"]
        regime = "early"
    elif hours_to_game > 4:
        vol = profile["mid_vol"]
        regime = "mid"
    else:
        vol = profile["late_vol"]
        regime = "late"

    steam_prob = profile["steam_prob"]

    # EV of betting now is simply the current edge
    bet_now_ev = current_edge

    # Model expected edge change from waiting:
    # 1. Edge decay: markets correct toward true price.
    #    Expected decay per hour = decay_rate * vol (faster decay = more correction).
    #    Over T hours, remaining edge fraction:
    #    edge_remaining = edge * exp(-decay_rate * vol * T)
    hours_remaining = max(0.01, hours_to_game)
    edge_remaining_frac = math.exp(-decay_rate * vol * hours_remaining)
    expected_edge_after_decay = current_edge * edge_remaining_frac

    # 2. Steam move probability: chance of a sharp move in your direction.
    #    If steam comes in on YOUR side, the line moves toward you and you
    #    lose the edge (line corrects).  But occasionally steam comes on the
    #    OTHER side, increasing your edge briefly.
    #    Net steam effect is slightly negative (market is more likely to
    #    correct than diverge further).
    #    P(favorable steam) = steam_prob * 0.3 (most steam corrects toward true)
    #    P(unfavorable steam) = steam_prob * 0.7
    favorable_steam_boost = steam_prob * 0.3 * vol * hours_remaining
    unfavorable_steam_cost = steam_prob * 0.7 * vol * hours_remaining

    # 3. Stale-line opportunity: in less efficient markets, there's a chance
    #    of a BETTER number appearing at a different book as lines adjust.
    #    This is a function of market inefficiency.
    efficiency = 1.0 - (1.0 / max(decay_rate, 0.01)) * 0.1
    efficiency = max(0.0, min(1.0, efficiency))
    stale_line_bonus = (1.0 - efficiency) * vol * 0.5 * min(hours_remaining, 12.0)

    # Composite wait EV
    wait_ev = (
        expected_edge_after_decay
        + favorable_steam_boost
        - unfavorable_steam_cost
        + stale_line_bonus
    )

    # Uncertainty on wait estimate (wider with more time)
    wait_uncertainty = vol * math.sqrt(hours_remaining)

    # Decision
    ev_diff = wait_ev - bet_now_ev
    if bet_now_ev <= 0:
        recommendation = "NO_BET"
        reasoning = "Current edge is zero or negative. No bet at any timing."
    elif ev_diff > wait_uncertainty * 0.5:
        recommendation = "WAIT"
        reasoning = (
            f"Waiting has +{ev_diff*100:.2f}% higher expected edge. "
            f"Market regime: {regime}. Volatility {vol:.3f}/hr. "
            f"Edge decay to {edge_remaining_frac:.1%} but stale-line "
            f"opportunity outweighs ({stale_line_bonus*100:.2f}% bonus)."
        )
    elif ev_diff < -wait_uncertainty * 0.25:
        recommendation = "BET_NOW"
        reasoning = (
            f"Line is likely to move against you. Edge decays to "
            f"{expected_edge_after_decay*100:.2f}% (from {current_edge*100:.2f}%). "
            f"Market regime: {regime}. Lock it in now."
        )
    else:
        recommendation = "SLIGHT_LEAN_NOW"
        reasoning = (
            f"Close call. Wait EV ({wait_ev*100:.2f}%) vs now EV ({bet_now_ev*100:.2f}%). "
            f"Difference {ev_diff*100:.3f}% is within noise ({wait_uncertainty*100:.2f}%). "
            f"Default to betting now to avoid execution risk."
        )

    return {
        "bet_now_ev": round(bet_now_ev, 5),
        "wait_ev": round(wait_ev, 5),
        "ev_difference": round(ev_diff, 5),
        "recommendation": recommendation,
        "reasoning": reasoning,
        "details": {
            "sport": sport,
            "market": market,
            "hours_to_game": round(hours_to_game, 2),
            "regime": regime,
            "volatility_per_hour": round(vol, 4),
            "edge_decay_rate": round(decay_rate, 3),
            "edge_remaining_fraction": round(edge_remaining_frac, 4),
            "expected_edge_after_decay": round(expected_edge_after_decay, 5),
            "steam_move_probability": round(steam_prob, 3),
            "favorable_steam_boost": round(favorable_steam_boost, 5),
            "unfavorable_steam_cost": round(unfavorable_steam_cost, 5),
            "stale_line_bonus": round(stale_line_bonus, 5),
            "wait_uncertainty": round(wait_uncertainty, 5),
        },
    }
