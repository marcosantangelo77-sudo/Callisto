"""
Free prop scraper cascade — player props from DK, FanDuel, BetMGM.

Scrapes player prop markets (points, rebounds, assists, threes, PRA, etc.)
from all three sportsbook APIs for FREE, then merges into a unified format
compatible with prop_scanner.py's edge detection pipeline.

This is the prop equivalent of line_monitor's free_cascade for game-level odds.
Each scraper hits the same public APIs already used for game odds, just
parsing the prop/player market categories that were previously ignored.

Zero API cost. Rate-limited per source (2s intervals).
"""

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Optional

import aiosqlite
import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("callisto.prop_scraper_free")

DB_PATH = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")

# ─────────────────────────────────────────────────────────────────────
# Standard prop market keys (matches The Odds API / prop_scanner.py)
# ─────────────────────────────────────────────────────────────────────

PROP_MARKETS = {
    "player_points",
    "player_rebounds",
    "player_assists",
    "player_threes",
    "player_points_rebounds_assists",
    "player_points_rebounds",
    "player_points_assists",
    "player_rebounds_assists",
    "player_steals",
    "player_blocks",
    "player_turnovers",
    "player_double_double",
    # MLB
    "pitcher_strikeouts",
    "pitcher_outs",
    "batter_hits",
    "batter_total_bases",
    "batter_rbis",
    "batter_runs",
    "batter_stolen_bases",
    "batter_home_runs",
    # NHL
    "player_points_nhl",
    "player_shots_on_goal",
    "player_goals",
    "player_assists_nhl",
    "player_saves",
    # NFL
    "player_pass_yds",
    "player_pass_tds",
    "player_rush_yds",
    "player_rec_yds",
    "player_receptions",
    "player_touchdowns",
    "player_interceptions",
}

# ─────────────────────────────────────────────────────────────────────
# DRAFTKINGS — Nash endpoint prop extraction
# ─────────────────────────────────────────────────────────────────────

# The Nash API returns marketType.name for ALL markets, including props.
# These are the mappings from Nash market type names to our standard keys.
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
    "points o/u": "player_points",  # context-dependent, same key
    "saves o/u": "player_saves",
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


def _classify_dk_nash_prop(market_type_name: str) -> Optional[str]:
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


def _parse_nash_american_odds(odds_str: str) -> int:
    """Parse American odds string from Nash (handles Unicode minus)."""
    if not odds_str:
        return 0
    cleaned = odds_str.replace("\u2212", "-").replace("\u2013", "-").replace("+", "")
    try:
        return int(round(float(cleaned)))
    except (ValueError, TypeError):
        return 0


try:
    from curl_cffi.requests import Session as CffiSession
    _HAS_CURL_CFFI = True
except ImportError:
    _HAS_CURL_CFFI = False

_cffi_session = None
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}

# Rate limiting (shared across all sources within this module)
_last_dk_request: float = 0.0
_last_fd_request: float = 0.0
_last_mgm_request: float = 0.0
_RATE_LIMIT = 2.0


def _get_cffi_session():
    global _cffi_session
    if _cffi_session is None and _HAS_CURL_CFFI:
        _cffi_session = CffiSession(impersonate="chrome131")
    return _cffi_session


async def _cffi_get(url: str) -> dict:
    """Rate-limited GET via curl_cffi with Chrome TLS impersonation."""
    global _last_dk_request
    now = time.monotonic()
    wait = _RATE_LIMIT - (now - _last_dk_request)
    if wait > 0:
        await asyncio.sleep(wait)
    _last_dk_request = time.monotonic()

    def _do():
        session = _get_cffi_session()
        resp = session.get(url, timeout=15)
        resp.raise_for_status()
        return resp.json()

    return await asyncio.to_thread(_do)


