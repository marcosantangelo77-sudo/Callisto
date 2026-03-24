"""
Dead number theory and margin-of-victory analysis — exploit key number math.

Every spread is NOT created equal. In the NFL, the difference between -2.5
and -3 is worth ~14.8% of outcomes (games decided by exactly 3). The
difference between -5.5 and -6 is worth ~4.5%. Sportsbooks know this,
the public mostly doesn't, and the pricing reflects it.

Key number theory:
1. Certain margins of victory occur FAR more frequently than others
2. Lines crossing these key numbers have disproportionate value
3. Lines NOT on key numbers ("dead numbers") move cheaply — exploit this
4. Books shade lines TOWARD key numbers to attract public money
5. Half/quarter numbers follow different distributions than full game

This module provides:
- Historical margin-of-victory frequency tables (NFL, NBA, NCAAB, NCAAF, MLB, NHL)
- Dead number detection (is this spread worth fighting for?)
- Key number valuation (how much is crossing this number worth?)
- Line shopping value calculator (actual probability delta between two lines)
- Push probability tables (exact landing frequency by number)
- Public shading detection (is the book pulling the line toward a key number?)
- Half/quarter key numbers (different distributions for live and partial-game lines)

All data is hardcoded from historical distributions. No API calls needed.
"""

import logging
from typing import Optional

logger = logging.getLogger("callisto.dead_numbers")


# ---------------------------------------------------------------------------
# Historical margin-of-victory frequency distributions
# ---------------------------------------------------------------------------
# Source: compiled from NFL/NBA/NCAAF/NCAAB/MLB/NHL historical data.
# Values represent the percentage of all games decided by exactly that margin.
# Negative margins are symmetric (margin of 3 applies to both favorites and dogs).

# NFL: ~16,000+ regular-season games, 2000-2024
# Key numbers dominate because of the scoring structure (3-pt FG, 7-pt TD)
NFL_MARGIN_FREQ: dict[int, float] = {
    0: 0.3,    # Ties (extremely rare post-OT rules)
    1: 4.0,
    2: 3.3,
    3: 14.8,   # KEY: field goal margin — the most common margin in football
    4: 3.8,
    5: 2.8,
    6: 4.5,    # KEY: two field goals or TD without extra point scenario
    7: 9.1,    # KEY: touchdown + PAT — second most common
    8: 3.2,
    9: 2.0,
    10: 4.8,   # KEY: TD + FG
    11: 2.5,
    12: 1.5,
    13: 2.8,
    14: 4.2,   # KEY: two touchdowns
    15: 1.2,
    16: 2.0,
    17: 3.5,   # KEY: two TDs + FG
    18: 1.3,
    19: 1.0,
    20: 2.2,
    21: 2.8,   # KEY: three touchdowns
    22: 1.0,
    23: 1.2,
    24: 2.0,   # KEY: three TDs + FG
    25: 0.9,
    26: 0.6,
    27: 1.3,
    28: 1.5,   # KEY: four touchdowns
    29: 0.5,
    30: 0.6,
    31: 0.7,
    32: 0.4,
    33: 0.4,
    34: 0.4,
    35: 0.5,
}

# NBA: ~25,000+ regular-season games, 2000-2024
# More normally distributed — scoring is continuous (free throws), no structural
# clustering. Key numbers matter less but 1-5 point margins are most common.
NBA_MARGIN_FREQ: dict[int, float] = {
    0: 0.0,    # No ties (OT eliminates)
    1: 5.8,
    2: 5.5,
    3: 5.3,
    4: 5.1,
    5: 5.0,
    6: 4.7,
    7: 4.5,
    8: 4.2,
    9: 3.9,
    10: 3.7,
    11: 3.5,
    12: 3.2,
    13: 2.9,
    14: 2.7,
    15: 2.5,
    16: 2.3,
    17: 2.1,
    18: 1.9,
    19: 1.7,
    20: 1.5,
    21: 1.4,
    22: 1.2,
    23: 1.1,
    24: 1.0,
    25: 0.9,
    26: 0.8,
    27: 0.7,
    28: 0.6,
    29: 0.5,
    30: 0.4,
    31: 0.35,
    32: 0.3,
    33: 0.25,
    34: 0.2,
    35: 0.18,
}

# NCAAF: similar to NFL but slightly more variance due to talent disparity
# Two-point conversions attempted more often in college, slightly changes key #s
NCAAF_MARGIN_FREQ: dict[int, float] = {
    0: 0.3,
    1: 3.8,
    2: 3.2,
    3: 12.5,   # KEY: still dominant but less so than NFL (more 2-pt attempts)
    4: 4.0,
    5: 2.9,
    6: 4.3,
    7: 8.5,    # KEY: still second most common
    8: 3.5,    # Slightly higher than NFL (more 2-pt conversions)
    9: 2.2,
    10: 4.5,   # KEY
    11: 2.5,
    12: 1.8,
    13: 2.6,
    14: 4.0,   # KEY
    15: 1.3,
    16: 2.1,
    17: 3.2,   # KEY
    18: 1.5,
    19: 1.2,
    20: 2.3,
    21: 2.5,   # KEY
    22: 1.2,
    23: 1.3,
    24: 1.8,
    25: 1.1,
    26: 0.8,
    27: 1.2,
    28: 1.5,   # KEY
    29: 0.6,
    30: 0.7,
    31: 0.8,
    32: 0.5,
    33: 0.5,
    34: 0.4,
    35: 0.6,
}

