"""
BetMGM prop extraction from the CDS API.

BetMGM returns all bet offers (including props) in the fixtures response.
Prop offers have names like "Player Points", "Player Rebounds", etc.
Each outcome within a prop offer has a player name and line.
"""

import asyncio
import logging
import re
import time
from typing import Optional

import httpx

from tools.propscrape.common import (
    _HAS_CURL_CFFI,
    _RATE_LIMIT,
    _last_mgm_request,
    get_cffi_session,
)

logger = logging.getLogger("callisto.prop_scraper_free")

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


def get_mgm_cffi_session():
    """Get or create a curl_cffi session for BetMGM (avoids 403)."""
    global _mgm_cffi_session
    if _mgm_cffi_session is None and _HAS_CURL_CFFI:
        from curl_cffi.requests import Session as CffiSession
        _mgm_cffi_session = CffiSession(impersonate="chrome131")
    return _mgm_cffi_session


async def mgm_rate_limited_get(url: str, params: dict) -> dict:
    """Rate-limited GET for BetMGM. Uses curl_cffi if available, httpx fallback."""
    global _last_mgm_request
    now = time.monotonic()
    wait = _RATE_LIMIT - (now - _last_mgm_request)
    if wait > 0:
        await asyncio.sleep(wait)
    _last_mgm_request = time.monotonic()

    if _HAS_CURL_CFFI:
        def _do():
            session = get_mgm_cffi_session()
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


def classify_mgm_prop(market_name: str) -> Optional[str]:
    """Map BetMGM market name to standard prop key."""
    name = market_name.lower().strip()
    for kw, key in _MGM_PROP_MAP.items():
        if kw in name:
            return key
    for pattern, key in _MGM_PROP_PATTERNS:
        if pattern.search(name):
            return key
    return None


def mgm_decimal_to_american(dec: float) -> int:
    if dec >= 2.0:
        return round((dec - 1) * 100)
    elif dec > 1.0:
        return round(-100 / (dec - 1))
    return -10000


def mgm_parse_odds(outcome: dict) -> Optional[int]:
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
                return mgm_decimal_to_american(float(dec))
            except (ValueError, TypeError):
                continue
    return None


def close_mgm_session() -> None:
    global _mgm_cffi_session
    if _mgm_cffi_session is not None:
        try:
            _mgm_cffi_session.close()
        except Exception:
            pass
        _mgm_cffi_session = None


async def scrape_mgm_props(sport: str) -> dict:
    """
    Scrape player props from BetMGM's CDS API.
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
        data = await mgm_rate_limited_get(_MGM_BASE_URL, params)

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

                prop_key = classify_mgm_prop(market_name)
                if not prop_key:
                    continue

                outcomes = offer.get("results", offer.get("outcomes", offer.get("selections", [])))
                for o in outcomes:
                    price = mgm_parse_odds(o)
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
