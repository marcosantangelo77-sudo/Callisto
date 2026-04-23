"""
Local compute helpers — run math locally instead of burning Claude tokens.

These functions wrap existing pure-Python implementations in tools/devig.py
and tools/ev.py so that any caller can use them directly without importing
the underlying modules.

All functions are async for consistency with the rest of the codebase,
but the actual computation is synchronous (instant).

Usage:
    from tools.local_compute import local_devig, local_significance_test

    probs = await local_devig([-110, -110], method="power")
    result = await local_significance_test(events)
"""

import math
import logging
from typing import Optional

logger = logging.getLogger("callisto.local_compute")


async def local_devig(prices: list[float], method: str = "power") -> list[float]:
    """
    Run devig calculation locally — no Claude tokens needed.

    Args:
        prices: List of American odds (e.g., [-110, -110]) or implied
                probabilities (e.g., [0.5238, 0.5238]).
        method: "power" (default, most accurate), "multiplicative",
                or "shin" (accounts for insider info).

    Returns:
        List of fair probabilities (sum to ~1.0).
    """
    from tools.devig import power_devig, multiplicative_devig, shin_devig

    # Convert American odds to implied probabilities if needed
    converted = []
    for p in prices:
        if abs(p) > 1:
            # American odds
            converted.append(_american_to_implied(p))
        else:
            converted.append(p)

    if method == "power":
        result, _ = power_devig(converted)
        return result
    elif method == "shin":
        result, _ = shin_devig(converted)
        return result
    else:
        return multiplicative_devig(converted)


async def local_significance_test(events: list[dict]) -> dict:
    """
    Run statistical significance test locally — no Claude tokens needed.

    Performs a one-sample binomial/z-test on backtest event outcomes.

    Args:
        events: List of dicts with at least:
            - "edge": float (the predicted edge)
            - "won": bool or int (1/0, whether the bet won)
            Optional:
            - "odds": float (American odds for EV calculation)

    Returns:
        Dict with:
            - "n": sample size
            - "wins": win count
            - "hit_rate": observed win rate
            - "expected_rate": expected rate from edges (null hypothesis)
            - "z_score": z-statistic
            - "p_value": two-tailed p-value
            - "significant": bool (p < 0.05)
            - "mean_edge": average edge across events
    """
    if not events:
        return {
            "n": 0, "wins": 0, "hit_rate": 0, "expected_rate": 0,
            "z_score": 0, "p_value": 1.0, "significant": False, "mean_edge": 0,
        }

    n = len(events)
    wins = sum(1 for e in events if e.get("won"))
    hit_rate = wins / n if n > 0 else 0

    edges = [e.get("edge", 0) for e in events]
    mean_edge = sum(edges) / len(edges) if edges else 0

    # Expected rate: if edge is 0, expect 50% (fair coin); otherwise
    # use the average implied probability from edges
    # edge = (fair_prob * decimal_odds) - 1, so fair_prob ~ 0.5 + edge/2 for 2-way
    expected_rate = 0.5 + (mean_edge / 2) if abs(mean_edge) < 1 else 0.5

    # Z-test for proportion
    if 0 < expected_rate < 1 and n > 0:
        se = math.sqrt(expected_rate * (1 - expected_rate) / n)
        z_score = (hit_rate - expected_rate) / se if se > 0 else 0
    else:
        z_score = 0

    # Two-tailed p-value from z-score (approximation without scipy)
    p_value = _z_to_p(abs(z_score))

    return {
        "n": n,
        "wins": wins,
        "hit_rate": round(hit_rate, 4),
        "expected_rate": round(expected_rate, 4),
        "z_score": round(z_score, 3),
        "p_value": round(p_value, 4),
        "significant": p_value < 0.05,
        "mean_edge": round(mean_edge, 4),
    }


async def local_kelly(edge: float, odds: float, bankroll: float,
                       fraction: float = 0.25) -> dict:
    """
    Calculate Kelly criterion bet size locally.

    Args:
        edge: Estimated edge as decimal (e.g., 0.03 for 3%)
        odds: American odds (e.g., -110, +150)
        bankroll: Current bankroll in dollars
        fraction: Kelly fraction (default 0.25 = quarter Kelly)

    Returns:
        Dict with kelly_fraction, recommended_stake, full_kelly_pct
    """
    decimal_odds = _american_to_decimal(odds)
    if decimal_odds <= 1:
        return {"kelly_fraction": 0, "recommended_stake": 0, "full_kelly_pct": 0}

    implied_prob = 1 / decimal_odds
    fair_prob = implied_prob + edge

    # Kelly: f* = (bp - q) / b where b = decimal_odds - 1, p = fair_prob, q = 1 - p
    b = decimal_odds - 1
    p = min(max(fair_prob, 0.01), 0.99)
    q = 1 - p

    full_kelly = (b * p - q) / b if b > 0 else 0
    full_kelly = max(0, full_kelly)  # Never negative

    fractional = full_kelly * fraction
    stake = round(bankroll * fractional, 2)

    return {
        "kelly_fraction": round(fractional, 4),
        "full_kelly_pct": round(full_kelly * 100, 2),
        "recommended_stake": stake,
        "edge": round(edge, 4),
        "decimal_odds": round(decimal_odds, 3),
    }


# ── Helpers ──

def _american_to_implied(odds: float) -> float:
    """Convert American odds to implied probability."""
    if odds >= 100:
        return 100 / (odds + 100)
    elif odds <= -100:
        return abs(odds) / (abs(odds) + 100)
    return 0.5


def _american_to_decimal(odds: float) -> float:
    """Convert American odds to decimal odds."""
    if odds >= 100:
        return (odds / 100) + 1
    elif odds <= -100:
        return (100 / abs(odds)) + 1
    return 2.0


def _z_to_p(z: float) -> float:
    """
    Approximate two-tailed p-value from z-score.
    Uses Abramowitz & Stegun approximation of the normal CDF.
    """
    if z < 0:
        z = -z
    if z > 8:
        return 0.0

    # Abramowitz & Stegun 26.2.17
    b0 = 0.2316419
    b1 = 0.319381530
    b2 = -0.356563782
    b3 = 1.781477937
    b4 = -1.821255978
    b5 = 1.330274429

    t = 1.0 / (1.0 + b0 * z)
    phi = (1.0 / math.sqrt(2 * math.pi)) * math.exp(-0.5 * z * z)
    one_tail = phi * (b1 * t + b2 * t**2 + b3 * t**3 + b4 * t**4 + b5 * t**5)

    return 2 * one_tail  # Two-tailed
