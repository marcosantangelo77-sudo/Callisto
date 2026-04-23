"""
BetMGM public API scraper — free, no API key required.

BetMGM (powered by bwin/Entain) exposes a public CDS API for fixture and
betting-offer data. This scraper pulls pregame odds and normalizes them to
match the format returned by tools/odds_api.py so the rest of the system can
consume them interchangeably.

Zero API cost. Rate-limited to 1 request per 2 seconds to be polite.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger("callisto.betmgm_scraper")

# ---------------------------------------------------------------------------
# BetMGM CDS API configuration
# ---------------------------------------------------------------------------

_ACCESS_ID = "OTU4NDk3MzEtOTAyNS00MjQzLWIxNWEtNTI2MjdhNWM3Zjk3"

_BASE_URL = "https://sports.nj.betmgm.com/cds-api/bettingoffer/fixtures"

_DEFAULT_PARAMS = {
    "x-bwin-accessid": _ACCESS_ID,
    "lang": "en-us",
    "country": "US",
    "userCountry": "US",
    "offerMapping": "Ede",
    "scorecard": "true",
    "state": "Latest",
}

# BetMGM sport IDs (numeric identifiers used by the CDS API)
BETMGM_SPORT_IDS = {
    "basketball_nba": 7,
    "americanfootball_nfl": 11,
    "icehockey_nhl": 12,
    "baseball_mlb": 23,
    "golf_pga": 16,
}

# BetMGM competition IDs narrow results to major US leagues
BETMGM_COMPETITION_IDS = {
    "basketball_nba": 6004,
    "americanfootball_nfl": 35,
    "icehockey_nhl": 237,
    "baseball_mlb": 84,
    # Golf does not need a competition filter — sport ID is enough
}

# ---------------------------------------------------------------------------
# Rate limiting & shared client
# ---------------------------------------------------------------------------

_last_request_time: float = 0.0
_RATE_LIMIT_SECONDS = 2.0

_client: Optional[httpx.AsyncClient] = None

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://sports.nj.betmgm.com/",
}


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=15.0, headers=_HEADERS, follow_redirects=True, max_redirects=5,
        )
    return _client


async def close_client() -> None:
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
        _client = None


async def _rate_limited_get(url: str, params: dict | None = None) -> httpx.Response:
    """GET with rate limiting — 1 request per 2 seconds."""
    global _last_request_time
    now = time.monotonic()
    elapsed = now - _last_request_time
    if elapsed < _RATE_LIMIT_SECONDS:
        await asyncio.sleep(_RATE_LIMIT_SECONDS - elapsed)
    _last_request_time = time.monotonic()

    client = _get_client()
    resp = await client.get(url, params=params)
    resp.raise_for_status()
    return resp


# ---------------------------------------------------------------------------
# Odds conversion helpers
# ---------------------------------------------------------------------------

def _decimal_to_american(dec: float) -> int:
    """Convert decimal (European) odds to American odds."""
    if dec >= 2.0:
        return round((dec - 1) * 100)
    elif dec > 1.0:
        return round(-100 / (dec - 1))
    else:
        return -10000


def _fractional_to_american(numerator: float, denominator: float) -> int:
    """Convert fractional odds to American."""
    if denominator == 0:
        return -10000
    ratio = numerator / denominator
    if ratio >= 1.0:
        return round(ratio * 100)
    else:
        return round(-100 / ratio)


def _parse_american_odds(outcome: dict) -> Optional[int]:
    """
    Extract American odds from a BetMGM outcome object.

    BetMGM provides odds in multiple formats depending on the endpoint
    version.  We prefer americanOdds, then fall back to decimal conversion.
    """
    # Direct American odds field
    american = outcome.get("americanOdds")
    if american is not None:
        try:
            return int(american)
        except (ValueError, TypeError):
            pass

    # Decimal odds field (oddsDecimal, odds, or price)
    for key in ("oddsDecimal", "odds", "price"):
        dec = outcome.get(key)
        if dec is not None:
            try:
                return _decimal_to_american(float(dec))
            except (ValueError, TypeError):
                continue

    # Fractional odds
    num = outcome.get("numerator")
    den = outcome.get("denominator")
    if num is not None and den is not None:
        try:
            return _fractional_to_american(float(num), float(den))
        except (ValueError, TypeError):
            pass

    return None


# ---------------------------------------------------------------------------
# Market classification
# ---------------------------------------------------------------------------

# BetMGM uses numeric "optionMarketId" / "betOfferTypeId" values, but the
# names embedded in the JSON are more reliable across API revisions.

_H2H_KEYWORDS = frozenset([
    "moneyline", "money line", "match winner", "match result",
    "to win", "game winner", "fight winner", "tournament winner",
])

_SPREAD_KEYWORDS = frozenset([
    "spread", "handicap", "point spread", "run line", "puck line",
])

_TOTAL_KEYWORDS = frozenset([
    "total", "over/under", "over / under", "total points",
    "total runs", "total goals",
])


def _classify_market(market_name: str) -> Optional[str]:
    """Classify a BetMGM market/offer name into h2h, spreads, or totals."""
    name = market_name.lower().strip()

    for kw in _SPREAD_KEYWORDS:
        if kw in name:
            return "spreads"

    for kw in _TOTAL_KEYWORDS:
        if kw in name:
            return "totals"

    for kw in _H2H_KEYWORDS:
        if kw in name:
            return "h2h"

    return None


def _classify_market_from_outcomes(outcomes: list[dict]) -> Optional[str]:
    """Fallback classification by inspecting outcome labels."""
    labels = [o.get("name", "").lower() for o in outcomes]
    if any("over" in lb for lb in labels) and any("under" in lb for lb in labels):
        return "totals"
    # Two outcomes with a line => spread
    if len(outcomes) == 2 and all(_get_line(o) is not None for o in outcomes):
        return "spreads"
    # Two outcomes, no line => moneyline
    if len(outcomes) == 2 and all(_get_line(o) is None for o in outcomes):
        return "h2h"
    return None


def _get_line(outcome: dict) -> Optional[float]:
    """Extract the point/line value from an outcome."""
    for key in ("line", "handicap", "specialBetValue", "attr"):
        val = outcome.get(key)
        if val is not None:
            try:
                return float(val)
            except (ValueError, TypeError):
                continue
    return None


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def _parse_fixture(fixture: dict) -> Optional[dict]:
    """
    Parse a single BetMGM fixture into the normalized game dict.

    Returns None if the fixture can't be meaningfully parsed (e.g. no
    usable odds or unrecognizable structure).
    """
    # --- Event metadata ---------------------------------------------------
    fixture_id = str(fixture.get("id", fixture.get("fixtureId", "")))

    # Participants: BetMGM nests participants differently across versions
    participants = fixture.get("participants", [])
    home_team = ""
    away_team = ""
    for p in participants:
        # "home" / "away" tag, or positional index
        properties = p.get("properties", {})
        participant_type = (
            properties.get("type", "")
            or p.get("type", "")
            or ""
        ).lower()
        name = p.get("name", {})
        # name can be a string or a dict with "value"
        if isinstance(name, dict):
            name = name.get("value", "")
        if participant_type == "home":
            home_team = name
        elif participant_type == "away":
            away_team = name

    # Fallback: use first two participants if tagging wasn't found
    if not home_team and not away_team and len(participants) >= 2:
        for p in participants:
            n = p.get("name", {})
            if isinstance(n, dict):
                n = n.get("value", "")
            if not away_team:
                away_team = n
            elif not home_team:
                home_team = n

    # Additional fallback: fixture name like "Away @ Home" or "Away vs Home"
    if not home_team and not away_team:
        fixture_name = fixture.get("name", {})
        if isinstance(fixture_name, dict):
            fixture_name = fixture_name.get("value", "")
        if isinstance(fixture_name, str) and fixture_name:
            parts = fixture_name.replace(" vs ", " @ ").split(" @ ")
            away_team = parts[0].strip() if len(parts) >= 1 else ""
            home_team = parts[1].strip() if len(parts) >= 2 else ""

    # Start time
    start_date = fixture.get("startDate", fixture.get("startTime", ""))

    # --- Markets / betting offers -----------------------------------------
    games_offers = fixture.get("games", fixture.get("betOffers", fixture.get("optionMarkets", [])))

    markets_by_key: dict[str, list[dict]] = {"h2h": [], "spreads": [], "totals": []}

    for game_offer in games_offers:
        # Market name can live in several places
        market_name = ""
        for name_key in ("name", "betOfferType", "optionMarketName", "templateName"):
            raw = game_offer.get(name_key, "")
            if isinstance(raw, dict):
                raw = raw.get("value", raw.get("name", ""))
            if raw:
                market_name = str(raw)
                break

        # Outcomes / selections
        outcomes_raw = game_offer.get("results", game_offer.get("outcomes", game_offer.get("selections", [])))
        if not outcomes_raw:
            continue

        market_key = _classify_market(market_name)
        if market_key is None:
            market_key = _classify_market_from_outcomes(outcomes_raw)
        if market_key is None:
            continue

        for o in outcomes_raw:
            price = _parse_american_odds(o)
            if price is None:
                continue

            outcome_name = o.get("name", {})
            if isinstance(outcome_name, dict):
                outcome_name = outcome_name.get("value", "")
            outcome_name = str(outcome_name)

            entry: dict = {
                "name": outcome_name,
                "price": price,
            }

            line = _get_line(o)
            if line is not None:
                entry["point"] = line

            # Normalize totals names
            if market_key == "totals":
                ln = outcome_name.lower()
                if "over" in ln:
                    entry["name"] = "Over"
                elif "under" in ln:
                    entry["name"] = "Under"

            markets_by_key[market_key].append(entry)

    # Build markets list (only include non-empty)
    markets = []
    now_iso = datetime.now(timezone.utc).isoformat()
    for key in ("h2h", "spreads", "totals"):
        if markets_by_key[key]:
            markets.append({
                "key": key,
                "last_update": now_iso,
                "outcomes": markets_by_key[key],
            })

    if not markets:
        return None

    return {
        "id": f"betmgm_{fixture_id}",
        "sport_key": "",  # filled by caller
        "sport_title": "",  # filled by caller
        "home_team": home_team,
        "away_team": away_team,
        "commence_time": start_date,
        "bookmakers": [
            {
                "key": "betmgm",
                "title": "BetMGM",
                "last_update": now_iso,
                "markets": markets,
            }
        ],
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def scrape_betmgm_odds(sport: str) -> dict:
    """
    Scrape BetMGM pregame odds for a sport.

    Returns data in the same format as tools/odds_api.get_odds() so the
    rest of the system can consume it interchangeably.

    Args:
        sport: Sport key matching odds_api conventions
               ('basketball_nba', 'americanfootball_nfl', 'icehockey_nhl',
                'baseball_mlb', 'golf_pga')

    Returns:
        Dict with 'sport', 'game_count', 'games' list, and
        'source': 'betmgm_scraper'.
    """
    sport_id = BETMGM_SPORT_IDS.get(sport)
    if sport_id is None:
        logger.warning(f"No BetMGM sport ID for: {sport}")
        return {"error": f"Unsupported sport: {sport}", "games": []}

    params = dict(_DEFAULT_PARAMS)
    params["sportIds"] = str(sport_id)

    competition_id = BETMGM_COMPETITION_IDS.get(sport)
    if competition_id is not None:
        params["competitionIds"] = str(competition_id)

    try:
        resp = await _rate_limited_get(_BASE_URL, params=params)
        data = resp.json()

        # BetMGM wraps fixtures in a top-level key — try several known shapes
        fixtures = _extract_fixtures(data)

        games = []
        for fixture in fixtures:
            parsed = _parse_fixture(fixture)
            if parsed is None:
                continue
            parsed["sport_key"] = sport
            parsed["sport_title"] = _sport_title(sport)
            games.append(parsed)

        logger.info(f"BetMGM scrape {sport}: {len(games)} games found")
        return {
            "sport": sport,
            "game_count": len(games),
            "games": games,
            "source": "betmgm_scraper",
            "credits": {"remaining": None, "used": None, "api_key_set": True},
        }

    except httpx.HTTPStatusError as e:
        logger.error(f"BetMGM scrape HTTP error for {sport}: {e.response.status_code}")
        return {"error": f"BetMGM HTTP {e.response.status_code}", "games": []}
    except httpx.TimeoutException:
        logger.error(f"BetMGM scrape timeout for {sport}")
        return {"error": "BetMGM request timeout", "games": []}
    except Exception as e:
        logger.error(f"BetMGM scrape failed for {sport}: {e}")
        return {"error": str(e), "games": []}


async def scrape_betmgm_fixture(fixture_id: str) -> dict:
    """
    Scrape BetMGM odds for a single fixture by ID.

    Args:
        fixture_id: BetMGM fixture ID (numeric, without 'betmgm_' prefix)

    Returns:
        Parsed game dict or error dict.
    """
    clean_id = fixture_id.replace("betmgm_", "")

    params = dict(_DEFAULT_PARAMS)
    params["fixtureIds"] = clean_id

    try:
        resp = await _rate_limited_get(_BASE_URL, params=params)
        data = resp.json()

        fixtures = _extract_fixtures(data)
        if not fixtures:
            return {"error": f"Fixture {clean_id} not found", "games": []}

        parsed = _parse_fixture(fixtures[0])
        if parsed is None:
            return {"error": "Could not parse fixture", "games": []}

        return parsed

    except Exception as e:
        logger.error(f"BetMGM fixture scrape failed for {clean_id}: {e}")
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_fixtures(data) -> list[dict]:
    """
    Extract the fixture list from a BetMGM CDS API response.

    The API nests fixtures under different keys depending on the endpoint
    version, so we try several known shapes.
    """
    # If the response is already a list of fixtures
    if isinstance(data, list):
        return data

    if not isinstance(data, dict):
        return []

    # Common wrapper keys observed in the CDS API
    for key in ("fixtures", "items", "results", "data", "events"):
        if key in data and isinstance(data[key], list):
            return data[key]

    # Nested under a single wrapper object
    if "fixture" in data and isinstance(data["fixture"], dict):
        return [data["fixture"]]

    # If the response itself looks like a single fixture (has participants)
    if "participants" in data or "games" in data:
        return [data]

    return []


def _sport_title(sport_key: str) -> str:
    """Map sport key to display title."""
    titles = {
        "basketball_nba": "NBA",
        "americanfootball_nfl": "NFL",
        "icehockey_nhl": "NHL",
        "baseball_mlb": "MLB",
        "golf_pga": "PGA Golf",
    }
    return titles.get(sport_key, sport_key)