# DK Nash league IDs (same as dk_scraper.py)
_DK_LEAGUE_IDS = {
    "basketball_nba": 42648,
    "americanfootball_nfl": 88808,
    "basketball_ncaab": 92483,
    "icehockey_nhl": 42133,
    "baseball_mlb": 84240,
}

_NASH_BASE = "https://sportsbook-nash.draftkings.com/api/sportscontent/dkusnj/v1/leagues"



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
        league_data = await _cffi_get(league_url)

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
                cat_data = await _cffi_get(cat_url)

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
                        price = _parse_nash_american_odds(american_str)
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


# ─────────────────────────────────────────────────────────────────────
# FANDUEL — prop extraction from content-managed-page
# ─────────────────────────────────────────────────────────────────────

_FD_API_BASE = "https://sbapi.nj.sportsbook.fanduel.com/api"
_FD_AK = "FhMFpcPWXMeyZxOx"

_FD_COMPETITIONS = {
    "basketball_nba": {"sport": "basketball", "competition": "7522"},
    "americanfootball_nfl": {"sport": "american-football", "competition": "54"},
    "icehockey_nhl": {"sport": "ice-hockey", "competition": "7524"},
    "basketball_ncaab": {"sport": "basketball", "competition": "10547"},
    "baseball_mlb": {"sport": "baseball", "competition": "10336"},
}

# FanDuel market type -> standard prop key
_FD_PROP_MAP = {
    # NBA
    "PLAYER_POINTS": "player_points",
    "PLAYER_REBOUNDS": "player_rebounds",
    "PLAYER_ASSISTS": "player_assists",
    "PLAYER_THREES_MADE": "player_threes",
    "PLAYER_THREE_POINTERS_MADE": "player_threes",
    "PLAYER_POINTS_REBOUNDS_ASSISTS": "player_points_rebounds_assists",
    "PLAYER_PTS_REBS_ASTS": "player_points_rebounds_assists",
    "PLAYER_POINTS_PLUS_REBOUNDS": "player_points_rebounds",
    "PLAYER_POINTS_PLUS_ASSISTS": "player_points_assists",
    "PLAYER_REBOUNDS_PLUS_ASSISTS": "player_rebounds_assists",
    "PLAYER_STEALS": "player_steals",
    "PLAYER_BLOCKS": "player_blocks",
    "PLAYER_TURNOVERS": "player_turnovers",
    "PLAYER_DOUBLE_DOUBLE": "player_double_double",
    # MLB
    "PITCHER_STRIKEOUTS": "pitcher_strikeouts",
    "BATTER_TOTAL_BASES": "batter_total_bases",
    "BATTER_HITS": "batter_hits",
    "BATTER_RUNS": "batter_runs",
    "BATTER_RBIS": "batter_rbis",
    "BATTER_HOME_RUNS": "batter_home_runs",
    "BATTER_STOLEN_BASES": "batter_stolen_bases",
    "PITCHER_OUTS": "pitcher_outs",
    # NHL
    "PLAYER_SHOTS_ON_GOAL": "player_shots_on_goal",
    "PLAYER_GOALS": "player_goals",
    "PLAYER_POINTS": "player_points",
    "PLAYER_SAVES": "player_saves",
    # NFL
    "PLAYER_PASSING_YARDS": "player_pass_yds",
    "PLAYER_PASSING_TOUCHDOWNS": "player_pass_tds",
    "PLAYER_RUSHING_YARDS": "player_rush_yds",
    "PLAYER_RECEIVING_YARDS": "player_rec_yds",
    "PLAYER_RECEPTIONS": "player_receptions",
    "PLAYER_ANYTIME_TOUCHDOWN": "player_touchdowns",
    "PLAYER_INTERCEPTIONS": "player_interceptions",
}

