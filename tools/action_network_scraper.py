"""
Action Network public API scraper — free, no API key required.

Scrapes odds from Action Network's public scoreboard API, which returns
odds from up to 9 bookmakers: DraftKings, FanDuel, Caesars, BetMGM,
BetRivers, PointsBet, Bet365, Hard Rock Bet, and ESPNBet.

Also provides public betting percentages (ml_home_public / ml_away_public),
which is unique data no other free source exposes.

Zero API cost. Rate-limited to 1 request per 2 seconds to be polite.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

from tools.scraper_utils import (
    classify_status,
    mark_error,
    mark_success,
    register_scraper,
    retry_async,
    retry_sync,
)

try:
    from curl_cffi.requests import Session as CffiSession
    _HAS_CURL_CFFI = True
except ImportError:
    _HAS_CURL_CFFI = False

logger = logging.getLogger("callisto.action_network_scraper")

_SCRAPER_NAME = "action_network_scraper"
register_scraper(_SCRAPER_NAME)

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
_SPORT_TITLES = {
    "basketball_nba": "NBA",
    "americanfootball_nfl": "NFL",
    "icehockey_nhl": "NHL",
    "basketball_ncaab": "NCAAB",
    "baseball_mlb": "MLB",
}

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


# ---------------------------------------------------------------------------
# HTTP client setup (same pattern as dk_scraper.py)
# ---------------------------------------------------------------------------

_last_request_time: float = 0.0
_RATE_LIMIT_SECONDS = 2.0

_client: Optional[httpx.AsyncClient] = None
_cffi_session: Optional["CffiSession"] = None  # type: ignore[name-defined]

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.actionnetwork.com/",
}


def _get_client() -> httpx.AsyncClient:
    """Get or create an httpx async client (fallback when curl_cffi unavailable)."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=15.0, headers=_HEADERS, follow_redirects=True, max_redirects=5)
    return _client


def _get_cffi_session() -> "CffiSession":  # type: ignore[name-defined]
    """Get or create a curl_cffi session with Chrome TLS impersonation."""
    global _cffi_session
    if _cffi_session is None:
        _cffi_session = CffiSession(impersonate="chrome131")
    return _cffi_session


async def close_client() -> None:
    """Close HTTP clients and free resources."""
    global _client, _cffi_session
    if _client and not _client.is_closed:
        await _client.aclose()
        _client = None
    if _cffi_session is not None:
        try:
            _cffi_session.close()
        except Exception as e:
            logger.info(f"cffi session close error (non-critical): {e}")
        _cffi_session = None


def _cffi_get_sync(url: str) -> dict:
    """Synchronous GET via curl_cffi with Chrome impersonation. Returns parsed JSON."""
    def _once() -> dict:
        session = _get_cffi_session()
        resp = session.get(url, headers=_HEADERS, timeout=15)
        classify_status(resp.status_code, resp.headers.get("Retry-After") if hasattr(resp, "headers") else None)
        return resp.json()

    return retry_sync(_once, scraper=_SCRAPER_NAME)


async def _rate_limited_get(url: str) -> dict:
    """
    Async GET with rate limiting + retry. Prefers curl_cffi, falls back to httpx.
    Returns parsed JSON dict.
    """
    global _last_request_time
    now = time.monotonic()
    elapsed = now - _last_request_time
    if elapsed < _RATE_LIMIT_SECONDS:
        await asyncio.sleep(_RATE_LIMIT_SECONDS - elapsed)
    _last_request_time = time.monotonic()

    if _HAS_CURL_CFFI:
        return await asyncio.to_thread(_cffi_get_sync, url)

    async def _once() -> dict:
        client = _get_client()
        resp = await client.get(url)
        classify_status(resp.status_code, resp.headers.get("Retry-After"))
        return resp.json()

    return await retry_async(_once, scraper=_SCRAPER_NAME)


# ---------------------------------------------------------------------------
# Core scraper
# ---------------------------------------------------------------------------

def _build_url(sport: str, date_str: Optional[str] = None) -> str:
    """Build the Action Network scoreboard API URL."""
    league = LEAGUE_MAP.get(sport)
    if not league:
        raise ValueError(f"Unsupported sport: {sport}. Supported: {list(LEAGUE_MAP.keys())}")
    if date_str is None:
        date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"{_API_BASE}/{league}?period=game&bookIds={_BOOK_IDS}&date={date_str}"


