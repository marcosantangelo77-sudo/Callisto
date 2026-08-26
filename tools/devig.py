"""
Devig engine — remove bookmaker vig to find true fair probabilities.

Four methods, each with different strengths:
  multiplicative: Fast, good for low-vig books (Pinnacle). Proportional removal.
  additive:       Equal vig subtraction. Simple but can produce negatives on 3-way.
  power:          PRIMARY for retail books (DK, Fanatics). Corrects favorite-longshot bias.
  shin:           Best theoretical grounding for 3-way markets (soccer).

Auto-selection based on overround and number of outcomes.

All formulas verified numerically — see test_devig.py.
"""

import logging
import math
import sys

from tools.math_utils import (
    american_to_decimal,
    decimal_to_american,
    fair_prob_to_american,
)


def _brentq(f, a, b, xtol=1e-12, maxiter=200):
    """
    Brent's method root finder — pure Python, no scipy dependency.
    Finds x in [a, b] such that f(x) = 0.
    """
    fa, fb = f(a), f(b)
    if fa * fb > 0:
        raise ValueError(f"f(a)={fa} and f(b)={fb} must have opposite signs")
    if abs(fa) < xtol:
        return a
    if abs(fb) < xtol:
        return b

    c, fc = a, fa
    d = b - a
    e = d

    for _ in range(maxiter):
        if fb * fc > 0:
            c, fc = a, fa
            d = e = b - a

        if abs(fc) < abs(fb):
            a, b, c = b, c, b
            fa, fb, fc = fb, fc, fb

        tol = 2 * xtol * max(abs(b), 1.0)
        m = 0.5 * (c - b)

        if abs(m) <= tol or abs(fb) < xtol:
            return b

        if abs(e) >= tol and abs(fa) > abs(fb):
            s = fb / fa
            if abs(a - c) < xtol:
                p = 2 * m * s
                q = 1 - s
            else:
                q = fa / fc
                r = fb / fc
                p = s * (2 * m * q * (q - r) - (b - a) * (r - 1))
                q = (q - 1) * (r - 1) * (s - 1)

            if p > 0:
                q = -q
            else:
                p = -p

            if 2 * p < min(3 * m * q - abs(tol * q), abs(e * q)):
                e = d
                d = p / q
            else:
                d = e = m
        else:
            d = e = m

        a, fa = b, fb
        if abs(d) > tol:
            b += d
        else:
            b += tol if m > 0 else -tol

        fb = f(b)

    return b

import sys

logger = logging.getLogger("callisto.devig")

# Ceiling on the total hold of a book we are willing to devig. Real retail
# sportsbooks hold 2-8%; even the widest prediction-market spreads stay far
# below this. A hold at/above 20 points means the sides cannot belong to one
# live market — a stale snapshot mix or malformed input.
MAX_SANE_OVERROUND = 0.20

# Few-ulp relative slack for the ceiling comparison so the exact
# mathematical boundary stays invalid despite float representation.
_CEILING_ULP_SLACK = 64


def validate_implied_book(implied_probs: list[float]) -> list[float]:
    """Authoritative market-sanity gate on implied probabilities.

    Single shared boundary for every consumer (devig helpers, consensus
    engine, boost evaluator): a book is valid only when every implied
    probability is finite and strictly inside (0, 1), the overround is
    strictly positive (zero-hold and crossed books are rejected), and the
    total hold stays under ``MAX_SANE_OVERROUND``. Anything else raises
    ValueError rather than producing a trustworthy-looking fair value.
    """
    if not implied_probs:
        raise ValueError("Empty odds list")
    for ip in implied_probs:
        if isinstance(ip, bool) or not isinstance(ip, (int, float)) \
                or not math.isfinite(ip):
            raise ValueError(f"Non-finite or non-numeric implied probability: {ip!r}")
        if not 0.0 < ip < 1.0:
            raise ValueError(f"Implied probability out of (0, 1): {ip!r}")
    overround = sum(implied_probs) - 1.0
    if not math.isfinite(overround) or overround <= 1e-9:
        raise ValueError(f"Invalid book: overround {overround:.6f} is not strictly "
                         "positive (zero-hold or crossed book)")
    if overround >= MAX_SANE_OVERROUND * (1.0 - _CEILING_ULP_SLACK * sys.float_info.epsilon):
        raise ValueError(f"Invalid book: overround {overround:.4f} exceeds the "
                         f"{MAX_SANE_OVERROUND:.0%} market-sanity ceiling "
                         "(stale mix or absurd hold)")
    return implied_probs