# Regex fallbacks for FD market types
_FD_PROP_PATTERNS = [
    (re.compile(r"PLAYER.*POINT", re.I), "player_points"),
    (re.compile(r"PLAYER.*REBOUND", re.I), "player_rebounds"),
    (re.compile(r"PLAYER.*ASSIST", re.I), "player_assists"),
    (re.compile(r"PLAYER.*THREE|PLAYER.*3.*POINTER", re.I), "player_threes"),
    (re.compile(r"PITCHER.*STRIKEOUT", re.I), "pitcher_strikeouts"),
    (re.compile(r"BATTER.*TOTAL.*BASE", re.I), "batter_total_bases"),
    (re.compile(r"PLAYER.*SHOT.*GOAL", re.I), "player_shots_on_goal"),
    (re.compile(r"PASS.*YARD", re.I), "player_pass_yds"),
    (re.compile(r"RUSH.*YARD", re.I), "player_rush_yds"),
    (re.compile(r"RECEIV.*YARD", re.I), "player_rec_yds"),
    (re.compile(r"RECEPTION", re.I), "player_receptions"),
]

_fd_client: Optional[httpx.AsyncClient] = None


def _get_fd_client() -> httpx.AsyncClient:
    global _fd_client
    if _fd_client is None or _fd_client.is_closed:
        _fd_client = httpx.AsyncClient(timeout=15.0, headers=_HEADERS, follow_redirects=True, max_redirects=5)
    return _fd_client


async def _fd_rate_limited_get(url: str, params: dict = None) -> httpx.Response:
    global _last_fd_request
    now = time.monotonic()
    wait = _RATE_LIMIT - (now - _last_fd_request)
    if wait > 0:
        await asyncio.sleep(wait)
    client = _get_fd_client()
    resp = await client.get(url, params=params)
    _last_fd_request = time.monotonic()
    return resp


def _classify_fd_prop(market_type: str) -> Optional[str]:
    """Map FanDuel market type to standard prop key."""
    key = _FD_PROP_MAP.get(market_type)
    if key:
        return key
    # Regex fallback
    for pattern, prop_key in _FD_PROP_PATTERNS:
        if pattern.search(market_type):
            return prop_key
    return None


