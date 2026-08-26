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

Implementation layout (split)
-----------------------------
The implementation now lives in the ``tools.news`` package; this module is a
backwards-compatible facade re-exporting the full public surface:

* ``tools.news._http``       — shared pooled httpx client
* ``tools.news.models``      — InjuryEvent / LineupEvent / CoachingEvent
* ``tools.news.inference``    — severity/status/body-part inference
* ``tools.news.espn``        — ESPN fetchers (injuries, scoreboard, coaching)
* ``tools.news.rotowire``    — RotoWire scraper + rate limiter
* ``tools.news.dedup``       — cross-source dedupe
* ``tools.news.api``         — public fetch API + persistence
"""

from __future__ import annotations

import asyncio
import os

import aiosqlite

# Re-export the entire public surface from the split package.
from tools.news import (                      # noqa: F401
    DB_PATH,
    ESPN_BASE,
    CoachingEvent,
    InjuryEvent,
    LineupEvent,
    RotoWireLimiter as _RotoWireLimiter,
    close_client,
    dedup_key as _dedup_key,
    dedupe_injuries,
    ensure_schema as _ensure_schema,
    fetch_coaching_news,
    fetch_espn_coaching as _fetch_espn_coaching,
    fetch_espn_injuries as _fetch_espn_injuries,
    fetch_espn_scoreboard_lineups as _fetch_espn_scoreboard_lineups,
    fetch_injuries,
    fetch_lineup_changes,
    fetch_rotowire_news as _fetch_rotowire_news,
    get_client as _get_client,
    infer_body_part,
    infer_severity,
    now_iso as _now_iso,
    persist_news_rows,
)

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
]
