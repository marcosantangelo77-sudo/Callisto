"""tools.news — split-out implementation package for tools.news_ingestion.

The public surface is re-exported by ``tools.news_ingestion`` (the facade).
Consumers may import from either location; the facade remains canonical.
"""
from __future__ import annotations

from tools.news._http import close_client, get_client
from tools.news.api import (
    DB_PATH,
    ensure_schema,
    fetch_coaching_news,
    fetch_injuries,
    fetch_lineup_changes,
    persist_news_rows,
)
from tools.news.dedup import dedup_key, dedupe_injuries
from tools.news.espn import (
    ESPN_BASE,
    fetch_espn_coaching,
    fetch_espn_injuries,
    fetch_espn_scoreboard_lineups,
    now_iso,
)
from tools.news.inference import infer_body_part, infer_severity
from tools.news.models import CoachingEvent, InjuryEvent, LineupEvent
from tools.news.rotowire import RotoWireLimiter, fetch_rotowire_news

__all__ = [
    "InjuryEvent",
    "LineupEvent",
    "CoachingEvent",
    "fetch_injuries",
    "fetch_lineup_changes",
    "fetch_coaching_news",
    "dedupe_injuries",
    "dedup_key",
    "infer_severity",
    "infer_body_part",
    "persist_news_rows",
    "ensure_schema",
    "close_client",
    "get_client",
    "DB_PATH",
    "ESPN_BASE",
    "now_iso",
    "RotoWireLimiter",
]