# NCAAB: similar to NBA but wider variance — blowouts more common
NCAAB_MARGIN_FREQ: dict[int, float] = {
    0: 0.0,
    1: 5.2,
    2: 5.0,
    3: 4.8,
    4: 4.6,
    5: 4.5,
    6: 4.3,
    7: 4.1,
    8: 3.9,
    9: 3.7,
    10: 3.4,
    11: 3.2,
    12: 3.0,
    13: 2.8,
    14: 2.6,
    15: 2.4,
    16: 2.2,
    17: 2.0,
    18: 1.8,
    19: 1.7,
    20: 1.5,
    21: 1.4,
    22: 1.3,
    23: 1.2,
    24: 1.1,
    25: 1.0,
    26: 0.9,
    27: 0.85,
    28: 0.8,
    29: 0.7,
    30: 0.65,
    31: 0.6,
    32: 0.5,
    33: 0.45,
    34: 0.4,
    35: 0.35,
}

# MLB: low-scoring, run-based margins. 1-run games are dominant.
# Extra innings are included (they break ties, so 0 margin is impossible).
MLB_MARGIN_FREQ: dict[int, float] = {
    0: 0.0,    # No ties
    1: 28.0,   # KEY: nearly 1 in 3 games decided by a single run
    2: 20.0,   # KEY
    3: 15.0,   # KEY
    4: 10.5,
    5: 7.5,
    6: 5.5,
    7: 4.0,
    8: 3.0,
    9: 2.2,
    10: 1.6,
    11: 1.1,
    12: 0.7,
    13: 0.4,
    14: 0.25,
    15: 0.15,
}

# NHL: very similar to MLB — low-scoring, 1-goal games dominate.
# Includes OT/SO results.
NHL_MARGIN_FREQ: dict[int, float] = {
    0: 0.0,    # No ties (shootout)
    1: 30.0,   # KEY: nearly 1 in 3 decided by one goal
    2: 25.0,   # KEY
    3: 18.0,   # KEY
    4: 11.0,
    5: 7.0,
    6: 4.5,
    7: 2.5,
    8: 1.2,
    9: 0.5,
    10: 0.2,
}

# Master lookup
MARGIN_FREQ: dict[str, dict[int, float]] = {
    "NFL": NFL_MARGIN_FREQ,
    "NBA": NBA_MARGIN_FREQ,
    "NCAAF": NCAAF_MARGIN_FREQ,
    "NCAAB": NCAAB_MARGIN_FREQ,
    "MLB": MLB_MARGIN_FREQ,
    "NHL": NHL_MARGIN_FREQ,
}

# Sport key normalization (from Odds API sport keys to our keys)
SPORT_ALIASES: dict[str, str] = {
    "americanfootball_nfl": "NFL",
    "americanfootball_ncaaf": "NCAAF",
    "basketball_nba": "NBA",
    "basketball_ncaab": "NCAAB",
    "baseball_mlb": "MLB",
    "icehockey_nhl": "NHL",
    "nfl": "NFL",
    "nba": "NBA",
    "ncaaf": "NCAAF",
    "ncaab": "NCAAB",
    "mlb": "MLB",
    "nhl": "NHL",
}


def _normalize_sport(sport: str) -> str:
    """Normalize sport string to our canonical key."""
    return SPORT_ALIASES.get(sport.lower(), sport.upper())


def _get_freq_table(sport: str) -> dict[int, float]:
    """Get the margin-of-victory frequency table for a sport."""
    normalized = _normalize_sport(sport)
    table = MARGIN_FREQ.get(normalized)
    if table is None:
        raise ValueError(
            f"Unknown sport '{sport}'. Supported: {list(MARGIN_FREQ.keys())}"
        )
    return table


# ---------------------------------------------------------------------------
# Key number definitions — which spreads MATTER in each sport
# ---------------------------------------------------------------------------
# Scale: 0.0 = dead number (irrelevant), 1.0 = max importance
# These are the full-game key number importance scores.

