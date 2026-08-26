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

    # Convert American odds to DECIMAL odds. The tools.devig helpers take
    # decimal odds (they invert to implied internally); previously implied
    # probabilities were passed in here, so a documented call like
    # local_devig([-110, -110]) blew up on implied 0.5238 <= 1.0.
    converted: list[float] = []
    for p in prices:
        if isinstance(p, bool) or not isinstance(p, (int, float)) \
                or not math.isfinite(p):
            raise ValueError(f"Invalid price in list: {p!r}")
        if abs(p) >= 100:
            # American odds — validated and converted through the shared
            # math_utils boundary.
            from tools.math_utils import american_to_decimal
            converted.append(american_to_decimal(int(p)))
        elif p > 1:
            # Already decimal odds
            converted.append(float(p))
        elif 0 < p < 1:
            # Implied probability — convert to decimal for the helpers,
            # then re-validate the book through the authoritative gate.
            converted.append(1.0 / float(p))
        else:
            raise ValueError(f"Price {p!r} is not American odds, decimal "
                             "odds, or an implied probability")

    if method == "power":
        result, _ = power_devig(converted)
        return result
    elif method == "shin":
        result, _ = shin_devig(converted)
        return result
    elif method == "multiplicative":
        return multiplicative_devig(converted)
    else:
        raise ValueError(f"Unknown devig method: {method!r}")


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
    """DEPRECATED — do not call.

    Superseded by tools/edge.assess_edge, which devigs the market quote
    (this function computed fair_prob = RAW implied + edge, baking the
    vig in as phantom edge), applies the MAX_FRACTION_FULL_KELLY cap,
    and records the claim-time price for CLV grading. This stub raises
    so a stale caller fails loudly instead of silently mis-sizing.
    Kept as an error shim so an import does not break mid-upgrade.
    """
    raise NotImplementedError(
        "local_kelly is retired: it computed fair probability from the "
        "raw implied price plus edge with no devig (phantom-edge bug "
        "class). Use tools.edge.assess_edge(calibrated_prob, MarketQuote) "
        "instead — see attic/local_kelly/RESTORE_NOTE.md.")


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