async def scrape_fd_props(sport: str) -> dict:
    """
    Scrape player props from FanDuel's content-managed-page API.

    FanDuel returns all markets (including props) in the attachments.markets
    dict. Prop markets have types like PLAYER_POINTS, PLAYER_REBOUNDS, etc.
    Runner names in prop markets are player names.
    """
    config = _FD_COMPETITIONS.get(sport)
    if not config:
        return {"error": f"Sport '{sport}' not configured for FD props", "props": []}

    try:
        # FanDuel uses a "tab" parameter for player props
        # The main page returns game-level markets; we also need the player props tab
        tabs_to_try = [
            # Main page (has some props mixed in)
            {
                "page": f"SPORT/{config['sport']}/{config['competition']}",
                "pbHorizontal": "false",
                "_ak": _FD_AK,
                "timezone": "America/New_York",
            },
            # Player props specific tab
            {
                "page": f"SPORT/{config['sport']}/{config['competition']}/player-props",
                "pbHorizontal": "false",
                "_ak": _FD_AK,
                "timezone": "America/New_York",
            },
        ]

        all_props = []
        seen_keys = set()

        for params in tabs_to_try:
            try:
                resp = await _fd_rate_limited_get(f"{_FD_API_BASE}/content-managed-page", params)
                if resp.status_code != 200:
                    continue
                data = resp.json()
            except Exception:
                continue

            attachments = data.get("attachments", {})
            events = attachments.get("events", {})
            markets = attachments.get("markets", {})

            # Build event lookup
            event_info = {}
            for eid, ev in events.items():
                name = ev.get("name", "")
                home = away = ""
                if " @ " in name:
                    parts = name.split(" @ ")
                    away, home = parts[0].strip(), parts[1].strip()
                elif " v " in name:
                    parts = name.split(" v ")
                    away, home = parts[0].strip(), parts[1].strip()
                event_info[str(ev.get("eventId", eid))] = {
                    "home_team": home,
                    "away_team": away,
                }

            # Parse prop markets
            for mid, market in markets.items():
                market_type = market.get("marketType", "")
                prop_key = _classify_fd_prop(market_type)
                if not prop_key:
                    continue

                event_id = str(market.get("eventId", ""))
                ev = event_info.get(event_id, {})
                runners = market.get("runners", [])

                for runner in runners:
                    runner_name = runner.get("runnerName", "")
                    if not runner_name:
                        continue

                    # Get odds
                    win_odds = runner.get("winRunnerOdds", {})
                    price = win_odds.get("americanOdds")
                    if price is None:
                        dec = win_odds.get("decimalOdds")
                        if dec and dec > 1:
                            price = round((dec - 1) * 100) if dec >= 2.0 else round(-100 / (dec - 1))
                    if price is None:
                        continue
                    price = int(price)

                    # Get handicap/line
                    handicap = runner.get("handicap")
                    if handicap is None:
                        continue
                    line = float(handicap)

                    # Determine Over/Under from runner name
                    rn_lower = runner_name.lower()
                    if "over" in rn_lower:
                        side = "Over"
                        # Extract player name (everything before "Over")
                        player = re.sub(r"\s*over\s*$", "", runner_name, flags=re.I).strip()
                    elif "under" in rn_lower:
                        side = "Under"
                        player = re.sub(r"\s*under\s*$", "", runner_name, flags=re.I).strip()
                    else:
                        # Runner name IS the player name, side unclear
                        player = runner_name
                        # Check result type or other fields
                        result_type = runner.get("result", {}).get("type", "").lower()
                        if "over" in result_type:
                            side = "Over"
                        elif "under" in result_type:
                            side = "Under"
                        else:
                            side = runner_name  # fallback

                    if not player:
                        continue

                    # Dedup key
                    dedup = f"{event_id}|{player}|{prop_key}|{line}|{side}"
                    if dedup in seen_keys:
                        continue
                    seen_keys.add(dedup)

                    all_props.append({
                        "event_id": f"fd_{event_id}",
                        "home_team": ev.get("home_team", ""),
                        "away_team": ev.get("away_team", ""),
                        "player": player,
                        "market": prop_key,
                        "line": line,
                        "side": side,
                        "price": price,
                        "book": "fanduel",
                    })

        logger.info(f"FD props {sport}: {len(all_props)} prop lines")
        return {
            "sport": sport,
            "props": all_props,
            "prop_count": len(all_props),
            "source": "fd_props",
        }

    except Exception as e:
        logger.warning(f"FD prop scrape failed for {sport}: {e}")
        return {"error": str(e), "props": []}


# ─────────────────────────────────────────────────────────────────────
# BETMGM — prop extraction from CDS API
# ─────────────────────────────────────────────────────────────────────

_MGM_ACCESS_ID = "OTU4NDk3MzEtOTAyNS00MjQzLWIxNWEtNTI2MjdhNWM3Zjk3"
_MGM_BASE_URL = "https://sports.nj.betmgm.com/cds-api/bettingoffer/fixtures"

_MGM_SPORT_IDS = {
    "basketball_nba": 7,
    "americanfootball_nfl": 11,
    "icehockey_nhl": 12,
    "baseball_mlb": 23,
}

_MGM_COMPETITION_IDS = {
    "basketball_nba": 6004,
    "americanfootball_nfl": 35,
    "icehockey_nhl": 237,
    "baseball_mlb": 84,
}

