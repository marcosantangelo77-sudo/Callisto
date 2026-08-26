"""
DraftKings Nash endpoint prop extraction.

The Nash API returns marketType.name for ALL markets, including props.
These are the mappings from Nash market type names to our standard keys.
"""

import logging
import re
from typing import Optional

from tools.propscrape.common import (
    _HAS_CURL_CFFI,
    cffi_get,
    parse_nash_american_odds,
)

logger = logging.getLogger("callisto.prop_scraper_free")

# DK Nash league IDs (same as dk_scraper.py)
_DK_LEAGUE_IDS = {
    "basketball_nba": 42648,
    "americanfootball_nfl": 88808,
    "basketball_ncaab": 92483,
    "icehockey_nhl": 42133,
    "baseball_mlb": 84240,
}

_NASH_BASE = "https://sportsbook-nash.draftkings.com/api/sportscontent/dkusnj/v1/leagues"

_DK_NASH_PROP_MAP = {
    # NBA
    "points o/u": "player_points",
    "rebounds o/u": "player_rebounds",
    "assists o/u": "player_assists",
    "threes o/u": "player_threes",
    "three pointers made o/u": "player_threes",
    "pts + reb + ast o/u": "player_points_rebounds_assists",
    "pts + rebs + asts": "player_points_rebounds_assists",
    "pts + reb + ast": "player_points_rebounds_assists",
    "points + rebounds + assists o/u": "player_points_rebounds_assists",
    "pts + reb o/u": "player_points_rebounds",
    "points + rebounds o/u": "player_points_rebounds",
    "pts + ast o/u": "player_points_assists",
    "points + assists o/u": "player_points_assists",
    "reb + ast o/u": "player_rebounds_assists",
    "rebounds + assists o/u": "player_rebounds_assists",
    "steals o/u": "player_steals",
    "blocks o/u": "player_blocks",
    "turnovers o/u": "player_turnovers",
    "double double": "player_double_double",
    # MLB
    "strikeouts o/u": "pitcher_strikeouts",
    "pitcher strikeouts o/u": "pitcher_strikeouts",
    "total bases o/u": "batter_total_bases",
    "hits o/u": "batter_hits",
    "batter hits o/u": "batter_hits",
    "rbis o/u": "batter_rbis",
    "runs o/u": "batter_runs",
    "runs scored o/u": "batter_runs",
    "stolen bases o/u": "batter_stolen_bases",
    "home runs o/u": "batter_home_runs",
    "pitcher outs o/u": "pitcher_outs",
    "hits allowed o/u": "pitcher_hits_allowed",
    # NHL
    "shots on goal o/u": "player_shots_on_goal",
    "goals o/u": "player_goals",
    # NFL
    "passing yards o/u": "player_pass_yds",
    "passing tds o/u": "player_pass_tds",
    "rushing yards o/u": "player_rush_yds",
    "receiving yards o/u": "player_rec_yds",
    "receptions o/u": "player_receptions",
    "anytime touchdown scorer": "player_touchdowns",
    "interceptions o/u": "player_interceptions",
}

# Regex fallbacks for DK market names that don't match exactly
_DK_NASH_PROP_PATTERNS = [
    (re.compile(r"(?:player\s+)?points\b.*o/?u", re.I), "player_points"),
    (re.compile(r"(?:player\s+)?rebounds\b.*o/?u", re.I), "player_rebounds"),
    (re.compile(r"(?:player\s+)?assists\b.*o/?u", re.I), "player_assists"),
    (re.compile(r"(?:player\s+)?threes|three.?pointers?\b.*o/?u", re.I), "player_threes"),
    (re.compile(r"strikeouts\b.*o/?u", re.I), "pitcher_strikeouts"),
    (re.compile(r"total\s+bases\b.*o/?u", re.I), "batter_total_bases"),
    (re.compile(r"shots?\s+on\s+goal\b.*o/?u", re.I), "player_shots_on_goal"),
    (re.compile(r"passing\s+yards?\b.*o/?u", re.I), "player_pass_yds"),
    (re.compile(r"rushing\s+yards?\b.*o/?u", re.I), "player_rush_yds"),
    (re.compile(r"receiving\s+yards?\b.*o/?u", re.I), "player_rec_yds"),
    (re.compile(r"receptions?\b.*o/?u", re.I), "player_receptions"),
]

