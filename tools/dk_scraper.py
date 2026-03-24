"""
DraftKings live odds scraper — free, unlimited pregame odds.

DraftKings exposes undocumented public JSON endpoints for their sportsbook.
This scraper pulls pregame odds and normalizes them to match the format
returned by tools/odds_api.py so the rest of the system can consume them
interchangeably.

Zero API cost. Rate-limited to 1 request per 2 seconds to be polite.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger("callisto.dk_scraper")

# DraftKings eventgroup endpoints (undocumented but public)
DK_ENDPOINTS = {
    "basketball_nba": "https://sportsbook.draftkings.com/sites/US-SB/api/v5/eventgroups/42648?format=json",
    "americanfootball_nfl": "https://sportsbook.draftkings.com/sites/US-SB/api/v5/eventgroups/88808?format=json",
    "icehockey_nhl": "https://sportsbook.draftkings.com/sites/US-SB/api/v5/eventgroups/42133?format=json",
    "basketball_ncaab": "https://sportsbook.draftkings.com/sites/US-SB/api/v5/eventgroups/92483?format=json",
    "golf_pga": "https://sportsbook.draftkings.com/sites/US-SB/api/v5/eventgroups/92081?format=json",
}

# DK event-level endpoint for player props
DK_EVENT_BASE = "https://sportsbook.draftkings.com/sites/US-SB/api/v5/eventgroups/{group_id}/categories/{category_id}?format=json"

# Category IDs for player prop types on DK
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
        "tournament_winner": 487,
        "top_5_finish": 488,
        "top_10_finish": 489,
        "top_20_finish": 490,
        "make_cut": 491,
        "first_round_leader": 492,
        "matchups": 493,
        "round_score": 494,
    },
}

# Rate limiting
_last_request_time: float = 0.0
_RATE_LIMIT_SECONDS = 2.0

# Shared client
_client: Optional[httpx.AsyncClient] = None

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://sportsbook.draftkings.com/",
}


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=15.0, headers=_HEADERS, follow_redirects=True)
    return _client


async def close_client() -> None:
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
        _client = None


async def _rate_limited_get(url: str) -> httpx.Response:
    """GET with rate limiting — 1 request per 2 seconds."""
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


def _dk_american_odds(price: float) -> int:
    """Convert DraftKings decimal price to American odds."""
    if price >= 2.0:
        return round((price - 1) * 100)
    elif price > 1.0:
        return round(-100 / (price - 1))
    else:
        return -10000  # Edge case


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

    Args:
        sport: Sport key matching odds_api conventions
               ('basketball_nba', 'americanfootball_nfl', 'icehockey_nhl', 'basketball_ncaab')

    Returns:
        Dict with 'sport', 'game_count', 'games' list, and 'source': 'draftkings_scraper'
    """
    url = DK_ENDPOINTS.get(sport)
    if not url:
        logger.warning(f"No DK endpoint for sport: {sport}")
        return {"error": f"Unsupported sport: {sport}", "games": []}

    try:
        resp = await _rate_limited_get(url)
        data = resp.json()

        event_map = _build_event_map(data)
        offers_by_event = _extract_offers(data)

        games = []
        for event_id, offers in offers_by_event.items():
            meta = event_map.get(event_id, {})
            if not meta:
                # Skip offers without event metadata
                continue

            # Build markets list (only include non-empty markets)
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
                "bookmakers": [
                    {
                        "key": "draftkings",
                        "title": "DraftKings",
                        "last_update": datetime.now(timezone.utc).isoformat(),
                        "markets": markets,
                    }
                ],
            })

        logger.info(f"DK scrape {sport}: {len(games)} games found")
        return {
            "sport": sport,
            "game_count": len(games),
            "games": games,
            "source": "draftkings_scraper",
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
    }
    return titles.get(sport_key, sport_key)