# BetMGM prop market name keywords -> standard keys
_MGM_PROP_MAP = {
    # NBA
    "player points": "player_points",
    "player rebounds": "player_rebounds",
    "player assists": "player_assists",
    "player three pointers": "player_threes",
    "player threes": "player_threes",
    "player pts + reb + ast": "player_points_rebounds_assists",
    "player combos": "player_points_rebounds_assists",
    "player steals": "player_steals",
    "player blocks": "player_blocks",
    "player turnovers": "player_turnovers",
    "double double": "player_double_double",
    # MLB
    "strikeouts": "pitcher_strikeouts",
    "total bases": "batter_total_bases",
    "player hits": "batter_hits",
    "hits recorded": "batter_hits",
    "rbis": "batter_rbis",
    "runs scored": "batter_runs",
    "stolen bases": "batter_stolen_bases",
    "home runs": "batter_home_runs",
    # NHL
    "shots on goal": "player_shots_on_goal",
    "player goals": "player_goals",
    "player saves": "player_saves",
    # NFL
    "passing yards": "player_pass_yds",
    "passing touchdowns": "player_pass_tds",
    "rushing yards": "player_rush_yds",
    "receiving yards": "player_rec_yds",
    "receptions": "player_receptions",
    "anytime touchdown": "player_touchdowns",
}

_MGM_PROP_PATTERNS = [
    (re.compile(r"player\s+points", re.I), "player_points"),
    (re.compile(r"player\s+rebounds", re.I), "player_rebounds"),
    (re.compile(r"player\s+assists", re.I), "player_assists"),
    (re.compile(r"three\s*pointer|player\s+threes", re.I), "player_threes"),
    (re.compile(r"pts.*reb.*ast|points.*rebounds.*assists", re.I), "player_points_rebounds_assists"),
    (re.compile(r"strikeout", re.I), "pitcher_strikeouts"),
    (re.compile(r"total\s+bases", re.I), "batter_total_bases"),
    (re.compile(r"shots?\s+on\s+goal", re.I), "player_shots_on_goal"),
    (re.compile(r"passing\s+yard", re.I), "player_pass_yds"),
    (re.compile(r"rushing\s+yard", re.I), "player_rush_yds"),
    (re.compile(r"receiving\s+yard", re.I), "player_rec_yds"),
    (re.compile(r"receptions?(?:\s|$)", re.I), "player_receptions"),
]

_mgm_cffi_session = None


def _get_mgm_cffi_session():
    """Get or create a curl_cffi session for BetMGM (avoids 403)."""
    global _mgm_cffi_session
    if _mgm_cffi_session is None and _HAS_CURL_CFFI:
        _mgm_cffi_session = CffiSession(impersonate="chrome131")
    return _mgm_cffi_session


async def _mgm_rate_limited_get(url: str, params: dict) -> dict:
    """Rate-limited GET for BetMGM. Uses curl_cffi if available, httpx fallback."""
    global _last_mgm_request
    now = time.monotonic()
    wait = _RATE_LIMIT - (now - _last_mgm_request)
    if wait > 0:
        await asyncio.sleep(wait)
    _last_mgm_request = time.monotonic()

    if _HAS_CURL_CFFI:
        def _do():
            session = _get_mgm_cffi_session()
            # Build URL with params
            from urllib.parse import urlencode
            full_url = f"{url}?{urlencode(params)}"
            resp = session.get(full_url, timeout=15)
            resp.raise_for_status()
            return resp.json()
        return await asyncio.to_thread(_do)
    else:
        # Fallback to httpx (may 403)
        _mgm_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "application/json",
            "Referer": "https://sports.nj.betmgm.com/",
        }
        async with httpx.AsyncClient(timeout=15.0, headers=_mgm_headers, follow_redirects=True, max_redirects=5) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            return resp.json()


def _classify_mgm_prop(market_name: str) -> Optional[str]:
    """Map BetMGM market name to standard prop key."""
    name = market_name.lower().strip()
    for kw, key in _MGM_PROP_MAP.items():
        if kw in name:
            return key
    for pattern, key in _MGM_PROP_PATTERNS:
        if pattern.search(name):
            return key
    return None


def _mgm_decimal_to_american(dec: float) -> int:
    if dec >= 2.0:
        return round((dec - 1) * 100)
    elif dec > 1.0:
        return round(-100 / (dec - 1))
    return -10000


