"""
Public scraping entry points: game odds and player props (Nash primary,
legacy v5 fallback) plus sport title mapping.
"""
import logging
from datetime import datetime, timezone

import httpx

from tools.dkscrape.client import (
    _HAS_CURL_CFFI,
    _nash_get,
    _rate_limited_get,
    _dk_american_odds,
)
from tools.dkscrape.constants import (
    _NASH_BASE,
    _sport_title,
    DK_ENDPOINTS,
    DK_PROP_NAME_PATTERNS,
    DK_PROP_CATEGORIES,
    LEAGUE_IDS,
)
from tools.dkscrape.discover import _effective_prop_categories, discover_prop_categories
from tools.dkscrape.normalize import _normalize_nash_response
from tools.dkscrape.legacy import _build_event_map, _extract_offers

logger = logging.getLogger("callisto.dk_scraper")

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
    # Ingestion telemetry. Imported lazily so tests that monkey-patch the module
    # don't force-pull the tracking dependency.
    try:
        from tools.ingestion_tracking import tracked_ingestion  # noqa: F401
    except Exception:
        pass

    # Strip dk_ prefix if present
    clean_id = event_id.replace("dk_", "")

    # For MLB/NHL the hard-coded IDs are best-effort — prefer runtime-resolved
    # IDs when DK surfaces them via the league index. This is a no-op when the
    # hard-coded IDs are correct (discover_prop_categories uses a cache).
    resolved_extra: dict[str, int] = {}
    if sport in ("baseball_mlb", "icehockey_nhl"):
        try:
            resolved_extra = await discover_prop_categories(sport)
        except Exception as e:
            logger.debug(f"discover_prop_categories {sport} failed non-fatally: {e}")

    categories = _effective_prop_categories(sport, resolved_extra)
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
