"""
ESPN play-by-play + win-probability enrichment of game_contexts.
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

async def collect_play_by_play(
    dc,
    sport: str,
    date: Optional[str] = None,
) -> dict:
    """
    Collect play-by-play and win probability data from ESPN summary endpoint.

    Dense data: ~400-500 plays per NBA game with coordinates, scoring runs,
    momentum shifts, pace metrics, and real-time win probabilities.

    Stores in game_contexts.context_json under 'play_by_play' and 'win_probability' keys.
    """
    espn_sport = ESPN_SPORTS.get(sport)
    if not espn_sport:
        return {"error": f"Unsupported sport: {sport}", "games": 0}

    category, league = espn_sport
    if date is None:
        date = datetime.now(timezone.utc).strftime("%Y%m%d")

    client = await _get_client()
    url = f"{ESPN_BASE}/{category}/{league}/scoreboard"
    params = {"dates": date}

    try:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        scoreboard = resp.json()
    except Exception as e:
        logger.error(f"ESPN scoreboard error for PBP: {e}")
        return {"error": str(e), "games": 0}

    game_date_fmt = f"{date[:4]}-{date[4:6]}-{date[6:8]}"
    games_enriched = 0
    events = scoreboard.get("events", [])

    for event in events:
        if event.get("status", {}).get("type", {}).get("completed") is not True:
            continue

        event_id = event.get("id", "")
        summary_url = (
            f"https://site.api.espn.com/apis/site/v2/sports/"
            f"{category}/{league}/summary"
        )

        try:
            summary_resp = await client.get(summary_url, params={"event": event_id})
            summary_resp.raise_for_status()
            summary = summary_resp.json()
        except Exception as e:
            logger.warning(f"PBP fetch failed for {event_id}: {e}")
            continue

        plays = summary.get("plays", [])
        win_probs = summary.get("winprobability", [])

        if not plays:
            continue

        # Extract key PBP metrics
        scoring_plays = [p for p in plays if p.get("scoringPlay")]
        total_plays = len(plays)

        # Compute pace metrics per period
        periods = {}
        for play in plays:
            period_num = play.get("period", {}).get("number", 0)
            if period_num not in periods:
                periods[period_num] = {"plays": 0, "scoring_plays": 0}
            periods[period_num]["plays"] += 1
            if play.get("scoringPlay"):
                periods[period_num]["scoring_plays"] += 1

        # Win probability momentum: biggest swings
        momentum_swings = []
        if len(win_probs) >= 2:
            for i in range(1, len(win_probs)):
                prev_wp = win_probs[i - 1].get("homeWinPercentage", 0.5)
                curr_wp = win_probs[i].get("homeWinPercentage", 0.5)
                swing = abs(curr_wp - prev_wp)
                if swing >= 0.05:  # 5%+ swing = significant momentum shift
                    momentum_swings.append({
                        "play_id": win_probs[i].get("playId"),
                        "swing": round(swing, 3),
                        "direction": "home" if curr_wp > prev_wp else "away",
                        "wp_after": round(curr_wp, 3),
                    })

        # 2026-04-18: Prior version stored ONLY a 7-stat summary
        # (total_plays, scoring_plays, periods, momentum_swings, and
        # final/max/min win-prob). The raw `plays` and `winprobability`
        # arrays returned by ESPN were parsed and then thrown away,
        # meaning Callisto was not actually storing play-by-play — it
        # was storing a histogram. Downstream pace / scoring-run /
        # player-impact / in-game modelling had no timeline to read.
        #
        # This version stores a compact form of every play plus the
        # full win-prob series, while keeping the prior summary fields
        # for backward compatibility with any consumer reading them.
        def _parse_clock(play_obj):
            c = play_obj.get("clock", {}) or {}
            val = c.get("displayValue") or c.get("value")
            if isinstance(val, (int, float)):
                return int(val)
            if isinstance(val, str) and ":" in val:
                try:
                    mm, ss = val.split(":")
                    return int(mm) * 60 + int(ss)
                except ValueError:
                    return 0
            return 0

        wp_by_play_id = {
            wp.get("playId"): wp.get("homeWinPercentage")
            for wp in win_probs
            if wp.get("playId")
        }

        compact_plays = []
        for p in plays:
            st = p.get("scoringPlay") is True
            coord = p.get("coordinate") or {}
            pid = p.get("id")
            compact_plays.append({
                "p": p.get("period", {}).get("number", 0),
                "c": _parse_clock(p),
                "t": (p.get("type", {}) or {}).get("text") or p.get("shortText", ""),
                "hs": p.get("homeScore"),
                "as": p.get("awayScore"),
                "sc": p.get("homeAway") if st else None,
                "wp": (
                    round(float(wp_by_play_id[pid]), 4)
                    if pid in wp_by_play_id and wp_by_play_id[pid] is not None
                    else None
                ),
                "x": coord.get("x") if coord else None,
                "y": coord.get("y") if coord else None,
                "tx": (p.get("text") or "")[:200],  # cap description length
            })

        pbp_payload = {
            "plays": compact_plays,
            "wp_series": [
                round(float(wp.get("homeWinPercentage")), 4)
                for wp in win_probs
                if wp.get("homeWinPercentage") is not None
            ],
            "total_plays": total_plays,
            "scoring_plays": len(scoring_plays),
            "periods": periods,
            "momentum_swings": sorted(
                momentum_swings, key=lambda x: x["swing"], reverse=True
            )[:10],
            "final_home_wp": round(
                win_probs[-1].get("homeWinPercentage", 0.5), 3
            ) if win_probs else None,
            "max_home_wp": round(
                max(wp.get("homeWinPercentage", 0.5) for wp in win_probs), 3
            ) if win_probs else None,
            "min_home_wp": round(
                min(wp.get("homeWinPercentage", 0.5) for wp in win_probs), 3
            ) if win_probs else None,
        }

        # Update existing game_context with PBP data. Also mirror the final
        # home win-probability into a top-level `win_probability` key so
        # queries that filter on it (e.g. "rows with win_probability set")
        # find these games — the prior version only nested it inside
        # play_by_play, which left the top-level filter returning 0 matches.
        cursor = await dc._db.execute(
            "SELECT id, context_json FROM game_contexts "
            "WHERE sport = ? AND event_id = ?",
            (sport, event_id),
        )
        row = await cursor.fetchone()
        if row:
            ctx_id, ctx_json = row
            ctx = json.loads(ctx_json) if ctx_json else {}
            ctx["play_by_play"] = pbp_payload
            if pbp_payload["final_home_wp"] is not None:
                ctx["win_probability"] = pbp_payload["final_home_wp"]
            await dc._db.execute(
                "UPDATE game_contexts SET context_json = ? WHERE id = ?",
                (json.dumps(ctx), ctx_id),
            )
            games_enriched += 1
            logger.debug(
                f"PBP enrichment: {event_id} — {total_plays} plays stored, "
                f"{len(compact_plays)} timeline entries, "
                f"{len(momentum_swings)} momentum swings"
            )

    from tools.db_utils import commit_with_retry
    await commit_with_retry(dc._db, operation="data_collector collect_play_by_play")
    logger.info(
        f"Play-by-play: enriched {games_enriched} {sport} games on {game_date_fmt}"
    )
    return {
        "sport": sport,
        "date": game_date_fmt,
        "games_enriched": games_enriched,
    }