NFL_KEY_NUMBERS: dict[float, float] = {
    1.0: 0.35,   2.0: 0.25,   2.5: 0.55,   3.0: 1.00,   3.5: 0.60,
    4.0: 0.30,   4.5: 0.15,   5.0: 0.12,   5.5: 0.10,   6.0: 0.40,
    6.5: 0.50,   7.0: 0.85,   7.5: 0.45,   8.0: 0.20,   8.5: 0.12,
    9.0: 0.10,   9.5: 0.12,   10.0: 0.45,  10.5: 0.25,  11.0: 0.15,
    11.5: 0.08,  12.0: 0.08,  12.5: 0.10,  13.0: 0.20,  13.5: 0.25,
    14.0: 0.40,  14.5: 0.20,  15.0: 0.07,  15.5: 0.06,  16.0: 0.12,
    16.5: 0.15,  17.0: 0.30,  17.5: 0.18,  18.0: 0.08,  19.0: 0.06,
    20.0: 0.15,  20.5: 0.12,  21.0: 0.22,  24.0: 0.15,  27.0: 0.10,
    28.0: 0.12,
}

NBA_KEY_NUMBERS: dict[float, float] = {
    # NBA key numbers are less dramatic — margins are quasi-continuous.
    # Intentional fouling at end of games makes 1-4 point margins slightly more
    # common, and round numbers (5, 10) are modest key numbers for totals.
    1.0: 0.45,   1.5: 0.40,   2.0: 0.42,   2.5: 0.38,   3.0: 0.40,
    3.5: 0.35,   4.0: 0.38,   4.5: 0.33,   5.0: 0.37,   5.5: 0.32,
    6.0: 0.34,   6.5: 0.30,   7.0: 0.32,   7.5: 0.28,   8.0: 0.28,
    8.5: 0.25,   9.0: 0.25,   9.5: 0.22,   10.0: 0.23,  10.5: 0.20,
    11.0: 0.20,  11.5: 0.18,  12.0: 0.18,  12.5: 0.16,  13.0: 0.15,
    13.5: 0.14,  14.0: 0.14,  14.5: 0.12,  15.0: 0.12,
}

NCAAF_KEY_NUMBERS: dict[float, float] = {
    1.0: 0.30,   2.0: 0.22,   2.5: 0.48,   3.0: 0.90,   3.5: 0.55,
    4.0: 0.32,   4.5: 0.14,   5.0: 0.12,   5.5: 0.10,   6.0: 0.38,
    6.5: 0.45,   7.0: 0.78,   7.5: 0.42,   8.0: 0.25,   8.5: 0.12,
    9.0: 0.10,   9.5: 0.12,   10.0: 0.42,  10.5: 0.22,  11.0: 0.14,
    13.0: 0.18,  13.5: 0.22,  14.0: 0.38,  14.5: 0.18,  16.0: 0.12,
    17.0: 0.28,  17.5: 0.16,  20.0: 0.14,  21.0: 0.20,  24.0: 0.14,
    28.0: 0.12,
}

NCAAB_KEY_NUMBERS: dict[float, float] = {
    # Similar to NBA but slightly flatter — less intentional fouling strategy
    1.0: 0.40,   1.5: 0.37,   2.0: 0.38,   2.5: 0.35,   3.0: 0.36,
    3.5: 0.33,   4.0: 0.34,   4.5: 0.31,   5.0: 0.33,   5.5: 0.30,
    6.0: 0.30,   6.5: 0.27,   7.0: 0.28,   7.5: 0.25,   8.0: 0.25,
    8.5: 0.22,   9.0: 0.22,   9.5: 0.20,   10.0: 0.20,  10.5: 0.18,
    11.0: 0.17,  11.5: 0.15,  12.0: 0.15,  12.5: 0.14,  13.0: 0.13,
    13.5: 0.12,  14.0: 0.12,  14.5: 0.11,  15.0: 0.10,
}

MLB_KEY_NUMBERS: dict[float, float] = {
    # Run lines are typically -1.5 / +1.5. Key numbers are 1 and 2 runs.
    1.0: 0.95,   1.5: 1.00,   2.0: 0.70,   2.5: 0.55,   3.0: 0.50,
    3.5: 0.35,   4.0: 0.30,   4.5: 0.22,   5.0: 0.18,   5.5: 0.12,
    6.0: 0.10,
}

NHL_KEY_NUMBERS: dict[float, float] = {
    # Puck lines are typically -1.5 / +1.5. 1-goal games are dominant.
    1.0: 0.95,   1.5: 1.00,   2.0: 0.75,   2.5: 0.60,   3.0: 0.55,
    3.5: 0.35,   4.0: 0.25,   4.5: 0.15,   5.0: 0.10,
}

KEY_NUMBERS: dict[str, dict[float, float]] = {
    "NFL": NFL_KEY_NUMBERS,
    "NBA": NBA_KEY_NUMBERS,
    "NCAAF": NCAAF_KEY_NUMBERS,
    "NCAAB": NCAAB_KEY_NUMBERS,
    "MLB": MLB_KEY_NUMBERS,
    "NHL": NHL_KEY_NUMBERS,
}


