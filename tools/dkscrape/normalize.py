"""
Nash endpoint normalization into the standard Callisto odds format.
"""
import logging
from datetime import datetime, timezone

from tools.dkscrape.client import _dk_american_odds, _parse_nash_american_odds
from tools.dkscrape.constants import _expand_dk_short_name, _sport_title

logger = logging.getLogger("callisto.dk_scraper")

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
