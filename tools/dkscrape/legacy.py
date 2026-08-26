"""
Legacy v5 eventgroup helpers (kept for fallback and golf/props which still
use the old eventgroup format).
"""
from typing import Optional

from tools.dkscrape.client import _dk_american_odds

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