# ---------------------------------------------------------------------------
# Push probability tables — probability of landing on exactly this number
# ---------------------------------------------------------------------------
# These are derived from the margin frequency tables but expressed as the
# probability that a spread of exactly N results in a push.
# For half-point spreads, push probability is 0 by definition.

NFL_PUSH_PROB: dict[float, float] = {
    1.0: 0.040,   2.0: 0.033,   3.0: 0.148,   4.0: 0.038,   5.0: 0.028,
    6.0: 0.045,   7.0: 0.091,   8.0: 0.032,   9.0: 0.020,   10.0: 0.048,
    11.0: 0.025,  12.0: 0.015,  13.0: 0.028,  14.0: 0.042,  15.0: 0.012,
    16.0: 0.020,  17.0: 0.035,  18.0: 0.013,  19.0: 0.010,  20.0: 0.022,
    21.0: 0.028,  24.0: 0.020,  28.0: 0.015,
}

NBA_PUSH_PROB: dict[float, float] = {
    1.0: 0.058,   2.0: 0.055,   3.0: 0.053,   4.0: 0.051,   5.0: 0.050,
    6.0: 0.047,   7.0: 0.045,   8.0: 0.042,   9.0: 0.039,   10.0: 0.037,
    11.0: 0.035,  12.0: 0.032,  13.0: 0.029,  14.0: 0.027,  15.0: 0.025,
}

NCAAF_PUSH_PROB: dict[float, float] = {
    1.0: 0.038,   2.0: 0.032,   3.0: 0.125,   4.0: 0.040,   5.0: 0.029,
    6.0: 0.043,   7.0: 0.085,   8.0: 0.035,   9.0: 0.022,   10.0: 0.045,
    11.0: 0.025,  12.0: 0.018,  13.0: 0.026,  14.0: 0.040,  15.0: 0.013,
    16.0: 0.021,  17.0: 0.032,  18.0: 0.015,  20.0: 0.023,  21.0: 0.025,
    24.0: 0.018,  28.0: 0.015,
}

NCAAB_PUSH_PROB: dict[float, float] = {
    1.0: 0.052,   2.0: 0.050,   3.0: 0.048,   4.0: 0.046,   5.0: 0.045,
    6.0: 0.043,   7.0: 0.041,   8.0: 0.039,   9.0: 0.037,   10.0: 0.034,
    11.0: 0.032,  12.0: 0.030,  13.0: 0.028,  14.0: 0.026,  15.0: 0.024,
}

MLB_PUSH_PROB: dict[float, float] = {
    1.0: 0.280,   2.0: 0.200,   3.0: 0.150,   4.0: 0.105,   5.0: 0.075,
    6.0: 0.055,   7.0: 0.040,   8.0: 0.030,   9.0: 0.022,   10.0: 0.016,
}

NHL_PUSH_PROB: dict[float, float] = {
    1.0: 0.300,   2.0: 0.250,   3.0: 0.180,   4.0: 0.110,   5.0: 0.070,
    6.0: 0.045,   7.0: 0.025,   8.0: 0.012,
}

PUSH_PROB: dict[str, dict[float, float]] = {
    "NFL": NFL_PUSH_PROB,
    "NBA": NBA_PUSH_PROB,
    "NCAAF": NCAAF_PUSH_PROB,
    "NCAAB": NCAAB_PUSH_PROB,
    "MLB": MLB_PUSH_PROB,
    "NHL": NHL_PUSH_PROB,
}


# ---------------------------------------------------------------------------
# Half-game and quarter-game key numbers
# ---------------------------------------------------------------------------
# Different distributions than full game — shorter sample, less scoring,
# different strategic dynamics (clock management less relevant in 1H, etc.)

NFL_FIRST_HALF_KEY: dict[float, float] = {
    # First half NFL: 3 is still king but 7 is less dominant (fewer TDs in 1H)
    # Field goals more common relative to TDs in first half
    0.0: 0.15,   1.0: 0.30,   2.0: 0.20,   3.0: 0.85,   3.5: 0.50,
    4.0: 0.25,   5.0: 0.10,   6.0: 0.35,   6.5: 0.40,   7.0: 0.65,
    7.5: 0.35,   8.0: 0.15,   9.0: 0.10,   10.0: 0.35,  10.5: 0.20,
    13.0: 0.15,  13.5: 0.18,  14.0: 0.30,  17.0: 0.20,
}

NFL_FIRST_QUARTER_KEY: dict[float, float] = {
    # First quarter: very low scoring, 0-0 is extremely common (~25% of games)
    # 3 and 7 are still key but with massive 0 frequency
    0.0: 0.50,   3.0: 0.65,   3.5: 0.35,   6.0: 0.20,   7.0: 0.45,
    7.5: 0.25,   10.0: 0.20,  14.0: 0.15,
}

