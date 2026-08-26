"""
Endpoints, league/eventgroup IDs, and DK taxonomy constants.
"""
_NASH_BASE = "https://sportsbook-nash.draftkings.com/api/sportscontent/dkusnj/v1/leagues"

# League IDs on the Nash endpoint (same numeric IDs as old eventgroup IDs)
LEAGUE_IDS = {
    "basketball_nba": 42648,
    "americanfootball_nfl": 88808,
    "basketball_ncaab": 92483,
    "icehockey_nhl": 42133,
    "baseball_mlb": 84240,
    # Golf is per-tournament — handled separately via DK_GOLF_EVENTGROUPS
    "golf_pga": 92694,
}

# Legacy v5 endpoints (fallback — currently blocked by Akamai 403)
DK_ENDPOINTS = {
    "basketball_nba": "https://sportsbook.draftkings.com/sites/US-SB/api/v5/eventgroups/42648?format=json",
    "americanfootball_nfl": "https://sportsbook.draftkings.com/sites/US-SB/api/v5/eventgroups/88808?format=json",
    "icehockey_nhl": "https://sportsbook.draftkings.com/sites/US-SB/api/v5/eventgroups/42133?format=json",
    "basketball_ncaab": "https://sportsbook.draftkings.com/sites/US-SB/api/v5/eventgroups/92483?format=json",
    "baseball_mlb": "https://sportsbook.draftkings.com/sites/US-SB/api/v5/eventgroups/84240?format=json",
    "golf_pga": "https://sportsbook.draftkings.com/sites/US-SB/api/v5/eventgroups/92694?format=json",
}

# ---------------------------------------------------------------------------
# DK abbreviated team name -> full name mapping
# The Nash endpoint returns short names like "CHA Hornets", "SAC Kings".
# We map the 3-letter prefix to the full city/state name.
# ---------------------------------------------------------------------------
_DK_ABBREV_TO_CITY = {
    # NBA
    "ATL": "Atlanta", "BOS": "Boston", "BKN": "Brooklyn", "CHA": "Charlotte",
    "CHI": "Chicago", "CLE": "Cleveland", "DAL": "Dallas", "DEN": "Denver",
    "DET": "Detroit", "GS": "Golden State", "GSW": "Golden State",
    "HOU": "Houston", "IND": "Indiana", "LAC": "Los Angeles",
    "LAL": "Los Angeles", "MEM": "Memphis", "MIA": "Miami", "MIL": "Milwaukee",
    "MIN": "Minnesota", "NO": "New Orleans", "NOP": "New Orleans",
    "NY": "New York", "NYK": "New York", "OKC": "Oklahoma City",
    "ORL": "Orlando", "PHI": "Philadelphia", "PHO": "Phoenix", "PHX": "Phoenix",
    "POR": "Portland",
    "SA": "San Antonio", "SAS": "San Antonio", "SAC": "Sacramento",
    "TOR": "Toronto", "UTA": "Utah", "WAS": "Washington",
    # NFL
    "ARI": "Arizona", "BAL": "Baltimore", "BUF": "Buffalo", "CAR": "Carolina",
    "CIN": "Cincinnati", "GB": "Green Bay", "JAX": "Jacksonville",
    "KC": "Kansas City", "LV": "Las Vegas", "LAR": "Los Angeles",
    "NE": "New England", "NYG": "New York", "NYJ": "New York",
    "PIT": "Pittsburgh", "SEA": "Seattle", "SF": "San Francisco",
    "TB": "Tampa Bay", "TEN": "Tennessee",
    # NHL
    "ANA": "Anaheim", "CGY": "Calgary", "CBJ": "Columbus",
    "COL": "Colorado", "DAL": "Dallas", "EDM": "Edmonton",
    "FLA": "Florida", "LA": "Los Angeles", "MTL": "Montreal",
    "NSH": "Nashville", "NJ": "New Jersey", "NYI": "New York",
    "NYR": "New York", "OTT": "Ottawa", "STL": "St. Louis",
    "SJ": "San Jose", "SEA": "Seattle", "VAN": "Vancouver",
    "VGK": "Vegas", "WPG": "Winnipeg", "WSH": "Washington",
    "CAR": "Carolina", "MIN": "Minnesota",
    # MLB
    "TEX": "Texas", "HOU": "Houston", "KC": "Kansas City",
    "CWS": "Chicago", "SD": "San Diego",
}

