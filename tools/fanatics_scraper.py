"""
Fanatics Sportsbook odds scraper — game-level markets for the US leagues.

Why this exists
---------------
Callisto treats Fanatics as a secondary book (DraftKings is primary per
``project_sportsbooks``). Until now the only Fanatics integration was a
bookmaker-key string in odds_api_io's ``_SELECTED_BOOKMAKERS`` list, which
means we got whatever Fanatics lines odds-api.io happened to carry and
nothing else. This scraper lets us:

    1. Freshen Fanatics lines directly at ~2s rate-limit, independent of
       odds-api.io's cache latency.
    2. Keep Fanatics coverage alive if odds-api.io drops the book.
    3. Fall back to unauthenticated public odds when no session cookie
       is set — Fanatics surfaces pregame moneyline/spread/total publicly.
    4. Leave the door open for per-account max-stake discovery once we
       feed ``CALLISTO_FANATICS_SESSION_COOKIE`` into the scraper.

Endpoint discovery notes
------------------------
Fanatics' frontend (https://sportsbook.fanatics.com/) talks to a few
backend surfaces. The two paths we exercise here, in preference order:

    1. ``https://api.sportsbook.fanatics.com/api/v1/sportsbook/events``
       Query string: ``league=<league_key>``, ``type=upcoming``.
       Returns a JSON array of event objects with nested ``markets`` and
       ``selections`` arrays. This is the primary path.

    2. ``https://sportsbook.fanatics.com/api/v2/content/events``
       Query string: ``league=<league_key>``.
       Fallback for when (1) returns 403/404. Same overall shape with
       slightly different field names; our parser tolerates both.

Both paths are UNDOCUMENTED — discovered by reverse-engineering the
frontend. They WILL break. When they do, open DevTools on the live
sportsbook, reload an NFL page, and watch the XHR tab for any JSON
response that contains a ``selections`` or ``outcomes`` array; the new
endpoint's path goes in ``_ENDPOINT_CANDIDATES`` below.

League keys observed on the Fanatics frontend are string slugs (not
numeric IDs as DK uses). We store them in ``FANATICS_LEAGUE_KEYS``; if
Fanatics ever renames a league slug, only that one entry needs touching.

Rate limiting: 1 request per 2 seconds per host, matching the other
book scrapers. On 403/429 we record ``status='rate_limited'`` via
``@tracked_ingestion`` and return an empty payload. On 5xx we record
``status='failed'`` and return the empty payload — NEVER raise out of
the scraper into the line_monitor loop.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

from tools.credentials import (
    FIELD_SESSION_COOKIE,
    FIELD_USERNAME,
    get_credential,
)

# Ingestion tracking — every fetch wraps through @tracked_ingestion. Import
# lazily inside the decorator lookup so unit tests that import this
# module without the ingestion schema migrated don't explode.
from tools.ingestion_tracking import tracked_ingestion

logger = logging.getLogger("callisto.fanatics_scraper")


# ---------------------------------------------------------------------------
# Endpoints & leagues
# ---------------------------------------------------------------------------

# Primary + fallback paths. We iterate in order until one returns a usable
# body. Every entry MUST include a ``{league}`` placeholder; the league key
# comes from FANATICS_LEAGUE_KEYS below.
_ENDPOINT_CANDIDATES: tuple[str, ...] = (
    # Primary: REST sportsbook events endpoint
    "https://api.sportsbook.fanatics.com/api/v1/sportsbook/events"
    "?league={league}&type=upcoming",
    # Fallback: content delivery endpoint used by the web app
    "https://sportsbook.fanatics.com/api/v2/content/events?league={league}",
)

# Callisto sport key  -> Fanatics league slug.
FANATICS_LEAGUE_KEYS: dict[str, str] = {
    "basketball_nba": "nba",
    "americanfootball_nfl": "nfl",
    "icehockey_nhl": "nhl",
    "baseball_mlb": "mlb",
}

# Canonical bookmaker key emitted in the normalized output. MUST match
# ``tools.book_keys.canonicalize_book("Fanatics")`` for enrichment merges
# and edge-scanner soft-book membership to line up.
_BOOK_KEY = "fanatics"
_BOOK_TITLE = "Fanatics"


# ---------------------------------------------------------------------------
# Rate limiting & shared client
# ---------------------------------------------------------------------------

_last_request_time: float = 0.0
_RATE_LIMIT_SECONDS = 2.0

_client: Optional[httpx.AsyncClient] = None

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://sportsbook.fanatics.com/",
    # Fanatics returns 403 on requests missing the x-geolocation-token.
    # Public pages set a default value when the user is in-region. We send
    # a plausible but neutral token; if Fanatics ever hard-validates it,
    # the session cookie path (authenticated) takes over.
    "x-client-type": "web",
}


def _get_client() -> httpx.AsyncClient:
    """Return the shared async httpx client, creating it on first use."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=15.0,
            headers=_HEADERS,
            follow_redirects=True,
            max_redirects=5,
        )
    return _client


