"""Team name mapping for Action Network short names."""

# ---------------------------------------------------------------------------
# Team name mapping — Action Network uses short names (mascots only).
# Map to full "City Mascot" names for cross-source matching.
# ---------------------------------------------------------------------------

TEAM_NAME_MAP = {
    # NBA
    "Hawks": "Atlanta Hawks",
    "Celtics": "Boston Celtics",
    "Nets": "Brooklyn Nets",
    "Hornets": "Charlotte Hornets",
    "Bulls": "Chicago Bulls",
    "Cavaliers": "Cleveland Cavaliers",
    "Mavericks": "Dallas Mavericks",
    "Nuggets": "Denver Nuggets",
    "Pistons": "Detroit Pistons",
    "Warriors": "Golden State Warriors",
    "Rockets": "Houston Rockets",
    "Pacers": "Indiana Pacers",
    "Clippers": "Los Angeles Clippers",
    "Lakers": "Los Angeles Lakers",
    "Grizzlies": "Memphis Grizzlies",
    "Heat": "Miami Heat",
    "Bucks": "Milwaukee Bucks",
    "Timberwolves": "Minnesota Timberwolves",
    "Pelicans": "New Orleans Pelicans",
    "Knicks": "New York Knicks",
    "Thunder": "Oklahoma City Thunder",
    "Magic": "Orlando Magic",
    "76ers": "Philadelphia 76ers",
    "Suns": "Phoenix Suns",
    "Trail Blazers": "Portland Trail Blazers",
    "Blazers": "Portland Trail Blazers",
    "Kings": "Sacramento Kings",
    "Spurs": "San Antonio Spurs",
    "Raptors": "Toronto Raptors",
    "Jazz": "Utah Jazz",
    "Wizards": "Washington Wizards",

    # NFL
    "Cardinals": "Arizona Cardinals",
    "Falcons": "Atlanta Falcons",
    "Ravens": "Baltimore Ravens",
    "Bills": "Buffalo Bills",
    "Panthers": "Carolina Panthers",
    "Bears": "Chicago Bears",
    "Bengals": "Cincinnati Bengals",
    "Browns": "Cleveland Browns",
    "Cowboys": "Dallas Cowboys",
    "Broncos": "Denver Broncos",
    "Lions": "Detroit Lions",
    "Packers": "Green Bay Packers",
    "Texans": "Houston Texans",
    "Colts": "Indianapolis Colts",
    "Jaguars": "Jacksonville Jaguars",
    "Chiefs": "Kansas City Chiefs",
    "Raiders": "Las Vegas Raiders",
    "Chargers": "Los Angeles Chargers",
    "Rams": "Los Angeles Rams",
    "Dolphins": "Miami Dolphins",
    "Vikings": "Minnesota Vikings",
    "Patriots": "New England Patriots",
    "Saints": "New Orleans Saints",
    "Giants": "New York Giants",
    "Jets": "New York Jets",
    "Eagles": "Philadelphia Eagles",
    "Steelers": "Pittsburgh Steelers",
    "49ers": "San Francisco 49ers",
    "Seahawks": "Seattle Seahawks",
    "Buccaneers": "Tampa Bay Buccaneers",
    "Titans": "Tennessee Titans",
    "Commanders": "Washington Commanders",

    # NHL
    "Ducks": "Anaheim Ducks",
    "Coyotes": "Arizona Coyotes",
    "Bruins": "Boston Bruins",
    "Sabres": "Buffalo Sabres",
    "Flames": "Calgary Flames",
    "Hurricanes": "Carolina Hurricanes",
    "Blackhawks": "Chicago Blackhawks",
    "Avalanche": "Colorado Avalanche",
    "Blue Jackets": "Columbus Blue Jackets",
    "Stars": "Dallas Stars",
    "Red Wings": "Detroit Red Wings",
    "Oilers": "Edmonton Oilers",
    "Panthers": "Florida Panthers",
    "Kings": "Los Angeles Kings",
    "Wild": "Minnesota Wild",
    "Canadiens": "Montreal Canadiens",
    "Predators": "Nashville Predators",
    "Devils": "New Jersey Devils",
    "Islanders": "New York Islanders",
    "Rangers": "New York Rangers",
    "Senators": "Ottawa Senators",
    "Flyers": "Philadelphia Flyers",
    "Penguins": "Pittsburgh Penguins",
    "Sharks": "San Jose Sharks",
    "Kraken": "Seattle Kraken",
    "Blues": "St. Louis Blues",
    "Lightning": "Tampa Bay Lightning",
    "Maple Leafs": "Toronto Maple Leafs",
    "Utah Hockey Club": "Utah Hockey Club",
    "Canucks": "Vancouver Canucks",
    "Golden Knights": "Vegas Golden Knights",
    "Capitals": "Washington Capitals",
    "Jets": "Winnipeg Jets",

    # MLB
    "Diamondbacks": "Arizona Diamondbacks",
    "D-backs": "Arizona Diamondbacks",
    "Braves": "Atlanta Braves",
    "Orioles": "Baltimore Orioles",
    "Red Sox": "Boston Red Sox",
    "Cubs": "Chicago Cubs",
    "White Sox": "Chicago White Sox",
    "Reds": "Cincinnati Reds",
    "Guardians": "Cleveland Guardians",
    "Rockies": "Colorado Rockies",
    "Tigers": "Detroit Tigers",
    "Astros": "Houston Astros",
    "Royals": "Kansas City Royals",
    "Angels": "Los Angeles Angels",
    "Dodgers": "Los Angeles Dodgers",
    "Marlins": "Miami Marlins",
    "Brewers": "Milwaukee Brewers",
    "Twins": "Minnesota Twins",
    "Mets": "New York Mets",
    "Yankees": "New York Yankees",
    "Athletics": "Oakland Athletics",
    "A's": "Oakland Athletics",
    "Phillies": "Philadelphia Phillies",
    "Pirates": "Pittsburgh Pirates",
    "Padres": "San Diego Padres",
    "Mariners": "Seattle Mariners",
    "Reds": "Cincinnati Reds",
    "Rangers": "Texas Rangers",
    "Blue Jays": "Toronto Blue Jays",
    "Nationals": "Washington Nationals",
}