NBA_FIRST_HALF_KEY: dict[float, float] = {
    # NBA first half: slightly wider distribution than full game
    # Intentional fouling not a factor so margins are more organic
    1.0: 0.35,   1.5: 0.32,   2.0: 0.33,   2.5: 0.30,   3.0: 0.30,
    3.5: 0.28,   4.0: 0.28,   4.5: 0.26,   5.0: 0.26,   5.5: 0.24,
    6.0: 0.24,   6.5: 0.22,   7.0: 0.22,   7.5: 0.20,   8.0: 0.20,
    8.5: 0.18,   9.0: 0.18,   9.5: 0.16,   10.0: 0.16,
}

NBA_FIRST_QUARTER_KEY: dict[float, float] = {
    # NBA first quarter: very tight margins
    1.0: 0.30,   2.0: 0.28,   3.0: 0.27,   4.0: 0.25,   5.0: 0.24,
    6.0: 0.22,   7.0: 0.20,   8.0: 0.18,   9.0: 0.16,   10.0: 0.14,
}

HALF_QUARTER_KEY: dict[str, dict[str, dict[float, float]]] = {
    "NFL": {"1H": NFL_FIRST_HALF_KEY, "1Q": NFL_FIRST_QUARTER_KEY},
    "NBA": {"1H": NBA_FIRST_HALF_KEY, "1Q": NBA_FIRST_QUARTER_KEY},
    # NCAAF/NCAAB use same structure as NFL/NBA with slight adjustments
    "NCAAF": {"1H": NFL_FIRST_HALF_KEY, "1Q": NFL_FIRST_QUARTER_KEY},
    "NCAAB": {"1H": NBA_FIRST_HALF_KEY, "1Q": NBA_FIRST_QUARTER_KEY},
}


# ---------------------------------------------------------------------------
# Dead number thresholds — below this importance score, a number is "dead"
# ---------------------------------------------------------------------------
DEAD_THRESHOLD: dict[str, float] = {
    "NFL": 0.12,    # NFL has clear dead numbers (5, 9, 12, 15, 19, etc.)
    "NBA": 0.15,    # NBA dead numbers are less meaningful
    "NCAAF": 0.12,
    "NCAAB": 0.15,
    "MLB": 0.15,
    "NHL": 0.15,
}


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


# ---------------------------------------------------------------------------
# Public shading detection
# ---------------------------------------------------------------------------

# Typical shading behavior: books move lines toward key numbers to attract
# public money. The public loves betting on -3 and -7 in the NFL because
# those "feel right." Books exploit this by shading a -2.5 true line to
# -3, knowing the public will pile on at -3.
#
# The shade is usually 0.5 to 1.5 points and creates value on the OTHER side.

# Shading gravity: how much each key number "pulls" lines toward it.
# Higher gravity = books are more likely to shade toward this number.
NFL_SHADE_GRAVITY: dict[float, float] = {
    3.0: 1.0,    # Maximum gravity — books love posting -3
    7.0: 0.85,   # Second strongest pull
    6.0: 0.30,   # Moderate pull
    6.5: 0.25,
    10.0: 0.40,
    14.0: 0.30,
    17.0: 0.20,
    21.0: 0.15,
}

NBA_SHADE_GRAVITY: dict[float, float] = {
    # NBA shading is less pronounced because key numbers are less dramatic.
    # Books still shade toward round numbers for totals.
    1.0: 0.15,   2.0: 0.12,   3.0: 0.12,   5.0: 0.15,   7.0: 0.10,
    10.0: 0.12,
}

SHADE_GRAVITY: dict[str, dict[float, float]] = {
    "NFL": NFL_SHADE_GRAVITY,
    "NBA": NBA_SHADE_GRAVITY,
    "NCAAF": NFL_SHADE_GRAVITY,     # Same shading patterns as NFL
    "NCAAB": NBA_SHADE_GRAVITY,     # Same shading patterns as NBA
    "MLB": {},                       # Run lines don't really get shaded
    "NHL": {},                       # Puck lines don't really get shaded
}