async def close_client() -> None:
    """Close the shared client. Called from the line_monitor shutdown path."""
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
        _client = None


def _auth_cookie_header() -> Optional[str]:
    """Return a ``Cookie`` header value for authenticated requests, or None.

    Uses the credential manager — never reads env vars directly. When
    ``CALLISTO_FANATICS_SESSION_COOKIE`` is absent we run unauthenticated.
    """
    cookie = get_credential("fanatics", FIELD_SESSION_COOKIE)
    if not cookie:
        return None
    # Allow raw "name=value; name2=value2" strings; wrap a bare token as
    # a best-effort session cookie.
    if "=" in cookie:
        return cookie
    return f"fanatics_session={cookie}"


async def _rate_limited_get(url: str) -> httpx.Response:
    """GET with a token-bucket style rate limit (1 req / 2s)."""
    global _last_request_time
    now = time.monotonic()
    elapsed = now - _last_request_time
    if elapsed < _RATE_LIMIT_SECONDS:
        await asyncio.sleep(_RATE_LIMIT_SECONDS - elapsed)
    _last_request_time = time.monotonic()

    client = _get_client()
    headers = {}
    cookie_hdr = _auth_cookie_header()
    if cookie_hdr:
        headers["Cookie"] = cookie_hdr
    resp = await client.get(url, headers=headers)
    # Don't raise — we want the caller to inspect status so rate-limit /
    # forbidden cases turn into structured payloads rather than exceptions.
    return resp


# ---------------------------------------------------------------------------
# Odds parsing
# ---------------------------------------------------------------------------

def _decimal_to_american(dec: float) -> int:
    """Convert decimal (European) odds to American odds."""
    if dec >= 2.0:
        return round((dec - 1) * 100)
    elif dec > 1.0:
        return round(-100 / (dec - 1))
    return -10000


