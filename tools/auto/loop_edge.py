"""AutonomousLoop injury/line-analysis helpers extracted from tools.auto.loop.

``AutonomousLoop._run_injury_analysis_for_edge`` and
``AutonomousLoop._compute_line_analysis_signals`` stay defined on the class as
thin delegates so slice2 ``hasattr`` pins keep passing. The bodies live here
so tools/auto/loop.py can keep shrinking without changing behaviour.

Psychology helpers, ``_find_analysis_candidates``, and ``get_status`` stay on
AutonomousLoop. Do not import the autonomous facade (no cycles).
Do not arm live betting. Do not add live to paper-signal.
"""
from __future__ import annotations

import logging
import time

from tools.dead_numbers import (
    SPORT_ALIASES as _DEAD_NUM_SPORT_ALIASES,
    is_dead_number as _is_dead_number,
    key_number_value as _key_number_value,
)
from tools.injury_model import full_injury_analysis
from tools.line_analysis import (
    contrarian_value,
    detect_rlm,
    detect_steam,
    estimate_public_side,
)

from tools.auto.loop import _SPORT_TO_MODEL

logger = logging.getLogger("callisto.autonomous")


def run_injury_analysis_for_edge(loop, sport: str, game_name: str,
                                  team_name: str) -> dict:
    self = loop
    """Run injury model on injuries relevant to an edge candidate.

    Returns dict with keys: has_injury_edge, injury_analyses,
    market_adjustment_summary, confidence_modifier (-0.10..+0.10),
    is_contrarian, prop_opportunities.
    """
    cache_key = f"{sport}:{game_name}"
    cached = self._injury_analysis_cache.get(cache_key)
    if cached is not None:
        return cached

    model_sport = _SPORT_TO_MODEL.get(sport, "")
    empty = {"has_injury_edge": False, "injury_analyses": [],
             "market_adjustment_summary": "", "confidence_modifier": 0.0,
             "is_contrarian": False, "prop_opportunities": []}
    if not model_sport:
        self._injury_analysis_cache[cache_key] = empty
        return empty

    game_injuries = self._get_injuries_for_game(sport, game_name)
    if not game_injuries:
        empty["market_adjustment_summary"] = "No significant injuries"
        self._injury_analysis_cache[cache_key] = empty
        return empty

    # Parse opponent from game name
    opponent = ""
    for sep in [" at ", " vs ", " @ ", " vs. "]:
        if sep in game_name:
            parts = game_name.split(sep)
            if len(parts) == 2:
                opponent = (parts[1].strip() if team_name.lower() in parts[0].lower()
                            else parts[0].strip())
            break

    analyses = []
    conf_mod = 0.0
    is_contrarian = False
    prop_opps = []

    for inj in game_injuries:
        player = inj.get("player", "Unknown")
        team = inj.get("team", team_name)
        position = inj.get("position", "")
        status = (inj.get("status") or "").lower()
        minutes_since = 30.0 if status == "out" else 15.0
        try:
            analysis = full_injury_analysis(
                player_name=player, team=team, sport=model_sport,
                opponent=opponent or "Unknown", position=position,
                minutes_since_announced=minutes_since,
            )
            analyses.append(analysis)
            mkt = analysis.get("market_timing")
            if mkt and hasattr(mkt, "pct_adjusted"):
                if mkt.pct_adjusted < 0.70:
                    conf_mod += min(0.06, mkt.edge_remaining * 0.05)
                elif mkt.pct_adjusted > 0.98 and mkt.edge_remaining < 0.01:
                    matchup = analysis.get("matchup_adjusted")
                    if matchup and hasattr(matchup, "adjusted_spread_impact"):
                        if matchup.adjusted_spread_impact < 2.0 and status == "out":
                            is_contrarian = True
                            conf_mod += 0.04
                for r in analysis.get("redistribution", [])[:3]:
                    if hasattr(r, "usage_increase") and r.usage_increase > 2.0:
                        prop_opps.append({
                            "player": getattr(r, "player", "Unknown"),
                            "role": getattr(r, "role", ""),
                            "usage_increase": r.usage_increase,
                            "stat_change": getattr(r, "projected_stat_change", {}),
                            "absent_player": player, "absent_team": team,
                        })
        except Exception as e:
            logger.warning(f"Injury analysis failed for {player}: {e}")

    conf_mod = max(-0.10, min(0.10, conf_mod))
    market_summary = ""
    out_names = [a["player"] for a in analyses if a.get("impact")]
    if out_names:
        market_summary = f"Key absences: {', '.join(out_names[:5])}"
        adj_pcts = [a["market_timing"].pct_adjusted for a in analyses
                    if a.get("market_timing") and hasattr(a["market_timing"], "pct_adjusted")]
        if adj_pcts:
            market_summary += f" | Market ~{sum(adj_pcts)/len(adj_pcts):.0%} adjusted"

    result = {
        "has_injury_edge": conf_mod > 0.02 or is_contrarian,
        "injury_analyses": analyses,
        "market_adjustment_summary": market_summary,
        "confidence_modifier": round(conf_mod, 3),
        "is_contrarian": is_contrarian,
        "prop_opportunities": prop_opps,
    }
    self._injury_analysis_cache[cache_key] = result
    return result


