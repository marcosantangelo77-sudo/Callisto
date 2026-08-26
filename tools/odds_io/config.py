"""Configuration and shared state for the odds-api.io integration.

Split out of tools/odds_api_io.py — see tools/odds_io package docstring.
"""

import os
from typing import Optional

import httpx

# Configuration
ODDS_API_IO_KEY = os.getenv("ODDS_API_IO_KEY", "")
ODDS_API_IO_BASE = "https://api.odds-api.io/v3"

# Rate limit: 30,000 requests per hour (Pro plan)
HOURLY_LIMIT = 30000

# ---------------------------------------------------------------------------
# Sport/league mapping: Callisto canonical keys -> odds-api.io slugs
# ---------------------------------------------------------------------------

SPORT_MAP = {
    "basketball_nba":       {"sport": "basketball",        "league": "usa-nba"},
    "americanfootball_nfl": {"sport": "american-football",  "league": "usa-nfl"},
    "icehockey_nhl":        {"sport": "ice-hockey",         "league": "usa-nhl"},
    "basketball_ncaab":     {"sport": "basketball",         "league": "usa-ncaa-division-i-national-championship"},
    "baseball_mlb":         {"sport": "baseball",           "league": "usa-mlb"},
    "golf_pga":             {"sport": "golf",               "league": None},  # varies per tournament
    # Aliases
    "nba":   {"sport": "basketball",       "league": "usa-nba"},
    "nfl":   {"sport": "american-football", "league": "usa-nfl"},
    "nhl":   {"sport": "ice-hockey",        "league": "usa-nhl"},
    "ncaab": {"sport": "basketball",        "league": "usa-ncaa-division-i-national-championship"},
    "mlb":   {"sport": "baseball",          "league": "usa-mlb"},
}

# Display titles
SPORT_TITLES = {
    "basketball_nba": "NBA",
    "americanfootball_nfl": "NFL",
    "icehockey_nhl": "NHL",
    "basketball_ncaab": "NCAAB",
    "baseball_mlb": "MLB",
    "golf_pga": "PGA Golf",
}

# Pro plan: 15 bookmakers selected via /bookmakers/selected/select
SELECTED_BOOKMAKERS = (
    "DraftKings,Fanatics,FanDuel,BetMGM,Caesars,BetRivers,bet365 NJ,"
    "Hard Rock,Bovada,Circa,BetOnline.ag,WilliamHill NJ,"
    "Betfair Exchange,Betfair Sportsbook,Sbobet"
)

# Bookmaker name -> normalized slug for output
BOOKMAKER_SLUG_MAP = {
    "BetMGM": "betmgm",
    "bet365 NJ": "bet365",
    "DraftKings": "draftkings",
    "FanDuel": "fanduel",
    "Fanatics": "fanatics",
    "Caesars": "caesars",
    "BetRivers": "betrivers",
    "Hard Rock": "hardrock",
    "Bovada": "bovada",
    "Circa": "circa",
    "BetOnline.ag": "betonlineag",
    "WilliamHill NJ": "williamhill",
    "Betfair Exchange": "betfair_exchange",
    "Betfair Sportsbook": "betfair",
    "Sbobet": "sbobet",
    "Pinnacle": "pinnacle",
    "FanDuel NJ": "fanduel",
    "BetMGM NJ": "betmgm",
}


# ---------------------------------------------------------------------------
# Client management
# ---------------------------------------------------------------------------

_client: Optional[httpx.AsyncClient] = None


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=20.0, follow_redirects=True, max_redirects=5)
    return _client


async def close_client() -> None:
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
        _client = None


def resolve_sport(sport: str):
    """Resolve a Callisto sport key (or alias) to its odds-api.io mapping."""
    return SPORT_MAP.get(sport) or SPORT_MAP.get((sport or "").lower().strip())