def _validate_book(odds_list: list[float]) -> list[float]:
    """Shared market-sanity gate for every public devig entry point.

    Validates decimal odds and returns implied probabilities only for a
    book that is finite, well-formed, has a strictly positive overround,
    and stays under the sane-hold ceiling. Anything else raises ValueError
    rather than producing a trustworthy-looking fair probability.
    """
    if not odds_list:
        raise ValueError("Empty odds list")
    for o in odds_list:
        if isinstance(o, bool) or not isinstance(o, (int, float)) \
                or not math.isfinite(o):
            raise ValueError(f"Non-finite or non-numeric odds in list: {o!r}")
        if o <= 1.0:
            raise ValueError(
                f"Decimal odds must exceed 1.0, got {o!r}")
    implied = [1 / o for o in odds_list]
    return validate_implied_book(implied)
    # Market-sanity gate on the book as a whole. A real two-sided book has a
    # small POSITIVE hold (the vig/spread). Zero hold means a no-vig (or
    # self-consistent-arbitrage-free but unpriceable) book and a negative
    # hold means crossed asks — neither may be devigged into an actionable
    # fair probability.
    if not math.isfinite(overround) or overround <= 1e-9:
        raise ValueError(f"Invalid book: overround {overround:.6f} is not strictly "
                         "positive (zero-hold or crossed book)")
    # Boundary-tolerant ceiling: a book at the exact configured hold must be
    # invalid even when float representation nudges `overround` an ulp below
    # MAX_SANE_OVERROUND (e.g. devig_market([1/.6, 1/.6]) sums to
    # 0.19999999999999996). Allow only a few-ulp relative slack so the true
    # mathematical boundary is rejected while healthy below-limit books
    # (which sit far from the ceiling) still pass untouched.
    if overround >= MAX_SANE_OVERROUND * (1.0 - _CEILING_ULP_SLACK * sys.float_info.epsilon):
        raise ValueError(f"Invalid book: overround {overround:.4f} exceeds the "
                         f"{MAX_SANE_OVERROUND:.0%} market-sanity ceiling "
                         "(stale mix or absurd hold)")
    return implied


def multiplicative_devig(odds_list: list[float]) -> list[float]:
    """
    Proportional vig removal. Fast, good for low-vig books (Pinnacle).

    Verified:
      [1.909, 1.909] → [0.50, 0.50]
      [2.0, 2.0] → [0.50, 0.50]

    Limitation: Overestimates longshot probability in lopsided markets.

    Raises ValueError on any invalid book (malformed odds, zero-hold,
    crossed, or excessive overround) — never silently coerces one into
    fair probabilities. Use devig_market for an error-dict audit instead.
    """
    implied = _validate_book(odds_list)
    total = sum(implied)
    return [ip / total for ip in implied]


def additive_devig(odds_list: list[float]) -> list[float]:
    """
    Equal vig subtraction. Simplest method.

    FLAW: Can produce NEGATIVE probabilities on 3-way markets.
    If any result < 0, falls back to multiplicative.

    Raises ValueError on any invalid book, same as the other helpers.
    """
    implied = _validate_book(odds_list)
    overround = sum(implied) - 1.0
    vig_per = overround / len(odds_list)
    result = [ip - vig_per for ip in implied]
    if any(r < 0 for r in result):
        return multiplicative_devig(odds_list)
    return result