# DK Nash category IDs for props by sport
_DK_PROP_CATEGORY_IDS = {
    "basketball_nba": {
        1215: "player_points",        # Player Points
        1216: "player_rebounds",       # Player Rebounds
        1217: "player_assists",        # Player Assists
        1218: "player_threes",         # Player Threes
        583: "player_combos",          # Player Combos (PRA, P+R, P+A, R+A)
        1293: "player_defense",        # Player Defense (Steals, Blocks)
    },
    "baseball_mlb": {
        743: "batter_props",           # Batter Props (total bases, hits, runs, RBIs, etc)
        1031: "pitcher_strikeouts",    # Pitcher Props (strikeouts, outs, etc)
    },
    "icehockey_nhl": {
        1190: "player_goals",          # Goalscorer
        1189: "player_shots_on_goal",  # Shots on Goal
        1675: "player_points",         # Points
        1676: "player_assists_nhl",    # Assists
        1679: "player_blocks",         # Blocks
        1064: "player_saves",          # Goalie Props
    },
    "americanfootball_nfl": {
        # NFL category IDs — these need to be discovered when NFL is in season
        # Placeholder IDs from DK_PROP_CATEGORIES
        1000: "player_pass_yds",
        1001: "player_rush_yds",
        1002: "player_rec_yds",
        1003: "player_touchdowns",
    },
}

# Milestones market type -> standard prop key
_DK_MILESTONE_TYPE_MAP = {
    "points milestones": "player_points",
    "rebounds milestones": "player_rebounds",
    "assists milestones": "player_assists",
    "three pointers made milestones": "player_threes",
    "threes milestones": "player_threes",
    "points + rebounds + assists milestones": "player_points_rebounds_assists",
    "steals milestones": "player_steals",
    "blocks milestones": "player_blocks",
    # MLB
    "strikeouts milestones": "pitcher_strikeouts",
    "total bases milestones": "batter_total_bases",
    "hits milestones": "batter_hits",
    "runs milestones": "batter_runs",
    "rbis milestones": "batter_rbis",
    "home runs milestones": "batter_home_runs",
    # NHL
    "shots on goal milestones": "player_shots_on_goal",
    "goals milestones": "player_goals",
    "saves milestones": "player_saves",
    # NFL
    "passing yards milestones": "player_pass_yds",
    "rushing yards milestones": "player_rush_yds",
    "receiving yards milestones": "player_rec_yds",
    "receptions milestones": "player_receptions",
}

# Player name suffix patterns to strip from market names
_DK_MARKET_SUFFIXES = re.compile(
    r"\s+(?:Points \+ Rebounds \+ Assists|Points \+ Rebounds|Points \+ Assists|"
    r"Rebounds \+ Assists|Pts \+ Reb \+ Ast|Pts \+ Reb|Pts \+ Ast|Reb \+ Ast|Ast \+ Reb|"
    r"Points|Rebounds|Assists|Threes|Three Pointers Made|"
    r"Steals|Blocks|Turnovers|Strikeouts|Total Bases|Hits|Runs|RBIs|"
    r"Home Runs|Stolen Bases|Shots on Goal|Goals|Saves|"
    r"Passing Yards|Rushing Yards|Receiving Yards|Receptions|Touchdowns)$",
    re.I,
)


def classify_dk_nash_prop(market_type_name: str) -> Optional[str]:
    """Map a DK Nash market type name to a standard prop key."""
    name = market_type_name.lower().strip()
    # Exact match first
    key = _DK_NASH_PROP_MAP.get(name)
    if key:
        return key
    # Regex fallback
    for pattern, prop_key in _DK_NASH_PROP_PATTERNS:
        if pattern.search(name):
            return prop_key
    return None


