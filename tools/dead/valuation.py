"""
Core dead number valuation functions: key number value, push probability,
dead number detection, line shopping value, half/quarter key values.
"""

from .data import (
    DEAD_THRESHOLD,
    HALF_QUARTER_KEY,
    KEY_NUMBERS,
    MARGIN_FREQ,
    PUSH_PROB,
    SPORT_ALIASES,
    _get_freq_table,
    _normalize_sport,
)


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------

def get_margin_distribution(sport: str) -> dict[int, float]:
    """
    Get the full margin-of-victory frequency distribution for a sport.

    Returns dict of {margin: percentage} where percentage is out of 100.
    """
    return dict(_get_freq_table(sport))


def is_dead_number(spread: float, sport: str) -> bool:
    """
    Determine if a spread is a "dead number" — a number where the probability
    of landing is negligible and therefore moving across it has minimal value.

    Dead numbers are where line shopping matters LEAST. If you're getting
    -8.5 instead of -9, that's much less valuable than getting -2.5 instead
    of -3 in the NFL.

    Args:
        spread: The spread value (e.g., 3.0, 7.5, -2.5). Sign is ignored.
        sport: Sport identifier.

    Returns:
        True if this is a dead number (low importance), False if it's a key number.
    """
    normalized = _normalize_sport(sport)
    abs_spread = abs(spread)
    threshold = DEAD_THRESHOLD.get(normalized, 0.15)

    importance = key_number_value(spread, sport)
    return importance < threshold


def key_number_value(spread: float, sport: str) -> float:
    """
    Get the importance of a spread number on a 0-1 scale.

    1.0 = maximum importance (e.g., 3 in NFL, 1.5 in MLB)
    0.0 = completely dead number

    This score reflects how much the outcome probability changes when a line
    crosses this number. High-value numbers are where line shopping and
    buying points have the most impact.

    Args:
        spread: The spread value. Sign is ignored; 3 and -3 are equivalent.
        sport: Sport identifier.

    Returns:
        Float from 0.0 to 1.0 indicating importance.
    """
    normalized = _normalize_sport(sport)
    key_table = KEY_NUMBERS.get(normalized)
    if key_table is None:
        raise ValueError(
            f"Unknown sport '{sport}'. Supported: {list(KEY_NUMBERS.keys())}"
        )

    abs_spread = abs(spread)

    # Direct lookup
    if abs_spread in key_table:
        return key_table[abs_spread]

    # For numbers not in the table, interpolate or return a low default
    # Find the two nearest entries
    keys = sorted(key_table.keys())
    if abs_spread < keys[0]:
        return key_table[keys[0]] * 0.5
    if abs_spread > keys[-1]:
        return 0.02  # Extremely high spreads are all dead

    # Linear interpolation between nearest known values
    lower = max(k for k in keys if k <= abs_spread)
    upper = min(k for k in keys if k >= abs_spread)
    if lower == upper:
        return key_table[lower]

    fraction = (abs_spread - lower) / (upper - lower)
    return round(
        key_table[lower] + fraction * (key_table[upper] - key_table[lower]),
        3,
    )


def push_probability(number: float, sport: str) -> float:
    """
    Get the probability that a game lands on exactly this number (causing a push).

    Half-point spreads always return 0.0 (no push possible).

    This is critical for evaluating the real cost of buying half a point.
    Buying from -3 to -2.5 in the NFL costs the push equity at 3 (~14.8%).
    Buying from -8 to -7.5 costs the push equity at 8 (~3.2%).

    Args:
        number: The spread number. Sign is ignored.
        sport: Sport identifier.

    Returns:
        Probability as a float (e.g., 0.148 for NFL 3).
    """
    abs_num = abs(number)

    # Half-point spreads can never push
    if abs_num != int(abs_num):
        return 0.0

    normalized = _normalize_sport(sport)
    push_table = PUSH_PROB.get(normalized)
    if push_table is None:
        raise ValueError(
            f"Unknown sport '{sport}'. Supported: {list(PUSH_PROB.keys())}"
        )

    return push_table.get(abs_num, 0.0)


