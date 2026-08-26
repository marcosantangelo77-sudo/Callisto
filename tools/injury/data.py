"""Hardcoded research baselines, lookup tables, and adjustment curves.

Extracted verbatim from tools/injury_model.py — pure data, no logic.
Sources: RAPTOR/EPM/on/off differentials (NBA), PFF WAR & EPA models (NFL),
FanGraphs WAR splits (MLB).
"""

# ---------------------------------------------------------------------------
# Section 1: Position impact databases — hardcoded from analytics research
# ---------------------------------------------------------------------------

# NBA: Points of spread impact when a player at this position is OUT.
# Range is (role_player_low, star_high). Actual value depends on player tier.
# Source: Historical on/off differentials, RAPTOR, EPM baselines.
NBA_POSITION_IMPACT = {
    # position: (bench_player, avg_starter, good_starter, all_star, mvp_candidate)
    "PG": (0.3, 1.2, 2.0, 3.2, 4.5),
    "SG": (0.2, 1.0, 1.8, 2.8, 4.0),
    "SF": (0.2, 1.0, 1.8, 3.0, 4.2),
    "PF": (0.3, 1.0, 1.8, 3.0, 4.0),
    "C":  (0.3, 1.2, 2.0, 3.2, 4.5),
}

# NBA player tier thresholds — approximate using PPG + BPM proxy
# tier_index: 0=bench, 1=avg_starter, 2=good_starter, 3=all_star, 4=mvp
NBA_TIER_THRESHOLDS = {
    # (min_ppg, min_bpm) — meet EITHER threshold to qualify for tier
    0: (0.0, -5.0),    # bench
    1: (8.0, -1.0),    # avg starter
    2: (15.0, 1.5),    # good starter
    3: (22.0, 4.0),    # all-star
    4: (27.0, 7.0),    # mvp candidate
}

# NFL: Points of spread impact by position when starter is OUT.
# Based on historical spread movements post-injury (2018-2024 NFL data).
NFL_POSITION_IMPACT = {
    # position: (backup_quality_high, backup_quality_avg, backup_quality_low)
    # "high" backup = competent fill-in, "low" = significant downgrade
    "QB":   (3.0, 4.5, 7.0),
    "RB":   (0.3, 0.8, 1.5),
    "WR":   (0.5, 1.0, 2.0),
    "TE":   (0.3, 0.6, 1.0),
    "OL":   (0.3, 0.6, 1.0),  # per lineman
    "EDGE": (0.5, 1.0, 2.0),
    "DT":   (0.3, 0.6, 1.2),
    "LB":   (0.3, 0.5, 1.0),
    "CB":   (0.3, 0.8, 1.5),
    "S":    (0.2, 0.5, 1.0),
    "K":    (0.3, 0.5, 1.0),  # kickers matter for close games
    "P":    (0.1, 0.2, 0.5),
}

# NFL target share redistribution patterns when a pass catcher is out.
# Values are share of the absent player's targets that flow to each position.
NFL_TARGET_REDISTRIBUTION = {
    "WR1_out": {"WR2": 0.35, "WR3": 0.20, "TE1": 0.20, "RB1": 0.15, "WR4": 0.10},
    "WR2_out": {"WR3": 0.30, "WR1": 0.15, "TE1": 0.25, "RB1": 0.15, "WR4": 0.15},
    "TE1_out": {"WR1": 0.20, "WR2": 0.25, "TE2": 0.25, "RB1": 0.20, "WR3": 0.10},
    "RB1_out": {"RB2": 0.55, "WR1": 0.10, "WR2": 0.10, "TE1": 0.15, "WR3": 0.10},
}

# MLB: Impact in cents on moneyline (moves the fair ML price).
# Starting pitcher is the dominant factor in baseball.
MLB_POSITION_IMPACT_CENTS = {
    # position: (replacement_level, avg, above_avg, ace/star)
    "SP":  (15, 30, 40, 55),   # starting pitcher — massive
    "RP":  (3, 7, 10, 15),     # relief pitcher (closer especially)
    "C":   (3, 6, 10, 18),     # catcher — framing + game calling
    "1B":  (2, 5, 8, 15),
    "2B":  (3, 6, 10, 16),
    "3B":  (3, 6, 10, 17),
    "SS":  (3, 7, 12, 20),     # premium defensive position
    "LF":  (2, 5, 8, 14),
    "CF":  (3, 6, 10, 17),     # premium defensive position
    "RF":  (2, 5, 8, 15),
    "DH":  (2, 5, 8, 14),
}

# MLB pitcher tier thresholds by ERA
MLB_PITCHER_TIERS = {
    0: (5.50, 999.0),   # replacement
    1: (4.20, 5.49),    # avg
    2: (3.40, 4.19),    # above avg
    3: (0.00, 3.39),    # ace
}