def _mgm_parse_odds(outcome: dict) -> Optional[int]:
    """Extract American odds from BetMGM outcome."""
    american = outcome.get("americanOdds")
    if american is not None:
        try:
            return int(american)
        except (ValueError, TypeError):
            pass
    for key in ("oddsDecimal", "odds", "price"):
        dec = outcome.get(key)
        if dec is not None:
            try:
                return _mgm_decimal_to_american(float(dec))
            except (ValueError, TypeError):
                continue
    return None


async def scrape_mgm_props(sport: str) -> dict:
    """
    Scrape player props from BetMGM's CDS API.

    BetMGM returns all bet offers (including props) in the fixtures response.
    Prop offers have names like "Player Points", "Player Rebounds", etc.
    Each outcome within a prop offer has a player name and line.
    """
    sport_id = _MGM_SPORT_IDS.get(sport)
    if sport_id is None:
        return {"error": f"Sport '{sport}' not configured for BetMGM props", "props": []}

    params = {
        "x-bwin-accessid": _MGM_ACCESS_ID,
        "lang": "en-us",
        "country": "US",
        "userCountry": "US",
        "offerMapping": "Ede",
        "scorecard": "true",
        "state": "Latest",
        "sportIds": str(sport_id),
    }
    comp_id = _MGM_COMPETITION_IDS.get(sport)
    if comp_id:
        params["competitionIds"] = str(comp_id)

    try:
        data = await _mgm_rate_limited_get(_MGM_BASE_URL, params)

        # Extract fixtures
        fixtures = []
        if isinstance(data, list):
            fixtures = data
        elif isinstance(data, dict):
            for key in ("fixtures", "items", "results", "data", "events"):
                if key in data and isinstance(data[key], list):
                    fixtures = data[key]
                    break

        props = []
        for fixture in fixtures:
            # Event metadata
            fixture_id = str(fixture.get("id", fixture.get("fixtureId", "")))
            participants = fixture.get("participants", [])
            home = away = ""
            for p in participants:
                ptype = (p.get("properties", {}).get("type", "") or p.get("type", "") or "").lower()
                pname = p.get("name", {})
                if isinstance(pname, dict):
                    pname = pname.get("value", "")
                if ptype == "home":
                    home = pname
                elif ptype == "away":
                    away = pname

            # Parse bet offers for props
            offers = fixture.get("games", fixture.get("betOffers", fixture.get("optionMarkets", [])))
            for offer in offers:
                # Market name
                market_name = ""
                for nk in ("name", "betOfferType", "optionMarketName", "templateName"):
                    raw = offer.get(nk, "")
                    if isinstance(raw, dict):
                        raw = raw.get("value", raw.get("name", ""))
                    if raw:
                        market_name = str(raw)
                        break

                prop_key = _classify_mgm_prop(market_name)
                if not prop_key:
                    continue

                outcomes = offer.get("results", offer.get("outcomes", offer.get("selections", [])))
                for o in outcomes:
                    price = _mgm_parse_odds(o)
                    if price is None:
                        continue

                    outcome_name = o.get("name", {})
                    if isinstance(outcome_name, dict):
                        outcome_name = outcome_name.get("value", "")
                    outcome_name = str(outcome_name)

                    # Get line
                    line_val = None
                    for lk in ("line", "handicap", "specialBetValue", "attr"):
                        v = o.get(lk)
                        if v is not None:
                            try:
                                line_val = float(v)
                                break
                            except (ValueError, TypeError):
                                continue
                    if line_val is None:
                        continue

                    # Determine side and player
                    on_lower = outcome_name.lower()
                    if "over" in on_lower:
                        side = "Over"
                        player = re.sub(r"\s*over\s*$", "", outcome_name, flags=re.I).strip()
                    elif "under" in on_lower:
                        side = "Under"
                        player = re.sub(r"\s*under\s*$", "", outcome_name, flags=re.I).strip()
                    else:
                        # Try participant field
                        participant = o.get("participant", {})
                        if isinstance(participant, dict):
                            player = participant.get("name", {})
                            if isinstance(player, dict):
                                player = player.get("value", "")
                        elif isinstance(participant, str):
                            player = participant
                        else:
                            player = outcome_name
                        side = outcome_name

                    if not player:
                        continue

                    props.append({
                        "event_id": f"betmgm_{fixture_id}",
                        "home_team": home,
                        "away_team": away,
                        "player": player,
                        "market": prop_key,
                        "line": line_val,
                        "side": side,
                        "price": price,
                        "book": "betmgm",
                    })

        logger.info(f"BetMGM props {sport}: {len(props)} prop lines")
        return {
            "sport": sport,
            "props": props,
            "prop_count": len(props),
            "source": "mgm_props",
        }

    except Exception as e:
        logger.warning(f"BetMGM prop scrape failed for {sport}: {e}")
        return {"error": str(e), "props": []}


