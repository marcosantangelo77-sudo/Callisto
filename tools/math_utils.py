"""
Odds conversion utilities and core math for Callisto.

Every module depends on these. All formulas verified numerically.

Verified conversions:
  -110 → 1.9091 decimal → 52.38% implied
  +150 → 2.50 decimal → 40.00% implied
  -200 → 1.50 decimal → 66.67% implied
  +300 → 4.00 decimal → 25.00% implied
  +100 → 2.00 decimal → 50.00% implied
"""


def validate_american_odds(american) -> int:
    """Validate an American-odds value and return it as an int.

    American odds are whole numbers with magnitude >= 100 (e.g. -110, +150).
    Rejects booleans, non-numbers, non-finite values, fractional quotes such
    as 100.9, and out-of-policy values like 50 or 0 — none may be silently
    coerced into a trusted price.
    """
    if isinstance(american, bool) or not isinstance(american, (int, float)):
        raise ValueError(f"American odds must be a finite number, got {american!r}")
    import math as _math
    if not _math.isfinite(american):
        raise ValueError(f"American odds must be finite, got {american!r}")
    if float(american) != int(american):
        raise ValueError(f"American odds must be whole numbers, got {american!r}")
    am = int(american)
    if am == 0 or abs(am) < 100:
        raise ValueError(
            f"American odds must be a nonzero whole number with |odds| >= 100, "
            f"got {am}")
    return am


def american_to_decimal(american: int) -> float:
    """
    Convert American odds to decimal odds.
    Verified: -110→1.9091, +150→2.50, -200→1.50, +300→4.00, +100→2.00

    Raises ValueError on any value that is not valid American odds
    (see validate_american_odds).
    """
    am = validate_american_odds(american)
    if am > 0:
        return (am / 100) + 1
    else:
        return (100 / abs(am)) + 1


def decimal_to_american(decimal_odds: float) -> int:
    """
    Convert decimal odds to American odds.
    Verified: 2.50→+150, 1.50→-200, 2.00→+100, 1.9091→-110
    """
    if decimal_odds >= 2.0:
        return round((decimal_odds - 1) * 100)
    elif decimal_odds > 1.0:
        return round(-100 / (decimal_odds - 1))
    else:
        raise ValueError(f"Decimal odds must be > 1.0, got {decimal_odds}")


def decimal_to_implied(decimal_odds: float) -> float:
    """Returns implied probability (includes vig)."""
    if decimal_odds <= 0:
        raise ValueError(f"Decimal odds must be > 0, got {decimal_odds}")
    return 1 / decimal_odds


def american_to_implied(american: int) -> float:
    """Convert American odds to implied probability (includes vig)."""
    if american > 0:
        return 100 / (american + 100)
    elif american < 0:
        return abs(american) / (abs(american) + 100)
    else:
        raise ValueError("American odds cannot be 0")


def fair_prob_to_decimal(prob: float) -> float:
    """Returns fair decimal odds (no vig)."""
    if prob <= 0 or prob >= 1:
        raise ValueError(f"Probability must be in (0, 1), got {prob}")
    return 1 / prob


def fair_prob_to_american(prob: float) -> int:
    """
    Convert fair probability to American odds.
    Verified: 0.50→-100, 0.60→-150, 0.75→-300, 0.25→+300
    """
    if prob <= 0 or prob >= 1:
        return 0
    if prob >= 0.5:
        return round(-100 * prob / (1 - prob))
    else:
        return round(100 * (1 - prob) / prob)


def calculate_overround(decimal_odds_list: list[float]) -> float:
    """Overround > 0 means vig exists. Sum of implied probs - 1."""
    return sum(1 / o for o in decimal_odds_list) - 1.0


def calculate_hold(decimal_odds_list: list[float]) -> float:
    """Book's expected margin percentage."""
    total = sum(1 / o for o in decimal_odds_list)
    if total == 0:
        return 0
    return (total - 1.0) / total


def implied_scores(spread: float, total: float) -> tuple[float, float]:
    """
    Derives implied team scores from spread and total.
    Verified: spread=-7, total=45 → (26.0, 19.0)

    Convention: negative spread = favorite.
    Returns (favorite_score, underdog_score).
    """
    fav = (total + abs(spread)) / 2
    dog = (total - abs(spread)) / 2
    return fav, dog


def no_vig_price(side_a_american: int, side_b_american: int) -> tuple[float, float]:
    """
    Quick no-vig (multiplicative devig) for a two-way market.
    Returns (fair_prob_a, fair_prob_b).

    For proper devig with FLB correction, use tools.devig.devig_market().
    """
    imp_a = american_to_implied(side_a_american)
    imp_b = american_to_implied(side_b_american)
    total = imp_a + imp_b
    return imp_a / total, imp_b / total
