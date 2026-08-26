"""
Market psychology submodules — split from the former tools/market_psychology.py.
"""

"""Empirical constants: shading profiles, scoring distributions, velocities."""

# ---------------------------------------------------------------------------
# Constants: empirical data baked into the model
# ---------------------------------------------------------------------------

# NFL margin-of-victory frequencies from ~20 years of data (approximate %).
# Key numbers 3 and 7 occur far more often than adjacent margins.
NFL_MARGIN_FREQ = {
    1: 0.054, 2: 0.038, 3: 0.155, 4: 0.048, 5: 0.035,
    6: 0.060, 7: 0.095, 8: 0.038, 9: 0.021, 10: 0.065,
    11: 0.030, 12: 0.018, 13: 0.030, 14: 0.065, 15: 0.015,
    16: 0.025, 17: 0.045, 18: 0.015, 19: 0.015, 20: 0.020,
    21: 0.040, 22: 0.012, 23: 0.012, 24: 0.025, 25: 0.012,
}

# Shade profiles: how many cents (in American-odds terms) books typically
# shade lines toward public-magnet numbers.  Derived from historical
# opening-vs-closing line analysis.
NFL_SPREAD_SHADE = {
    3.0: 12,   # -3 is the most shaded number in all of sports betting
    7.0: 8,
    6.0: 5,
    10.0: 5,
    14.0: 4,
    1.0: 3,
    6.5: 3,
    7.5: 3,
    2.5: 2,    # Public prefers -3, so -2.5 gets LESS public money
    3.5: 2,
}

NBA_TOTAL_SHADE = {
    200.0: 6, 205.0: 4, 210.0: 6, 215.0: 4, 220.0: 6,
    225.0: 4, 230.0: 6, 235.0: 4, 240.0: 6, 245.0: 4, 250.0: 5,
}

NBA_SPREAD_SHADE = {
    1.0: 3, 1.5: 3, 2.0: 2, 2.5: 2, 3.0: 3, 3.5: 2,
    4.0: 2, 4.5: 2, 5.0: 3, 5.5: 2, 6.0: 2, 6.5: 2,
    7.0: 3, 7.5: 2, 8.0: 2,
}

# Typical scoring distribution by half/quarter for each sport.
# Values are fraction of full-game points scored in that period.
SCORING_DISTRIBUTION = {
    "americanfootball_nfl": {
        "first_half": 0.48,      # Slightly less than half — conservative early
        "second_half": 0.52,
        "first_quarter": 0.20,   # Slow starts common
        "second_quarter": 0.28,
        "third_quarter": 0.24,
        "fourth_quarter": 0.28,
    },
    "basketball_nba": {
        "first_half": 0.505,     # NBA is close to 50/50
        "second_half": 0.495,
        "first_quarter": 0.245,  # Starters, structured play, slightly lower
        "second_quarter": 0.260,
        "third_quarter": 0.250,
        "fourth_quarter": 0.245,
    },
    "baseball_mlb": {
        "first_5": 0.54,         # Starters pitch first 5 — different dynamic
        "last_4": 0.46,          # Bullpen era is volatile
    },
    "icehockey_nhl": {
        "first_period": 0.33,
        "second_period": 0.33,
        "third_period": 0.34,    # Slight uptick with empty-net goals
    },
}

# Half/quarter market edge coefficients.
# Positive = books historically underprice the over for that segment.
# These are in percentage-point terms of implied probability.
HALF_QUARTER_EDGES = {
    "americanfootball_nfl": {
        "first_half_under": 0.015,    # NFL 1H unders slightly underpriced
        "second_half_total": 0.025,   # 2H totals are less efficient
        "first_quarter_under": 0.020,
    },
    "basketball_nba": {
        "first_quarter_under": 0.022,  # Structured play, starters, pace ramp-up
        "third_quarter_over": 0.012,   # Post-halftime adjustments create runs
    },
    "baseball_mlb": {
        "first_5_under": 0.018,   # Starter dominance if ace is pitching
        "first_5_over": 0.015,    # If a bad starter, books underreact
    },
}

# Typical hourly line movement (in American odds cents) by hours-to-game.
# Closer to game = faster movement. Based on empirical CLV data.
LINE_MOVEMENT_VELOCITY = {
    "americanfootball_nfl": {
        168: 0.5, 72: 1.0, 48: 1.5, 24: 2.5, 12: 4.0,
        6: 6.0, 3: 8.0, 1: 12.0, 0.5: 15.0,
    },
    "basketball_nba": {
        48: 1.0, 24: 2.0, 12: 3.5, 6: 5.0, 3: 7.0,
        1: 10.0, 0.5: 14.0,
    },
    "baseball_mlb": {
        48: 0.8, 24: 1.5, 12: 3.0, 6: 5.0, 3: 7.0,
        1: 10.0, 0.5: 13.0,
    },
    "icehockey_nhl": {
        48: 0.8, 24: 1.5, 12: 3.0, 6: 4.5, 3: 6.5,
        1: 9.0, 0.5: 12.0,
    },
}

# Attention weighting: events that draw disproportionate public/book focus.
# Higher weight = more book attention = less opportunity for thin-market edges.
ATTENTION_WEIGHTS = {
    "americanfootball_nfl": {
        "Monday Night Football": 10, "Sunday Night Football": 9,
        "Thursday Night Football": 8, "playoffs": 10, "Super Bowl": 10,
    },
    "basketball_nba": {
        "nationally_televised": 7, "playoffs": 9, "finals": 10,
    },
    "baseball_mlb": {
        "nationally_televised": 5, "playoffs": 8, "world_series": 10,
    },
}