# Sport-specific overrides for ambiguous mascots that appear in multiple leagues.
# Key: (sport, display_name) -> full team name
_SPORT_SPECIFIC_NAMES = {
    # Panthers: Carolina in NFL, Florida in NHL
    ("americanfootball_nfl", "Panthers"): "Carolina Panthers",
    ("icehockey_nhl", "Panthers"): "Florida Panthers",
    # Kings: Sacramento in NBA, Los Angeles in NHL
    ("basketball_nba", "Kings"): "Sacramento Kings",
    ("icehockey_nhl", "Kings"): "Los Angeles Kings",
    # Jets: New York in NFL, Winnipeg in NHL
    ("americanfootball_nfl", "Jets"): "New York Jets",
    ("icehockey_nhl", "Jets"): "Winnipeg Jets",
    # Rangers: Texas in MLB, New York in NHL
    ("baseball_mlb", "Rangers"): "Texas Rangers",
    ("icehockey_nhl", "Rangers"): "New York Rangers",
    # Cardinals: Arizona in NFL, St. Louis (no longer in MLB but keeping for safety)
    ("americanfootball_nfl", "Cardinals"): "Arizona Cardinals",
    # Stars: Dallas in NHL
    ("icehockey_nhl", "Stars"): "Dallas Stars",
    # Blues: St. Louis in NHL
    ("icehockey_nhl", "Blues"): "St. Louis Blues",
}


def _resolve_team_name(display_name: str, sport: str) -> str:
    """
    Resolve an Action Network short team name to a full name.

    Checks sport-specific overrides first (for ambiguous mascots like
    Panthers, Kings, Jets), then falls back to the general mapping.
    If no mapping is found, returns the display_name as-is.
    """
    # Sport-specific override
    specific = _SPORT_SPECIFIC_NAMES.get((sport, display_name))
    if specific:
        return specific

    # General mapping
    mapped = TEAM_NAME_MAP.get(display_name)
    if mapped:
        return mapped

    # No mapping — return as-is
    return display_name