def power_devig(odds_list: list[float]) -> tuple[list[float], float]:
    """
    PRIMARY METHOD for retail books (DK, Fanatics).
    Finds exponent k such that implied^k sums to 1.0.
    Corrects for favorite-longshot bias: gives LESS to longshots, MORE to favorites.

    This is the correct correction because longshots are empirically overbet.

    Verified:
      [1.909, 1.909] → [0.50, 0.50], k≈1.072
      [1.20, 5.00]   → [0.822, 0.178], k≈1.073
      Power shifts probability TOWARD favorite, AWAY from longshot.

    Returns: (fair_probs, k)

    Raises ValueError on any invalid book (zero-hold, crossed, excessive
    overround), same as the other helpers.
    """
    implied = _validate_book(odds_list)

    # No "already fair" early return here: a book whose hold is merely
    # *tiny* (e.g. implied values summing to 1.000099) must still be
    # normalised through the solver, not echoed back as raw implied.
    # Exactly-fair input never reaches this point — _validate_book rejects
    # any non-strictly-positive overround.

    def objective(k):
        return sum(ip ** k for ip in implied) - 1.0

    try:
        k = _brentq(objective, 0.0001, 100.0, xtol=1e-12)
        fair = [ip ** k for ip in implied]
        total_fair = sum(fair)
        fair = [p / total_fair for p in fair]
        return fair, k
    except ValueError:
        # Fallback if solver fails
        logger.warning("Power devig solver failed, falling back to multiplicative")
        return multiplicative_devig(odds_list), 1.0


def shin_devig(odds_list: list[float]) -> tuple[list[float], float]:
    """
    Shin's method (1993). Best theoretical grounding for 3-way markets.
    Models market as containing fraction z of informed bettors.

    Formula per outcome:
      fair_i = [sqrt(z² + 4(1-z)·ip_i²/total) - z] / [2(1-z)]

    Solve for z via brentq such that sum(fair_i) = 1.0.

    Verified:
      [1.909, 1.909] → [0.50, 0.50], z≈0.048
      [1.667, 2.400] → [0.592, 0.408], z≈0.017

    Returns: (fair_probs, z)

    Raises ValueError on any invalid book (zero-hold, crossed, excessive
    overround), same as the other helpers.
    """
    implied = _validate_book(odds_list)

    # No "already fair" early return: see power_devig. Exactly-fair input
    # is rejected by _validate_book before we get here.
    total = sum(implied)

    def shin_probs(z):
        probs = []
        for ip in implied:
            val = z ** 2 + 4 * (1 - z) * ip ** 2 / total
            if val < 0:
                val = 0
            probs.append((math.sqrt(val) - z) / (2 * (1 - z)))
        return probs

    def objective(z):
        return sum(shin_probs(z)) - 1.0

    try:
        z = _brentq(objective, 0.0001, 0.5)
        fair = shin_probs(z)
        total_fair = sum(fair)
        fair = [p / total_fair for p in fair]
        return fair, z
    except ValueError:
        logger.warning("Shin devig solver failed, falling back to multiplicative")
        return multiplicative_devig(odds_list), 0.0


def devig_market(
    odds_list: list[float],
    method: str = "auto",
) -> dict:
    """
    Wrapper. Auto-selects method based on overround and number of outcomes.

    AUTO SELECTION (verified: method choice matters <0.3 cents at low vig,
    1-2 cents at retail vig levels):
      overround < 3%: multiplicative (fast, equally accurate at low vig)
      2-way market with overround >= 3%: power (best FLB correction)
      3-way market: shin (best theoretical grounding)
      fallback: power

    Args:
        odds_list: Decimal odds for all outcomes in a market.
        method: 'auto', 'multiplicative', 'additive', 'power', 'shin'

    Returns dict with: method, raw_implied, overround, fair_probabilities,
    fair_decimal_odds, fair_american_odds, solver_param
    """
    try:
        implied = _validate_book(odds_list)
    except ValueError as e:
        overround = None
        try:
            overround = sum(1 / o for o in odds_list
                            if isinstance(o, (int, float)) and not isinstance(o, bool)
                            and o > 0 and math.isfinite(o)) - 1.0
        except (ZeroDivisionError, TypeError):
            pass
        return {"error": str(e),
                "fair_probabilities": [],
                "overround": round(overround, 6)
                             if overround is not None and math.isfinite(overround)
                             else 0.0}
    overround = sum(implied) - 1.0
    param = None

    if method == "auto":
        if overround < 0.03:
            method = "multiplicative"
        elif len(odds_list) == 2:
            method = "power"
        elif len(odds_list) == 3:
            method = "shin"
        else:
            method = "power"

    if method == "multiplicative":
        fair = multiplicative_devig(odds_list)
    elif method == "additive":
        fair = additive_devig(odds_list)
    elif method == "power":
        fair, param = power_devig(odds_list)
    elif method == "shin":
        fair, param = shin_devig(odds_list)
    else:
        raise ValueError(f"Unknown devig method: {method}")

    return {
        "method": method,
        "raw_implied": [round(1 / o, 6) for o in odds_list],
        "overround": round(overround, 6),
        "overround_pct": round(overround * 100, 2),
        "hold_pct": round(overround / sum(1 / o for o in odds_list) * 100, 2),
        "fair_probabilities": [round(p, 6) for p in fair],
        "fair_decimal_odds": [round(1 / p, 4) if p > 0 else 999 for p in fair],
        "fair_american_odds": [fair_prob_to_american(p) for p in fair],
        "solver_param": round(param, 6) if param is not None else None,
    }


