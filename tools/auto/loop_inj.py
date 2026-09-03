"""AutonomousLoop pace-confirmation and injury-cache helpers extracted from loop.

``AutonomousLoop._get_pace_model_confirmation``, ``_refresh_injury_cache``,
and ``_get_injuries_for_game`` stay defined on the class as thin delegates
so hasattr pins keep passing. The bodies live here so tools/auto/loop.py
can keep shrinking without changing behaviour.

Do not import the autonomous facade (no cycles).
Do not arm live betting. Do not add live to paper-signal.
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger("callisto.autonomous")


def get_pace_model_confirmation(loop, sport: str, game_name: str, report: dict) -> dict:
    """Check if pace model independently confirms a total edge direction.

    Returns dict with pace_model_confirms (bool), pace_model_direction,
    pace_model_edge_pct, and pace_model_total.
    """
    self = loop
    result = {
        "pace_model_confirms": False,
        "pace_model_direction": None,
        "pace_model_edge_pct": 0.0,
        "pace_model_total": None,
    }
    pace_edges = report.get("pace_model_totals", [])
    for pe in pace_edges:
        if pe.get("game") == game_name:
            result["pace_model_direction"] = pe.get("direction")
            result["pace_model_edge_pct"] = pe.get("edge_pct", 0.0)
            result["pace_model_total"] = pe.get("model_total")
            # Confirms if both cross-book and pace model agree on direction
            # (caller compares this with the cross-book edge direction)
            result["pace_model_confirms"] = True
            break
    return result


async def refresh_injury_cache(loop, sport: str) -> dict:
    """Fetch and cache injury data for a sport. Returns cached injuries."""
    self = loop
    now = time.time()
    if now - self._injury_ts.get(sport, 0) < 300:
        return self._injury_cache.get(sport, {})
    try:
        from tools.contextual_data import get_injuries as _fetch_inj
        data = await _fetch_inj(sport)
        if data and not data.get("error"):
            self._injury_cache[sport] = data
            self._injury_ts[sport] = now
            cnt = data.get("injury_count", 0)
            if cnt:
                logger.info(f"Injury cache refreshed for {sport}: {cnt} injuries")
        return self._injury_cache.get(sport, {})
    except Exception as e:
        logger.warning(f"Injury cache refresh failed for {sport}: {e}")
        return self._injury_cache.get(sport, {})


def get_injuries_for_game(loop, sport: str, game_name: str) -> list[dict]:
    """Extract injuries relevant to a specific game from cache."""
    self = loop
    injuries = self._injury_cache.get(sport, {}).get("injuries", [])
    if not injuries or not game_name:
        return []
    game_lower = game_name.lower()
    relevant = []
    for inj in injuries:
        team = inj.get("team", "")
        team_abbr = inj.get("team_abbr", "")
        status = (inj.get("status") or "").lower()
        if status not in ("out", "doubtful", "questionable"):
            continue
        if (team.lower() in game_lower
                or team_abbr.lower() in game_lower
                or any(w in game_lower for w in team.lower().split() if len(w) > 3)):
            relevant.append(inj)
    return relevant
