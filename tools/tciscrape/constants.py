"""Shared constants for the Team Cohesion Index (TCI) scraper."""

import os

DB_PATH = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"

# US state -> region mapping for geographic concentration
STATE_REGIONS = {
    # Southeast
    "AL": "Southeast", "AR": "Southeast", "FL": "Southeast", "GA": "Southeast",
    "KY": "Southeast", "LA": "Southeast", "MS": "Southeast", "NC": "Southeast",
    "SC": "Southeast", "TN": "Southeast", "VA": "Southeast", "WV": "Southeast",
    # Northeast
    "CT": "Northeast", "DE": "Northeast", "MA": "Northeast", "MD": "Northeast",
    "ME": "Northeast", "NH": "Northeast", "NJ": "Northeast", "NY": "Northeast",
    "PA": "Northeast", "RI": "Northeast", "VT": "Northeast", "DC": "Northeast",
    # Midwest
    "IA": "Midwest", "IL": "Midwest", "IN": "Midwest", "KS": "Midwest",
    "MI": "Midwest", "MN": "Midwest", "MO": "Midwest", "NE": "Midwest",
    "ND": "Midwest", "OH": "Midwest", "SD": "Midwest", "WI": "Midwest",
    # Southwest
    "AZ": "Southwest", "NM": "Southwest", "OK": "Southwest", "TX": "Southwest",
    # West
    "CA": "West", "CO": "West", "HI": "West", "ID": "West", "MT": "West",
    "NV": "West", "OR": "West", "UT": "West", "WA": "West", "WY": "West",
    "AK": "West",
}

# Religious-affiliated programs (major ones with institutional stability signal)
RELIGIOUS_PROGRAMS = {
    "Notre Dame": "Catholic",
    "Villanova": "Catholic",
    "Georgetown": "Catholic",
    "Gonzaga": "Catholic",
    "Marquette": "Catholic",
    "Creighton": "Catholic",
    "Seton Hall": "Catholic",
    "DePaul": "Catholic",
    "St. John's": "Catholic",
    "Xavier": "Catholic",
    "BYU": "LDS",
    "Baylor": "Baptist",
    "TCU": "Disciples of Christ",
    "SMU": "Methodist",
    "Wake Forest": "Baptist (historical)",
    "Duke": "Methodist (historical)",
    "Boston College": "Catholic",
    "Holy Cross": "Catholic",
    "Loyola": "Catholic",
    "Dayton": "Catholic",
    "Oklahoma State": "secular",  # Included for reference
}

# Coaching tenure fallback — ESPN API doesn't return coach data for all teams.
# Source: public coaching records as of 2025-26 season (years at current school).
COACHING_TENURE_FALLBACK = {
    # --- Original 15 (ESPN API gaps) ---
    "Notre Dame Fighting Irish": ("Niele Ivey", 6),
    "Kentucky Wildcats": ("Kenny Brooks", 2),
    "Vanderbilt Commodores": ("Shea Ralph", 4),
    "Baylor Bears": ("Nicki Collen", 5),
    "Duke Blue Devils": ("Kara Lawson", 6),
    "Princeton Tigers": ("Carla Berube", 4),
    "Texas Longhorns": ("Vic Schaefer", 6),
    "Michigan State Spartans": ("Robyn Fralick", 2),
    "TCU Horned Frogs": ("Mark Campbell", 3),
    "Ole Miss Rebels": ("Yolett McPhee-McCuin", 8),
    "Iowa Hawkeyes": ("Jan Jensen", 3),
    "North Carolina Tar Heels": ("Courtney Banghart", 6),
    "Oklahoma Sooners": ("Jennie Baranczyk", 5),
    "West Virginia Mountaineers": ("Dawn Plitzuweit", 3),
    "Minnesota Golden Gophers": ("Dawn Plitzuweit", 1),
    # --- 26 additional teams with 0-tenure gap (Task #31) ---
    # Major programs
    "Arizona State Sun Devils": ("Molly Miller", 1),
    "Clemson Tigers": ("Shawn Poppie", 2),
    "Illinois Fighting Illini": ("Shauna Green", 4),
    "Oklahoma State Cowgirls": ("Jacie Hoyt", 4),
    "Tennessee Lady Volunteers": ("Kim Caldwell", 2),
    "UC San Diego Tritons": ("Heidi VanDerveer", 14),
    "USC Trojans": ("Lindsay Gottlieb", 5),
    "UTSA Roadrunners": ("Karen Aston", 5),
    "Vermont Catamounts": ("Alisa Kresge", 7),
    "Virginia Cavaliers": ("Amaka Agugua-Hamilton", 4),
    "Virginia Tech Hokies": ("Megan Duffy", 2),
    "Rhode Island Rams": ("Tammi Reiss", 7),
    "Holy Cross Crusaders": ("Candice Green", 2),
    # Mid-major programs
    "California Baptist Lancers": ("Jarrod Olson", 14),
    "Fairfield Stags": ("Carly Thibault-DuDonis", 4),
    "Fairleigh Dickinson Knights": ("Stephanie Gaitley", 3),
    "Green Bay Phoenix": ("Kayla Karius", 2),
    "High Point Panthers": ("Chelsea Banbury", 7),
    "Idaho Vandals": ("Arthur Moreira", 2),
    "Jacksonville Dolphins": ("Special Jennings", 3),
    "James Madison Dukes": ("Sean O'Regan", 10),
    "Miami (OH) RedHawks": ("Glenn Box", 3),
    "Murray State Racers": ("Rechelle Turner", 9),
    "Samford Bulldogs": ("Matt Wise", 1),
    "Southern Jaguars": ("Carlos Funchess", 8),
    "Stephen F. Austin Ladyjacks": ("Leonard Bishop", 3),
}

