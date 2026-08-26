"""
Golf collector: DataGolf public archive — per-round strokes gained.
"""

from __future__ import annotations

import difflib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from tools.collect.http import _get_client


logger = logging.getLogger("callisto.data_collector")

async def collect_golf_player_rounds(dc, season: Optional[int] = None) -> dict:
    """Ingest per-round SG data for the PGA Tour via DataGolf public JSON.

    Graceful on 403/404 — if the public feed is unreachable, log and
    return 0 rows rather than raising; callers can retry later.
    """
    import httpx as _httpx
    if season is None:
        season = datetime.now(timezone.utc).year

    rows: list[tuple] = []
    async with _httpx.AsyncClient(
        timeout=60.0, follow_redirects=True, max_redirects=5,
        headers={"User-Agent": "Mozilla/5.0 (Callisto)"},
    ) as c:
        try:
            r = await c.get(
                "https://feeds.datagolf.com/preds/archive",
                params={"tour": "pga", "year": season, "file_format": "json"},
            )
            r.raise_for_status()
            archive = r.json()
        except Exception as e:
            logger.info(f"DataGolf archive {season} unreachable ({e})")
            archive = []

        for ev in archive or []:
            event_id = str(ev.get("event_id") or ev.get("eventId") or "")
            if not event_id:
                continue
            event_name = ev.get("event_name") or ev.get("eventName")
            course = ev.get("course") or ev.get("courseName")
            for round_entry in ev.get("rounds", []) or []:
                round_num = round_entry.get("round_num") or round_entry.get("round")
                round_date = round_entry.get("round_date") or round_entry.get("date")
                for p in round_entry.get("players", []) or []:
                    pid = str(p.get("dg_id") or p.get("player_id") or "")
                    if not pid:
                        continue
                    rows.append((
                        pid,
                        p.get("player_name") or p.get("name") or "",
                        event_id,
                        event_name,
                        course,
                        season,
                        round_num,
                        round_date,
                        p.get("tee_time"),
                        p.get("score") or p.get("round_score"),
                        p.get("score_to_par") or p.get("round_to_par"),
                        p.get("thru"),
                        p.get("sg_total"),
                        p.get("sg_ott"),
                        p.get("sg_app"),
                        p.get("sg_arg"),
                        p.get("sg_putt") or p.get("sg_putting"),
                        p.get("sg_t2g"),
                        p.get("driving_distance") or p.get("dd"),
                        p.get("driving_accuracy") or p.get("da"),
                        p.get("gir_pct") or p.get("gir"),
                        p.get("scrambling_pct") or p.get("scrambling"),
                        p.get("putts_per_round") or p.get("putts"),
                        1 if p.get("made_cut") else 0,
                    ))

    stored = 0
    if rows:
        try:
            await dc._db.executemany(
                "INSERT OR REPLACE INTO golf_player_rounds ("
                "player_id, player_name, event_id, event_name, course, "
                "season, round_num, round_date, tee_time, score, "
                "score_to_par, thru, sg_total, sg_ott, sg_app, sg_arg, "
                "sg_putt, sg_t2g, driving_distance, driving_accuracy, "
                "gir_pct, scrambling_pct, putts_per_round, made_cut"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            stored = len(rows)
        except Exception as e:
            logger.warning(f"golf_player_rounds upsert failed: {e}")

    from tools.db_utils import commit_with_retry
    await commit_with_retry(dc._db, operation="data_collector collect_golf_player_rounds")
    logger.info(f"Golf rounds {season}: {stored} rows upserted")
    return {"season": season, "rows_upserted": stored}