def line_shopping_value(
    line_a: float,
    line_b: float,
    sport: str,
) -> dict:
    """
    Calculate the actual probability difference between two lines.

    This is the core of dead number theory applied to line shopping.
    Getting -2.5 vs -3 in the NFL is worth ~7.4% of outcomes (half the push
    probability at 3 goes to each side). Getting -8.5 vs -9 is worth ~1%.

    The result includes:
    - prob_difference: the actual win probability change between the lines
    - cents_value: approximate value in cents (per dollar wagered)
    - recommendation: whether this difference is worth pursuing

    Args:
        line_a: First line (e.g., -2.5)
        line_b: Second line (e.g., -3.0)
        sport: Sport identifier.

    Returns:
        Dict with probability difference analysis.
    """
    normalized = _normalize_sport(sport)
    freq_table = _get_freq_table(sport)

    # Normalize so line_a is the better line (closer to 0 or more points for the dog)
    # For spreads: higher (less negative) is better for the favorite
    # -2.5 is better than -3 for a favorite; +3 is better than +2.5 for a dog
    better = max(line_a, line_b)
    worse = min(line_a, line_b)

    # Calculate the sum of margin frequencies between the two lines
    # This is the probability mass between the two numbers
    abs_better = abs(better)
    abs_worse = abs(worse)

    # Ensure correct ordering for the calculation
    low = min(abs_better, abs_worse)
    high = max(abs_better, abs_worse)

    prob_diff = 0.0
    crossed_key_numbers = []

    # Walk through each integer margin between the lines
    for margin in range(int(low) + 1 if low == int(low) else int(low) + 1,
                        int(high) + 1):
        margin_freq = freq_table.get(margin, 0.0) / 100.0
        prob_diff += margin_freq
        if margin_freq > 0.02:
            crossed_key_numbers.append({
                "number": margin,
                "frequency": margin_freq,
            })

    # Handle the boundary numbers
    # If one line is on a whole number, half the push probability goes to each side
    if low == int(low) and low > 0:
        push_prob = freq_table.get(int(low), 0.0) / 100.0
        # The .5 line doesn't push; the whole number does.
        # Moving from X to X-.5 gains half the push probability
        if better != int(better) and worse == int(worse):
            # e.g., -2.5 to -3: the -2.5 bettor avoids the push at 3
            # They win half the pushes (the other half become losses)
            prob_diff += push_prob / 2
        elif better == int(better) and worse != int(worse):
            # e.g., -3 to -3.5: crossing the whole number
            prob_diff += push_prob / 2

    if high == int(high) and high > 0 and high != low:
        push_prob = freq_table.get(int(high), 0.0) / 100.0
        if better == int(better) or worse == int(worse):
            prob_diff += push_prob / 2

    # Convert probability difference to cents of EV
    # At standard -110 juice, 1% of probability = ~1.91 cents per dollar
    # (because you're risking 110 to win 100, so each % point of edge =
    #  0.01 * (100/110) * 110 = 1.0, but accounting for juice it's ~1.91)
    cents_per_pct = 1.91
    cents_value = prob_diff * 100 * cents_per_pct

    # Recommendation based on magnitude
    if prob_diff >= 0.05:
        recommendation = "CRITICAL — massive probability shift, always shop for this"
    elif prob_diff >= 0.03:
        recommendation = "HIGH VALUE — significant edge, worth shopping multiple books"
    elif prob_diff >= 0.015:
        recommendation = "MODERATE — meaningful difference, shop if convenient"
    elif prob_diff >= 0.005:
        recommendation = "LOW — small edge, only matters for high volume bettors"
    else:
        recommendation = "NEGLIGIBLE — dead number territory, don't spend time on this"

    return {
        "line_a": line_a,
        "line_b": line_b,
        "better_line": better,
        "worse_line": worse,
        "sport": normalized,
        "prob_difference": round(prob_diff, 4),
        "prob_difference_pct": round(prob_diff * 100, 2),
        "cents_value": round(cents_value, 2),
        "crossed_key_numbers": crossed_key_numbers,
        "recommendation": recommendation,
    }


def half_quarter_key_value(
    spread: float,
    sport: str,
    period: str = "1H",
) -> float:
    """
    Get the key number importance for half or quarter lines.

    Half and quarter distributions differ from full game:
    - First half NFL: 3 is still the biggest key number but 7 is less dominant
    - First quarter: extremely low-scoring, 0 is a massive key "number"
    - NBA halves: tighter margins, less separation between key and non-key

    Args:
        spread: The spread value. Sign is ignored.
        sport: Sport identifier.
        period: "1H" for first half, "1Q" for first quarter.

    Returns:
        Float from 0.0 to 1.0 indicating importance.
    """
    normalized = _normalize_sport(sport)
    sport_periods = HALF_QUARTER_KEY.get(normalized)
    if sport_periods is None:
        # Fall back to full-game key numbers with a dampening factor
        # (half/quarter numbers are generally less predictable)
        return key_number_value(spread, sport) * 0.7

    period_table = sport_periods.get(period.upper())
    if period_table is None:
        return key_number_value(spread, sport) * 0.7

    abs_spread = abs(spread)
    if abs_spread in period_table:
        return period_table[abs_spread]

    # Interpolate
    keys = sorted(period_table.keys())
    if abs_spread < keys[0]:
        return period_table[keys[0]] * 0.5
    if abs_spread > keys[-1]:
        return 0.02

    lower = max(k for k in keys if k <= abs_spread)
    upper = min(k for k in keys if k >= abs_spread)
    if lower == upper:
        return period_table[lower]

    fraction = (abs_spread - lower) / (upper - lower)
    return round(
        period_table[lower] + fraction * (period_table[upper] - period_table[lower]),
        3,
    )