def _parse_game(game_data: dict, sport: str) -> Optional[dict]:
    """
    Parse a single game from the Action Network response into
    the standard Callisto odds format.
    """
    # Extract teams
    teams = game_data.get("teams", [])
    if len(teams) < 2:
        return None

    home_team_raw = None
    away_team_raw = None
    for team in teams:
        # Prefer full_name (e.g. "Charlotte Hornets") over display_name ("Hornets")
        name = team.get("full_name") or team.get("display_name", "")
        if team.get("is_home") is True:
            home_team_raw = name
        elif team.get("is_away") is True:
            away_team_raw = name

    # Fallback: if is_home/is_away not set, use position.
    # Action Network API returns teams[0]=HOME, teams[1]=AWAY.
    # ml_home/ml_away in odds correspond to teams[0]/teams[1] respectively.
    if home_team_raw is None or away_team_raw is None:
        if len(teams) >= 2:
            t0 = teams[0].get("full_name") or teams[0].get("display_name", "Unknown")
            t1 = teams[1].get("full_name") or teams[1].get("display_name", "Unknown")
            home_team_raw = home_team_raw or t0
            away_team_raw = away_team_raw or t1
        else:
            return None

    # If full_name was available, use it directly; otherwise resolve short name
    home_team = home_team_raw if " " in home_team_raw else _resolve_team_name(home_team_raw, sport)
    away_team = away_team_raw if " " in away_team_raw else _resolve_team_name(away_team_raw, sport)

    # Commence time
    start_time = game_data.get("start_time", "")
    if start_time:
        # Action Network returns ISO format — normalize to Z suffix
        if not start_time.endswith("Z") and "+" not in start_time:
            start_time = start_time + "Z"
    else:
        start_time = datetime.now(timezone.utc).isoformat()

    # Game ID
    game_id = game_data.get("id", "")
    normalized_id = f"action_{game_id}" if game_id else f"action_{home_team}_{away_team}"

    # Parse odds from each bookmaker
    odds_list = game_data.get("odds", [])
    bookmakers = []

    for odds_entry in odds_list:
        book_id = odds_entry.get("book_id")
        book_info = BOOK_ID_MAP.get(book_id)
        if not book_info:
            continue  # Unknown book — skip

        book_key, book_title = book_info
        markets = []

        # Moneyline (h2h)
        ml_home = odds_entry.get("ml_home")
        ml_away = odds_entry.get("ml_away")
        if ml_home is not None and ml_away is not None:
            try:
                markets.append({
                    "key": "h2h",
                    "outcomes": [
                        {"name": home_team, "price": int(ml_home)},
                        {"name": away_team, "price": int(ml_away)},
                    ],
                })
            except (ValueError, TypeError):
                pass

        # Spreads
        spread_home = odds_entry.get("spread_home")
        spread_away = odds_entry.get("spread_away")
        spread_home_line = odds_entry.get("spread_home_line")
        spread_away_line = odds_entry.get("spread_away_line")
        if all(v is not None for v in [spread_home, spread_away, spread_home_line, spread_away_line]):
            try:
                markets.append({
                    "key": "spreads",
                    "outcomes": [
                        {"name": home_team, "price": int(spread_home_line), "point": float(spread_home)},
                        {"name": away_team, "price": int(spread_away_line), "point": float(spread_away)},
                    ],
                })
            except (ValueError, TypeError):
                pass

        # Totals
        total = odds_entry.get("total")
        over = odds_entry.get("over")
        under = odds_entry.get("under")
        if all(v is not None for v in [total, over, under]):
            try:
                markets.append({
                    "key": "totals",
                    "outcomes": [
                        {"name": "Over", "price": int(over), "point": float(total)},
                        {"name": "Under", "price": int(under), "point": float(total)},
                    ],
                })
            except (ValueError, TypeError):
                pass

        if markets:
            bookmakers.append({
                "key": book_key,
                "title": book_title,
                "last_update": datetime.now(timezone.utc).isoformat(),
                "markets": markets,
            })

    if not bookmakers:
        return None

    return {
        "id": normalized_id,
        "sport_key": sport,
        "sport_title": _SPORT_TITLES.get(sport, sport),
        "home_team": home_team,
        "away_team": away_team,
        "commence_time": start_time,
        "bookmakers": bookmakers,
    }


async def scrape_action_network(sport: str, date_str: Optional[str] = None) -> dict:
    """
    Scrape Action Network pregame odds for a sport.

    Returns data in the same format as tools/odds_api.get_odds() so the
    rest of the system can consume it interchangeably.

    Args:
        sport: Callisto sport key (e.g., "basketball_nba", "americanfootball_nfl")
        date_str: Optional date in YYYYMMDD format. Defaults to today (UTC).

    Returns:
        Standard Callisto odds dict with games, bookmakers, and markets.
    """
    if sport not in LEAGUE_MAP:
        return {
            "sport": sport,
            "game_count": 0,
            "games": [],
            "source": "action_network",
            "error": f"Unsupported sport: {sport}. Supported: {list(LEAGUE_MAP.keys())}",
        }

    try:
        url = _build_url(sport, date_str)
        logger.info(f"Scraping Action Network {LEAGUE_MAP[sport].upper()}: {url}")
        data = await _rate_limited_get(url)
    except Exception as e:
        logger.error(f"Action Network request failed for {sport}: {e}")
        mark_error(_SCRAPER_NAME, f"{sport}: {e}")
        return {
            "sport": sport,
            "game_count": 0,
            "games": [],
            "source": "action_network",
            "error": str(e),
        }

    raw_games = (data or {}).get("games") or []
    games = []
    for raw_game in raw_games:
        parsed = _parse_game(raw_game, sport)
        if parsed:
            games.append(parsed)

    logger.info(
        f"Action Network {sport}: {len(games)} games with odds "
        f"(from {len(raw_games)} total games)"
    )
    mark_success(_SCRAPER_NAME)

    return {
        "sport": sport,
        "game_count": len(games),
        "games": games,
        "source": "action_network",
        "credits": {"remaining": None, "used": None, "api_key_set": True},
    }