def detect_public_shading(
    line: float,
    sport: str,
    market_type: str = "spread",
) -> dict:
    """
    Detect whether a line is likely being shaded toward a key number
    to attract public money.

    Public shading works like this:
    1. Book's true model says the line should be -2.5
    2. They know the public loves betting -3 (it "feels" like a field goal game)
    3. They post -3 to attract public money on the favorite
    4. This creates value on the underdog at +3 (the true line is +2.5)

    Signs of shading:
    - Line is ON a key number (3, 7, 10, 14 in NFL)
    - Key number is "close" to where the true line likely is (within 1 point)
    - Line opened at a non-key number and moved TO the key number without
      apparent sharp action

    Args:
        line: The current spread (e.g., -3.0).
        sport: Sport identifier.
        market_type: "spread" or "total" — different shading patterns.

    Returns:
        Dict with shading analysis.
    """
    normalized = _normalize_sport(sport)
    abs_line = abs(line)
    gravity_table = SHADE_GRAVITY.get(normalized, {})

    # Check if the line is on or very near a key number with high gravity
    nearest_key = None
    nearest_dist = float("inf")
    nearest_gravity = 0.0

    for key_num, gravity in gravity_table.items():
        dist = abs(abs_line - key_num)
        if dist < nearest_dist:
            nearest_dist = dist
            nearest_key = key_num
            nearest_gravity = gravity

    # Is the line sitting on a gravitational key number?
    on_key = nearest_dist < 0.01
    near_key = nearest_dist <= 1.0

    if not near_key or nearest_key is None:
        return {
            "line": line,
            "sport": normalized,
            "shaded_toward": None,
            "estimated_shade_cents": 0,
            "true_line_estimate": line,
            "confidence": "LOW",
            "detail": "Line is not near a key number with public gravity.",
        }

    # Estimate shade magnitude based on gravity and proximity
    # On the key number: maximum shade estimate
    # Near the key number: proportional shade estimate
    if on_key:
        # Line is ON the key number — most likely shaded
        # Estimate the true line is 0.5-1.0 points away from the key
        # Direction: shade is TOWARD the key from the true line
        shade_estimate_pts = nearest_gravity * 0.5  # Max 0.5 points shade at gravity 1.0
        shade_direction = "toward_zero" if line < 0 else "away_from_zero"

        # True line is slightly less extreme (closer to pick'em)
        if line < 0:
            true_line_estimate = line + shade_estimate_pts
        else:
            true_line_estimate = line - shade_estimate_pts

        # Convert shade to cents
        # At -110 juice, half a point on a key number is worth roughly
        # push_prob/2 * 191 cents (the cents_per_pct factor)
        push_prob = push_probability(abs_line, sport)
        estimated_shade_cents = round(push_prob / 2 * 191 * nearest_gravity, 1)

        confidence = "HIGH" if nearest_gravity >= 0.7 else "MODERATE"
        detail = (
            f"Line is ON key number {nearest_key} (gravity {nearest_gravity:.2f}). "
            f"Public loves this number. Estimated true line: {true_line_estimate:+.1f}. "
            f"The OTHER side likely has {estimated_shade_cents:.1f} cents of value "
            f"from public shading."
        )
    else:
        # Line is near but not on a key number
        # This could mean: the true line IS here, and the book resisted shading
        # (less common), or the line is in transition
        shade_estimate_pts = 0.0
        true_line_estimate = line
        estimated_shade_cents = 0
        confidence = "LOW"
        detail = (
            f"Line {line:+.1f} is near key number {nearest_key} (distance: "
            f"{nearest_dist:.1f}) but not on it. This may indicate the book's "
            f"true model places the line here and they resisted shading, or "
            f"the line is moving toward {nearest_key}."
        )

    return {
        "line": line,
        "sport": normalized,
        "shaded_toward": nearest_key if on_key else None,
        "nearest_key_number": nearest_key,
        "distance_from_key": round(nearest_dist, 2),
        "key_gravity": nearest_gravity,
        "estimated_shade_cents": estimated_shade_cents,
        "true_line_estimate": round(true_line_estimate, 1),
        "shade_direction": shade_direction if on_key else None,
        "confidence": confidence,
        "detail": detail,
        "actionable": on_key and nearest_gravity >= 0.5,
        "contrarian_side": (
            f"Bet {'underdog' if line < 0 else 'favorite'} "
            f"({'+'if line < 0 else '-'}{abs_line}) — "
            f"public is on the other side"
        ) if on_key and nearest_gravity >= 0.5 else None,
    }


# ---------------------------------------------------------------------------
# Composite analysis functions
# ---------------------------------------------------------------------------

