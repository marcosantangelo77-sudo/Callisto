"""Constants shared across the Kelly package (split from tools/kelly.py)."""

# ---------------------------------------------------------------------------
# AGP confidence tier -> Kelly multiplier mapping
# VERIFIED gets full fraction; lower tiers get proportionally reduced.
# These are multiplicative on top of the fractional Kelly factor.
# ---------------------------------------------------------------------------
AGP_TIER_MULTIPLIERS = {
    "VERIFIED":      1.00,   # >= 0.90: sharp-book confirmed edge
    "CORROBORATED":  0.80,   # >= 0.75: multi-source confirmed
    "PROBABLE":      0.55,   # >= 0.55: reasonable evidence
    "SPECULATIVE":   0.30,   # >= 0.30: thin evidence, size down hard
    "UNVERIFIED":    0.00,   # <  0.30: do not bet
}

# Sport-level line movement volatility profiles (std dev of closing line
# movement in points/percentage per hour remaining).  Estimated from
# historical CLV distributions.  Used by timing_value().
LINE_MOVEMENT_PROFILES = {
    "basketball_nba": {
        "early_vol": 0.025,   # 24h+ out: low volatility
        "mid_vol":   0.040,   # 4-24h out: moderate
        "late_vol":  0.070,   # <4h out:   highest
        "steam_prob": 0.12,   # probability of a steam move in any hour
    },
    "basketball_ncaab": {
        "early_vol": 0.030,
        "mid_vol":   0.050,
        "late_vol":  0.080,
        "steam_prob": 0.15,
    },
    "americanfootball_nfl": {
        "early_vol": 0.015,
        "mid_vol":   0.025,
        "late_vol":  0.050,
        "steam_prob": 0.08,
    },
    "americanfootball_ncaaf": {
        "early_vol": 0.020,
        "mid_vol":   0.035,
        "late_vol":  0.060,
        "steam_prob": 0.10,
    },
    "baseball_mlb": {
        "early_vol": 0.020,
        "mid_vol":   0.045,
        "late_vol":  0.075,
        "steam_prob": 0.14,
    },
    "icehockey_nhl": {
        "early_vol": 0.018,
        "mid_vol":   0.030,
        "late_vol":  0.055,
        "steam_prob": 0.09,
    },
}

# Default profile for sports not explicitly listed
_DEFAULT_MOVEMENT_PROFILE = {
    "early_vol": 0.025,
    "mid_vol":   0.040,
    "late_vol":  0.065,
    "steam_prob": 0.11,
}

# Market-level CLV decay — how quickly edges close as game approaches.
# 1.0 = edge closes linearly; higher = faster decay.
MARKET_CLV_DECAY = {
    "h2h":       1.2,    # moneylines close fast
    "spreads":   1.1,    # spreads nearly as fast
    "totals":    0.9,    # totals are stickier
    "player_points": 0.6,  # props can hold value longer
    "player_rebounds": 0.6,
    "player_assists": 0.6,
    "player_threes": 0.5,
    "alternate_spreads": 0.7,
    "alternate_totals": 0.7,
}