# ─────────────────────────────────────────────────────────────────────
# UNIFIED PROP CASCADE — merge all sources
# ─────────────────────────────────────────────────────────────────────

async def scrape_all_props(sport: str) -> dict:
    """
    Full free prop cascade: DK → FanDuel → BetMGM.
    Runs all three in parallel, merges results.

    Returns unified prop data with multi-book coverage per player/market/line.
    """
    # Run scrapers concurrently (BetMGM disabled — redundant with odds-api.io Pro)
    dk_task = asyncio.create_task(scrape_dk_props(sport))
    fd_task = asyncio.create_task(scrape_fd_props(sport))

    dk_result, fd_result = await asyncio.gather(
        dk_task, fd_task, return_exceptions=True
    )

    all_props = []
    sources_ok = []

    for name, result in [("dk", dk_result), ("fd", fd_result)]:
        if isinstance(result, Exception):
            logger.warning(f"{name} prop scrape raised: {result}")
            continue
        if result.get("error"):
            logger.warning(f"{name} prop scrape error: {result['error']}")
            continue
        props = result.get("props", [])
        if props:
            all_props.extend(props)
            sources_ok.append(name)

    # Build summary by player and market
    player_markets = {}
    for p in all_props:
        key = f"{p['player']}|{p['market']}|{p['line']}"
        if key not in player_markets:
            player_markets[key] = {"books": set()}
        player_markets[key]["books"].add(p["book"])

    multi_book_count = sum(1 for pm in player_markets.values() if len(pm["books"]) >= 2)

    logger.info(
        f"Prop cascade {sport}: {len(all_props)} total lines from {sources_ok}, "
        f"{len(player_markets)} unique player/market/lines, "
        f"{multi_book_count} with 2+ books"
    )

    return {
        "sport": sport,
        "props": all_props,
        "prop_count": len(all_props),
        "sources": sources_ok,
        "unique_player_markets": len(player_markets),
        "multi_book_count": multi_book_count,
        "source": "free_prop_cascade",
    }


# ─────────────────────────────────────────────────────────────────────
# DATABASE STORAGE
# ─────────────────────────────────────────────────────────────────────

PROP_SCHEMA_SQL = """
-- Player prop snapshots — one row per book/player/market/line/side/timestamp
CREATE TABLE IF NOT EXISTS prop_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sport TEXT NOT NULL,
    event_id TEXT,
    home_team TEXT,
    away_team TEXT,
    player TEXT NOT NULL,
    market TEXT NOT NULL,
    line REAL NOT NULL,
    side TEXT NOT NULL,
    book TEXT NOT NULL,
    price_american INTEGER NOT NULL,
    snapshot_time DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_prop_snap_player
    ON prop_snapshots(player, market, line, book, snapshot_time);
CREATE INDEX IF NOT EXISTS idx_prop_snap_sport_time
    ON prop_snapshots(sport, snapshot_time);
CREATE INDEX IF NOT EXISTS idx_prop_snap_event
    ON prop_snapshots(event_id, market, snapshot_time);
"""