# 2026 NCAAW tournament teams (68) — ESPN display names
TOURNAMENT_TEAMS_2026 = list({
    "Alabama Crimson Tide", "Arizona State Sun Devils", "Baylor Bears",
    "California Baptist Lancers", "Charleston Cougars", "Clemson Tigers",
    "Colorado Buffaloes", "Colorado State Rams", "Duke Blue Devils",
    "Fairfield Stags", "Fairleigh Dickinson Knights", "Georgia Lady Bulldogs",
    "Gonzaga Bulldogs", "Green Bay Phoenix", "High Point Panthers",
    "Holy Cross Crusaders", "Howard Bison", "Idaho Vandals",
    "Illinois Fighting Illini", "Iowa Hawkeyes", "Iowa State Cyclones",
    "Jacksonville Dolphins", "James Madison Dukes", "Kentucky Wildcats",
    "Louisville Cardinals", "LSU Tigers", "Maryland Terrapins",
    "Miami (OH) RedHawks", "Michigan Wolverines", "Michigan State Spartans",
    "Minnesota Golden Gophers", "Missouri State Lady Bears", "Murray State Racers",
    "NC State Wolfpack", "Nebraska Cornhuskers", "North Carolina Tar Heels",
    "Notre Dame Fighting Irish", "Ohio State Buckeyes", "Oklahoma Sooners",
    "Oklahoma State Cowgirls", "Ole Miss Rebels", "Oregon Ducks",
    "Princeton Tigers", "Rhode Island Rams", "Richmond Spiders",
    "Samford Bulldogs", "South Carolina Gamecocks", "South Dakota State Jackrabbits",
    "Southern Jaguars", "Stephen F. Austin Ladyjacks", "Syracuse Orange",
    "TCU Horned Frogs", "Tennessee Lady Volunteers", "Texas Longhorns",
    "Texas Tech Lady Raiders", "UC San Diego Tritons", "UConn Huskies",
    "UCLA Bruins", "USC Trojans", "UTSA Roadrunners", "Vanderbilt Commodores",
    "Vermont Catamounts", "Villanova Wildcats", "Virginia Cavaliers",
    "Virginia Tech Hokies", "Washington Huskies", "West Virginia Mountaineers",
    "Western Illinois Leathernecks",
})

# ──────────────────────────────────────────────────
# DECOMPOSED SIGNAL GENERATORS — backtest-calibrated thresholds
# ──────────────────────────────────────────────────
# Backtest evidence: composite TCI is flat (51.9%), but sub-components
# have predictive power when isolated and filtered by differential magnitude.

EXP_RATIO_MIN_DIFF = 10     # |diff| >= 10 on 0-100 scale -> 57.1% hit rate
EXP_RATIO_STRONG_DIFF = 15  # |diff| >= 15 -> 66.7% hit rate (preferred)
STAB_SCORE_MIN_DIFF = 5     # Stability differential threshold