async def scrape_dk_props(sport: str) -> dict:
    """
    Scrape player props from DraftKings Nash endpoint.

    Uses two approaches:
    1. Per-category endpoint: leagues/{id}/categories/{catId} — gets O/U and milestones
    2. Per-event endpoint: events/{id}/categories — gets all prop markets for an event

    The Nash API uses "Milestones" format for player props: each selection is
    a threshold like "25+" meaning "Over 24.5". We convert these to standard
    Over/Under format with half-point lines.
    """
    league_id = _DK_LEAGUE_IDS.get(sport)
    if not league_id:
        return {"error": f"Sport '{sport}' not configured for DK props", "props": []}

    if not _HAS_CURL_CFFI:
        return {"error": "curl_cffi not installed -- needed for DK Nash", "props": []}

    try:
        # Step 1: Get events and their IDs
        league_url = f"{_NASH_BASE}/{league_id}"
        league_data = await cffi_get(league_url)

        events_raw = league_data.get("events") or []
        if not events_raw:
            return {"sport": sport, "props": [], "prop_count": 0, "source": "dk_props"}

        event_map = {}
        for ev in events_raw:
            eid = str(ev.get("id", ""))
            participants = ev.get("participants") or []
            home = away = ""
            for p in participants:
                role = (p.get("venueRole") or "").lower()
                name = p.get("name", "")
                if role == "home":
                    home = name
                elif role == "away":
                    away = name
            event_map[eid] = {"home_team": home, "away_team": away}

        # Step 2: Fetch prop categories at league level
        # This gets all players across all events for each category
        cat_ids = _DK_PROP_CATEGORY_IDS.get(sport, {})
        props = []
        milestone_re = re.compile(r"^(\d+)\+$")

        for cat_id, base_prop_key in cat_ids.items():
            try:
                cat_url = f"{_NASH_BASE}/{league_id}/categories/{cat_id}"
                cat_data = await cffi_get(cat_url)

                markets = cat_data.get("markets") or []
                selections = cat_data.get("selections") or []

                if not markets:
                    continue

                # Build selection lookup: marketId -> [selections]
                sel_by_market = {}
                for sel in selections:
                    mid = sel.get("marketId")
                    if mid:
                        sel_by_market.setdefault(mid, []).append(sel)

                for mkt in markets:
                    mid = mkt.get("id")
                    eid = str(mkt.get("eventId", ""))
                    ev = event_map.get(eid, {})
                    market_name = mkt.get("name", "")
                    mtype_name = (mkt.get("marketType") or {}).get("name", "").lower()

                    # Determine the specific prop key
                    prop_key = _DK_MILESTONE_TYPE_MAP.get(mtype_name, base_prop_key)

                    # Refine combos: "Pts + Reb + Ast" vs "Pts + Reb" etc
                    if base_prop_key == "player_combos":
                        lower_name = market_name.lower()
                        if "pts" in lower_name and "reb" in lower_name and "ast" in lower_name:
                            prop_key = "player_points_rebounds_assists"
                        elif "pts" in lower_name and "reb" in lower_name:
                            prop_key = "player_points_rebounds"
                        elif "pts" in lower_name and "ast" in lower_name:
                            prop_key = "player_points_assists"
                        elif "reb" in lower_name and "ast" in lower_name:
                            prop_key = "player_rebounds_assists"

                    # Refine defense: steals vs blocks
                    if base_prop_key == "player_defense":
                        lower_name = mtype_name
                        if "steal" in lower_name:
                            prop_key = "player_steals"
                        elif "block" in lower_name:
                            prop_key = "player_blocks"

                    # Refine batter props by market type name
                    if base_prop_key == "batter_props":
                        lower_name = mtype_name
                        if "total bases" in lower_name:
                            prop_key = "batter_total_bases"
                        elif "hits" in lower_name:
                            prop_key = "batter_hits"
                        elif "runs" in lower_name and "home" not in lower_name:
                            prop_key = "batter_runs"
                        elif "rbis" in lower_name or "rbi" in lower_name:
                            prop_key = "batter_rbis"
                        elif "home run" in lower_name:
                            prop_key = "batter_home_runs"
                        elif "stolen" in lower_name:
                            prop_key = "batter_stolen_bases"
                        else:
                            prop_key = "batter_total_bases"  # default

                    # Extract player name from market name
                    player = _DK_MARKET_SUFFIXES.sub("", market_name).strip()
                    if not player:
                        player = market_name

                    # Parse selections (milestones: "25+" = Over 24.5)
                    mkt_sels = sel_by_market.get(mid, [])
                    for sel in mkt_sels:
                        label = sel.get("label", "")
                        display_odds = sel.get("displayOdds") or {}
                        american_str = display_odds.get("american", "")
                        price = parse_nash_american_odds(american_str)
                        if price == 0:
                            continue

                        # Parse milestone threshold
                        m = milestone_re.match(label)
                        if m:
                            threshold = int(m.group(1))
                            line = threshold - 0.5
                            side = "Over"
                        else:
                            # Check for explicit Over/Under
                            points = sel.get("points")
                            if points is not None:
                                try:
                                    line = float(points)
                                except (ValueError, TypeError):
                                    continue
                                otype = (sel.get("outcomeType") or "").lower()
                                if otype == "over" or "over" in label.lower():
                                    side = "Over"
                                elif otype == "under" or "under" in label.lower():
                                    side = "Under"
                                else:
                                    continue
                            else:
                                continue

                        props.append({
                            "event_id": f"dk_{eid}",
                            "home_team": ev.get("home_team", ""),
                            "away_team": ev.get("away_team", ""),
                            "player": player,
                            "market": prop_key,
                            "line": line,
                            "side": side,
                            "price": price,
                            "book": "draftkings",
                        })

            except Exception as e:
                logger.warning(f"DK prop category {cat_id} ({base_prop_key}) failed: {e}")
                continue

        logger.info(f"DK props {sport}: {len(props)} prop lines")
        return {
            "sport": sport,
            "props": props,
            "prop_count": len(props),
            "source": "dk_props",
        }

    except Exception as e:
        logger.warning(f"DK prop scrape failed for {sport}: {e}")
        return {"error": str(e), "props": []}
