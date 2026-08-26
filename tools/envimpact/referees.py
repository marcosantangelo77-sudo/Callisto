"""
Referee / umpire tendency databases.

NBA referee crews, MLB home plate umpires, and NFL head referees with their
measured tendencies: foul/flag rates, pace impact, strike zone size, and
net total adjustments.
"""

# =============================================================================
# REFEREE / UMPIRE TENDENCY DATABASES
# =============================================================================

# NBA referee tendencies: based on multi-year foul rate and pace data
# foul_rate_delta: % above/below league average foul calling rate
# pace_impact: points of pace adjustment (positive = faster)
# total_adj: net adjustment to game total (points)
NBA_REFEREES = {
    "Scott Foster": {
        "foul_rate_delta": 0.12, "pace_impact": -1.5, "total_adj": -2.0,
        "notes": "High foul rate but slows pace significantly. Lots of FT shooting. Road teams perform slightly better.",
    },
    "Tony Brothers": {
        "foul_rate_delta": 0.15, "pace_impact": -1.0, "total_adj": -1.5,
        "notes": "Very whistle-heavy. Creates choppy game flow. Techs are frequent.",
    },
    "Ed Malloy": {
        "foul_rate_delta": 0.08, "pace_impact": -0.5, "total_adj": -0.8,
        "notes": "Slightly above average foul rate. Fairly neutral.",
    },
    "Zach Zarba": {
        "foul_rate_delta": -0.05, "pace_impact": 0.8, "total_adj": 0.5,
        "notes": "Lets them play. Slightly faster pace. Star players benefit.",
    },
    "Marc Davis": {
        "foul_rate_delta": 0.06, "pace_impact": -0.3, "total_adj": -0.5,
        "notes": "Close to neutral. Slight lean toward more whistles.",
    },
    "Kane Fitzgerald": {
        "foul_rate_delta": 0.10, "pace_impact": -1.0, "total_adj": -1.2,
        "notes": "Above average foul caller. Slows things down.",
    },
    "James Capers": {
        "foul_rate_delta": -0.03, "pace_impact": 0.5, "total_adj": 0.3,
        "notes": "Experienced, lets physicality go. Slight over lean.",
    },
    "John Goble": {
        "foul_rate_delta": 0.03, "pace_impact": 0.0, "total_adj": 0.0,
        "notes": "League average across the board.",
    },
    "Josh Tiven": {
        "foul_rate_delta": -0.07, "pace_impact": 1.2, "total_adj": 1.0,
        "notes": "Swallows the whistle. Games flow freely. Favors physical teams.",
    },
    "Ben Taylor": {
        "foul_rate_delta": -0.04, "pace_impact": 0.6, "total_adj": 0.4,
        "notes": "Slightly under average on calls. Games move.",
    },
    "Courtney Kirkland": {
        "foul_rate_delta": 0.11, "pace_impact": -1.2, "total_adj": -1.5,
        "notes": "Whistle-heavy. Slows pace. Heavy free throw games.",
    },
    "David Guthrie": {
        "foul_rate_delta": 0.02, "pace_impact": 0.2, "total_adj": 0.1,
        "notes": "Nearly perfectly neutral.",
    },
    "Rodney Mott": {
        "foul_rate_delta": 0.09, "pace_impact": -0.8, "total_adj": -1.0,
        "notes": "Above average caller. Games can drag.",
    },
    "Pat Fraher": {
        "foul_rate_delta": -0.06, "pace_impact": 1.0, "total_adj": 0.8,
        "notes": "Under average on whistles. Allows physical play.",
    },
}