def analyze_spread(
    spread: float,
    sport: str,
    alt_spread: Optional[float] = None,
    period: str = "FG",
) -> dict:
    """
    Full dead number and key number analysis for a single spread.

    This is the main entry point for spread analysis. Pass a spread and sport,
    get back everything: is it dead, how important is it, what's the push
    probability, is it being shaded, and if there's an alternate available,
    what's the line shopping value.

    Args:
        spread: The spread (e.g., -3.0, +7.5).
        sport: Sport identifier.
        alt_spread: Optional alternate spread for line shopping comparison.
        period: "FG" (full game), "1H" (first half), "1Q" (first quarter).
    """
    normalized = _normalize_sport(sport)

    if period == "FG":
        importance = key_number_value(spread, sport)
    else:
        importance = half_quarter_key_value(spread, sport, period)

    dead = is_dead_number(spread, sport)
    push_prob = push_probability(spread, sport)
    shading = detect_public_shading(spread, sport)

    result = {
        "spread": spread,
        "sport": normalized,
        "period": period,
        "key_number_importance": importance,
        "is_dead_number": dead,
        "push_probability": push_prob,
        "push_probability_pct": round(push_prob * 100, 2),
        "public_shading": shading,
    }

    if alt_spread is not None:
        shopping = line_shopping_value(spread, alt_spread, sport)
        result["line_shopping"] = shopping

    # Context-specific commentary
    if normalized in ("NFL", "NCAAF"):
        abs_s = abs(spread)
        if 2.5 <= abs_s <= 3.5:
            result["commentary"] = (
                "This is in the 3-point zone — the most critical number in "
                f"{'NFL' if normalized == 'NFL' else 'college'} football. "
                "14.8% of NFL games are decided by exactly 3. Every half point "
                "matters enormously here."
            )
        elif 6.5 <= abs_s <= 7.5:
            result["commentary"] = (
                "This is in the 7-point zone — the second most critical number. "
                "9.1% of games decided by exactly 7. Getting on the right side "
                "of 7 is a major edge."
            )
        elif dead:
            result["commentary"] = (
                f"Dead number territory ({abs_s}). Very few games land here. "
                "Line shopping between adjacent dead numbers has minimal value."
            )
    elif normalized in ("MLB", "NHL"):
        abs_s = abs(spread)
        if abs_s <= 1.5:
            result["commentary"] = (
                f"Standard {'run' if normalized == 'MLB' else 'puck'} line zone. "
                f"{'28%' if normalized == 'MLB' else '30%'} of games are decided "
                f"by 1 {'run' if normalized == 'MLB' else 'goal'}. The .5 matters "
                "enormously here."
            )
    elif normalized in ("NBA", "NCAAB"):
        if dead:
            result["commentary"] = (
                "Basketball margins are more uniformly distributed — key numbers "
                "matter less than in football. Focus on line value over number theory."
            )

    return result


def rank_line_shopping_opportunities(
    lines: list[dict],
    sport: str,
) -> list[dict]:
    """
    Given multiple spread offerings for the same game across different books,
    rank them by the actual probability value of the differences.

    This tells you: "DraftKings -3 vs FanDuel -2.5 on the same game —
    that half point is worth X% of outcomes."

    Args:
        lines: List of {"bookmaker": str, "spread": float, "price": int}
        sport: Sport identifier.

    Returns:
        Sorted list of line comparisons with value analysis.
    """
    if len(lines) < 2:
        return []

    comparisons = []

    # Compare each pair of bookmakers
    for i in range(len(lines)):
        for j in range(i + 1, len(lines)):
            a = lines[i]
            b = lines[j]
            spread_a = a["spread"]
            spread_b = b["spread"]

            if spread_a == spread_b:
                # Same spread, only juice differs — still valuable but different calc
                price_diff = abs(a.get("price", -110) - b.get("price", -110))
                comparisons.append({
                    "book_a": a["bookmaker"],
                    "book_b": b["bookmaker"],
                    "spread_a": spread_a,
                    "spread_b": spread_b,
                    "price_a": a.get("price", -110),
                    "price_b": b.get("price", -110),
                    "spread_diff": 0,
                    "prob_difference": 0,
                    "prob_difference_pct": 0,
                    "juice_difference": price_diff,
                    "type": "JUICE_ONLY",
                    "recommendation": (
                        f"Same spread — take the better juice "
                        f"({price_diff} cents difference)"
                    ),
                })
                continue

            shopping = line_shopping_value(spread_a, spread_b, sport)

            # Determine which book is better
            if spread_a > spread_b:
                better_book = a["bookmaker"]
                worse_book = b["bookmaker"]
            else:
                better_book = b["bookmaker"]
                worse_book = a["bookmaker"]

            comparisons.append({
                "book_a": a["bookmaker"],
                "book_b": b["bookmaker"],
                "spread_a": spread_a,
                "spread_b": spread_b,
                "price_a": a.get("price", -110),
                "price_b": b.get("price", -110),
                "spread_diff": abs(spread_a - spread_b),
                "prob_difference": shopping["prob_difference"],
                "prob_difference_pct": shopping["prob_difference_pct"],
                "cents_value": shopping["cents_value"],
                "crossed_key_numbers": shopping["crossed_key_numbers"],
                "type": "SPREAD_DIFF",
                "better_book": better_book,
                "recommendation": shopping["recommendation"],
            })

    # Sort by probability difference descending
    comparisons.sort(key=lambda x: x.get("prob_difference", 0), reverse=True)
    return comparisons