def devig_american(
    side_a_american: int,
    side_b_american: int,
    method: str = "auto",
) -> dict:
    """
    Convenience: devig a two-way market from American odds.
    Returns same dict as devig_market plus labeled sides.
    """
    try:
        dec_a = american_to_decimal(side_a_american)
        dec_b = american_to_decimal(side_b_american)
    except (ValueError, TypeError) as e:
        return {"error": f"Invalid American odds: {e}",
                "fair_probabilities": [],
                "side_a": {"american": side_a_american},
                "side_b": {"american": side_b_american}}
    result = devig_market([dec_a, dec_b], method=method)
    if "error" in result:
        # Propagate the invalid-book audit instead of indexing into an
        # empty fair_probabilities list.
        result["side_a"] = {"american": side_a_american}
        result["side_b"] = {"american": side_b_american}
        return result
    result["side_a"] = {
        "american": side_a_american,
        "fair_prob": result["fair_probabilities"][0],
        "fair_american": result["fair_american_odds"][0],
    }
    result["side_b"] = {
        "american": side_b_american,
        "fair_prob": result["fair_probabilities"][1],
        "fair_american": result["fair_american_odds"][1],
    }
    return result


def _devig_pair_via_gate(dec_a: float, dec_b: float,
                         method: str = "auto") -> tuple[float, float]:
    """Route a two-way book through devig_market's market-sanity gate.

    The convenience helpers must not be able to bypass the validation that
    devig_market applies (positive finite overround within the sane ceiling);
    previously they did, returning fair probabilities for crossed/invalid
    paired odds like (-200, -200).
    """
    result = devig_market([dec_a, dec_b], method=method)
    if "error" in result:
        raise ValueError(result["error"])
    fair = result["fair_probabilities"]
    return fair[0], fair[1]


def devig_pinnacle(
    pinnacle_odds_a: int,
    pinnacle_odds_b: int,
) -> tuple[float, float]:
    """
    Quick Pinnacle devig — returns (fair_prob_a, fair_prob_b).
    Uses multiplicative since Pinnacle vig is < 3% — this is the documented
    semantic identity of this helper and does NOT change with the auto
    selector's choice for higher-hold books.

    Raises ValueError on invalid American odds or an invalid book
    (non-positive / excessive overround) instead of returning probabilities.
    """
    dec_a = american_to_decimal(pinnacle_odds_a)
    dec_b = american_to_decimal(pinnacle_odds_b)
    return _devig_pair_via_gate(dec_a, dec_b, method="multiplicative")


def devig_retail(
    retail_odds_a: int,
    retail_odds_b: int,
) -> tuple[float, float]:
    """
    Quick retail book (DK/Fanatics) devig — returns (fair_prob_a, fair_prob_b).
    Uses power method for FLB correction.

    Raises ValueError on invalid American odds or an invalid book
    (non-positive / excessive overround) instead of returning probabilities.
    """
    dec_a = american_to_decimal(retail_odds_a)
    dec_b = american_to_decimal(retail_odds_b)
    # Power is the documented semantic identity of this helper: it must be
    # explicit, never delegated to the auto selector (which would switch to
    # multiplicative on low-vig books and silently drop the FLB correction).
    return _devig_pair_via_gate(dec_a, dec_b, method="power")
