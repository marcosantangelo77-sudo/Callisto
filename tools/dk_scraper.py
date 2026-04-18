"""
DraftKings live odds scraper — free, unlimited pregame odds.

Uses the DraftKings Nash sportsbook content API which returns full
event/market/selection data without Akamai bot blocking. Falls back
to the legacy v5 eventgroups endpoint if curl_cffi is unavailable.

Zero API cost. Rate-limited to 1 request per 2 seconds to be polite.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

# curl_cffi is the preferred HTTP client — it impersonates a real browser TLS
# fingerprint and bypasses Akamai/Cloudflare bot detection on the nash endpoint.
# If not installed, we fall back to httpx (which will 403 on old endpoints).
try:
    from curl_cffi.requests import Session as CffiSession
    _HAS_CURL_CFFI = True
except ImportError:
    _HAS_CURL_CFFI = False

logger = logging.getLogger("callisto.dk_scraper")

# ---------------------------------------------------------------------------
# Nash endpoint (primary — no Akamai blocking)
# ---------------------------------------------------------------------------
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
    "golf_pga": {
        # These must be discovered at runtime from the API response.
        # Fetch any golf eventgroup and inspect offerCategories[].offerCategoryId
        # paired with offerCategories[].name to build this mapping.
        # Common golf offerCategory names on DK:
        #   "Tournament Lines", "Top Finish", "Matchups", "Round Props",
        #   "Tournament Props", "Golfer Parlays", "Golfer Props", "Nationality Props"
    },
}

# Rate limiting
_last_request_time: float = 0.0
_RATE_LIMIT_SECONDS = 2.0

# Shared clients
_client: Optional[httpx.AsyncClient] = None
_cffi_session: Optional["CffiSession"] = None  # type: ignore[name-defined]

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://sportsbook.draftkings.com/",
}


def _get_client() -> httpx.AsyncClient:
    """Legacy httpx client (fallback when curl_cffi unavailable)."""
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


async def _rate_limited_get(url: str) -> httpx.Response:
    """GET with rate limiting via legacy httpx — 1 request per 2 seconds."""
    global _last_request_time
    now = time.monotonic()
    elapsed = now - _last_request_time
    if elapsed < _RATE_LIMIT_SECONDS:
        await asyncio.sleep(_RATE_LIMIT_SECONDS - elapsed)
    _last_request_time = time.monotonic()

    client = _get_client()
    resp = await client.get(url)
    resp.raise_for_status()
    return resp


def _cffi_get_sync(url: str) -> dict:
    """Synchronous GET via curl_cffi with Chrome impersonation. Returns parsed JSON."""
    session = _get_cffi_session()
    resp = session.get(url, timeout=15)
    resp.raise_for_status()
    return resp.json()


async def _nash_get(url: str) -> dict:
    """
    Async wrapper around the synchronous curl_cffi GET.
    Uses asyncio.to_thread() so the event loop isn't blocked.
    Rate-limited to 1 request per 2 seconds.
    """
    global _last_request_time
    now = time.monotonic()
    elapsed = now - _last_request_time
    if elapsed < _RATE_LIMIT_SECONDS:
        await asyncio.sleep(_RATE_LIMIT_SECONDS - elapsed)
    _last_request_time = time.monotonic()

    return await asyncio.to_thread(_cffi_get_sync, url)


def _dk_american_odds(price: float) -> int:
    """Convert DraftKings decimal price to American odds."""
    if price >= 2.0:
        return round((price - 1) * 100)
    elif price > 1.0:
        return round(-100 / (price - 1))
    else:
        return -10000  # Edge case


def _parse_nash_american_odds(odds_str: str) -> int:
    """
    Parse American odds string from the Nash endpoint.

    The Nash API returns displayOdds.american as strings that may use
    the Unicode MINUS SIGN (U+2212, '−') instead of a regular ASCII
    hyphen-minus (U+002D, '-'). Examples: '−112', '+150', '−5.5'.
    """
    if not odds_str:
        return 0
    # Replace Unicode minus (U+2212) and EN DASH (U+2013) with ASCII minus
    cleaned = odds_str.replace("\u2212", "-").replace("\u2013", "-").replace("+", "")
    try:
        return int(round(float(cleaned)))
    except (ValueError, TypeError):
        return 0


# ---------------------------------------------------------------------------
# Nash endpoint normalization
# ---------------------------------------------------------------------------

_NASH_MARKET_TYPE_MAP = {
    "moneyline": "h2h",
    "spread": "spreads",
    "total": "totals",
}


def _normalize_nash_response(data: dict, sport: str) -> dict:
    """
    Convert the Nash endpoint flat response into the standard Callisto
    odds format (same shape as odds_api.get_odds / the old v5 scraper).

    Nash response has three top-level arrays: events, markets, selections.
    They are linked by eventId (events<->markets) and marketId (markets<->selections).
    """
    events_raw = data.get("events") or []
    markets_raw = data.get("markets") or []
    selections_raw = data.get("selections") or []

    # --- Build event map: eventId -> metadata ---
    event_map = {}  # eventId -> {home_team, away_team, commence_time}
    for ev in events_raw:
        eid = str(ev.get("id", ""))
        if not eid:
            continue
        participants = ev.get("participants") or []
        home = away = ""
        for p in participants:
            role = (p.get("venueRole") or "").lower()
            raw_name = p.get("name", "")
            full_name = _expand_dk_short_name(raw_name)
            if role == "home":
                home = full_name
            elif role == "away":
                away = full_name
        # Fallback: parse event name "Away @ Home"
        if not home or not away:
            name = ev.get("name", "")
            parts = name.replace(" vs ", " @ ").split(" @ ")
            if len(parts) >= 2:
                away = away or _expand_dk_short_name(parts[0].strip())
                home = home or _expand_dk_short_name(parts[1].strip())
            elif not away:
                away = _expand_dk_short_name(name)

        event_map[eid] = {
            "home_team": home,
            "away_team": away,
            "commence_time": ev.get("startEventDate", ""),
        }

    # --- Build market map: marketId -> {eventId, market_key} ---
    market_info = {}  # marketId -> {eventId, market_key}
    for mkt in markets_raw:
        mid = str(mkt.get("id", ""))
        eid = str(mkt.get("eventId", ""))
        mtype = (mkt.get("marketType") or {})
        type_name = (mtype.get("name") or mkt.get("name") or "").lower().strip()
        market_key = _NASH_MARKET_TYPE_MAP.get(type_name)
        if mid and eid and market_key:
            market_info[mid] = {"eventId": eid, "market_key": market_key}

    # --- Group selections by event and market type ---
    # {eventId: {"h2h": [...], "spreads": [...], "totals": [...]}}
    offers_by_event: dict[str, dict[str, list]] = {}
    for sel in selections_raw:
        mid = str(sel.get("marketId", ""))
        minfo = market_info.get(mid)
        if not minfo:
            continue
        eid = minfo["eventId"]
        mkey = minfo["market_key"]

        if eid not in offers_by_event:
            offers_by_event[eid] = {"h2h": [], "spreads": [], "totals": []}

        # Parse odds
        display_odds = sel.get("displayOdds") or {}
        american_str = display_odds.get("american", "")
        price = _parse_nash_american_odds(american_str)

        # If no American odds, fall back to trueOdds (decimal)
        if price == 0:
            true_odds = sel.get("trueOdds")
            if true_odds and float(true_odds) > 1.0:
                price = _dk_american_odds(float(true_odds))

        if price == 0:
            continue

        # Selection label — expand DK abbreviations
        label = _expand_dk_short_name(sel.get("label", ""))

        # For totals, normalize to Over/Under
        if mkey == "totals":
            outcome_type = (sel.get("outcomeType") or "").lower()
            if outcome_type == "over" or "over" in label.lower():
                label = "Over"
            elif outcome_type == "under" or "under" in label.lower():
                label = "Under"

        entry: dict = {"name": label, "price": price}

        # Point / line (spreads and totals)
        points = sel.get("points")
        if points is not None:
            try:
                entry["point"] = float(points)
            except (ValueError, TypeError):
                pass

        offers_by_event[eid][mkey].append(entry)

    # --- Assemble final games list ---
    games = []
    for eid, offers in offers_by_event.items():
        meta = event_map.get(eid)
        if not meta:
            continue

        markets = []
        for key in ("h2h", "spreads", "totals"):
            if offers.get(key):
                markets.append({
                    "key": key,
                    "last_update": datetime.now(timezone.utc).isoformat(),
                    "outcomes": offers[key],
                })

        if not markets:
            continue

        games.append({
            "id": f"dk_{eid}",
            "sport_key": sport,
            "sport_title": _sport_title(sport),
            "home_team": meta["home_team"],
            "away_team": meta["away_team"],
            "commence_time": meta["commence_time"],
            "bookmakers": [{
                "key": "draftkings",
                "title": "DraftKings",
                "last_update": datetime.now(timezone.utc).isoformat(),
                "markets": markets,
            }],
        })

    return {
        "sport": sport,
        "game_count": len(games),
        "games": games,
        "source": "dk_scraper",
        "credits": {"remaining": None, "used": None, "api_key_set": True},
    }


# ---------------------------------------------------------------------------
# Legacy v5 endpoint helpers (kept for fallback and golf/props which still
# use the old eventgroup format)
# ---------------------------------------------------------------------------

def _extract_events(data: dict) -> list[dict]:
    """Extract event list from DK eventgroup response."""
    events = []
    event_group = data.get("eventGroup", {})
    if not event_group:
        return events

    # Events can be nested under offerCategories or directly
    # DK structure: eventGroup -> offerCategories[] -> offerSubcategoryDescriptors[] -> offerSubcategory -> offers[][]
    # Also: eventGroup -> events[]

    raw_events = event_group.get("events", [])
    if not raw_events:
        # Try alternate path
        for cat in event_group.get("offerCategories", []):
            for sub in cat.get("offerSubcategoryDescriptors", []):
                sub_offers = sub.get("offerSubcategory", {})
                for offer_group in sub_offers.get("offers", []):
                    for offer in offer_group:
                        eid = offer.get("eventId")
                        if eid and not any(e.get("eventId") == eid for e in raw_events):
                            raw_events.append({"eventId": eid})

    return raw_events


def _extract_offers(data: dict) -> dict:
    """
    Extract offers grouped by eventId from DK response.

    Returns: {eventId: {"h2h": [...], "spreads": [...], "totals": [...]}}
    """
    offers_by_event = {}
    event_group = data.get("eventGroup", {})

    for cat in event_group.get("offerCategories", []):
        cat_name = (cat.get("name") or "").lower()

        for sub_desc in cat.get("offerSubcategoryDescriptors", []):
            sub_name = (sub_desc.get("name") or "").lower()
            sub_cat = sub_desc.get("offerSubcategory", {})

            for offer_group in sub_cat.get("offers", []):
                for offer in offer_group:
                    event_id = str(offer.get("eventId", ""))
                    if not event_id:
                        continue

                    if event_id not in offers_by_event:
                        offers_by_event[event_id] = {"h2h": [], "spreads": [], "totals": []}

                    label = offer.get("label", "").lower()
                    outcomes = offer.get("outcomes", [])

                    market_key = _classify_market(cat_name, sub_name, label, offer)

                    if market_key and outcomes:
                        parsed = _parse_outcomes(outcomes, market_key)
                        if parsed:
                            offers_by_event[event_id][market_key].extend(parsed)

    return offers_by_event


def _classify_market(cat_name: str, sub_name: str, label: str, offer: dict) -> Optional[str]:
    """Classify a DK offer into h2h, spreads, or totals."""
    # DK uses different naming conventions
    offer_cat_id = offer.get("offerCategoryId", 0)
    offer_sub_id = offer.get("offerSubcategoryId", 0)

    # Moneyline / h2h
    if any(kw in sub_name for kw in ("moneyline", "money line", "winner")):
        return "h2h"
    if any(kw in label for kw in ("moneyline", "money line")):
        return "h2h"

    # Spread / handicap
    if any(kw in sub_name for kw in ("spread", "handicap", "point spread")):
        return "spreads"
    if any(kw in label for kw in ("spread", "handicap")):
        return "spreads"

    # Totals / over-under
    if any(kw in sub_name for kw in ("total", "over/under", "over under")):
        return "totals"
    if any(kw in label for kw in ("total", "over/under")):
        return "totals"

    # Fallback: check outcomes for clues
    outcomes = offer.get("outcomes", [])
    if outcomes:
        names = [o.get("label", "").lower() for o in outcomes]
        if any("over" in n for n in names) and any("under" in n for n in names):
            return "totals"
        # 2-way with team names = probably moneyline
        if len(outcomes) == 2 and not any(o.get("line") for o in outcomes):
            return "h2h"
        if len(outcomes) == 2 and all(o.get("line") for o in outcomes):
            return "spreads"

    return None


def _parse_outcomes(outcomes: list[dict], market_key: str) -> list[dict]:
    """Parse DK outcomes into normalized format."""
    parsed = []
    for o in outcomes:
        price_decimal = o.get("oddsDecimal", 0) or o.get("odds", 0)
        price_american = o.get("oddsAmerican")

        # Use American odds if provided, otherwise convert
        if price_american is not None:
            try:
                price = int(price_american.replace("+", "")) if isinstance(price_american, str) else int(price_american)
            except (ValueError, TypeError):
                price = _dk_american_odds(float(price_decimal)) if price_decimal else 0
        elif price_decimal:
            price = _dk_american_odds(float(price_decimal))
        else:
            continue

        entry = {
            "name": o.get("label", ""),
            "price": price,
        }

        line = o.get("line")
        if line is not None:
            try:
                entry["point"] = float(line)
            except (ValueError, TypeError):
                pass

        # For totals, use Over/Under as name
        if market_key == "totals":
            label = o.get("label", "").strip()
            if label.lower().startswith("over"):
                entry["name"] = "Over"
            elif label.lower().startswith("under"):
                entry["name"] = "Under"

        parsed.append(entry)
    return parsed


def _build_event_map(data: dict) -> dict:
    """Build event metadata map from DK response: eventId -> {home_team, away_team, commence_time}."""
    event_map = {}
    event_group = data.get("eventGroup", {})

    for event in event_group.get("events", []):
        eid = str(event.get("eventId", ""))
        if not eid:
            continue

        name = event.get("name", "")
        # DK format: "Away Team @ Home Team" or "Away Team vs Home Team"
        teams = name.replace(" vs ", " @ ").split(" @ ")
        away = teams[0].strip() if len(teams) >= 2 else name
        home = teams[1].strip() if len(teams) >= 2 else ""

        start_date = event.get("startDate", "")
        # Convert DK date to ISO format if needed
        commence_time = start_date

        event_map[eid] = {
            "home_team": home,
            "away_team": away,
            "commence_time": commence_time,
        }

    return event_map


async def scrape_dk_odds(sport: str) -> dict:
    """
    Scrape DraftKings pregame odds for a sport.

    Returns data in the same format as tools/odds_api.get_odds() so the
    rest of the system can consume it interchangeably.

    Primary path: Nash sportsbook content API via curl_cffi (no Akamai blocking).
    Fallback: Legacy v5 eventgroups endpoint via httpx (may 403).

    Args:
        sport: Sport key matching odds_api conventions
               ('basketball_nba', 'americanfootball_nfl', 'icehockey_nhl',
                'basketball_ncaab', 'baseball_mlb')

    Returns:
        Dict with 'sport', 'game_count', 'games' list, and 'source': 'dk_scraper'
    """
    league_id = LEAGUE_IDS.get(sport)
    if not league_id and sport not in DK_ENDPOINTS:
        logger.warning(f"No DK endpoint for sport: {sport}")
        return {"error": f"Unsupported sport: {sport}", "games": []}

    # --- Primary path: Nash endpoint via curl_cffi ---
    if _HAS_CURL_CFFI and league_id:
        nash_url = f"{_NASH_BASE}/{league_id}"
        try:
            data = await _nash_get(nash_url)
            result = _normalize_nash_response(data, sport)
            logger.info(f"DK Nash scrape {sport}: {result['game_count']} games found")
            return result
        except Exception as e:
            logger.warning(f"DK Nash scrape failed for {sport}, falling back to legacy: {e}")
            # Fall through to legacy path

    # --- Fallback: legacy v5 eventgroups via httpx (likely 403) ---
    url = DK_ENDPOINTS.get(sport)
    if not url:
        return {"error": f"No legacy endpoint for {sport} and Nash failed", "games": []}

    try:
        resp = await _rate_limited_get(url)
        data = resp.json()

        event_map = _build_event_map(data)
        offers_by_event = _extract_offers(data)

        games = []
        for event_id, offers in offers_by_event.items():
            meta = event_map.get(event_id, {})
            if not meta:
                continue

            markets = []
            for key in ("h2h", "spreads", "totals"):
                if offers.get(key):
                    markets.append({
                        "key": key,
                        "last_update": datetime.now(timezone.utc).isoformat(),
                        "outcomes": offers[key],
                    })

            if not markets:
                continue

            games.append({
                "id": f"dk_{event_id}",
                "sport_key": sport,
                "sport_title": _sport_title(sport),
                "home_team": meta["home_team"],
                "away_team": meta["away_team"],
                "commence_time": meta["commence_time"],
                "bookmakers": [{
                    "key": "draftkings",
                    "title": "DraftKings",
                    "last_update": datetime.now(timezone.utc).isoformat(),
                    "markets": markets,
                }],
            })

        logger.info(f"DK legacy scrape {sport}: {len(games)} games found")
        return {
            "sport": sport,
            "game_count": len(games),
            "games": games,
            "source": "dk_scraper",
            "credits": {"remaining": None, "used": None, "api_key_set": True},
        }

    except httpx.HTTPStatusError as e:
        logger.error(f"DK scrape HTTP error for {sport}: {e.response.status_code}")
        return {"error": f"DK HTTP {e.response.status_code}", "games": []}
    except httpx.TimeoutException:
        logger.error(f"DK scrape timeout for {sport}")
        return {"error": "DK request timeout", "games": []}
    except Exception as e:
        logger.error(f"DK scrape failed for {sport}: {e}")
        return {"error": str(e), "games": []}


async def scrape_dk_props(sport: str, event_id: str) -> dict:
    """
    Scrape DraftKings player props for a specific event.

    Args:
        sport: Sport key ('basketball_nba', etc.)
        event_id: DK event ID (numeric, without 'dk_' prefix)

    Returns:
        Dict with player props organized by player name.
    """
    # Strip dk_ prefix if present
    clean_id = event_id.replace("dk_", "")

    categories = DK_PROP_CATEGORIES.get(sport, {})
    if not categories:
        return {"error": f"No prop categories defined for {sport}", "players": {}}

    # Determine the eventgroup ID from sport
    url = DK_ENDPOINTS.get(sport, "")
    group_id_str = url.split("/eventgroups/")[-1].split("?")[0] if "/eventgroups/" in url else ""
    if not group_id_str:
        return {"error": f"Cannot determine group ID for {sport}", "players": {}}

    players = {}
    errors = []

    for prop_type, cat_id in categories.items():
        try:
            prop_url = (
                f"https://sportsbook.draftkings.com/sites/US-SB/api/v5/"
                f"eventgroups/{group_id_str}/categories/{cat_id}?format=json"
            )
            resp = await _rate_limited_get(prop_url)
            data = resp.json()

            # Extract props from the response
            event_group = data.get("eventGroup", {})
            for cat in event_group.get("offerCategories", []):
                for sub_desc in cat.get("offerSubcategoryDescriptors", []):
                    sub_cat = sub_desc.get("offerSubcategory", {})
                    for offer_group in sub_cat.get("offers", []):
                        for offer in offer_group:
                            # Filter to our event
                            offer_event = str(offer.get("eventId", ""))
                            if offer_event != clean_id:
                                continue

                            for outcome in offer.get("outcomes", []):
                                player = outcome.get("participant", "") or outcome.get("label", "")
                                if not player:
                                    continue

                                price_american = outcome.get("oddsAmerican")
                                price_decimal = outcome.get("oddsDecimal", 0)
                                if price_american:
                                    try:
                                        price = int(str(price_american).replace("+", ""))
                                    except (ValueError, TypeError):
                                        price = _dk_american_odds(float(price_decimal)) if price_decimal else 0
                                elif price_decimal:
                                    price = _dk_american_odds(float(price_decimal))
                                else:
                                    continue

                                line = outcome.get("line")
                                name = outcome.get("label", "")
                                # Determine Over/Under
                                if "over" in name.lower():
                                    ou = "Over"
                                elif "under" in name.lower():
                                    ou = "Under"
                                else:
                                    ou = name

                                if player not in players:
                                    players[player] = []

                                entry = {
                                    "bookmaker": "DraftKings",
                                    "market": prop_type,
                                    "name": ou,
                                    "price": price,
                                }
                                if line is not None:
                                    try:
                                        entry["point"] = float(line)
                                    except (ValueError, TypeError):
                                        pass

                                players[player].append(entry)

        except Exception as e:
            logger.warning(f"DK prop scrape failed for {prop_type}: {e}")
            errors.append(f"{prop_type}: {e}")

    logger.info(f"DK props {sport} event {clean_id}: {len(players)} players")
    return {
        "event_id": event_id,
        "sport": sport,
        "players": players,
        "player_count": len(players),
        "source": "draftkings_scraper",
        "errors": errors if errors else None,
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


# ---------------------------------------------------------------------------
# Golf-specific functions
# ---------------------------------------------------------------------------

# Cache for discovered golf category IDs (populated at runtime)
# Capped at 100 entries to prevent unbounded memory growth during tournament runs.
_golf_category_cache: dict[int, dict[str, int]] = {}
_GOLF_CACHE_MAX = 100


async def discover_golf_categories(eventgroup_id: int) -> dict[str, int]:
    """
    Discover golf offerCategory IDs by fetching an eventgroup and inspecting
    the offerCategories array in the response.

    Returns a dict mapping category name -> offerCategoryId, e.g.:
        {"Tournament Lines": 487, "Matchups": 493, ...}

    These IDs are dynamic and may differ between tournaments.
    """
    if eventgroup_id in _golf_category_cache:
        return _golf_category_cache[eventgroup_id]

    url = f"https://sportsbook.draftkings.com/sites/US-SB/api/v5/eventgroups/{eventgroup_id}?format=json"
    try:
        resp = await _rate_limited_get(url)
        data = resp.json()
        event_group = data.get("eventGroup", {})
        categories = {}
        for cat in event_group.get("offerCategories", []):
            cat_id = cat.get("offerCategoryId")
            cat_name = cat.get("name", "")
            if cat_id and cat_name:
                categories[cat_name] = cat_id

                # Also extract subcategories
                for sub in cat.get("offerSubcategoryDescriptors", []):
                    sub_id = sub.get("subcategoryId")
                    sub_name = sub.get("name", "")
                    if sub_id and sub_name:
                        categories[f"{cat_name}/{sub_name}"] = sub_id

        if len(_golf_category_cache) >= _GOLF_CACHE_MAX:
            oldest = next(iter(_golf_category_cache))
            del _golf_category_cache[oldest]
        _golf_category_cache[eventgroup_id] = categories
        logger.info(f"Discovered {len(categories)} golf categories for eventgroup {eventgroup_id}")
        return categories

    except Exception as e:
        logger.error(f"Failed to discover golf categories for eventgroup {eventgroup_id}: {e}")
        return {}


async def scrape_dk_golf_odds(eventgroup_id: Optional[int] = None) -> dict:
    """
    Scrape DraftKings golf odds for a specific tournament.

    Golf on DK is organized by individual tournament eventgroups, not by tour.
    If no eventgroup_id is provided, iterates through all known golf eventgroups.

    Returns data with tournament winner odds, matchups, top finishes, etc.
    """
    if eventgroup_id:
        targets = {"custom": eventgroup_id}
    else:
        targets = DK_GOLF_EVENTGROUPS

    all_tournaments = []

    for name, eg_id in targets.items():
        url = f"https://sportsbook.draftkings.com/sites/US-SB/api/v5/eventgroups/{eg_id}?format=json"
        try:
            resp = await _rate_limited_get(url)
            data = resp.json()
            event_group = data.get("eventGroup", {})

            if not event_group:
                continue

            tournament_name = event_group.get("name", name)

            # Extract all offer categories and their data
            markets = {}
            for cat in event_group.get("offerCategories", []):
                cat_id = cat.get("offerCategoryId")
                cat_name = cat.get("name", "Unknown")

                for sub_desc in cat.get("offerSubcategoryDescriptors", []):
                    sub_name = sub_desc.get("name", "")
                    sub_cat = sub_desc.get("offerSubcategory", {})
                    market_key = f"{cat_name}: {sub_name}" if sub_name else cat_name

                    outcomes_list = []
                    for offer_group in sub_cat.get("offers", []):
                        for offer in offer_group:
                            for outcome in offer.get("outcomes", []):
                                player = outcome.get("participant") or outcome.get("label", "")
                                price_american = outcome.get("oddsAmerican")
                                price_decimal = outcome.get("oddsDecimal", 0)

                                if price_american:
                                    try:
                                        price = int(str(price_american).replace("+", ""))
                                    except (ValueError, TypeError):
                                        price = _dk_american_odds(float(price_decimal)) if price_decimal else 0
                                elif price_decimal:
                                    price = _dk_american_odds(float(price_decimal))
                                else:
                                    continue

                                entry = {
                                    "name": player,
                                    "price": price,
                                }
                                line = outcome.get("line")
                                if line is not None:
                                    try:
                                        entry["point"] = float(line)
                                    except (ValueError, TypeError):
                                        pass
                                outcomes_list.append(entry)

                    if outcomes_list:
                        markets[market_key] = {
                            "category_id": cat_id,
                            "outcomes": outcomes_list,
                        }

            if markets:
                all_tournaments.append({
                    "tournament": tournament_name,
                    "eventgroup_id": eg_id,
                    "markets": markets,
                    "market_count": len(markets),
                })

        except httpx.HTTPStatusError as e:
            logger.warning(f"DK golf HTTP {e.response.status_code} for {name} (eventgroup {eg_id})")
        except httpx.TimeoutException:
            logger.warning(f"DK golf timeout for {name} (eventgroup {eg_id})")
        except Exception as e:
            logger.warning(f"DK golf scrape failed for {name}: {e}")

    logger.info(f"DK golf scrape: {len(all_tournaments)} tournaments with data")
    return {
        "sport": "golf_pga",
        "tournament_count": len(all_tournaments),
        "tournaments": all_tournaments,
        "source": "draftkings_scraper",
        "credits": {"remaining": None, "used": None, "api_key_set": True},
    }


async def list_dk_golf_tournaments() -> list[dict]:
    """
    Return the list of known DK golf tournament eventgroups with their IDs.
    Useful for discovering which tournaments currently have odds.
    """
    tournaments = []
    for name, eg_id in DK_GOLF_EVENTGROUPS.items():
        tournaments.append({
            "name": name.replace("_", " ").title(),
            "eventgroup_id": eg_id,
            "url": f"https://sportsbook.draftkings.com/sites/US-SB/api/v5/eventgroups/{eg_id}?format=json",
        })
    return tournaments