def _parse_american_odds(raw: object) -> Optional[int]:
    """Extract American odds from a Fanatics selection value.

    Fanatics returns odds in one of:
      - ``americanPrice`` / ``americanOdds`` as a signed integer or
        string, possibly with a Unicode minus (U+2212) prefix.
      - ``decimalPrice`` / ``oddsDecimal`` as a float.
      - Nested ``{"american": "+150", "decimal": 2.5}`` under ``price``.
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        # Numeric > ~10 is almost certainly American (decimal caps ~15);
        # numeric < 10 is definitely decimal. Use a conservative cutoff.
        if isinstance(raw, int) or abs(raw) >= 10:
            return int(raw)
        if raw > 1.0:
            return _decimal_to_american(float(raw))
        return None
    if isinstance(raw, str):
        s = raw.strip().replace("−", "-").replace("–", "-").replace("+", "")
        if not s:
            return None
        try:
            return int(round(float(s)))
        except ValueError:
            return None
    if isinstance(raw, dict):
        # Try american first
        for key in ("american", "americanPrice", "americanOdds"):
            v = raw.get(key)
            if v is not None:
                parsed = _parse_american_odds(v)
                if parsed is not None:
                    return parsed
        for key in ("decimal", "decimalPrice", "oddsDecimal"):
            v = raw.get(key)
            if v is not None:
                try:
                    return _decimal_to_american(float(v))
                except (ValueError, TypeError):
                    continue
    return None


def _parse_selection_price(sel: dict) -> Optional[int]:
    """Pull an American odds integer out of any Fanatics selection shape."""
    # Try flat fields first
    for key in ("americanPrice", "americanOdds", "american_odds"):
        if key in sel:
            parsed = _parse_american_odds(sel.get(key))
            if parsed is not None:
                return parsed
    # Nested price objects
    for key in ("price", "odds"):
        if key in sel:
            parsed = _parse_american_odds(sel.get(key))
            if parsed is not None:
                return parsed
    # Decimal-only fallback
    for key in ("decimalPrice", "oddsDecimal", "decimal"):
        if key in sel:
            try:
                return _decimal_to_american(float(sel[key]))
            except (ValueError, TypeError):
                continue
    return None


def _get_line(sel: dict) -> Optional[float]:
    """Return the spread/total line for a selection, or None."""
    for key in ("line", "points", "handicap", "pointsHandicap", "specialBetValue"):
        v = sel.get(key)
        if v is None:
            continue
        try:
            return float(v)
        except (ValueError, TypeError):
            continue
    return None


# ---------------------------------------------------------------------------
# Market classification
# ---------------------------------------------------------------------------

_H2H_KEYWORDS = ("moneyline", "money line", "match winner", "game winner", "to win")
_SPREAD_KEYWORDS = ("spread", "point spread", "handicap", "run line", "puck line", "runline", "puckline")
_TOTAL_KEYWORDS = ("total", "over/under", "over under", "total points", "total runs", "total goals")


def _classify_market(market: dict) -> Optional[str]:
    """Classify a Fanatics market into h2h / spreads / totals, or None."""
    candidates = []
    for key in ("marketType", "type", "name", "displayName", "templateName"):
        v = market.get(key)
        if isinstance(v, str):
            candidates.append(v.lower())
        elif isinstance(v, dict):
            n = v.get("name") or v.get("value") or ""
            if isinstance(n, str):
                candidates.append(n.lower())

    blob = " ".join(candidates)
    if not blob:
        return None

    # Order matters: check spread / total before moneyline because the
    # keyword "spread" is a prefix-free match but "winner" overlaps.
    if any(kw in blob for kw in _SPREAD_KEYWORDS):
        return "spreads"
    if any(kw in blob for kw in _TOTAL_KEYWORDS):
        return "totals"
    if any(kw in blob for kw in _H2H_KEYWORDS):
        return "h2h"
    return None


def _get_selections(market: dict) -> list[dict]:
    """Extract the selection/outcome list from a market under any field name."""
    for key in ("selections", "outcomes", "results", "runners"):
        v = market.get(key)
        if isinstance(v, list):
            return v
    return []


def _selection_label(sel: dict) -> str:
    """Return the display label of a selection."""
    for key in ("name", "displayName", "participantName", "label", "selectionName"):
        v = sel.get(key)
        if isinstance(v, str) and v:
            return v
        if isinstance(v, dict):
            val = v.get("value") or v.get("name")
            if isinstance(val, str) and val:
                return val
    return ""


def _event_participants(event: dict) -> tuple[str, str]:
    """Return (home_team, away_team) parsed from a Fanatics event."""
    home = away = ""
    parts = event.get("participants") or event.get("competitors") or []
    for p in parts:
        role = (p.get("type") or p.get("role") or p.get("homeAway") or "").lower()
        name = p.get("name") or p.get("fullName") or ""
        if isinstance(name, dict):
            name = name.get("value", "")
        if not isinstance(name, str):
            continue
        if role in ("home",):
            home = name
        elif role in ("away", "road", "visitor"):
            away = name

    # Fallback: parse the event name.
    if not home or not away:
        title = event.get("name") or event.get("eventName") or ""
        if isinstance(title, str) and title:
            pieces = title.replace(" vs ", " @ ").split(" @ ")
            if len(pieces) == 2:
                away = away or pieces[0].strip()
                home = home or pieces[1].strip()
    return home, away


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def _normalize_event(event: dict, sport: str) -> Optional[dict]:
    """Convert one Fanatics event to the standard Callisto game dict.

    Returns None if the event carries no usable markets.
    """
    event_id = str(event.get("id") or event.get("eventId") or "")
    home, away = _event_participants(event)
    start = (
        event.get("startTime")
        or event.get("startDate")
        or event.get("scheduledStartTime")
        or ""
    )

    markets_raw = event.get("markets") or event.get("offers") or []

    buckets: dict[str, list[dict]] = {"h2h": [], "spreads": [], "totals": []}

    for mkt in markets_raw:
        key = _classify_market(mkt)
        if key is None:
            continue
        sels = _get_selections(mkt)
        if not sels:
            continue

        for sel in sels:
            price = _parse_selection_price(sel)
            if price is None:
                continue
            label = _selection_label(sel)
            entry: dict = {"name": label, "price": price}
            line = _get_line(sel)
            if line is not None:
                entry["point"] = line
            if key == "totals":
                lname = label.lower()
                if "over" in lname:
                    entry["name"] = "Over"
                elif "under" in lname:
                    entry["name"] = "Under"
            buckets[key].append(entry)

    markets: list[dict] = []
    now_iso = datetime.now(timezone.utc).isoformat()
    for k in ("h2h", "spreads", "totals"):
        if buckets[k]:
            markets.append({"key": k, "last_update": now_iso, "outcomes": buckets[k]})

    if not markets:
        return None

    return {
        "id": f"fanatics_{event_id}" if event_id else f"fanatics_{home}_{away}",
        "sport_key": sport,
        "sport_title": _sport_title(sport),
        "home_team": home,
        "away_team": away,
        "commence_time": start,
        "bookmakers": [
            {
                "key": _BOOK_KEY,
                "title": _BOOK_TITLE,
                "last_update": now_iso,
                "markets": markets,
            }
        ],
    }


def _extract_events(data: object) -> list[dict]:
    """Pull the event list from any of the response shapes Fanatics uses."""
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("events", "items", "results", "data", "fixtures"):
        v = data.get(key)
        if isinstance(v, list):
            return [x for x in v if isinstance(x, dict)]
    # Single event response
    if "participants" in data or "markets" in data:
        return [data]
    return []


def _sport_title(sport_key: str) -> str:
    return {
        "basketball_nba": "NBA",
        "americanfootball_nfl": "NFL",
        "icehockey_nhl": "NHL",
        "baseball_mlb": "MLB",
    }.get(sport_key, sport_key)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def _fetch_and_parse(sport: str) -> dict:
    """Inner fetch — walk endpoint candidates until one gives us a body.

    Returns the same shape as the other book scrapers:
        {sport, game_count, games, source, credits}
    On rate-limit / forbidden:
        {error: "...", games: [], source: "fanatics_scraper", status: "rate_limited"}
    """
    league_key = FANATICS_LEAGUE_KEYS.get(sport)
    if not league_key:
        return {"error": f"Unsupported sport: {sport}", "games": []}

    last_err: Optional[str] = None
    for template in _ENDPOINT_CANDIDATES:
        url = template.format(league=league_key)
        try:
            resp = await _rate_limited_get(url)
        except httpx.TimeoutException:
            last_err = "timeout"
            continue
        except Exception as e:  # noqa: BLE001 — network layer can raise many types
            last_err = f"request-error: {e}"
            continue

        status = resp.status_code
        # Rate-limit / forbidden: surface as a structured sentinel so the
        # @tracked_ingestion wrapper can tag status='rate_limited'.
        if status in (403, 429):
            logger.info(f"Fanatics {sport}: {status} on {url} — rate-limited / forbidden")
            return {
                "error": f"rate limit (HTTP {status})",
                "games": [],
                "source": "fanatics_scraper",
                "status": "rate_limited",
            }
        if status >= 500:
            last_err = f"HTTP {status}"
            continue
        if status >= 400:
            last_err = f"HTTP {status}"
            continue

        try:
            data = resp.json()
        except Exception as e:  # noqa: BLE001
            last_err = f"json-decode: {e}"
            continue

        events = _extract_events(data)
        games: list[dict] = []
        for ev in events:
            parsed = _normalize_event(ev, sport)
            if parsed is not None:
                games.append(parsed)

        logger.info(f"Fanatics scrape {sport}: {len(games)} games from {url}")
        return {
            "sport": sport,
            "game_count": len(games),
            "games": games,
            "source": "fanatics_scraper",
            "credits": {"remaining": None, "used": None, "api_key_set": bool(get_credential("fanatics", FIELD_USERNAME))},
        }

    return {
        "error": last_err or "all endpoints failed",
        "games": [],
        "source": "fanatics_scraper",
    }


@tracked_ingestion(source=lambda sport, *a, **kw: f"fanatics.odds.{sport}")
async def fetch_fanatics_odds(sport: str) -> dict:
    """
    Scrape Fanatics pregame odds for a sport.

    Returns data in the same format as ``tools/odds_api.get_odds()`` and
    the other book scrapers, so line_monitor can plug the result through
    the same enrichment / snapshot path.

    Args:
        sport: Callisto sport key — one of
            ``basketball_nba``, ``americanfootball_nfl``,
            ``icehockey_nhl``, ``baseball_mlb``.

    Never raises — on any failure returns a dict with an ``error`` field
    and empty ``games`` list. The @tracked_ingestion wrapper records
    status='rate_limited' for 403/429 and status='failed' for other
    errors, so data_collector health picks up silent outages.
    """
    return await _fetch_and_parse(sport)


# Back-compat alias — reads more naturally at call sites.
scrape_fanatics_odds = fetch_fanatics_odds


__all__ = [
    "FANATICS_LEAGUE_KEYS",
    "fetch_fanatics_odds",
    "scrape_fanatics_odds",
    "close_client",
]