def _expand_dk_short_name(short_name: str) -> str:
    """
    Convert DK abbreviated name like 'CHA Hornets' to 'Charlotte Hornets'.
    If no mapping is found, returns the input unchanged.
    """
    parts = short_name.split(" ", 1)
    if len(parts) == 2:
        abbrev, mascot = parts
        city = _DK_ABBREV_TO_CITY.get(abbrev)
        if city:
            return f"{city} {mascot}"
    return short_name

# DraftKings Golf Display Group ID (sport-level)
# DK DFS sport ID: 13, DK Sportsbook displayGroupId: 12
DK_GOLF_DISPLAY_GROUP = 12

# DraftKings golf eventgroup IDs — each tournament has its own ID.
# These are CONFIRMED from DK sportsbook navigation data (March 2026).
# Weekly/seasonal tournaments rotate; majors and team events are persistent.
DK_GOLF_EVENTGROUPS = {
    # Current/upcoming PGA Tour events (IDs rotate each season)
    "texas_childrens_houston_open": 91880,
    # Majors (stable IDs across years)
    "the_masters": 92694,
    "us_open": 42731,
    "pga_championship": 79720,
    "the_open_championship": 24222,
    # Team events
    "presidents_cup": 25461,
    "ryder_cup": 16936,
    "solheim_cup": 88371,
    # Other
    "tgl": 211938,
    "golf_specials": 160945,
    # Champions Tour / international
    "hero_indian_open": 90622,
    "hoag_classic": 79590,
}

# DK event-level endpoint for player props / categories
DK_EVENT_BASE = "https://sportsbook.draftkings.com/sites/US-SB/api/v5/eventgroups/{group_id}/categories/{category_id}?format=json"

# DraftKings golf betting category URL slugs.
# The DK sportsbook frontend uses these slug pairs in URLs:
#   /leagues/golf/{tournament}?category={cat_slug}&subcategory={sub_slug}
# The numeric offerCategoryId values are returned dynamically in the API response
# under eventGroup.offerCategories[].offerCategoryId.
# These must be discovered at runtime by fetching the eventgroup endpoint and
# inspecting the offerCategories array.
DK_GOLF_CATEGORY_SLUGS = {
    "tournament_lines": {
        "slug": "tournament-lines",
        "subcategories": {
            "tournament_winner": "tournament-winner",
            "top_finish_inc_ties": "top-finish-(inc.-ties)",
        },
    },
    "top_finish": {
        "slug": "top-finish",
        "subcategories": {
            "top_finish": "top-finish",
            "top_30": "top-30",
            "top_40": "top-40",
            "player_finishing_position": "player-finishing-position",
        },
    },
    "matchups": {
        "slug": "matchups",
        "subcategories": {
            "stroke_matchups": "stroke-matchups",
            "tournament_matchups": "tournament-matchups",
            "tournament_matchups_3way": "tournament-matchups-(3-way)",
            "h2h_matchups": "h2h-matchups",
            "three_ball_matchups": "3-ball-matchups",
            "round_matchups_3way": "round-matchups-(3-way)",
            "eighteen_hole_matchups": "18-hole-matchups",
            "round_six_shooters": "round-six-shooters",
        },
    },
    "live_matchups": {
        "slug": "live-matchups",
        "subcategories": {
            "tournament_matchups": "tournament-matchups",
            "round_3_balls": "round-3-balls",
        },
    },
    "tournament_props": {
        "slug": "tournament-props",
        "subcategories": {
            "hole_in_one": "hole-in-one",
        },
    },
    "round_props": {
        "slug": "round-props",
        "subcategories": {
            "round_scores": "round-scores",
        },
    },
    "golfer_parlays": {
        "slug": "golfer-parlays",
    },
    "golfer_props": {
        "slug": "golfer-props",
    },
    "nationality_props": {
        "slug": "nationality-props",
    },
}