# MLB position player tiers by WAR/162
MLB_POSITION_TIERS = {
    0: (-1.0, 0.5),    # replacement
    1: (0.5, 2.0),     # avg
    2: (2.0, 4.0),     # above avg
    3: (4.0, 12.0),    # star
}

# ---------------------------------------------------------------------------
# Section 2: NBA matchup modifiers
# ---------------------------------------------------------------------------

# How much MORE a player's absence hurts based on opponent style.
# Values are multiplicative adjustments (1.0 = no change, 1.3 = 30% worse).
NBA_MATCHUP_MODIFIERS = {
    # absent player archetype -> opponent style -> multiplier
    "rim_protector": {
        "interior_dominant": 1.35,  # opponent attacks rim heavily
        "balanced": 1.0,
        "perimeter_dominant": 0.75,  # opponent shoots 3s anyway
    },
    "perimeter_defender": {
        "star_guard_driven": 1.30,  # opponent relies on guard scoring
        "balanced": 1.0,
        "post_dominant": 0.70,
    },
    "floor_spacer": {
        "packing_paint": 1.25,  # losing spacing kills you vs paint-packing D
        "balanced": 1.0,
        "switching_defense": 0.85,
    },
    "playmaker": {
        "pressing_defense": 1.30,  # need ballhandling vs pressure
        "balanced": 1.0,
        "drop_coverage": 0.80,
    },
    "scorer": {
        "elite_defense": 1.20,  # need your best player vs good D
        "balanced": 1.0,
        "bad_defense": 0.85,  # can get by without star vs weak D
    },
}

# ---------------------------------------------------------------------------
# Section 3: NFL matchup modifiers
# ---------------------------------------------------------------------------

NFL_MATCHUP_MODIFIERS = {
    "QB": {
        "vs_strong_pass_rush": 1.25,   # backup behind bad OL vs pass rush = disaster
        "balanced": 1.0,
        "vs_weak_pass_rush": 0.80,
    },
    "pass_rusher": {
        "vs_pocket_passer": 1.30,      # edge rushers matter most vs statue QBs
        "balanced": 1.0,
        "vs_mobile_qb": 0.70,          # mobile QB neutralizes pass rush
    },
    "CB": {
        "vs_elite_wr": 1.35,           # losing your CB1 vs a top WR = torched
        "balanced": 1.0,
        "vs_run_heavy": 0.65,          # CB less important vs ground game
    },
    "RB": {
        "vs_weak_run_defense": 1.20,
        "balanced": 1.0,
        "vs_strong_run_defense": 0.85,  # was going to be bottled up anyway
    },
    "WR": {
        "vs_weak_secondary": 1.15,      # WR1 out matters more when you could exploit
        "balanced": 1.0,
        "vs_lockdown_secondary": 0.80,  # was going to be shadowed anyway
    },
    "OL": {
        "vs_strong_pass_rush": 1.35,
        "balanced": 1.0,
        "vs_weak_pass_rush": 0.75,
    },
}

# ---------------------------------------------------------------------------
# Section 4: Market adjustment speed curves
# ---------------------------------------------------------------------------

# How quickly markets adjust after injury news breaks.
# Format: list of (minutes_elapsed, pct_adjusted)
# These are cumulative — at 60 minutes, ~95% of the move has happened.
MARKET_ADJUSTMENT_CURVE = {
    "star": [
        # Star injuries (top-50 player in sport) adjust fastest
        (1, 0.20),   # 20% in first minute (sharp bettors + algorithms)
        (2, 0.35),
        (5, 0.55),
        (10, 0.75),
        (15, 0.85),
        (30, 0.92),
        (60, 0.97),
        (120, 0.99),
    ],
    "starter": [
        # Regular starter — slower but still fast
        (1, 0.10),
        (2, 0.20),
        (5, 0.40),
        (10, 0.55),
        (15, 0.70),
        (30, 0.82),
        (60, 0.93),
        (120, 0.98),
    ],
    "role_player": [
        # Role players — books are slow, casual bettors don't care
        (1, 0.05),
        (2, 0.10),
        (5, 0.20),
        (10, 0.35),
        (15, 0.50),
        (30, 0.65),
        (60, 0.82),
        (120, 0.93),
    ],
}

# Injury significance tiers determine which curve to use
SIGNIFICANCE_TIERS = {
    "NBA": {
        "mvp_candidate": "star",
        "all_star": "star",
        "good_starter": "starter",
        "avg_starter": "starter",
        "bench": "role_player",
    },
    "NFL": {
        "QB": "star",       # Any starting QB out is star-level news
        "EDGE": "starter",
        "CB": "starter",
        "WR": "starter",
        "RB": "role_player",  # RBs are more fungible
        "TE": "role_player",
        "OL": "role_player",
        "DT": "role_player",
        "LB": "role_player",
        "S": "role_player",
        "K": "role_player",
        "P": "role_player",
    },
    "MLB": {
        "SP": "star",       # Starting pitcher always moves lines
        "RP": "role_player",
        "position": "role_player",
    },
}

