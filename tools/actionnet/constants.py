"""Static mappings for the Action Network scraper."""

# ---------------------------------------------------------------------------
# API configuration
# ---------------------------------------------------------------------------

_API_BASE = "https://api.actionnetwork.com/web/v1/scoreboard"

# Book IDs used in the bookIds query parameter
_BOOK_IDS = "15,30,76,75,69,68,123,972,71"

# Book ID -> (key, title) mapping for output normalization
BOOK_ID_MAP = {
    15: ("draftkings", "DraftKings"),
    30: ("fanduel", "FanDuel"),
    68: ("caesars", "Caesars"),
    69: ("betmgm", "BetMGM"),
    71: ("betrivers", "BetRivers"),
    75: ("pointsbet", "PointsBet"),
    76: ("bet365", "Bet365"),
    123: ("hardrock", "Hard Rock Bet"),
    972: ("espnbet", "ESPNBet"),
}

# Sport key -> Action Network league slug
LEAGUE_MAP = {
    "basketball_nba": "nba",
    "americanfootball_nfl": "nfl",
    "basketball_ncaab": "ncaab",
    "basketball_ncaaw": "ncaaw",
    "icehockey_nhl": "nhl",
    "baseball_mlb": "mlb",
}

# Sport key -> display title
SPORT_TITLES = {
    "basketball_nba": "NBA",
    "americanfootball_nfl": "NFL",
    "icehockey_nhl": "NHL",
    "basketball_ncaab": "NCAAB",
    "baseball_mlb": "MLB",
}
