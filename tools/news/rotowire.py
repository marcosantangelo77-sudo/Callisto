"""RotoWire scraper (secondary source).

FRAGILE: RotoWire doesn't publish a JSON API on their public news page.
These selectors are bound to HTML classes that occasionally get renamed —
if the scraper starts returning 0 events consistently, first check these.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time

from tools.ingestion_tracking import tracked_ingestion
from tools.news._http import facade_get_client as _get_client
from tools.news.espn import now_iso
from tools.news.inference import infer_body_part, infer_severity
from tools.news.models import InjuryEvent

logger = logging.getLogger("callisto.news_ingestion")

_ROTOWIRE_SELECTORS = {
    # The sport-specific news list endpoint (no auth required).
    "base_url": "https://www.rotowire.com/{sport}/news.php",
    # Items are <div class="news-update"> blocks on the page. The headline
    # sits in .news-update__headline a, the body in .news-update__news, and
    # the player anchor (when present) carries data-player-id.
    "item_regex":   r'<div class="news-update[^"]*"[^>]*>(.*?)(?=<div class="news-update[^"]*"|$)',
    "headline":     r'class="news-update__headline"[^>]*>(.*?)<',
    "player":       r'<a[^>]+data-player-id="(\d+)"[^>]*>([^<]+)</a>',
    # Body is terminated by the next opening <div or </div tag — handles
    # RotoWire's non-well-formed nesting and our test fixtures alike.
    "body":         r'class="news-update__news[^"]*"[^>]*>(.*?)(?:</div|<div|$)',
    "timestamp":    r'datetime="([^"]+)"',
}

# Rotowire sport slugs
_ROTOWIRE_SPORT_MAP = {
    "basketball_nba":       "basketball",
    "americanfootball_nfl": "football",
    "baseball_mlb":         "baseball",
    "icehockey_nhl":        "hockey",
}


class RotoWireLimiter:
    """Token-bucket-ish gate — 1 request / 3 seconds across all callers."""
    _lock = asyncio.Lock()
    _next_ok_at = 0.0

    @classmethod
    async def wait(cls, min_interval_s: float = 3.0) -> None:
        async with cls._lock:
            now = time.monotonic()
            wait_s = cls._next_ok_at - now
            if wait_s > 0:
                await asyncio.sleep(wait_s)
            cls._next_ok_at = time.monotonic() + min_interval_s


_TAG_RE = re.compile(r"<[^>]+")


def _strip_tags(s: str) -> str:
    return _TAG_RE.sub("", s or "").strip()


@tracked_ingestion(
    source=lambda sport, **_: f"rotowire.news.{sport}",
    sla_seconds=3600,
)
async def fetch_rotowire_news(sport: str) -> list[InjuryEvent]:
    """Scrape RotoWire's public news page. Rate-limited to 1 req / 3s.

    We only extract injury-adjacent blurbs here — lineup/coaching headlines
    come from different parsers (``fetch_rotowire_lineups``).
    """
    slug = _ROTOWIRE_SPORT_MAP.get(sport)
    if not slug:
        return []

    await RotoWireLimiter.wait()
    url = _ROTOWIRE_SELECTORS["base_url"].format(sport=slug)
    try:
        resp = await _get_client().get(url)
        resp.raise_for_status()
        html = resp.text
    except Exception as e:
        logger.warning(f"RotoWire news {sport} fetch error: {e}")
        return []

    now = now_iso()
    out: list[InjuryEvent] = []
    # Strategy: find every player anchor, then look ahead in the document
    # for the nearest news-update__news body. This tolerates RotoWire's
    # ever-changing outer-div structure far better than a rigid item regex.
    player_iter = re.finditer(_ROTOWIRE_SELECTORS["player"], html)
    for player_m in player_iter:
        player = _strip_tags(player_m.group(2))
        # Search the substring from the anchor forward for the nearest body.
        tail = html[player_m.end(): player_m.end() + 4000]
        body_m = re.search(_ROTOWIRE_SELECTORS["body"], tail, re.DOTALL)
        if not body_m:
            continue
        body = _strip_tags(body_m.group(1))
        if not player or not body:
            continue
        low = body.lower()
        # Only retain injury-flavoured items at this layer. Lineup blurbs
        # hit a separate fetcher.
        if not any(k in low for k in (
            "injur", "sore", "doubt", "question", "out ", "ruled out",
            "day-to-day", "day to day", "sprain", "strain", "concussion",
            "placed on", "ir", "injured reserve",
        )):
            continue
        status, severity = infer_severity(None, body)
        body_part = infer_body_part(body)
        out.append(InjuryEvent(
            sport=sport,
            player_name=player,
            team=None,
            body_part=body_part,
            status=status,
            severity=severity,
            first_seen_at=now,
            source="rotowire.news",
            source_url=url,
            raw={"headline": body[:300]},
        ))
    logger.info(f"RotoWire news {sport}: parsed {len(out)} injury-flavoured items")
    return out
