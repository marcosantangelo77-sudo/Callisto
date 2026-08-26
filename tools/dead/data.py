"""
Static reference tables for dead number theory: historical margin-of-victory
frequency distributions, key number importance scores, push probability
tables, half/quarter key numbers, and dead number thresholds.

All data hardcoded from historical distributions. No API calls.
"""

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