# ---------------------------------------------------------------------------
# Public betting percentages — unique data from Action Network
# ---------------------------------------------------------------------------

def _extract_public_betting(game_data: dict, sport: str) -> Optional[dict]:
    """Extract public betting percentages from a single game's odds entries."""
    teams = game_data.get("teams", [])
    if len(teams) < 2:
        return None

    home_team_raw = None
    away_team_raw = None
    for team in teams:
        name = team.get("full_name") or team.get("display_name", "")
        if team.get("is_home") is True:
            home_team_raw = name
        elif team.get("is_away") is True:
            away_team_raw = name

    if not home_team_raw or not away_team_raw:
        if len(teams) >= 2:
            t0 = teams[0].get("full_name") or teams[0].get("display_name", "Unknown")
            t1 = teams[1].get("full_name") or teams[1].get("display_name", "Unknown")
            home_team_raw = home_team_raw or t0
            away_team_raw = away_team_raw or t1
        else:
            return None

    home_team = _resolve_team_name(home_team_raw, sport)
    away_team = _resolve_team_name(away_team_raw, sport)

    # Collect public betting data from all books that report it
    public_entries = []
    for odds_entry in game_data.get("odds", []):
        ml_home_public = odds_entry.get("ml_home_public")
        ml_away_public = odds_entry.get("ml_away_public")
        spread_home_public = odds_entry.get("spread_home_public")
        spread_away_public = odds_entry.get("spread_away_public")
        total_over_public = odds_entry.get("total_over_public")
        total_under_public = odds_entry.get("total_under_public")

        book_id = odds_entry.get("book_id")
        book_info = BOOK_ID_MAP.get(book_id, (str(book_id), str(book_id)))

        entry = {"book_key": book_info[0], "book_title": book_info[1]}
        has_data = False

        if ml_home_public is not None and ml_away_public is not None:
            entry["ml_home_pct"] = ml_home_public
            entry["ml_away_pct"] = ml_away_public
            has_data = True

        if spread_home_public is not None and spread_away_public is not None:
            entry["spread_home_pct"] = spread_home_public
            entry["spread_away_pct"] = spread_away_public
            has_data = True

        if total_over_public is not None and total_under_public is not None:
            entry["total_over_pct"] = total_over_public
            entry["total_under_pct"] = total_under_public
            has_data = True

        if has_data:
            public_entries.append(entry)

    if not public_entries:
        return None

    # Compute average public percentages across all books that report them
    avg = {}
    for field_pair in [("ml_home_pct", "ml_away_pct"),
                       ("spread_home_pct", "spread_away_pct"),
                       ("total_over_pct", "total_under_pct")]:
        vals_a = [e[field_pair[0]] for e in public_entries if field_pair[0] in e]
        vals_b = [e[field_pair[1]] for e in public_entries if field_pair[1] in e]
        if vals_a and vals_b:
            avg[field_pair[0]] = round(sum(vals_a) / len(vals_a), 1)
            avg[field_pair[1]] = round(sum(vals_b) / len(vals_b), 1)

    return {
        "home_team": home_team,
        "away_team": away_team,
        "game_id": game_data.get("id", ""),
        "start_time": game_data.get("start_time", ""),
        "averages": avg,
        "by_book": public_entries,
    }


async def get_public_betting(sport: str, date_str: Optional[str] = None) -> dict:
    """
    Get public betting percentages from Action Network.

    This data shows what percentage of public bets are on each side —
    valuable for contrarian / fading-the-public strategies.

    Args:
        sport: Callisto sport key (e.g., "basketball_nba")
        date_str: Optional date in YYYYMMDD format. Defaults to today (UTC).

    Returns:
        Dict with per-game public betting percentages from all available books.
    """
    if sport not in LEAGUE_MAP:
        return {
            "sport": sport,
            "games": [],
            "error": f"Unsupported sport: {sport}",
        }

    try:
        url = _build_url(sport, date_str)
        logger.info(f"Fetching public betting data from Action Network: {LEAGUE_MAP[sport].upper()}")
        data = await _rate_limited_get(url)
    except Exception as e:
        logger.error(f"Action Network public betting request failed for {sport}: {e}")
        mark_error(_SCRAPER_NAME, f"public {sport}: {e}")
        return {
            "sport": sport,
            "games": [],
            "error": str(e),
        }

    raw_games = (data or {}).get("games") or []
    results = []
    for raw_game in raw_games:
        parsed = _extract_public_betting(raw_game, sport)
        if parsed:
            results.append(parsed)

    logger.info(f"Action Network public betting {sport}: {len(results)} games with data")
    mark_success(_SCRAPER_NAME)

    return {
        "sport": sport,
        "game_count": len(results),
        "games": results,
        "source": "action_network_public_betting",
    }
