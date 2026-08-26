"""
FanDuel prop extraction from the content-managed-page API.

FanDuel returns all markets (including props) in the attachments.markets
dict. Prop markets have types like PLAYER_POINTS, PLAYER_REBOUNDS, etc.
Runner names in prop markets are player names.
"""

import asyncio
import logging
import re
import time
from typing import Optional

import httpx

from tools.propscrape.common import _HEADERS, _RATE_LIMIT, _last_fd_request

logger = logging.getLogger("callisto.prop_scraper_free")

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


def get_fd_client() -> httpx.AsyncClient:
    global _fd_client
    if _fd_client is None or _fd_client.is_closed:
        _fd_client = httpx.AsyncClient(timeout=15.0, headers=_HEADERS, follow_redirects=True, max_redirects=5)
    return _fd_client


async def fd_rate_limited_get(url: str, params: dict = None) -> httpx.Response:
    global _last_fd_request
    now = time.monotonic()
    wait = _RATE_LIMIT - (now - _last_fd_request)
    if wait > 0:
        await asyncio.sleep(wait)
    client = get_fd_client()
    resp = await client.get(url, params=params)
    _last_fd_request = time.monotonic()
    return resp


def classify_fd_prop(market_type: str) -> Optional[str]:
    """Map FanDuel market type to standard prop key."""
    key = _FD_PROP_MAP.get(market_type)
    if key:
        return key
    # Regex fallback
    for pattern, prop_key in _FD_PROP_PATTERNS:
        if pattern.search(market_type):
            return prop_key
    return None


async def close_fd_client() -> None:
    global _fd_client
    if _fd_client and not _fd_client.is_closed:
        await _fd_client.aclose()
        _fd_client = None


async def scrape_fd_props(sport: str) -> dict:
    """
    Scrape player props from FanDuel's content-managed-page API.
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
                resp = await fd_rate_limited_get(f"{_FD_API_BASE}/content-managed-page", params)
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
                prop_key = classify_fd_prop(market_type)
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