# MLB umpire tendencies: strike zone size and run impact
# zone_size_delta: % larger/smaller than average strike zone (positive = bigger zone = fewer runs)
# total_adj: runs adjustment (negative = fewer runs expected)
# k_rate_impact: % change to strikeout rate
MLB_UMPIRES = {
    "Angel Hernandez": {
        "zone_size_delta": -0.08, "total_adj": 0.4, "k_rate_impact": -0.03,
        "notes": "Inconsistent zone. Slightly smaller but erratic. More walks, more chaos.",
    },
    "Joe West": {
        "zone_size_delta": 0.12, "total_adj": -0.6, "k_rate_impact": 0.05,
        "notes": "Large zone. Pitchers love him. Suppresses runs significantly.",
    },
    "CB Bucknor": {
        "zone_size_delta": -0.06, "total_adj": 0.3, "k_rate_impact": -0.02,
        "notes": "Small, inconsistent zone. Slightly hitter-friendly.",
    },
    "Laz Diaz": {
        "zone_size_delta": 0.10, "total_adj": -0.5, "k_rate_impact": 0.04,
        "notes": "Generous zone. Pitcher-friendly. Games tend to go under.",
    },
    "Doug Eddings": {
        "zone_size_delta": 0.05, "total_adj": -0.2, "k_rate_impact": 0.02,
        "notes": "Slightly large zone. Modest pitcher lean.",
    },
    "Pat Hoberg": {
        "zone_size_delta": 0.01, "total_adj": -0.05, "k_rate_impact": 0.005,
        "notes": "One of the most accurate umpires. Nearly perfectly neutral.",
    },
    "Ron Kulpa": {
        "zone_size_delta": 0.08, "total_adj": -0.4, "k_rate_impact": 0.03,
        "notes": "Large zone. Favors pitchers.",
    },
    "Mark Wegner": {
        "zone_size_delta": -0.04, "total_adj": 0.2, "k_rate_impact": -0.015,
        "notes": "Slightly tight zone. Hitters walk more.",
    },
    "Marvin Hudson": {
        "zone_size_delta": 0.06, "total_adj": -0.3, "k_rate_impact": 0.025,
        "notes": "Slightly large zone. Modestly pitcher-friendly.",
    },
    "Lance Barksdale": {
        "zone_size_delta": 0.03, "total_adj": -0.15, "k_rate_impact": 0.01,
        "notes": "Near neutral. Slightly wide zone on corners.",
    },
    "Todd Tichenor": {
        "zone_size_delta": -0.03, "total_adj": 0.15, "k_rate_impact": -0.01,
        "notes": "Slightly tight. Modest hitter advantage.",
    },
    "Dan Iassogna": {
        "zone_size_delta": 0.02, "total_adj": -0.1, "k_rate_impact": 0.008,
        "notes": "Very close to neutral. Consistent.",
    },
    "Jim Wolf": {
        "zone_size_delta": 0.07, "total_adj": -0.35, "k_rate_impact": 0.03,
        "notes": "Pitcher-friendly zone. Games trend under.",
    },
    "Chris Guccione": {
        "zone_size_delta": -0.05, "total_adj": 0.25, "k_rate_impact": -0.02,
        "notes": "Tight zone. Hitters get favorable counts.",
    },
    "Adam Hamari": {
        "zone_size_delta": 0.09, "total_adj": -0.45, "k_rate_impact": 0.035,
        "notes": "Generous zone. Pitchers thrive. Strong under lean.",
    },
}

# NFL referee tendencies: penalty frequency and game flow
# penalty_rate_delta: % above/below average penalties per game
# total_adj: points adjustment
# pass_interference_rate: relative PI calling rate (1.0 = average)
NFL_REFEREES = {
    "Brad Allen": {
        "penalty_rate_delta": 0.08, "total_adj": -0.8, "pass_interference_rate": 1.15,
        "notes": "Above average penalties. Calls PI frequently. Slows game flow.",
    },
    "Shawn Hochuli": {
        "penalty_rate_delta": 0.12, "total_adj": -1.2, "pass_interference_rate": 1.20,
        "notes": "Flag-happy crew. Heavy penalty games. Stop-start rhythm.",
    },
    "Craig Wrolstad": {
        "penalty_rate_delta": -0.06, "total_adj": 0.5, "pass_interference_rate": 0.85,
        "notes": "Below average flags. Lets them play. Games move.",
    },
    "Clete Blakeman": {
        "penalty_rate_delta": -0.04, "total_adj": 0.3, "pass_interference_rate": 0.90,
        "notes": "Clean games. Below average penalties. Experienced crew.",
    },
    "Bill Vinovich": {
        "penalty_rate_delta": 0.02, "total_adj": -0.2, "pass_interference_rate": 1.05,
        "notes": "Close to neutral. Slight lean toward more flags.",
    },
    "Carl Cheffers": {
        "penalty_rate_delta": 0.06, "total_adj": -0.5, "pass_interference_rate": 1.10,
        "notes": "Above average penalty crew. Calls holding often.",
    },
    "Clay Martin": {
        "penalty_rate_delta": -0.03, "total_adj": 0.2, "pass_interference_rate": 0.95,
        "notes": "Slightly under average. Fairly neutral.",
    },
    "Jerome Boger": {
        "penalty_rate_delta": 0.10, "total_adj": -1.0, "pass_interference_rate": 1.18,
        "notes": "Penalty-heavy. Games slow down. Lots of flags on secondary.",
    },
    "Ron Torbert": {
        "penalty_rate_delta": 0.03, "total_adj": -0.3, "pass_interference_rate": 1.02,
        "notes": "Near neutral. Slight lean toward whistles.",
    },
    "Tra Blake": {
        "penalty_rate_delta": -0.05, "total_adj": 0.4, "pass_interference_rate": 0.88,
        "notes": "Below average flags. Games flow. Benefits offensive play.",
    },
    "Alex Kemp": {
        "penalty_rate_delta": 0.04, "total_adj": -0.4, "pass_interference_rate": 1.08,
        "notes": "Slightly above average. Calls OPI more than most.",
    },
    "Land Clark": {
        "penalty_rate_delta": -0.07, "total_adj": 0.6, "pass_interference_rate": 0.82,
        "notes": "Fewest flags in the league. Very hands-off. Benefits physical teams.",
    },
    "Adrian Hill": {
        "penalty_rate_delta": 0.05, "total_adj": -0.5, "pass_interference_rate": 1.12,
        "notes": "Above average caller. Games can be sloppy with stoppages.",
    },
    "John Hussey": {
        "penalty_rate_delta": 0.01, "total_adj": -0.1, "pass_interference_rate": 1.00,
        "notes": "Perfectly neutral. League average in all categories.",
    },
}