async def ensure_prop_schema(db_path: str = DB_PATH) -> None:
    """Create prop_snapshots table if it doesn't exist."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout = 60000")
        # SECURITY (audit C-6): per-statement DDL avoids EXCLUSIVE lock contention.
        for stmt in (s.strip() for s in PROP_SCHEMA_SQL.split(";") if s.strip()):
            await db.execute(stmt)
        await db.commit()
    logger.info("Prop schema ensured")


async def store_prop_snapshot(props: list[dict], sport: str, db_path: str = DB_PATH) -> int:
    """
    Store a batch of prop lines into the database.

    Args:
        props: List of prop dicts from scrape_all_props()
        sport: Sport key
        db_path: Database path

    Returns:
        Number of rows inserted
    """
    if not props:
        return 0

    now = datetime.now(timezone.utc).isoformat()

    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout = 60000")
        rows = [
            (
                sport,
                p.get("event_id", ""),
                p.get("home_team", ""),
                p.get("away_team", ""),
                p["player"],
                p["market"],
                p["line"],
                p["side"],
                p["book"],
                p["price"],
                now,
            )
            for p in props
        ]
        await db.executemany(
            "INSERT INTO prop_snapshots "
            "(sport, event_id, home_team, away_team, player, market, line, side, book, price_american, snapshot_time) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        await db.commit()

    logger.info(f"Stored {len(rows)} prop snapshot rows for {sport}")
    return len(rows)


# ─────────────────────────────────────────────────────────────────────
# CONVERSION TO PROP_SCANNER FORMAT
# ─────────────────────────────────────────────────────────────────────

def props_to_scanner_format(props: list[dict]) -> dict:
    """
    Convert flat prop list to the format expected by prop_scanner.scan_props_ev().

    This bridges the free scrapers with the existing EV detection pipeline.
    The prop_scanner expects data shaped like The Odds API's player props response:
    {
        "bookmakers": [
            {
                "key": "draftkings",
                "title": "DraftKings",
                "markets": [
                    {
                        "key": "player_points",
                        "outcomes": [
                            {"name": "Over", "price": -110, "point": 24.5, "description": "LeBron James"},
                        ]
                    }
                ]
            }
        ]
    }
    """
    # Group by book -> market -> outcomes
    books = {}
    for p in props:
        book_key = p["book"]
        if book_key not in books:
            books[book_key] = {}

        market_key = p["market"]
        if market_key not in books[book_key]:
            books[book_key][market_key] = []

        books[book_key][market_key].append({
            "name": p["side"],
            "price": p["price"],
            "point": p["line"],
            "description": p["player"],
        })

    book_titles = {
        "draftkings": "DraftKings",
        "fanduel": "FanDuel",
        "betmgm": "BetMGM",
    }

    bookmakers = []
    for book_key, markets_dict in books.items():
        markets = []
        for mkt_key, outcomes in markets_dict.items():
            markets.append({
                "key": mkt_key,
                "outcomes": outcomes,
            })
        bookmakers.append({
            "key": book_key,
            "title": book_titles.get(book_key, book_key),
            "markets": markets,
        })

    return {"bookmakers": bookmakers}


# ─────────────────────────────────────────────────────────────────────
# CLEANUP
# ─────────────────────────────────────────────────────────────────────

async def close_clients() -> None:
    """Close all HTTP clients."""
    global _fd_client, _cffi_session, _mgm_cffi_session
    if _fd_client and not _fd_client.is_closed:
        await _fd_client.aclose()
        _fd_client = None
    for sess_ref in ("_cffi_session", "_mgm_cffi_session"):
        sess = globals().get(sess_ref)
        if sess is not None:
            try:
                sess.close()
            except Exception:
                pass
            globals()[sess_ref] = None
