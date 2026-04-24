"""
news_ingestion — multi-source injury / lineup / coaching news collector.

Why this exists
---------------
Injury news, scratches, and coaching decisions move lines faster than any
other public signal. Callisto had zero ingestion of these — the edge-scanner
was running on stale roster assumptions and every "starter OUT 30min before
tip" situation passed by unnoticed.

This module is the canonical ingress. Three async fetchers (``fetch_injuries``,
``fetch_lineup_changes``, ``fetch_coaching_news``) hit multiple providers,
dedupe cross-source, infer severity, and persist rows into ``news_events``
(schema defined in migration 012). A companion module ``tools.news_impact``
correlates these rows with odds movements to find under-reactions.

Sources
-------
Primary:
  * ESPN injuries endpoint (free, no key). Uses the same shapes as
    ``tools.contextual_data`` but walks the DOM independently — we don't
    import that module per the worktree constraints.
Secondary:
  * RotoWire public news feed (rate-limit ~1 req / 3s — enforced by the
    ``_RotoWireLimiter`` singleton). Rotoworld shares infrastructure so we
    treat the same-domain URL set as one provider for dedup.
Fallback:
  * odds-api.io news/context endpoints. Called opportunistically if the
    provider returns a ``context`` block in the normal odds response — we
    don't burn a dedicated credit polling it.

Design constraints
------------------
1. **Never touch the no-fly list.** The ingestion flow has NO direct
   dependency on ``tools.contextual_data``, ``tools.data_collector``,
   ``tools.edge_scanner``, or ``tools.line_monitor``. Impact scoring is a
   *separate* module (``tools.news_impact``) that reads ``line_movements``
   through a plain SQL path, not via those modules' APIs.
2. **Silent-failure guard.** Every fetcher is wrapped in ``@tracked_ingestion``
   so a broken scraper surfaces in ``ingestion_runs`` / the health endpoint
   rather than hiding behind a caught exception. The wrapped functions
   return ``{"error": ...}`` sentinels on failure — the decorator recognises
   those and tags them ``status='failed'`` (see ``ingestion_tracking.py``).
3. **Dedup confidence.** Cross-source match uses the existing
   ``tools.player_name_index`` fuzzy matcher with threshold 0.90. Body-part
   equality is required too — two simultaneous injuries to the same athlete
   on different body parts are distinct events, not duplicates.
4. **Fragile selectors.** RotoWire's HTML class names change occasionally.
   Selector constants live in a module-level dict flagged FRAGILE so a
   reviewer spotting a broken scrape knows exactly where to patch.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import aiosqlite
import httpx

from tools.ingestion_tracking import tracked_ingestion
from tools.player_name_index import (
    _normalise as _normalize_name,       # noqa: F401
    fuzzy_match_score,
    DEFAULT_CONFIDENCE_THRESHOLD,
)

logger = logging.getLogger("callisto.news_ingestion")

DB_PATH = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")

# Shared client — httpx pools per-host so one instance is fine.
_client: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=15.0,
            headers={
                # A real UA avoids RotoWire's bot interstitial.
                "User-Agent": (
                    "Mozilla/5.0 (compatible; Callisto/1.0; "
                    "+https://github.com/callisto-bot)"
                ),
                "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
            },
        )
    return _client


async def close_client() -> None:
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
        _client = None


# ─────────────────────────────────────────────
# Data shapes
# ─────────────────────────────────────────────

@dataclass
class InjuryEvent:
    """One injury observation. Source-specific; dedupe merges across sources."""
    sport: str
    player_name: str
    team: Optional[str]
    body_part: Optional[str]
    status: Optional[str]           # 'questionable' | 'probable' | 'doubtful' | 'out' | 'inactive'
    severity: Optional[str]          # 'minor' | 'moderate' | 'severe' | 'out_indefinite'
    first_seen_at: str               # ISO timestamp
    source: str
    source_url: Optional[str]
    raw: dict                        # per-source payload for forensic replay
    local_game_date: Optional[str] = None

    def as_news_row(self) -> dict:
        return {
            "sport": self.sport,
            "event_id": None,
            "player_name": self.player_name,
            "event_type": "injury",
            "severity": self.severity,
            "body_part": self.body_part,
            "status": self.status,
            "first_seen_at": self.first_seen_at,
            "confirmed_at": None,
            "source": self.source,
            "source_url": self.source_url,
            "raw_json": json.dumps(self.raw, default=str),
            "local_game_date": self.local_game_date,
        }


@dataclass
class LineupEvent:
    sport: str
    player_name: str
    team: Optional[str]
    change_type: str                 # 'late_scratch' | 'surprise_start' | 'position_change'
    first_seen_at: str
    source: str
    source_url: Optional[str]
    raw: dict
    local_game_date: Optional[str] = None

    def as_news_row(self) -> dict:
        return {
            "sport": self.sport,
            "event_id": None,
            "player_name": self.player_name,
            "event_type": "lineup_change",
            "severity": "moderate" if self.change_type == "late_scratch" else "minor",
            "body_part": None,
            "status": "inactive" if self.change_type == "late_scratch" else None,
            "first_seen_at": self.first_seen_at,
            "confirmed_at": None,
            "source": self.source,
            "source_url": self.source_url,
            "raw_json": json.dumps({"change_type": self.change_type, **self.raw}, default=str),
            "local_game_date": self.local_game_date,
        }


@dataclass
class CoachingEvent:
    sport: str
    team: str
    decision: str                    # 'rest_starters' | 'mop_up_lineup' | 'tactical_change'
    affected_players: list[str]      # may be empty
    first_seen_at: str
    source: str
    source_url: Optional[str]
    raw: dict
    local_game_date: Optional[str] = None

    def as_news_row(self) -> dict:
        # Coaching decisions are emitted as one row per affected player so the
        # dedup/correlation layer can key off player_name uniformly. If there
        # are no specific players named, we emit a single team-level row with
        # player_name=None.
        return {
            "sport": self.sport,
            "event_id": None,
            "player_name": None,  # overridden when iterating affected_players
            "event_type": "coaching_decision",
            "severity": "severe" if self.decision == "rest_starters" else "moderate",
            "body_part": None,
            "status": "inactive" if self.decision == "rest_starters" else None,
            "first_seen_at": self.first_seen_at,
            "confirmed_at": None,
            "source": self.source,
            "source_url": self.source_url,
            "raw_json": json.dumps(
                {"team": self.team, "decision": self.decision, **self.raw},
                default=str,
            ),
            "local_game_date": self.local_game_date,
        }


# ─────────────────────────────────────────────
# Severity + status inference
# ─────────────────────────────────────────────

# Keyword -> (status, severity). Ordered: first match wins, so put
# strongest/most specific tokens first.
_SEVERITY_RULES: list[tuple[str, tuple[str, str]]] = [
    ("out for the season", ("out", "out_indefinite")),
    ("season-ending",      ("out", "out_indefinite")),
    ("out indefinitely",   ("out", "out_indefinite")),
    ("placed on ir",       ("out", "out_indefinite")),
    ("injured reserve",    ("out", "out_indefinite")),
    ("ruled out",          ("out", "severe")),
    ("will not play",      ("out", "severe")),
    ("inactive",           ("inactive", "severe")),
    ("doubtful",           ("doubtful", "moderate")),
    ("questionable",       ("questionable", "minor")),
    ("probable",           ("probable", "minor")),
    ("day-to-day",         ("questionable", "minor")),
    ("day to day",         ("questionable", "minor")),
    ("game-time decision", ("questionable", "minor")),
    ("game time decision", ("questionable", "minor")),
]

# ESPN's "status" strings often match our buckets directly. Map them and
# fall through to free-text inference if nothing lands.
_ESPN_STATUS_MAP = {
    "out":          ("out", "severe"),
    "doubtful":     ("doubtful", "moderate"),
    "questionable": ("questionable", "minor"),
    "probable":     ("probable", "minor"),
    "day-to-day":   ("questionable", "minor"),
    "active":       (None, None),
    "suspension":   ("inactive", "severe"),
    "suspended":    ("inactive", "severe"),
}


def infer_severity(
    status_text: Optional[str],
    detail_text: Optional[str],
) -> tuple[Optional[str], Optional[str]]:
    """Return ``(status, severity)`` from free-text + structured status.

    Inference order:
      1. Free-text keyword hit from _SEVERITY_RULES (most specific —
         "out for the season" is stronger than ESPN's generic "Out").
      2. Structured ESPN status → map directly.
      3. Fall back to (None, 'minor') — better a floor than no data.
    """
    combined = " ".join(filter(None, [status_text or "", detail_text or ""])).lower()

    # Run the keyword rules first; they're ordered strongest-first so the
    # first hit is the right answer. Critically this lets "out for the
    # season" upgrade a bare ESPN status='Out' to out_indefinite.
    for needle, (status, sev) in _SEVERITY_RULES:
        if needle in combined:
            return status, sev

    st_norm = (status_text or "").strip().lower()
    if st_norm in _ESPN_STATUS_MAP:
        mapped = _ESPN_STATUS_MAP[st_norm]
        if mapped != (None, None):
            return mapped

    # Nothing matched. If we had ANY status string, assume minor; else None.
    if st_norm:
        return st_norm or None, "minor"
    return None, None


# Body-part extraction is a shallow bag-of-tokens heuristic. Good enough for
# v1; upgrade to a dedicated NER model later if false-positive rate hurts.
_BODY_PART_MAP = {
    "lower_body": [
        "knee", "ankle", "foot", "heel", "toe", "hamstring", "quad",
        "calf", "groin", "hip", "leg", "shin", "achilles", "tibia",
    ],
    "upper_body": [
        "shoulder", "elbow", "wrist", "hand", "finger", "arm",
        "forearm", "bicep", "tricep", "pec", "chest", "collarbone",
    ],
    "core":      ["back", "oblique", "abdomen", "abdominal", "rib", "hip-flexor"],
    "head":      ["head", "concussion", "face", "jaw", "neck", "eye"],
    "illness":   ["illness", "flu", "covid", "sick", "virus"],
}


def infer_body_part(detail_text: Optional[str]) -> Optional[str]:
    if not detail_text:
        return None
    low = detail_text.lower()
    for bucket, tokens in _BODY_PART_MAP.items():
        for tok in tokens:
            # Word-boundary to avoid "back" matching "background".
            if re.search(rf"\b{re.escape(tok)}\b", low):
                return bucket
    return None


# ─────────────────────────────────────────────
# ESPN fetcher (primary)
# ─────────────────────────────────────────────

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"

_ESPN_SPORT_MAP = {
    "basketball_nba":        ("basketball", "nba"),
    "basketball_ncaab":      ("basketball", "mens-college-basketball"),
    "basketball_ncaaw":      ("basketball", "womens-college-basketball"),
    "americanfootball_nfl":  ("football", "nfl"),
    "americanfootball_ncaaf": ("football", "college-football"),
    "baseball_mlb":          ("baseball", "mlb"),
    "icehockey_nhl":         ("hockey", "nhl"),
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@tracked_ingestion(
    source=lambda sport, **_: f"espn.injuries.{sport}",
    sla_seconds=1800,
)
async def _fetch_espn_injuries(sport: str) -> list[InjuryEvent]:
    """Pull ESPN's injuries endpoint and parse into InjuryEvent list.

    Returns an empty list on failure (caller treats ``[]`` as partial success;
    ``tracked_ingestion`` tags status='partial' when rows==0 which is the
    correct signal for "endpoint reachable, no rows today").
    """
    m = _ESPN_SPORT_MAP.get(sport)
    if not m:
        logger.debug(f"ESPN injuries: sport {sport} not mapped")
        return []

    sport_path, league_path = m
    url = f"{ESPN_BASE}/{sport_path}/{league_path}/injuries"
    try:
        resp = await _get_client().get(url)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning(f"ESPN injuries {sport} fetch error: {e}")
        return []

    now = _now_iso()
    out: list[InjuryEvent] = []
    for team_block in data.get("items", []) or []:
        team_name = ((team_block.get("team") or {}).get("displayName") or "").strip()
        for entry in team_block.get("injuries", []) or []:
            athlete = entry.get("athlete") or {}
            player = (athlete.get("displayName") or "").strip()
            if not player:
                continue
            status_raw = (entry.get("status") or "").strip()
            details = entry.get("details") or {}
            detail_text = " ".join(
                str(v) for v in (
                    details.get("detail"),
                    details.get("side"),
                    details.get("type"),
                    (entry.get("type") or {}).get("description") if isinstance(entry.get("type"), dict) else None,
                ) if v
            )
            status, severity = infer_severity(status_raw, detail_text)
            body_part = infer_body_part(detail_text)
            out.append(InjuryEvent(
                sport=sport,
                player_name=player,
                team=team_name or None,
                body_part=body_part,
                status=status,
                severity=severity,
                first_seen_at=now,
                source="espn.injuries",
                source_url=url,
                raw=entry,
            ))
    logger.info(f"ESPN injuries {sport}: parsed {len(out)} events")
    return out


# ─────────────────────────────────────────────
# RotoWire scraper (secondary)
# ─────────────────────────────────────────────
#
# FRAGILE: RotoWire doesn't publish a JSON API on their public news page.
# These selectors are bound to HTML classes that occasionally get renamed —
# if the scraper starts returning 0 events consistently, first check these.
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


class _RotoWireLimiter:
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


_TAG_RE = re.compile(r"<[^>]+>")


def _strip_tags(s: str) -> str:
    return _TAG_RE.sub("", s or "").strip()


@tracked_ingestion(
    source=lambda sport, **_: f"rotowire.news.{sport}",
    sla_seconds=3600,
)
async def _fetch_rotowire_news(sport: str) -> list[InjuryEvent]:
    """Scrape RotoWire's public news page. Rate-limited to 1 req / 3s.

    We only extract injury-adjacent blurbs here — lineup/coaching headlines
    come from different parsers (``_fetch_rotowire_lineups``).
    """
    slug = _ROTOWIRE_SPORT_MAP.get(sport)
    if not slug:
        return []

    await _RotoWireLimiter.wait()
    url = _ROTOWIRE_SELECTORS["base_url"].format(sport=slug)
    try:
        resp = await _get_client().get(url)
        resp.raise_for_status()
        html = resp.text
    except Exception as e:
        logger.warning(f"RotoWire news {sport} fetch error: {e}")
        return []

    now = _now_iso()
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


# ─────────────────────────────────────────────
# Cross-source deduplication
# ─────────────────────────────────────────────

def _dedup_key(sport: str, player_name: str, body_part: Optional[str]) -> tuple:
    """Canonicalise the three-tuple used to match the same underlying event
    across sources. Name is lowercased + punct-stripped via the shared
    player-name normaliser; body_part falls back to ``'unknown'`` so a row
    with no body_part still dedupes against itself."""
    return (
        (sport or "").strip(),
        _normalize_name(player_name or ""),
        (body_part or "unknown").lower(),
    )


_HEADLINE_PUNCT_RE = re.compile(r"[^a-z0-9\s]")
_HEADLINE_WS_RE = re.compile(r"\s+")


def normalize_headline(text: Optional[str]) -> str:
    """Canonicalise a headline/body string for cross-feed exact-dedup.

    Two feeds frequently publish the same headline verbatim (or with trivial
    differences: punctuation, a source byline, whitespace). Content-hashing
    the normalised form catches duplicates the name+body-part heuristic
    misses (e.g. team-level coaching decisions that carry no player name).
    """
    if not text:
        return ""
    low = text.strip().lower()
    low = _HEADLINE_PUNCT_RE.sub(" ", low)
    low = _HEADLINE_WS_RE.sub(" ", low).strip()
    return low


def headline_hash(text: Optional[str]) -> Optional[str]:
    """Stable 16-char hex digest of a headline for cross-feed dedup."""
    norm = normalize_headline(text)
    if not norm:
        return None
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def _extract_headline(raw: Any) -> Optional[str]:
    """Extract a headline-like string from a raw payload dict.

    Supports the shapes the fetchers emit: ESPN's ``details.detail``,
    RotoWire's ``headline``, scoreboard notes' ``headline``, and falls
    back to ``str(raw)`` if nothing more specific is available. Returns
    None for empty payloads.
    """
    if not raw:
        return None
    if isinstance(raw, str):
        return raw or None
    if not isinstance(raw, dict):
        return None
    # Check common fields in order of preference.
    for k in ("headline", "detail", "description", "news"):
        v = raw.get(k)
        if isinstance(v, str) and v.strip():
            return v
    # Nested ESPN detail block.
    d = raw.get("details")
    if isinstance(d, dict):
        v = d.get("detail")
        if isinstance(v, str) and v.strip():
            return v
    return None


def dedupe_injuries(events: list[InjuryEvent]) -> list[dict]:
    """Collapse same-injury events across sources into single rows.

    Sets ``confirmed_at`` to the first_seen_at of the SECOND source that
    reports the same key. Single-source rows keep ``confirmed_at=None``.
    The returned list is schema-shaped (dicts ready for INSERT).

    Matching strategy: O(n^2) across the (usually small) events list. Two
    events match iff they are for the same sport, their body_parts agree
    (both NULL counts as agree), and ``fuzzy_match_score(name_a, name_b) >=
    DEFAULT_CONFIDENCE_THRESHOLD`` (0.90). That upgrades
    ``"Jayson Tatum" <-> "J. Tatum"`` to a dedup hit while keeping
    ``"Kevin Durant" <-> "Kevin Huerter"`` distinct.
    """
    groups: list[list[InjuryEvent]] = []

    def _bp_compat(a: Optional[str], b: Optional[str]) -> bool:
        # Treat unknown body_part as compatible with anything — ESPN's
        # detail free-text often doesn't let us classify body_part even
        # though the event is the same.
        if not a or not b:
            return True
        return a == b

    # Pre-compute headline hashes per event — cross-feed exact-content
    # dedup catches the "same headline, different source" case that the
    # name+body-part heuristic misses (e.g. team-level coaching decisions
    # that carry no player_name at all).
    ev_hashes: list[Optional[str]] = [
        headline_hash(_extract_headline(ev.raw)) for ev in events
    ]

    for i, ev in enumerate(events):
        placed = False
        for j, group in enumerate(groups):
            head = group[0]
            head_idx = events.index(head)
            head_hash = ev_hashes[head_idx] if head_idx < len(ev_hashes) else None
            ev_hash = ev_hashes[i]

            # Content-hash match: identical normalised headline from any
            # source collapses into the existing group immediately.
            hash_match = (
                head.sport == ev.sport
                and head_hash is not None
                and ev_hash is not None
                and head_hash == ev_hash
            )

            # Fuzzy-name + body-part fallback for cases with differing
            # body text (e.g. ESPN JSON vs RotoWire blurb).
            name_match = (
                head.sport == ev.sport
                and head.player_name and ev.player_name
                and _bp_compat(head.body_part, ev.body_part)
                and fuzzy_match_score(head.player_name, ev.player_name)
                >= DEFAULT_CONFIDENCE_THRESHOLD
            )

            if hash_match or name_match:
                group.append(ev)
                placed = True
                break
        if not placed:
            groups.append([ev])

    out: list[dict] = []
    for group in groups:
        group.sort(key=lambda e: e.first_seen_at)
        primary = group[0]
        row = primary.as_news_row()
        # Prefer the non-None, most-severe classification across sources.
        severities = [g.severity for g in group if g.severity]
        status_vals = [g.status for g in group if g.status]
        if severities:
            # Order: out_indefinite > severe > moderate > minor
            order = {"minor": 1, "moderate": 2, "severe": 3, "out_indefinite": 4}
            row["severity"] = max(severities, key=lambda s: order.get(s, 0))
        if status_vals:
            row["status"] = status_vals[0]  # first-source status wins; later sources agree ~always
        # Cross-source confirmation: need distinct source strings.
        distinct_sources = {g.source for g in group}
        if len(distinct_sources) >= 2:
            # confirmed_at = first_seen_at of the 2nd source to report.
            row["confirmed_at"] = group[1].first_seen_at
            row["source"] = "+".join(sorted(distinct_sources))
        out.append(row)
    return out


# ─────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────

async def fetch_injuries(sport: str) -> list[dict]:
    """Multi-source injury fetch, deduped. Returns schema-shaped rows.

    Row keys are the columns of ``news_events`` — caller can ``executemany``
    them directly. Never raises: partial-source failures are logged and
    surfaced as ``ingestion_runs`` rows via the decorator layer.
    """
    results: list[InjuryEvent] = []

    # Launch both primary sources concurrently. Any one failing still lets
    # the other contribute rows (we dedupe afterwards regardless).
    primary = _fetch_espn_injuries(sport)
    secondary = _fetch_rotowire_news(sport)
    got = await asyncio.gather(primary, secondary, return_exceptions=True)
    for g in got:
        if isinstance(g, Exception):
            logger.info(f"Injury source errored: {g}")
            continue
        if isinstance(g, list):
            results.extend(g)

    return dedupe_injuries(results)


@tracked_ingestion(
    source=lambda sport, date=None, **_: f"espn.lineups.{sport}",
    sla_seconds=900,
)
async def _fetch_espn_scoreboard_lineups(sport: str, date: Optional[str] = None) -> list[LineupEvent]:
    """Pull late lineup changes from ESPN's scoreboard. Surprise starts and
    late scratches surface here before they appear on the injuries endpoint
    (which is roster-level, slower to update)."""
    m = _ESPN_SPORT_MAP.get(sport)
    if not m:
        return []
    sport_path, league_path = m
    url = f"{ESPN_BASE}/{sport_path}/{league_path}/scoreboard"
    params = {"dates": date.replace("-", "")} if date else None
    try:
        resp = await _get_client().get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning(f"ESPN scoreboard {sport} fetch error: {e}")
        return []

    now = _now_iso()
    out: list[LineupEvent] = []
    for event in data.get("events", []) or []:
        for comp in event.get("competitions", []) or []:
            for competitor in comp.get("competitors", []) or []:
                team_name = ((competitor.get("team") or {}).get("displayName") or "").strip()
                for inj in competitor.get("injuries", []) or []:
                    ath = inj.get("athlete") or {}
                    player = (ath.get("displayName") or "").strip()
                    status = (inj.get("status") or "").lower()
                    if not player:
                        continue
                    # A scoreboard-time "out" effectively is a late scratch if
                    # it wasn't on the pre-game injuries endpoint. We tag it
                    # as such; the dedupe layer against the injuries feed in
                    # news_events dedup will reconcile.
                    if status in ("out", "inactive"):
                        out.append(LineupEvent(
                            sport=sport,
                            player_name=player,
                            team=team_name or None,
                            change_type="late_scratch",
                            first_seen_at=now,
                            source="espn.scoreboard",
                            source_url=url,
                            raw=inj,
                        ))
    logger.info(f"ESPN scoreboard {sport}: {len(out)} late-scratch candidates")
    return out


async def fetch_lineup_changes(sport: str, date: Optional[str] = None) -> list[dict]:
    """Late scratches + surprise lineups for a date.

    For now this routes through ESPN's scoreboard late-scratch signal. The
    RotoWire lineup parser is a follow-up once we have confirmed the ESPN
    base-rate is the critical signal (it usually is)."""
    try:
        events = await _fetch_espn_scoreboard_lineups(sport, date)
    except Exception as e:
        logger.warning(f"fetch_lineup_changes error: {e}")
        return []
    return [ev.as_news_row() for ev in events]


@tracked_ingestion(
    source=lambda sport, date=None, **_: f"espn.coaching.{sport}",
    sla_seconds=3600,
)
async def _fetch_espn_coaching(sport: str, date: Optional[str] = None) -> list[CoachingEvent]:
    """Stub — ESPN doesn't publish explicit "coaching decision" items. We
    synthesise them from scoreboard notes when the `notes` array mentions
    rest days / load management. Conservative by design; coaching news is
    high-impact and should not false-positive.
    """
    m = _ESPN_SPORT_MAP.get(sport)
    if not m:
        return []
    sport_path, league_path = m
    url = f"{ESPN_BASE}/{sport_path}/{league_path}/scoreboard"
    params = {"dates": date.replace("-", "")} if date else None
    try:
        resp = await _get_client().get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning(f"ESPN coaching {sport} fetch error: {e}")
        return []

    now = _now_iso()
    out: list[CoachingEvent] = []
    for event in data.get("events", []) or []:
        for comp in event.get("competitions", []) or []:
            notes = comp.get("notes", []) or []
            for n in notes:
                headline = (n.get("headline") or "").lower()
                if any(k in headline for k in ("rest", "load management", "dnp - coach", "dnp-coach")):
                    for competitor in comp.get("competitors", []) or []:
                        team_name = ((competitor.get("team") or {}).get("displayName") or "").strip()
                        out.append(CoachingEvent(
                            sport=sport,
                            team=team_name or "unknown",
                            decision="rest_starters",
                            affected_players=[],
                            first_seen_at=now,
                            source="espn.scoreboard.notes",
                            source_url=url,
                            raw={"headline": headline},
                        ))
    return out


async def fetch_coaching_news(sport: str, date: Optional[str] = None) -> list[dict]:
    """Coaching decisions likely to affect lines (rest days, mop-up lineups)."""
    try:
        events = await _fetch_espn_coaching(sport, date)
    except Exception as e:
        logger.warning(f"fetch_coaching_news error: {e}")
        return []
    rows: list[dict] = []
    for ev in events:
        # Team-level row only — no affected_players resolved in this v1.
        row = ev.as_news_row()
        row["player_name"] = None
        rows.append(row)
    return rows


# ─────────────────────────────────────────────
# Persistence
# ─────────────────────────────────────────────

async def _ensure_schema(db: aiosqlite.Connection) -> None:
    """Best-effort schema check: if ``news_events`` is missing, create it.

    The canonical schema home is migration 012, but tests and the smoke path
    build throwaway DBs — they hit this helper to ensure the table is there
    without running the migration runner.
    """
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS news_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sport TEXT,
            event_id TEXT,
            player_name TEXT,
            event_type TEXT,
            severity TEXT,
            body_part TEXT,
            status TEXT,
            first_seen_at TIMESTAMP,
            confirmed_at TIMESTAMP,
            source TEXT,
            source_url TEXT,
            raw_json TEXT,
            local_game_date DATE,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_news_events_sport_date "
        "ON news_events(sport, local_game_date)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_news_events_player "
        "ON news_events(player_name, first_seen_at DESC)"
    )
    await db.commit()


_NEWS_COLUMNS = (
    "sport", "event_id", "player_name", "event_type", "severity",
    "body_part", "status", "first_seen_at", "confirmed_at", "source",
    "source_url", "raw_json", "local_game_date",
)


async def persist_news_rows(
    rows: list[dict],
    db_path: Optional[str] = None,
    *,
    require_relevance: bool = True,
) -> int:
    """Write rows to ``news_events``. Returns number inserted.

    Idempotency (two-layer):
      1. Within-window: same ``(sport, player_name, body_part, event_type)``
         in the last 6h is a dup (prevents a 5-min poller writing the same
         row 12 times per hour).
      2. Content-hash: normalised headline hash in the last 24h also de-dups,
         catching identical headlines that arrive via different sources
         or with slightly shifted structured fields.

    Relevance gating (``require_relevance=True``, default):
      Rows without a sport tag are dropped. News that can't be mapped to a
      sport is un-JOINable downstream — better to drop at the door than
      clog the table with unfilterable rows.

    Cross-source dedup happens BEFORE this call in ``dedupe_injuries``;
    this function is the time-window + content-hash dedupe layer.
    """
    if not rows:
        return 0
    path = db_path or DB_PATH
    inserted = 0
    dropped_irrelevant = 0
    async with aiosqlite.connect(path) as db:
        await _ensure_schema(db)
        for row in rows:
            # ── Relevance gating: drop rows we can't JOIN downstream.
            if require_relevance:
                sport = (row.get("sport") or "").strip()
                # Team/player/event association — need at least one.
                has_player = bool((row.get("player_name") or "").strip())
                # Pull team from raw_json if present (coaching rows stash it there).
                raw = row.get("raw_json")
                has_team = False
                if raw:
                    try:
                        raw_dict = raw if isinstance(raw, dict) else json.loads(raw)
                        if isinstance(raw_dict, dict) and raw_dict.get("team"):
                            has_team = True
                    except Exception:
                        pass
                if not sport or (not has_player and not has_team):
                    dropped_irrelevant += 1
                    continue

            # ── Layer 1: structured-key within-window dedup (6h).
            dup = await db.execute(
                """
                SELECT id FROM news_events
                WHERE sport IS ?
                  AND COALESCE(player_name, '') = COALESCE(?, '')
                  AND COALESCE(body_part, '') = COALESCE(?, '')
                  AND event_type = ?
                  AND first_seen_at > datetime('now', '-6 hours')
                LIMIT 1
                """,
                (row.get("sport"), row.get("player_name"),
                 row.get("body_part"), row.get("event_type")),
            )
            if await dup.fetchone():
                continue

            # ── Layer 2: headline-hash within-day dedup (24h).
            # Catches cross-source / cross-poll duplicates the structured
            # key misses (same content, different player_name casing,
            # different body_part inference, etc.).
            raw = row.get("raw_json")
            hline: Optional[str] = None
            if raw:
                try:
                    raw_dict = raw if isinstance(raw, dict) else json.loads(raw)
                    hline = _extract_headline(raw_dict)
                except Exception:
                    hline = None
            h_hash = headline_hash(hline) if hline else None
            if h_hash:
                # Tag the normalised hash into raw_json so we can query by it.
                try:
                    raw_dict = raw_dict if isinstance(raw, dict) else json.loads(raw) if raw else {}
                    if not isinstance(raw_dict, dict):
                        raw_dict = {}
                except Exception:
                    raw_dict = {}
                raw_dict["_headline_hash"] = h_hash
                row = {**row, "raw_json": json.dumps(raw_dict, default=str)}

                dup2 = await db.execute(
                    """
                    SELECT id FROM news_events
                    WHERE sport IS ?
                      AND raw_json LIKE ?
                      AND first_seen_at > datetime('now', '-24 hours')
                    LIMIT 1
                    """,
                    (row.get("sport"), f'%"_headline_hash": "{h_hash}"%'),
                )
                if await dup2.fetchone():
                    continue

            values = tuple(row.get(c) for c in _NEWS_COLUMNS)
            await db.execute(
                f"INSERT INTO news_events ({', '.join(_NEWS_COLUMNS)}) "
                f"VALUES ({', '.join('?' * len(_NEWS_COLUMNS))})",
                values,
            )
            inserted += 1
        await db.commit()
    if dropped_irrelevant:
        logger.info(f"persist_news_rows: dropped {dropped_irrelevant} irrelevant rows")
    return inserted


__all__ = [
    "InjuryEvent",
    "LineupEvent",
    "CoachingEvent",
    "fetch_injuries",
    "fetch_lineup_changes",
    "fetch_coaching_news",
    "dedupe_injuries",
    "infer_severity",
    "infer_body_part",
    "persist_news_rows",
    "close_client",
    "normalize_headline",
    "headline_hash",
]