def buy_points_analysis(
    current_spread: float,
    target_spread: float,
    point_cost_cents: int,
    sport: str,
) -> dict:
    """
    Analyze whether buying points is +EV.

    Sportsbooks let you buy points (move the spread) for extra juice. Typically
    10-20 cents per half point, but crossing key numbers costs more (sometimes
    25-30 cents for the 3 in NFL).

    The question: is the probability gained worth the juice paid?

    Args:
        current_spread: The standard spread (e.g., -3.0).
        target_spread: The bought-down spread (e.g., -2.5).
        point_cost_cents: Extra juice in cents (e.g., 20 means -110 becomes -130).
        sport: Sport identifier.

    Returns:
        Dict with buy analysis and recommendation.
    """
    shopping = line_shopping_value(current_spread, target_spread, sport)
    prob_gain = shopping["prob_difference"]

    # Standard -110 implies 52.38% breakeven
    # Extra juice of N cents means the line becomes -(110+N)
    new_juice = -(110 + point_cost_cents)

    # Calculate implied probability at the new juice
    if new_juice < 0:
        implied_at_new_juice = abs(new_juice) / (abs(new_juice) + 100)
    else:
        implied_at_new_juice = 100 / (new_juice + 100)

    standard_implied = 110 / (110 + 100)  # 0.5238
    juice_cost_pct = implied_at_new_juice - standard_implied

    # Net value: probability gained minus probability cost of extra juice
    net_value = prob_gain - juice_cost_pct

    is_profitable = net_value > 0

    return {
        "current_spread": current_spread,
        "target_spread": target_spread,
        "sport": _normalize_sport(sport),
        "point_cost_cents": point_cost_cents,
        "new_juice_line": new_juice,
        "probability_gained": round(prob_gain, 4),
        "probability_gained_pct": round(prob_gain * 100, 2),
        "juice_cost_pct": round(juice_cost_pct, 4),
        "juice_cost_pct_display": round(juice_cost_pct * 100, 2),
        "net_value": round(net_value, 4),
        "net_value_pct": round(net_value * 100, 2),
        "is_profitable": is_profitable,
        "crossed_key_numbers": shopping["crossed_key_numbers"],
        "recommendation": (
            f"BUY — gaining {prob_gain*100:.1f}% probability for {juice_cost_pct*100:.1f}% "
            f"juice cost. Net +{net_value*100:.1f}% EV."
            if is_profitable
            else f"PASS — gaining only {prob_gain*100:.1f}% probability but paying "
            f"{juice_cost_pct*100:.1f}% in juice. Net {net_value*100:.1f}% EV."
        ),
    }


def find_dead_number_steals(
    lines: list[dict],
    sport: str,
) -> list[dict]:
    """
    Find opportunities where a book has moved OFF a key number onto a dead
    number, creating value.

    When a line moves from 3 to 4 in the NFL, the book crossed a key number
    (3) and landed on a semi-dead number (4). The move from 3 to 4 was
    expensive in probability terms, but from 4 to 5 is cheap. This means:
    - If you can still get 3 at another book, that's a steal
    - If the book has the favorite at -4 and you can get -3 elsewhere, massive value

    Args:
        lines: List of {"bookmaker": str, "spread": float, "price": int}
        sport: Sport identifier.

    Returns:
        Opportunities ranked by dead number value.
    """
    if len(lines) < 2:
        return []

    normalized = _normalize_sport(sport)
    opportunities = []

    # Find the best and worst spreads
    sorted_lines = sorted(lines, key=lambda x: x["spread"], reverse=True)
    best = sorted_lines[0]
    worst = sorted_lines[-1]

    if best["spread"] == worst["spread"]:
        return []

    # Check if any book is on a dead number while another is on a key number
    for line in lines:
        importance = key_number_value(line["spread"], sport)
        dead = is_dead_number(line["spread"], sport)

        # Find the nearest key number this book is AWAY from
        key_table = KEY_NUMBERS.get(normalized, {})
        abs_spread = abs(line["spread"])
        nearest_key = None
        nearest_key_dist = float("inf")

        for kn, val in key_table.items():
            if val >= 0.4:  # Only consider significant key numbers
                dist = abs(abs_spread - kn)
                if 0 < dist < nearest_key_dist:
                    nearest_key_dist = dist
                    nearest_key = kn

        # Check if another book is on that key number
        if nearest_key is not None and dead:
            for other in lines:
                if other["bookmaker"] == line["bookmaker"]:
                    continue
                if abs(abs(other["spread"]) - nearest_key) < 0.01:
                    shopping = line_shopping_value(
                        line["spread"], other["spread"], sport
                    )
                    opportunities.append({
                        "dead_book": line["bookmaker"],
                        "dead_spread": line["spread"],
                        "dead_price": line.get("price", -110),
                        "key_book": other["bookmaker"],
                        "key_spread": other["spread"],
                        "key_price": other.get("price", -110),
                        "key_number_crossed": nearest_key,
                        "prob_difference": shopping["prob_difference"],
                        "prob_difference_pct": shopping["prob_difference_pct"],
                        "cents_value": shopping["cents_value"],
                        "recommendation": (
                            f"STEAL at {other['bookmaker']}: {other['spread']:+.1f} vs "
                            f"{line['bookmaker']}'s {line['spread']:+.1f}. "
                            f"Crosses key number {nearest_key}, worth "
                            f"{shopping['prob_difference_pct']:.1f}% probability."
                        ),
                    })

    opportunities.sort(key=lambda x: x["prob_difference"], reverse=True)
    return opportunities
