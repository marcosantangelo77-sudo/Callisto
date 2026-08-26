"""Constants for line analysis: brand tiers, key numbers, contrarian ROI tables."""


# Team brand tiers — higher tier = more public action
# Tier 3: massive public brands that attract casual money
# Tier 2: popular teams with strong followings
# Tier 1: average public interest
TEAM_BRAND_TIERS: dict[str, int] = {
    # NFL
    "Dallas Cowboys": 3, "Kansas City Chiefs": 3, "San Francisco 49ers": 3,
    "Green Bay Packers": 3, "New England Patriots": 3, "Buffalo Bills": 2,
    "Philadelphia Eagles": 2, "Miami Dolphins": 2, "Detroit Lions": 2,
    "Baltimore Ravens": 2, "Las Vegas Raiders": 2, "Denver Broncos": 2,
    "Pittsburgh Steelers": 2, "Chicago Bears": 2, "New York Giants": 2,
    "Los Angeles Rams": 2, "Tampa Bay Buccaneers": 2,
    # NBA
    "Los Angeles Lakers": 3, "Golden State Warriors": 3, "Boston Celtics": 3,
    "Brooklyn Nets": 2, "New York Knicks": 2, "Chicago Bulls": 2,
    "Philadelphia 76ers": 2, "Dallas Mavericks": 2, "Miami Heat": 2,
    "Phoenix Suns": 2, "Milwaukee Bucks": 2, "Denver Nuggets": 2,
    # MLB
    "New York Yankees": 3, "Los Angeles Dodgers": 3, "Boston Red Sox": 3,
    "Chicago Cubs": 2, "Houston Astros": 2, "Atlanta Braves": 2,
    "San Francisco Giants": 2, "St. Louis Cardinals": 2,
    "Philadelphia Phillies": 2, "New York Mets": 2,
    # NCAAF / NCAAB — programs, not franchises
    "Alabama Crimson Tide": 3, "Ohio State Buckeyes": 3, "Notre Dame Fighting Irish": 3,
    "Michigan Wolverines": 3, "Georgia Bulldogs": 3, "Texas Longhorns": 3,
    "LSU Tigers": 2, "Clemson Tigers": 2, "USC Trojans": 2,
    "Duke Blue Devils": 2, "Kentucky Wildcats": 2, "North Carolina Tar Heels": 2,
    "Kansas Jayhawks": 2, "UCLA Bruins": 2,
}

# Sport-specific key numbers where lines cluster
NFL_KEY_NUMBERS = {3, 7, 6, 10, 14, 1, 4, 17, 21}

# Historical contrarian ROI by public percentage bucket (from database studies
# across 10+ NFL/NCAAF seasons — Bet Labs, SDQL, etc.).
# Format: (min_public_pct, max_public_pct) -> historical_roi for fading
CONTRARIAN_ROI_TABLE: dict[str, dict[tuple[int, int], float]] = {
    "americanfootball_nfl": {
        (50, 60): -0.5,   # Basically break-even minus vig
        (60, 70): 0.8,    # Slight positive
        (70, 80): 2.4,    # Meaningful edge
        (80, 90): 4.1,    # Strong contrarian zone
        (90, 100): 5.8,   # Rare, very strong
    },
    "americanfootball_ncaaf": {
        (50, 60): -0.3,
        (60, 70): 1.0,
        (70, 80): 2.8,
        (80, 90): 4.5,
        (90, 100): 6.2,
    },
    "basketball_nba": {
        (50, 60): -1.2,   # NBA market is more efficient
        (60, 70): -0.2,
        (70, 80): 1.1,
        (80, 90): 2.3,
        (90, 100): 3.5,
    },
    "basketball_ncaab": {
        (50, 60): -0.8,
        (60, 70): 0.5,
        (70, 80): 1.8,
        (80, 90): 3.2,
        (90, 100): 4.8,
    },
    "baseball_mlb": {
        (50, 60): -1.5,
        (60, 70): 0.3,
        (70, 80): 1.5,
        (80, 90): 3.0,
        (90, 100): 4.2,
    },
}

# Default ROI table for sports not explicitly modeled
_DEFAULT_ROI_TABLE: dict[tuple[int, int], float] = {
    (50, 60): -1.0,
    (60, 70): 0.0,
    (70, 80): 1.5,
    (80, 90): 3.0,
    (90, 100): 4.5,
}