def compute_line_analysis_signals(
    loop, sport: str, edge: dict, market: str, game: str, team: str,
) -> dict:
    self = loop
    """Compute line analysis signals for an edge candidate.

    Returns kwargs dict suitable for passing directly to score_edge().
    Signals: dead number, key number, public side, contrarian, RLM, steam.
    """
    result: dict = {}
    public_est = None

    # --- Dead number / key number analysis ---
    if market in ("spreads", "totals"):
        _dn_sport = sport.lower()
        if _dn_sport in _DEAD_NUM_SPORT_ALIASES:
            best_point = edge.get("best_line", {}).get("point")
            if best_point is not None:
                try:
                    result["is_dead_number"] = _is_dead_number(best_point, _dn_sport)
                    result["key_number_value"] = _key_number_value(best_point, _dn_sport)
                except (ValueError, KeyError):
                    pass

    # --- Public side estimation and contrarian value ---
    try:
        best_line = edge.get("best_line", {})
        worst_line = edge.get("worst_line", {})
        best_point = best_line.get("point", 0) or 0
        worst_point = worst_line.get("point", 0) or 0
        line_open = worst_point if worst_point else best_point
        line_current = best_point if best_point else worst_point

        if line_open != 0 or line_current != 0:
            public_est = estimate_public_side(
                line_open=line_open,
                line_current=line_current,
                sport=sport,
                team_a=team,
            )
            public_fav = public_est.get("public_favorite", "split")
            fade_side = public_est.get("fade_side", "neither")
            est_public_pct = max(
                public_est.get("estimated_public_pct_a", 50),
                public_est.get("estimated_public_pct_b", 50),
            )
            if fade_side != "neither":
                is_public_side = (
                    (public_fav == "A" and fade_side == "B") or
                    (public_fav == "B" and fade_side == "A")
                )
                result["public_side_edge"] = is_public_side
                cv = contrarian_value(
                    estimated_public_pct=est_public_pct,
                    sport=sport,
                    spread=best_point or 0,
                )
                result["contrarian_value_score"] = cv.get("adjusted_roi", 0)
                result["contrarian_edge_pct"] = cv.get("contrarian_edge", 0)
    except Exception as e:
        logger.debug(f"Public side estimation failed for {game}: {e}")

    # --- RLM detection ---
    try:
        best_line = edge.get("best_line", {})
        worst_line = edge.get("worst_line", {})
        best_price = best_line.get("price", 0)
        worst_price = worst_line.get("price", 0)
        movement_dir = best_price - worst_price
        if public_est and abs(movement_dir) > 5:
            est_pub = max(
                public_est.get("estimated_public_pct_a", 50),
                public_est.get("estimated_public_pct_b", 50),
            )
            est_money = est_pub * 0.8
            rlm_result = detect_rlm(
                line_movement_direction=movement_dir / 100.0,
                public_ticket_pct=est_pub,
                public_money_pct=est_money,
            )
            if rlm_result.get("is_rlm"):
                result["rlm_detected"] = True
                result["rlm_confidence"] = rlm_result.get("confidence", 0)
                result["rlm_edge_on_sharp_side"] = not result.get("public_side_edge", False)
    except Exception as e:
        logger.debug(f"RLM detection failed for {game}: {e}")

    # --- Steam detection ---
    try:
        snapshot = self.line_monitor._snapshots.get(sport)
        if snapshot and snapshot.get("games"):
            game_id = edge.get("game_id", "")
            if game_id:
                line_snaps = []
                for g in snapshot.get("games", []):
                    if g.get("id") != game_id:
                        continue
                    snap_ts = snapshot.get("timestamp", time.time())
                    for bm in g.get("bookmakers", []):
                        for mkt in bm.get("markets", []):
                            if mkt["key"] != market:
                                continue
                            for outcome in mkt.get("outcomes", []):
                                if outcome.get("name", "").lower() == team.lower() or market == "totals":
                                    line_snaps.append({
                                        "timestamp": snap_ts,
                                        "line": outcome.get("price", 0),
                                        "book": bm.get("title", bm.get("key", "unknown")),
                                    })
                if len(line_snaps) >= 4:
                    steam_results = detect_steam(line_snaps)
                    if steam_results:
                        top_steam = steam_results[0]
                        result["steam_detected"] = True
                        result["steam_confidence"] = top_steam.get("confidence", 0)
                        result["steam_edge_on_steam_side"] = True
    except Exception as e:
        logger.debug(f"Steam detection failed for {game}: {e}")

    return result