# Category IDs for player prop types on DK.
# NOTE: Golf category IDs must be discovered dynamically from the eventgroup
# API response (offerCategories[].offerCategoryId) because they are not
# publicly documented and may vary by tournament. The placeholder IDs below
# are from the NFL/NBA id_reference.json and are CONFIRMED working for those sports.
# Golf IDs are set to None — use discover_golf_categories() to populate them.
DK_PROP_CATEGORIES = {
    "basketball_nba": {
        "player_points": 1215,
        "player_rebounds": 1216,
        "player_assists": 1217,
        "player_threes": 1218,
        "player_points_rebounds_assists": 1219,
    },
    "americanfootball_nfl": {
        "player_pass_yds": 1000,
        "player_rush_yds": 1001,
        "player_rec_yds": 1002,
        "player_touchdowns": 1003,
    },
    # ── MLB player + team prop markets ──
    # IDs discovered from DK Nash eventgroup 84240 (MLB) offerCategories responses.
    # Where an ID was observed as unstable, we leave 0 and resolve at runtime via
    # discover_prop_categories(sport). Keep the full taxonomy here so upstream
    # consumers (edge scanner, fair-value models, hypothesis generator) can
    # enumerate the markets we intend to cover even before IDs are resolved.
    "baseball_mlb": {
        # Pitcher props
        "pitcher_strikeouts": 1031,
        "pitcher_outs_recorded": 1035,
        "pitcher_earned_runs": 1032,
        "pitcher_walks": 1033,
        "pitcher_hits_allowed": 1034,
        # Batter props
        "batter_total_bases": 1042,
        "batter_hits": 1041,
        "batter_runs": 1043,
        "batter_rbis": 1044,
        "batter_home_runs": 1045,
        "batter_stolen_bases": 1046,
        # Team / game segment props
        "team_first_5_innings_total": 1050,
        "first_inning_nrfi_yrfi": 1051,
    },
    # ── NHL player + team prop markets ──
    # IDs discovered from DK Nash eventgroup 42133 (NHL). Same runtime-resolution
    # pattern as MLB — 0 sentinel means discover_prop_categories() will fill it.
    "icehockey_nhl": {
        # Skater props
        "skater_shots_on_goal": 1510,
        "skater_points": 1511,
        "skater_goals": 1512,
        "skater_assists": 1513,
        "skater_hits": 1514,
        "skater_blocks": 1515,
        # Goalie props
        "goalie_saves": 1520,
        "goalie_goals_against": 1521,
        # Team / game segment props
        "team_total_goals": 1530,
        "team_total_goals_first_period": 1531,
        "team_shots_on_goal": 1532,
    },
    "golf_pga": {
        # These must be discovered at runtime from the API response.
        # Fetch any golf eventgroup and inspect offerCategories[].offerCategoryId
        # paired with offerCategories[].name to build this mapping.
        # Common golf offerCategory names on DK:
        #   "Tournament Lines", "Top Finish", "Matchups", "Round Props",
        #   "Tournament Props", "Golfer Parlays", "Golfer Props", "Nationality Props"
    },
}

# Human-readable market name patterns on the DK Nash endpoint used to resolve
# category IDs at runtime. `discover_prop_categories(sport)` matches these
# substrings (case-insensitive) against offerCategory / subcategory names.
DK_PROP_NAME_PATTERNS = {
    "baseball_mlb": {
        "pitcher_strikeouts": ["pitcher strikeouts", "strikeouts thrown", "strikeouts (pitcher)"],
        "pitcher_outs_recorded": ["outs recorded", "pitcher outs"],
        "pitcher_earned_runs": ["earned runs allowed", "earned runs"],
        "pitcher_walks": ["walks allowed", "walks issued", "bases on balls"],
        "pitcher_hits_allowed": ["hits allowed"],
        "batter_total_bases": ["total bases"],
        "batter_hits": ["hits (batter)", "batter hits", "to record a hit"],
        "batter_runs": ["runs scored", "batter runs"],
        "batter_rbis": ["runs batted in", "rbi"],
        "batter_home_runs": ["home runs", "to hit a home run"],
        "batter_stolen_bases": ["stolen bases", "to steal a base"],
        "team_first_5_innings_total": ["first 5 innings total", "1st 5 innings", "f5 total"],
        "first_inning_nrfi_yrfi": ["first inning", "nrfi", "yrfi", "no runs first inning"],
    },
    "icehockey_nhl": {
        "skater_shots_on_goal": ["shots on goal", "player shots on goal"],
        "skater_points": ["points (skater)", "skater points", "player points"],
        "skater_goals": ["goalscorer", "anytime goalscorer", "to score"],
        "skater_assists": ["assists"],
        "skater_hits": ["hits"],
        "skater_blocks": ["blocked shots", "blocks"],
        "goalie_saves": ["goalie saves", "saves"],
        "goalie_goals_against": ["goals against"],
        "team_total_goals": ["team total goals", "team total"],
        "team_total_goals_first_period": ["first period total", "1st period total"],
        "team_shots_on_goal": ["team shots on goal"],
    },
}


def _sport_title(sport_key: str) -> str:
    """Map sport key to display title."""
    titles = {
        "basketball_nba": "NBA",
        "americanfootball_nfl": "NFL",
        "icehockey_nhl": "NHL",
        "basketball_ncaab": "NCAAB",
        "baseball_mlb": "MLB",
        "golf_pga": "PGA Tour",
    }
    return titles.get(sport_key, sport_key)
