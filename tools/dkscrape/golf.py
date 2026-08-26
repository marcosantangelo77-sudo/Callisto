"""
Golf-specific scraping: category discovery, tournament odds, listings.

Golf on DK is organized by per-tournament eventgroups with dynamic offer
category IDs, so discovery happens at runtime.
"""
import logging
from typing import Optional

import httpx

from tools.dkscrape.client import _rate_limited_get, _dk_american_odds
from tools.dkscrape.constants import DK_GOLF_EVENTGROUPS

logger = logging.getLogger("callisto.dk_scraper")

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
