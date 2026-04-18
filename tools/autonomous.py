"""
Autonomous reasoning loop — makes Callisto think without being asked.

Two loops run concurrently:
  1. AutonomousLoop — real-time edge detection (existing, unchanged)
  2. ResearchLoop — 24/7 hypothesis machine (NEW)

ResearchLoop cycle:
  - Collect post-game data (ESPN scores, box scores) — FREE
  - Embed game contexts and prop outcomes into vector store
  - Generate hypotheses (Claude Code PRIMARY, templates FALLBACK)
  - Backtest hypotheses against historical data
  - Evaluate significance, auto-promote or auto-reject
  - Claude interprets backtest results (signal vs noise, threshold mods)
  - Paper trade promoted hypotheses on live odds
  - Claude deep analysis — actionable hypothesis/rejection work
  - System self-improvement (every 10 cycles) — pipeline optimization

Claude Code is the PRIMARY reasoning engine. Local models stay only
for fast classification (Sentinel) and embeddings.
"""

import asyncio
import gc
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from tools import telegram
from tools.backtest import _signal_confidence
from tools.edge_confidence import score_edge
from tools.market_psychology import (
    detect_number_shading,
    detect_trap_line,
    attention_arbitrage,
    predict_closing_line,
    full_market_psychology,
)
from tools.line_analysis import (
    detect_rlm,
    detect_steam,
    estimate_public_side,
    contrarian_value,
    optimal_bet_timing,
)
from tools.dead_numbers import (
    is_dead_number as _is_dead_number,
    key_number_value as _key_number_value,
    find_dead_number_steals,
    rank_line_shopping_opportunities,
    buy_points_analysis,
    SPORT_ALIASES as _DEAD_NUM_SPORT_ALIASES,
)
from tools.injury_model import (
    full_injury_analysis,
    redistribute_usage,
    estimate_market_adjustment,
    player_impact,
)

logger = logging.getLogger("callisto.autonomous")

# Map odds-API sport keys to injury_model sport codes
_SPORT_TO_MODEL = {
    "basketball_nba": "NBA",
    "americanfootball_nfl": "NFL",
    "baseball_mlb": "MLB",
    "basketball_ncaab": "NBA",  # model tables work for college too
    "americanfootball_ncaaf": "NFL",
    "icehockey_nhl": "NHL",
}

# Only analyze edges above these thresholds — don't waste GPU on noise
# Lowered from 4%/3% — with 3-5 scraped books, legitimate edges start at 2%
MIN_IMPLIED_RANGE = 0.02       # 2% cross-book disagreement minimum
MIN_SOFT_EDGE_VS_SHARP = 0.02  # 2% vs sharp consensus minimum
MIN_CONFIDENCE_TO_ALERT = 0.40 # Alert at moderate confidence

# Max concurrent AGP sessions to avoid GPU overload
MAX_CONCURRENT_SESSIONS = 1

# Cooldown between full analysis cycles (seconds)
ANALYSIS_COOLDOWN = 120  # 2 min between analysis runs

# Don't re-analyze the same edge within this window
EDGE_DEDUP_WINDOW = 1800  # 30 minutes

# Module-level regime cache — shared between AutonomousLoop and ResearchLoop.
# ResearchLoop populates it; AutonomousLoop reads it for edge enrichment.
# LRU-capped to prevent unbounded memory growth (~385 MB/hr leak source).
class _LRUCache:
    """Simple LRU dict with max size. Evicts oldest on overflow."""
    def __init__(self, maxsize: int = 5000):
        from collections import OrderedDict
        self._cache: OrderedDict = OrderedDict()
        self.maxsize = maxsize
    def get(self, key, default=None):
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        return default
    def __setitem__(self, key, value):
        if key in self._cache:
            self._cache.move_to_end(key)
        elif len(self._cache) >= self.maxsize:
            self._cache.popitem(last=False)
        self._cache[key] = value
    def __contains__(self, key):
        return key in self._cache
    def __bool__(self):
        return bool(self._cache)
    def items(self):
        return self._cache.items()
    def values(self):
        return self._cache.values()
    def __len__(self):
        return len(self._cache)

_regime_cache: _LRUCache = _LRUCache(maxsize=500)


def get_regime_for_team(sport: str, team_name: str) -> Optional[dict]:
    """Module-level lookup for cached regime analysis.

    Tries exact match first, then partial match for team name flexibility.
    """
    cache_key = f"{sport}:{team_name}"
    result = _regime_cache.get(cache_key)
    if result:
        return result
    # Partial match — team names vary across data sources
    team_lower = team_name.lower()
    for key, val in _regime_cache.items():
        if key.startswith(sport + ":") and team_lower in key.lower():
            return val
    return None


class AutonomousLoop:
    """Proactive reasoning engine — turns raw edges into analyzed recommendations."""

    def __init__(self, orchestrator, line_monitor):
        """
        Args:
            orchestrator: The Orchestrator instance (has run_session())
            line_monitor: The LineMonitor instance (has edge reports)
        """
        self.orchestrator = orchestrator
        self.line_monitor = line_monitor
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._analyzed_edges: dict[str, float] = {}  # edge_key -> timestamp
        self._session_count = 0
        self._alert_count = 0
        self._loop_cycle = 0  # cycle counter for periodic parlay scans
        self._parlay_scan_cache: dict[str, dict] = {}  # sport -> latest parlay scan results (keyed by sport, max ~10 entries)
        self._parlay_scan_ts: dict[str, float] = {}    # sport -> last scan timestamp
        self._psychology_cache: dict[str, dict] = {}  # sport -> latest psychology signals (keyed by sport, max ~10 entries)
        self._psychology_ts: dict[str, float] = {}    # sport -> last run timestamp
        self._injury_cache: dict[str, dict] = {}      # sport -> injury report from ESPN
        self._injury_ts: dict[str, float] = {}         # sport -> last fetch timestamp
        self._injury_analysis_cache: dict[str, dict] = {}  # "sport:game" -> injury analysis results (capped at 50)

    async def start(self) -> None:
        """Start the autonomous reasoning loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("Autonomous reasoning loop started")

    async def stop(self) -> None:
        """Stop the loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info(
            f"Autonomous loop stopped — {self._session_count} sessions, "
            f"{self._alert_count} alerts sent"
        )

    async def _loop(self) -> None:
        """Main loop — find edges, reason about them, alert if worthy."""
        # Wait for first snapshot cycle to populate data
        await asyncio.sleep(30)

        while self._running:
            try:
                self._loop_cycle += 1

                # Run market psychology analysis on latest snapshots
                self._run_market_psychology()

                # Refresh injury caches for active sports
                all_reports = self.line_monitor.get_edge_report()
                if isinstance(all_reports, dict):
                    for _sport_key in all_reports:
                        await self._refresh_injury_cache(_sport_key)

                # Run parlay/SGP correlation scan every 4 cycles
                if self._loop_cycle % 4 == 0:
                    await self._phase_parlay_correlation_scan()

                candidates = self._find_analysis_candidates()

                if candidates:
                    logger.info(
                        f"Autonomous: {len(candidates)} edge candidates found, "
                        f"analyzing top {min(len(candidates), 3)}"
                    )

                    # Analyze top candidates sequentially (GPU bound)
                    for candidate in candidates[:3]:
                        if not self._running:
                            break
                        await self._analyze_edge(candidate)

                # Clean up old dedup entries
                self._cleanup_dedup()

                await asyncio.sleep(ANALYSIS_COOLDOWN)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Autonomous loop error: {e}", exc_info=True)
                await asyncio.sleep(30)

    def _run_market_psychology(self) -> None:
        """Run market psychology analysis on latest snapshots.

        Produces per-sport psychology signals (number shading, attention
        arbitrage) that are cached and merged into edge candidates during
        scoring.  Runs at most once per ANALYSIS_COOLDOWN to avoid waste.
        """
        now = time.time()
        all_reports = self.line_monitor.get_edge_report()
        if not isinstance(all_reports, dict):
            return

        for sport, report in all_reports.items():
            if not isinstance(report, dict):
                continue
            # Throttle: skip if we ran psychology for this sport recently
            last_ts = self._psychology_ts.get(sport, 0)
            if now - last_ts < ANALYSIS_COOLDOWN:
                continue

            # Get the latest snapshot games for this sport
            snapshot = self.line_monitor._snapshots.get(sport)
            if not snapshot or not snapshot.get("games"):
                continue

            try:
                psych = full_market_psychology(
                    games=snapshot["games"],
                    sport=sport,
                )
                self._psychology_cache[sport] = psych
                self._psychology_ts[sport] = now

                shading_count = len(psych.get("number_shading", []))
                if shading_count > 0:
                    logger.info(
                        f"Psychology {sport}: {shading_count} shaded lines detected"
                    )
            except Exception as e:
                logger.warning(f"Market psychology failed for {sport}: {e}")

    def _get_psychology_for_edge(self, sport: str, game: str, team: str, market: str) -> dict:
        """Extract psychology signals relevant to a specific edge.

        Returns a dict with keys:
            number_shading_detected: bool
            shading_value_side: str or None
            shading_magnitude: int
            attention_opportunity: float (0-1, higher = thinner market)
        """
        result = {
            "number_shading_detected": False,
            "shading_value_side": None,
            "shading_magnitude": 0,
            "attention_opportunity": 0.0,
        }
        psych = self._psychology_cache.get(sport)
        if not psych:
            return result

        # Match number shading signals for this game/team/market
        for shade in psych.get("number_shading", []):
            shade_game = shade.get("game", "")
            shade_team = shade.get("team", "")
            shade_market = shade.get("market", "")
            if (shade_game == game and
                    shade_team == team and
                    shade_market == market):
                result["number_shading_detected"] = True
                result["shading_value_side"] = shade.get("value_side")
                result["shading_magnitude"] = shade.get("shade_magnitude_cents", 0)
                break

        # Attention arbitrage — sport-level signal
        attn = psych.get("attention_arbitrage", {})
        for thin in attn.get("thin_markets", []):
            if thin.get("sport") == sport:
                result["attention_opportunity"] = thin.get("opportunity_score", 0.0)
                break

        return result

    def _get_pace_model_confirmation(self, sport: str, game_name: str, report: dict) -> dict:
        """Check if pace model independently confirms a total edge direction.

        Returns dict with pace_model_confirms (bool), pace_model_direction,
        pace_model_edge_pct, and pace_model_total.
        """
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

    # ---- Injury model integration ----

    async def _refresh_injury_cache(self, sport: str) -> dict:
        """Fetch and cache injury data for a sport. Returns cached injuries."""
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

    def _get_injuries_for_game(self, sport: str, game_name: str) -> list[dict]:
        """Extract injuries relevant to a specific game from cache."""
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

    def _run_injury_analysis_for_edge(self, sport: str, game_name: str,
                                       team_name: str) -> dict:
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


    # ---- Line analysis signal computation ----

    def _compute_line_analysis_signals(
        self, sport: str, edge: dict, market: str, game: str, team: str,
    ) -> dict:
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

    def _find_analysis_candidates(self) -> list[dict]:
        """
        Scan latest edge reports for candidates worth full AGP analysis.

        Filters:
        - Implied range >= 4% (real disagreement, not noise)
        - Has soft book edges vs sharp consensus >= 3%
        - Not analyzed in the last 30 minutes
        - For totals: pace model confirmation is attached as supplementary signal
        """
        candidates = []
        now = time.time()

        all_reports = self.line_monitor.get_edge_report()
        if not isinstance(all_reports, dict):
            return []

        for sport, report in all_reports.items():
            if not isinstance(report, dict):
                continue

            # Cross-book divergence edges
            for market_key in ["cross_book_spreads", "cross_book_h2h", "cross_book_totals"]:
                for edge in report.get(market_key, []):
                    implied_range = edge.get("implied_range", 0)
                    if implied_range < MIN_IMPLIED_RANGE:
                        continue

                    # Check for soft book vs sharp edges
                    soft_edges = edge.get("soft_book_edges", [])
                    best_soft = max(
                        (se.get("edge_vs_sharp", 0) for se in soft_edges),
                        default=0,
                    )
                    if best_soft < MIN_SOFT_EDGE_VS_SHARP:
                        continue

                    # Dedup check
                    edge_key = f"{sport}:{edge.get('game', '')}:{edge.get('team', '')}:{market_key}"
                    last_analyzed = self._analyzed_edges.get(edge_key, 0)
                    if now - last_analyzed < EDGE_DEDUP_WINDOW:
                        continue

                    # Gather psychology signals for this edge
                    game_name = edge.get("game", "")
                    team_name = edge.get("team", "")
                    mkt_name = market_key.replace("cross_book_", "")
                    psych_signals = self._get_psychology_for_edge(
                        sport, game_name, team_name, mkt_name,
                    )

                    # Look up KL divergence metrics for this game
                    game_id = edge.get("game_id", "")
                    kl_data = self.line_monitor.get_kl_for_game(sport, game_id, mkt_name) if game_id else None
                    kl_kw = {}
                    if kl_data:
                        kl_kw["kl_divergence"] = kl_data.get("kl_divergence")
                        kl_kw["js_divergence"] = kl_data.get("js_divergence")

                    # Look up regime analysis for the team
                    team_regime = get_regime_for_team(sport, team_name)

                    # --- Line analysis signals (RLM, steam, dead number, contrarian) ---
                    line_analysis_kw = self._compute_line_analysis_signals(
                        sport, edge, mkt_name, game_name, team_name,
                    )

                    # --- Injury model analysis ---
                    injury_data = self._run_injury_analysis_for_edge(
                        sport, game_name, team_name,
                    )
                    injury_kw = {}
                    if injury_data.get("has_injury_edge"):
                        injury_kw["injury_market_adjustment"] = injury_data["confidence_modifier"]
                        injury_kw["injury_is_contrarian"] = injury_data["is_contrarian"]

                    # Compute hours_to_game from commence_time
                    hours_to_game = None
                    ct = edge.get("commence_time")
                    if ct:
                        try:
                            from datetime import datetime, timezone
                            if isinstance(ct, str):
                                ct_dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
                            else:
                                ct_dt = ct
                            if ct_dt.tzinfo is None:
                                ct_dt = ct_dt.replace(tzinfo=timezone.utc)
                            hours_to_game = max(0, (ct_dt - datetime.now(timezone.utc)).total_seconds() / 3600)
                        except (ValueError, TypeError):
                            pass

                    # Score confidence (psychology + line analysis + injury)
                    conf = score_edge(
                        edge_pct=round(best_soft * 100, 2),
                        books_compared=edge.get("num_bookmakers", edge.get("book_count", 1)),
                        book_names=[edge.get("best_line", {}).get("bookmaker", "")],
                        market=mkt_name,
                        has_sharp_book=edge.get("sharp_consensus") is not None,
                        regime_data=team_regime,
                        hours_to_game=hours_to_game,
                        **kl_kw,
                        **line_analysis_kw,
                        **injury_kw,
                    )

                    # Pace model confirmation for total edges
                    pace_confirm = {}
                    if mkt_name == "totals":
                        pace_confirm = self._get_pace_model_confirmation(
                            sport, game_name, report,
                        )

                    candidates.append({
                        "sport": sport,
                        "edge_key": edge_key,
                        "game": game_name,
                        "game_id": edge.get("game_id", ""),
                        "team": team_name,
                        "market": mkt_name,
                        "implied_range": implied_range,
                        "best_soft_edge": best_soft,
                        "soft_book_edges": soft_edges,
                        "best_line": edge.get("best_line", {}),
                        "worst_line": edge.get("worst_line", {}),
                        "sharp_consensus": edge.get("sharp_consensus"),
                        "num_bookmakers": edge.get("num_bookmakers", 0),
                        "hours_to_game": hours_to_game,
                        "confidence": conf,
                        "psychology": psych_signals,
                        "pace_model": pace_confirm,
                        "line_analysis": line_analysis_kw,
                        "injury_analysis": injury_data,
                    })

        # Sort by edge magnitude — biggest edges first
        candidates.sort(key=lambda c: c["best_soft_edge"], reverse=True)
        return candidates

    async def _analyze_edge(self, candidate: dict) -> None:
        """
        Run full AGP session on an edge candidate.

        The Architect gets the edge data as a structured query and can use
        tools (injuries, props, cross-book data) to build a complete picture.
        """
        sport = candidate["sport"]
        game = candidate["game"]
        team = candidate["team"]
        market = candidate["market"]
        edge_pct = round(candidate["best_soft_edge"] * 100, 1)
        conf = candidate["confidence"]

        logger.info(
            f"Autonomous: analyzing {team} {market} in {game} "
            f"(edge={edge_pct}%, confidence={conf.tier})"
        )

        # Mark as analyzed
        self._analyzed_edges[candidate["edge_key"]] = time.time()

        # Build a targeted query for the AGP session
        soft_detail = ""
        for se in candidate.get("soft_book_edges", [])[:3]:
            price = se.get("price", 0)
            price_str = f"+{price}" if price > 0 else str(price)
            soft_detail += (
                f"  - {se.get('bookmaker', '?')}: {price_str} "
                f"(edge {se.get('edge_vs_sharp', 0):.1%}, "
                f"EV ${se.get('ev', {}).get('expected_value', 0):.2f})\n"
            )

        best = candidate.get("best_line", {})
        worst = candidate.get("worst_line", {})
        best_price = best.get("price", 0)
        best_str = f"+{best_price}" if best_price > 0 else str(best_price)

        # Build market psychology context for the AGP session
        psych = candidate.get("psychology", {})
        psych_lines = []
        if psych.get("number_shading_detected"):
            psych_lines.append(
                f"NUMBER SHADING: Line is shaded (magnitude {psych['shading_magnitude']} cents). "
                f"Value side: {psych['shading_value_side']}."
            )
        if psych.get("attention_opportunity", 0) > 0.3:
            psych_lines.append(
                f"ATTENTION ARBITRAGE: Thin market opportunity score "
                f"{psych['attention_opportunity']:.2f} — edges may persist longer."
            )
        psych_section = (
            f"\nMarket Psychology Signals:\n" + "\n".join(f"  * {l}" for l in psych_lines) + "\n"
            if psych_lines else ""
        )

        # Build pace model confirmation context for totals
        pace_section = ""
        pace_data = candidate.get("pace_model", {})
        if pace_data.get("pace_model_confirms"):
            pace_section = (
                f"\nPace Model (Independent Confirmation):\n"
                f"  * Model total: {pace_data['pace_model_total']}\n"
                f"  * Model direction: {pace_data['pace_model_direction']}\n"
                f"  * Model edge: {pace_data['pace_model_edge_pct']:.1f}%\n"
            )

        # Build line analysis context (RLM, steam, dead numbers, contrarian, timing)
        la = candidate.get("line_analysis", {})
        la_lines = []
        if la.get("rlm_detected"):
            side = "SHARP (our edge)" if la.get("rlm_edge_on_sharp_side") else "PUBLIC (against us)"
            la_lines.append(
                f"RLM DETECTED (confidence {la.get('rlm_confidence', 0):.0%}): "
                f"Edge is on the {side} side."
            )
        if la.get("steam_detected"):
            la_lines.append(
                f"STEAM MOVE DETECTED (confidence {la.get('steam_confidence', 0):.0%}): "
                f"Coordinated sharp action across books."
            )
        if la.get("is_dead_number"):
            la_lines.append(
                f"DEAD NUMBER: Spread sits on a dead number "
                f"(key importance {la.get('key_number_value', 0):.2f}). "
                f"Book has less risk here."
            )
        elif la.get("key_number_value", 0) > 0.5:
            la_lines.append(
                f"KEY NUMBER PROXIMITY: Near high-value key number "
                f"(importance {la.get('key_number_value', 0):.2f})."
            )
        if la.get("contrarian_value_score", 0) > 1.0:
            la_lines.append(
                f"CONTRARIAN VALUE: Historical ROI {la.get('contrarian_value_score', 0):+.1f}% "
                f"fading public at this percentage."
            )
        if la.get("public_side_edge"):
            la_lines.append(
                "WARNING: Edge is on the PUBLIC side with no sharp confirmation."
            )

        # Add optimal bet timing recommendation
        try:
            timing = optimal_bet_timing(sport=sport, market=market)
            la_lines.append(
                f"BET TIMING: {timing.get('optimal_window', 'N/A')} "
                f"(estimated edge: {timing.get('historical_edge_pct', 0):.1f}%)"
            )
        except Exception:
            pass

        la_section = (
            f"\nLine Analysis Signals:\n" + "\n".join(f"  * {l}" for l in la_lines) + "\n"
            if la_lines else ""
        )

        # Build injury model context for the AGP session
        injury_section = ""
        inj_data = candidate.get("injury_analysis", {})
        if inj_data.get("has_injury_edge"):
            inj_lines = [f"  * {inj_data['market_adjustment_summary']}"]
            if inj_data.get("is_contrarian"):
                inj_lines.append(
                    "  * CONTRARIAN SIGNAL: Market may have over-adjusted to injury news. "
                    "Public overreaction to star name creates value on the injured team."
                )
            for a in inj_data.get("injury_analyses", [])[:3]:
                imp = a.get("impact")
                mtch = a.get("matchup_adjusted")
                mtm = a.get("market_timing")
                if imp and hasattr(imp, "spread_impact"):
                    iline = (f"  * {imp.player_name} ({imp.position}, {imp.tier}): "
                             f"spread impact {imp.spread_impact:+.1f} pts")
                    if mtch and hasattr(mtch, "adjusted_spread_impact"):
                        iline += f", matchup-adj {mtch.adjusted_spread_impact:+.1f} pts"
                    inj_lines.append(iline)
                if mtm and hasattr(mtm, "pct_adjusted"):
                    inj_lines.append(
                        f"    Market {mtm.pct_adjusted:.0%} adjusted, "
                        f"edge remaining: {mtm.edge_remaining:.2f} pts"
                    )
            for prop_opp in inj_data.get("prop_opportunities", [])[:3]:
                sc = prop_opp.get("stat_change", {})
                ppg_inc = sc.get("ppg_increase", sc.get("projected_ppg_increase", 0))
                inj_lines.append(
                    f"  * PROP OPP: {prop_opp['player']} usage +{prop_opp['usage_increase']:.1f}% "
                    f"(PPG +{ppg_inc:.1f}) with {prop_opp['absent_player']} out"
                )
            injury_section = "\nInjury Model Analysis:\n" + "\n".join(inj_lines) + "\n"
        elif inj_data.get("market_adjustment_summary"):
            injury_section = f"\nInjury Status: {inj_data['market_adjustment_summary']}\n"

        query = (
            f"AUTONOMOUS EDGE ANALYSIS — {sport}\n"
            f"Game: {game}\n"
            f"Team: {team} | Market: {market}\n"
            f"Cross-book implied range: {candidate['implied_range']:.1%}\n"
            f"Sharp consensus: {candidate.get('sharp_consensus', 'N/A')}\n"
            f"Best line: {best.get('bookmaker', '?')} {best_str}\n"
            f"Books compared: {candidate['num_bookmakers']}\n"
            f"\nSoft book edges vs sharp:\n{soft_detail}"
            f"{psych_section}"
            f"{pace_section}"
            f"{la_section}"
            f"{injury_section}\n"
            f"Pre-scored confidence: {conf.tier} ({conf.score:.2f})\n\n"
            f"TASK: Use available tools to verify this edge. Check injuries, "
            f"check if the line has moved, check player props if relevant. "
            f"Consider market psychology signals (shading, attention arbitrage), "
            f"pace model confirmation (if available), line analysis signals "
            f"(RLM, steam moves, dead numbers, contrarian value, bet timing), "
            f"and injury model analysis "
            f"(usage redistribution, market adjustment speed, contrarian signals) "
            f"in your confidence assessment. "
            f"Determine if this is a real exploitable edge on DraftKings or Fanatics, "
            f"or if it's noise. Give a final recommendation with confidence score."
        )

        try:
            result = await asyncio.wait_for(
                self.orchestrator.run_session(query, skip_search=True),
                timeout=180,  # 3 minute max per session
            )
            self._session_count += 1

            # Extract the session result
            summary = result.get("summary", {})
            conclusion = summary.get("conclusion", "No conclusion")
            final_confidence = summary.get("confidence_score", 0)
            tier = summary.get("confidence_tier", "UNVERIFIED")

            logger.info(
                f"Autonomous: {team} {market} → {tier} ({final_confidence:.2f}): "
                f"{conclusion[:100]}"
            )

            # Alert if above threshold
            if final_confidence >= MIN_CONFIDENCE_TO_ALERT:
                # Find best DK/Fanatics line from soft edges
                target_book = "?"
                target_price = 0
                for se in candidate.get("soft_book_edges", []):
                    bm = se.get("bookmaker", "").lower()
                    if "draftkings" in bm or "fanatics" in bm:
                        target_book = se.get("bookmaker", "?")
                        target_price = se.get("price", 0)
                        break

                if not target_price and candidate.get("soft_book_edges"):
                    se = candidate["soft_book_edges"][0]
                    target_book = se.get("bookmaker", "?")
                    target_price = se.get("price", 0)

                # Enrich alert with ruin probability, timing value, and unit sizing
                enrichment_lines = []
                try:
                    from tools.kelly import ruin_probability, timing_value, calculate_units
                    edge_decimal = edge_pct / 100.0
                    from tools.odds_api import calculate_implied_probability
                    if target_price:
                        implied = calculate_implied_probability(target_price)
                        est_win_rate = min(0.99, implied + edge_decimal)
                    else:
                        est_win_rate = 0.55

                    # Ruin probability at quarter-Kelly sizing
                    bankroll_est = 1000  # Default; real bankroll from DB in executor
                    avg_stake_est = bankroll_est * 0.01
                    ruin = ruin_probability(
                        bankroll=bankroll_est,
                        avg_stake=avg_stake_est,
                        win_rate=est_win_rate,
                        avg_odds=target_price or -110,
                    )
                    enrichment_lines.append(
                        f"Ruin: {ruin.get('ruin_pct', 0):.2f}% ({ruin.get('risk_level', '?')})"
                    )

                    # Timing value — bet now or wait?
                    hours_to_game = candidate.get("hours_to_game", 6.0)
                    timing = timing_value(
                        current_edge=edge_decimal,
                        hours_to_game=hours_to_game,
                        sport=sport,
                        market=market,
                    )
                    enrichment_lines.append(f"Timing: {timing['recommendation']}")

                    # Unit sizing
                    units = calculate_units(
                        bankroll=bankroll_est,
                        edge=edge_decimal,
                        confidence=final_confidence,
                    )
                    enrichment_lines.append(
                        f"Size: {units['units']:.1f}u ({units['unit_label']})"
                    )
                except Exception as e:
                    logger.debug(f"Edge enrichment failed: {e}")

                enriched_reasoning = conclusion[:200]
                if enrichment_lines:
                    enriched_reasoning += "\n" + " | ".join(enrichment_lines)

                await telegram.alert_edge(
                    game=game,
                    team=team,
                    market=market,
                    edge_pct=edge_pct,
                    confidence_tier=tier,
                    confidence_score=final_confidence,
                    best_book=target_book,
                    best_price=target_price,
                    reasoning=enriched_reasoning,
                )
                self._alert_count += 1
                logger.info(f"Autonomous: Telegram alert sent for {team} {market}")

        except asyncio.TimeoutError:
            logger.warning(f"Autonomous: session timed out for {team} {market}")
        except Exception as e:
            logger.error(f"Autonomous: session failed for {team} {market}: {e}", exc_info=True)

    async def _phase_parlay_correlation_scan(self) -> None:
        """Scan for correlated parlay edges across all monitored sports.

        Uses build_correlated_parlay() on games with existing single-game edges
        to check if correlated legs amplify the edge into a stronger parlay play.
        """
        from tools.correlation import (
            build_correlated_parlay,
            list_correlated_markets,
        )

        all_reports = self.line_monitor.get_edge_report()
        if not isinstance(all_reports, dict):
            return

        now = time.time()
        total_amplified = 0

        for sport, report in all_reports.items():
            if not isinstance(report, dict):
                continue
            if now - self._parlay_scan_ts.get(sport, 0) < 300:
                continue

            snapshot = self.line_monitor._snapshots.get(sport)
            if not snapshot or not snapshot.get("games"):
                continue

            sport_results = {"amplified_parlays": []}

            for game in snapshot["games"][:10]:
                home = game.get("home_team", "")
                away = game.get("away_team", "")
                game_data = {"home_team": home, "away_team": away}
                game_label = f"{away} @ {home}"

                available_props = []
                for bm in game.get("bookmakers", []):
                    for mkt in bm.get("markets", []):
                        for outcome in mkt.get("outcomes", []):
                            price = outcome.get("price", 0)
                            if price == 0:
                                continue
                            point = outcome.get("point")
                            desc = f"{outcome.get('name', '')} {mkt['key']}"
                            if point is not None:
                                desc += f" {point}"
                            available_props.append({
                                "market": mkt["key"],
                                "american_odds": price,
                                "description": f"{desc} ({bm['title']})",
                                "side": outcome.get("name", ""),
                            })

                for market_key in [
                    "cross_book_spreads",
                    "cross_book_h2h",
                    "cross_book_totals",
                ]:
                    for edge in report.get(market_key, []):
                        if edge.get("game", "") != game_label:
                            continue
                        edge_team = edge.get("team", "")
                        edge_market = market_key.replace("cross_book_", "")
                        best_soft = max(
                            (
                                se.get("edge_vs_sharp", 0)
                                for se in edge.get("soft_book_edges", [])
                            ),
                            default=0,
                        )
                        if best_soft < MIN_SOFT_EDGE_VS_SHARP:
                            continue
                        correlated = list_correlated_markets(
                            edge_market, sport, min_abs_rho=0.3
                        )
                        if not correlated:
                            continue
                        try:
                            suggestions = build_correlated_parlay(
                                available_props=available_props[:20],
                                game_data=game_data,
                                sport=sport,
                                min_correlation=0.3,
                                max_legs=3,
                            )
                            for s in suggestions[:3]:
                                if s.get("correlation_edge_pct", 0) > 1.0:
                                    s["amplifies_edge"] = {
                                        "original_edge_team": edge_team,
                                        "original_edge_market": edge_market,
                                        "original_edge_pct": round(best_soft * 100, 2),
                                    }
                                    sport_results["amplified_parlays"].append(s)
                                    total_amplified += 1
                        except Exception as e:
                            logger.debug(
                                f"Parlay amplification failed for {game_label}: {e}"
                            )

            self._parlay_scan_cache[sport] = sport_results
            self._parlay_scan_ts[sport] = now
            n = len(sport_results["amplified_parlays"])
            if n > 0:
                logger.info(f"Parlay scan {sport}: {n} amplified parlays found")

            for parlay in sport_results["amplified_parlays"]:
                if parlay.get("rating") in ("ELITE", "STRONG"):
                    try:
                        leg_desc = ", ".join(
                            leg.get("description", "?")
                            for leg in parlay.get("legs", [])
                        )
                        await telegram.alert_edge(
                            game=parlay.get("game", "?"),
                            team=parlay.get("amplifies_edge", {}).get(
                                "original_edge_team", "?"
                            ),
                            market="SGP_CORRELATED",
                            edge_pct=parlay.get("correlation_edge_pct", 0),
                            confidence_tier=parlay.get("rating", "UNKNOWN"),
                            confidence_score=0.0,
                            best_book="SGP",
                            best_price=parlay.get("fair_parlay_odds", 0),
                            reasoning=f"Correlated parlay ({parlay.get('num_legs', 0)} legs): {leg_desc[:150]}",
                        )
                    except Exception as e:
                        logger.debug(f"Failed to send parlay alert: {e}")

        if total_amplified > 0:
            logger.info(
                f"Parlay correlation scan: {total_amplified} amplified parlays total"
            )

    def get_parlay_scan_report(self) -> dict:
        """Return the latest parlay/SGP correlation scan results."""
        return dict(self._parlay_scan_cache)

    def _cleanup_dedup(self) -> None:
        """Remove old entries from the dedup and injury analysis caches."""
        now = time.time()
        expired = [
            k for k, t in self._analyzed_edges.items()
            if now - t > EDGE_DEDUP_WINDOW * 1.5
        ]
        for k in expired:
            del self._analyzed_edges[k]
        # Hard cap: if cache grows beyond 500 entries, keep only newest 250
        if len(self._analyzed_edges) > 500:
            sorted_keys = sorted(self._analyzed_edges, key=self._analyzed_edges.get)
            for k in sorted_keys[:len(sorted_keys) - 250]:
                del self._analyzed_edges[k]
        # ── Cap ALL in-memory caches to prevent unbounded growth (200 MB/hr leak) ──
        # Injury analysis: LRU-evict oldest entries instead of bulk clear
        if len(self._injury_analysis_cache) > 50:
            # Keep only the 25 most recent entries (approximation: evict half)
            keys = list(self._injury_analysis_cache.keys())
            for k in keys[:len(keys) - 25]:
                del self._injury_analysis_cache[k]
        # Parlay scan: keyed by sport so bounded by sport count (~10) — but
        # clear stale results older than 30 min to free nested data structures
        stale_parlay = [
            s for s, t in self._parlay_scan_ts.items()
            if now - t > 1800
        ]
        for s in stale_parlay:
            self._parlay_scan_cache.pop(s, None)
            self._parlay_scan_ts.pop(s, None)
        # Psychology: same pattern — clear stale entries > 30 min old
        stale_psych = [
            s for s, t in self._psychology_ts.items()
            if now - t > 1800
        ]
        for s in stale_psych:
            self._psychology_cache.pop(s, None)
            self._psychology_ts.pop(s, None)
        # Injury cache: clear stale ESPN injury reports > 30 min old.
        # Without this, _injury_cache grows unbounded (no eviction existed).
        stale_injury = [
            s for s, t in self._injury_ts.items()
            if now - t > 1800
        ]
        for s in stale_injury:
            self._injury_cache.pop(s, None)
            self._injury_ts.pop(s, None)

    def get_status(self) -> dict:
        """Return loop status."""
        now = time.time()
        psych_summary = {}
        for sport, psych in self._psychology_cache.items():
            psych_summary[sport] = {
                "shaded_lines": len(psych.get("number_shading", [])),
                "attention_recommendation": psych.get("attention_arbitrage", {}).get("recommendation", "N/A"),
                "age_seconds": round(now - self._psychology_ts.get(sport, 0)),
            }
        return {
            "running": self._running,
            "sessions_run": self._session_count,
            "alerts_sent": self._alert_count,
            "cached_edge_keys": len(self._analyzed_edges),
            "analysis_cooldown_seconds": ANALYSIS_COOLDOWN,
            "min_confidence_to_alert": MIN_CONFIDENCE_TO_ALERT,
            "market_psychology": psych_summary,
            "parlay_correlation": {
                sport: {
                    "amplified_parlays": len(scan.get("amplified_parlays", [])),
                    "age_seconds": round(now - self._parlay_scan_ts.get(sport, 0)),
                }
                for sport, scan in self._parlay_scan_cache.items()
            },
        }

    def get_psychology_report(self) -> dict:
        """Return the latest market psychology signals for all sports."""
        return {
            sport: psych for sport, psych in self._psychology_cache.items()
        }


# ──────────────────────────────────────────────────────────
# RESEARCH LOOP — Karpathy-style autonomous loop
# ──────────────────────────────────────────────────────────
# Design principles (from Karpathy's autoresearch):
#   1. Loop as tight as possible — rate limit is the only governor
#   2. Every iteration: hypothesize → test → measure → keep/discard
#   3. Never pause for human approval
#   4. Append-only experiment log for all attempts
#   5. Clean state between iterations (prevent error accumulation)
#   6. Maximize token throughput to Claude Code at all times

# Cadence controls — MAXIMUM THROUGHPUT (Karpathy loop: rate limit is the only governor)
RESEARCH_CYCLE_INTERVAL = 60        # 1 min between cycles — tight as possible
DATA_COLLECTION_INTERVAL = 300      # 5 min between data pulls — fresher data for live edges
HYPOTHESIS_GEN_INTERVAL = 120       # 2 min between hypothesis generation — Claude drives, smaller batches
BACKTEST_BATCH_SIZE = 5             # 50 was timing out every cycle (5min/hyp from DB locks). 5 fits in 600s.
CLAUDE_ESCALATION_COOLDOWN = 75      # 75s cooldown — prevents burst of 3-5 calls in 30s that was causing 5x/day stalls
SYSTEM_IMPROVEMENT_INTERVAL = 11    # Run system improvement every N cycles (prime — avoids collision with regime/integrity)
REGIME_ANALYSIS_INTERVAL = 7        # Run regime analysis every N cycles — regime changes are slow (coprime with 4,11,13)

# ── Temporal isolation defaults ──
# Hypotheses train on data before the cutoff, backtest on data after.
# This prevents look-ahead bias / circular testing.
DEFAULT_TRAINING_WINDOW_DAYS = 30    # Train on everything before (today - N days)
BACKTEST_GAP_DAYS = 2                # 2 days: enough temporal isolation to prevent leakage, but avoids the 7-day deadlock where start > end when training_period_end is recent

# ── Sport priority for backtest queue ──
# Sports with more historical data get tested first.
# This ensures NBA/NFL hypotheses (abundant data) are validated before
# MLB (season just started, sparse data). Lower number = higher priority.
SPORT_PRIORITY = {
    "basketball_nba": 1,
    "americanfootball_nfl": 2,
    "icehockey_nhl": 3,
    "baseball_mlb": 4,
    "basketball_ncaab": 5,
    "basketball_ncaaw": 6,
    "basketball_wnba": 7,
    "golf_pga": 8,
}

# Domains to research (ordered by data availability)
RESEARCH_SPORTS = [
    "basketball_nba",
    "americanfootball_nfl",
    "basketball_ncaab",
    "basketball_ncaaw",
    "basketball_wnba",
    "icehockey_nhl",
    "baseball_mlb",
    "golf_pga",
]

# Minimum game contexts required before a sport is eligible for hypothesis generation
MIN_GAMES_FOR_HYPOTHESIS = 100



class ResearchLoop:
    """
    24/7 autonomous research engine — Claude Code is the primary reasoning engine.

    Runs independently of AutonomousLoop. While AutonomousLoop handles
    real-time edge detection and alerting, ResearchLoop handles the
    slow, deep work: collecting data, discovering patterns, generating
    and testing hypotheses, interpreting results, and self-improving.

    Claude Code drives hypothesis generation, backtest interpretation,
    and system improvement. Local models handle fast classification
    (Sentinel) and embeddings only. Template generation is the fallback
    when Claude is rate-limited.

    NEVER IDLE: When Claude is unavailable, work is deferred to a
    persistent queue AND local model fallbacks keep the loop productive.
    When Claude returns, the deferred queue drains immediately.
    """

    def __init__(
        self,
        hypothesis_manager,
        hypothesis_generator,
        backtest_engine,
        data_collector,
        vector_store,
        orchestrator=None,
        line_monitor=None,
    ):
        self.hypothesis_manager = hypothesis_manager
        self.hypothesis_generator = hypothesis_generator
        self.backtest_engine = backtest_engine
        self.data_collector = data_collector
        self.vector_store = vector_store
        self.orchestrator = orchestrator
        self.line_monitor = line_monitor

        self._running = False
        self._task: Optional[asyncio.Task] = None

        # Timestamps for cadence control
        self._last_data_collect = 0.0
        self._last_hypothesis_gen = 0.0
        self._last_claude_call = 0.0

        # Bulk backfill tracking — one-time 30-day seed when data is thin
        self._bulk_backfill_done = False

        # Counters
        self._cycles = 0
        self._data_collections = 0
        self._hypotheses_generated = 0
        self._backtests_run = 0
        self._claude_escalations = 0
        self._promotions = 0
        self._rejections = 0

        # Self-diagnostics — track already-escalated issues to avoid spam
        # Capped at 500 entries; oldest keys evicted when full.
        self._diagnostic_issues: set[str] = set()
        self._DIAGNOSTIC_ISSUES_MAX = 500

        # ── Progress tracking (Ralph loop: detect spinning) ──
        self._progress_window: list[dict] = []  # last N cycle snapshots
        PROGRESS_WINDOW_SIZE = 10  # look at last 10 cycles
        self._spinning_detected = False
        self._last_progress_check = 0
        self._consecutive_no_progress = 0

        # Regime analysis — uses module-level _regime_cache (shared with AutonomousLoop)
        # Refreshed every REGIME_ANALYSIS_INTERVAL cycles
        self._last_regime_analysis = 0

        # Dedup reactive game completion handlers — prevents 14×14 ESPN calls
        # when 14 games complete on the same date. Cleared each research cycle.
        self._reactive_collected: set[tuple[str, str]] = set()

        # Deferred work queue + downtime tracker (never-idle loop)
        from tools.work_queue import get_work_queue, get_downtime_tracker
        self._work_queue = get_work_queue()
        self._downtime_tracker = get_downtime_tracker()
        self._was_claude_available = True  # track transitions

        # Mode control
        self._paused = False
        self._local_only = os.getenv("CALLISTO_LOCAL_ONLY", "").lower() in ("1", "true", "yes")

    async def start(self) -> None:
        """Start the research loop."""
        if self._running:
            return
        self._running = True
        logger.info(f"Research loop starting — all sports equal: {RESEARCH_SPORTS}")
        # Subscribe to event bus for reactive data collection
        try:
            from tools.event_bus import get_event_bus, EVENT_GAME_COMPLETED, EVENT_GAME_LINEUP_WINDOW
            bus = get_event_bus()
            bus.subscribe(EVENT_GAME_COMPLETED, self._on_game_completed)
            bus.subscribe(EVENT_GAME_LINEUP_WINDOW, self._on_game_lineup_window)
            logger.info("Research loop subscribed to game_completed and lineup_window events")
        except Exception as e:
            logger.debug(f"Event bus subscription failed (non-critical): {e}")
        # One-time backfill of temporal metadata on legacy hypotheses
        await self._backfill_temporal_metadata()
        # One-time: requeue hypotheses falsely rejected by high-threshold bug
        await self._requeue_threshold_rejections()
        # One-time: requeue player prop hypotheses now that prop backtesting is available
        await self._requeue_prop_rejections()
        # Edge thresholds: run AFTER requeues so newly-requeued hypotheses get
        # their thresholds lowered too (previously ran before requeues, missing them)
        await self._migrate_edge_thresholds()
        # Retroactively update signal_generated on existing backtest events
        # to match lowered thresholds — unblocks stalled promotions
        await self._retroactive_signal_update()
        # Requeue hypotheses falsely rejected for '0 signals' due to stale stats
        await self._requeue_stale_signal_rejections()
        # Reject any anti-predictive hypotheses still stuck in active states
        await self._reject_anti_predictive()
        await self._reject_low_signal_rate()
        self._task = asyncio.create_task(self._loop())
        # Quant scanner runs on a separate, faster cadence (~60s). It's the
        # live pricing engine — consumes multi-book odds, emits ranked edges
        # into live_edge_surface. Decoupled from the 5-minute research loop
        # so recommendations refresh at market-appropriate speed.
        self._quant_scan_task = asyncio.create_task(self._quant_scan_loop())
        logger.info("Research loop started — autonomous hypothesis machine online")
        logger.info("Quant scanner started — live edge surface refreshing every 60s")

    async def _on_game_completed(self, event_data: dict) -> None:
        """Reactive handler: immediately collect data when a game completes."""
        sport = event_data.get("sport", "")
        game_date = event_data.get("game_date", "")
        if not sport or not game_date:
            return

        # Dedup: collect_play_by_play fetches ALL games for a date, so
        # calling it once per (sport, date) is sufficient. Without this,
        # 14 completed MLB games fire 14 handlers × 14 ESPN calls each = 196.
        key = (sport, game_date)
        if key in self._reactive_collected:
            return
        self._reactive_collected.add(key)

        try:
            date_str = game_date.replace("-", "")
            await self.data_collector.collect_box_scores(sport, date_str)
            await self.data_collector.collect_play_by_play(sport, date_str)
            logger.info(f"Reactive collection: {sport} game completed on {game_date}")

            # Update learned correlations from this game's data
            try:
                from tools.correlation import get_learned_store
                lcs = get_learned_store()
                if lcs is not None and self.data_collector._db is not None:
                    await lcs.update_from_game_data(
                        self.data_collector._db, sport, game_date,
                    )
            except Exception as e:
                logger.debug(f"Reactive correlation update failed: {e}")

            # Compute per-game KL divergence (information flow measurement)
            try:
                from tools.kl_divergence import compute_game_kl, store_kl_metrics
                event_id = event_data.get("event_id", "")
                if event_id:
                    db_path = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")
                    kl_result = await compute_game_kl(db_path, event_id, sport)
                    if kl_result:
                        await store_kl_metrics(db_path, [kl_result])
            except Exception as e:
                logger.debug(f"KL divergence computation failed: {e}")
        except Exception as e:
            logger.debug(f"Reactive collection failed for {sport} {game_date}: {e}")

    async def _on_game_lineup_window(self, event_data: dict) -> None:
        """Reactive handler: re-scan edges when lineup cards may be posted (T-180min)."""
        sport = event_data.get("sport", "")
        event_id = event_data.get("event_id", "")
        home_team = event_data.get("home_team", "")
        away_team = event_data.get("away_team", "")
        commence_time = event_data.get("commence_time", "")

        if not sport or not event_id:
            return

        matchup = f"{away_team}@{home_team}" if away_team and home_team else event_id
        logger.info(
            f"Lineup window trigger: {matchup} ({sport}) — "
            f"re-scanning edges for lineup confirmation"
        )

        try:
            query = (
                f"LINEUP_WINDOW_RESCAN for {matchup}: Re-evaluate edges now that "
                f"lineup cards may be posted. Check market hold normalization, spread "
                f"compression, and whether pre-lineup phantom edges have resolved. "
                f"event_id={event_id} sport={sport} commence_time={commence_time}"
            )
            await self._work_queue.enqueue("lineup_rescan", query, priority=1)
            logger.info(f"Lineup rescan task enqueued for {matchup}")
        except Exception as e:
            logger.warning(f"Failed to enqueue lineup rescan for {matchup}: {e}")

    async def _backfill_temporal_metadata(self) -> None:
        """Backfill training_period_end on legacy hypotheses that lack temporal metadata.

        Sets reasonable defaults so the backtest engine can enforce temporal isolation
        on the 231 hypotheses created before the temporal split system existed.
        """
        db = self.hypothesis_manager._db
        if db is None:
            logger.warning("Cannot backfill temporal metadata — hypothesis DB not initialized")
            return

        cursor = await db.execute(
            "SELECT hypothesis_id, model_config FROM hypotheses "
            "WHERE model_config NOT LIKE '%training_period_end%'"
        )
        rows = await cursor.fetchall()

        if not rows:
            logger.info("Temporal metadata backfill: no legacy hypotheses need updating")
            return

        count = 0
        for hypothesis_id, model_config_raw in rows:
            try:
                config = json.loads(model_config_raw) if model_config_raw else {}
            except (json.JSONDecodeError, TypeError):
                config = {}

            config["training_period_end"] = "2026-02-22"
            config["training_period_start"] = "2023-01-01"
            config["temporal_split_gap_days"] = 7

            await db.execute(
                "UPDATE hypotheses SET model_config = ? WHERE hypothesis_id = ?",
                (json.dumps(config), hypothesis_id),
            )
            count += 1

        await db.commit()
        logger.info(
            f"Temporal metadata backfill complete: updated {count} legacy hypotheses "
            f"(training_period_end=2026-02-22, training_period_start=2023-01-01, gap=7d)"
        )

    async def _migrate_edge_thresholds(self) -> None:
        """Lower edge_thresholds that exceed real market edge range.

        Real market edges in our data top out at ~0.83% with most at 0.3-0.8%.
        Three-pass migration:
          Pass 1: thresholds >= 2.5% → 1.5% (legacy fix)
          Pass 2: thresholds >= 1.5% → 1.0% (93% zero-signal fix)
          Pass 3: thresholds >= 0.8% → 0.5% (max observed edge is 0.83%)
        Without pass 3, 2,845+ hypotheses at 1.0% can never fire a signal.
        """
        db = self.hypothesis_manager._db
        if db is None:
            return

        # Pass 1: legacy — >= 2.5% to 1.5%
        cursor = await db.execute(
            "SELECT COUNT(*) FROM hypotheses "
            "WHERE edge_threshold >= 0.025 AND status IN ('draft', 'backtesting')"
        )
        row = await cursor.fetchone()
        count_high = row[0] if row else 0

        if count_high > 0:
            await db.execute(
                "UPDATE hypotheses SET edge_threshold = 0.015 "
                "WHERE edge_threshold >= 0.025 AND status IN ('draft', 'backtesting')"
            )
            logger.info(
                f"Edge threshold migration pass 1: lowered {count_high} hypotheses "
                f"from ≥2.5% to 1.5%"
            )

        # Pass 2: lower 1.5-2.5% to 1.0%
        cursor = await db.execute(
            "SELECT COUNT(*) FROM hypotheses "
            "WHERE edge_threshold >= 0.015 AND edge_threshold < 0.025 "
            "AND status IN ('draft', 'backtesting')"
        )
        row = await cursor.fetchone()
        count_mid = row[0] if row else 0

        if count_mid > 0:
            await db.execute(
                "UPDATE hypotheses SET edge_threshold = 0.01 "
                "WHERE edge_threshold >= 0.015 AND edge_threshold < 0.025 "
                "AND status IN ('draft', 'backtesting')"
            )
            logger.info(
                f"Edge threshold migration pass 2: lowered {count_mid} hypotheses "
                f"from 1.5-2.5% to 1.0%"
            )

        # Pass 3: lower >= 0.8% to 0.5% — max observed edge is 0.83%,
        # so 1.0% and 1.2% thresholds are unreachable
        cursor = await db.execute(
            "SELECT COUNT(*) FROM hypotheses "
            "WHERE edge_threshold >= 0.008 AND status IN ('draft', 'backtesting')"
        )
        row = await cursor.fetchone()
        count_low = row[0] if row else 0

        if count_low > 0:
            await db.execute(
                "UPDATE hypotheses SET edge_threshold = 0.005 "
                "WHERE edge_threshold >= 0.008 AND status IN ('draft', 'backtesting')"
            )
            logger.info(
                f"Edge threshold migration pass 3: lowered {count_low} hypotheses "
                f"from ≥0.8% to 0.5% (max observed edge is 0.83%)"
            )

        # Pass 4: final sweep — lower any remaining threshold > 0.003 to 0.003
        # The 0.005 threshold from pass 3 still filters out edges in the 0.3-0.5%
        # range which are common and profitable at scale. 0.3% is the minimum
        # detectable edge that is consistently above noise.
        cursor = await db.execute(
            "SELECT COUNT(*) FROM hypotheses "
            "WHERE edge_threshold > 0.003 AND status IN ('draft', 'backtesting')"
        )
        row = await cursor.fetchone()
        count_final = row[0] if row else 0

        if count_final > 0:
            await db.execute(
                "UPDATE hypotheses SET edge_threshold = 0.003 "
                "WHERE edge_threshold > 0.003 AND status IN ('draft', 'backtesting')"
            )
            logger.info(
                f"Edge threshold migration pass 4: lowered {count_final} hypotheses "
                f"to 0.3% (final sweep — captures 0.3-0.5% edges)"
            )

        total = count_high + count_mid + count_low + count_final
        if total > 0:
            await db.commit()
            logger.info(
                f"Edge threshold migration complete: {total} hypotheses updated "
                f"(pass1={count_high}, pass2={count_mid}, pass3={count_low})"
            )
        else:
            logger.info("Edge threshold migration: no hypotheses need lowering")

    async def _retroactive_signal_update(self) -> None:
        """Retroactively update signal_generated on backtest events after threshold migration.

        When edge_threshold is lowered, existing backtest events may now qualify
        as signals (edge >= new threshold). Without this, hypotheses sit in 'held'
        state despite having edges that exceed the updated threshold.
        """
        db = self.hypothesis_manager._db
        if db is None:
            return

        # For each backtesting hypothesis, update signal_generated based on
        # current edge_threshold (which may have been lowered by migration)
        cursor = await db.execute(
            "SELECT hypothesis_id, edge_threshold FROM hypotheses "
            "WHERE status = 'backtesting'"
        )
        rows = await cursor.fetchall()

        total_updated = 0
        total_new_signals = 0
        for hypothesis_id, threshold in rows:
            if threshold is None:
                continue
            # Update signal_generated for events where edge >= threshold
            update_cursor = await db.execute(
                "UPDATE backtest_events "
                "SET signal_generated = CASE WHEN edge >= ? THEN 1 ELSE 0 END "
                "WHERE hypothesis_id = ? AND signal_generated = 0 AND edge IS NOT NULL "
                "AND edge >= ?",
                (threshold, hypothesis_id, threshold),
            )
            if update_cursor.rowcount > 0:
                total_updated += update_cursor.rowcount
                total_new_signals += update_cursor.rowcount

        if total_updated > 0:
            await db.commit()
            logger.info(
                f"Retroactive signal update: upgraded {total_updated} backtest events "
                f"to signals across {len(rows)} hypotheses (edge >= lowered threshold)"
            )
        else:
            logger.info("Retroactive signal update: no events needed updating")

    async def _requeue_threshold_rejections(self) -> None:
        """Requeue hypotheses that were rejected due to the high-threshold bug.

        These hypotheses were rejected with 'no_edge_after_backtest' because their
        edge_threshold was ≥3% while real market edges cap at ~2.5%. With thresholds
        now lowered, they deserve a second chance.
        """
        db = self.hypothesis_manager._db
        if db is None:
            return

        cursor = await db.execute(
            "SELECT hypothesis_id, model_config FROM hypotheses "
            "WHERE status = 'rejected' "
            "AND promoted_by LIKE '%no_edge_after_backtest%'"
        )
        rows = await cursor.fetchall()

        if not rows:
            logger.info("Threshold rejection requeue: no hypotheses to requeue")
            return

        count = 0
        for hypothesis_id, model_config_raw in rows:
            try:
                config = json.loads(model_config_raw) if model_config_raw else {}
            except (json.JSONDecodeError, TypeError):
                config = {}

            # Reset eval cycles so they get a fresh evaluation
            config["evaluate_cycles"] = 0
            config["requeued_from_threshold_bug"] = True

            await db.execute(
                "UPDATE hypotheses SET status = 'backtesting', "
                "edge_threshold = 0.015, model_config = ? "
                "WHERE hypothesis_id = ?",
                (json.dumps(config), hypothesis_id),
            )
            count += 1

        await db.commit()
        logger.info(
            f"Threshold rejection requeue: moved {count} hypotheses from rejected → backtesting "
            f"(were victims of edge_threshold ≥ 3% bug, now set to 1.5%)"
        )

    async def _requeue_prop_rejections(self) -> None:
        """Requeue player prop hypotheses rejected before prop backtesting was available.

        These were rejected with 'auto:untestable_no_prop_backtest' because
        historical_odds_cache lacked prop data. Now prop_snapshots is wired
        into BacktestEngine, so they can be properly tested.
        """
        db = self.hypothesis_manager._db
        if db is None:
            return

        cursor = await db.execute(
            "SELECT hypothesis_id FROM hypotheses "
            "WHERE status = 'rejected' "
            "AND promoted_by LIKE '%untestable_no_prop_backtest%'"
        )
        rows = await cursor.fetchall()

        if not rows:
            return

        count = 0
        for (hypothesis_id,) in rows:
            await db.execute(
                "UPDATE hypotheses SET status = 'draft', edge_threshold = 0.003 "
                "WHERE hypothesis_id = ?",
                (hypothesis_id,),
            )
            count += 1

        if count > 0:
            await db.commit()
            logger.info(
                f"Prop rejection requeue: moved {count} player prop hypotheses "
                f"from rejected → draft (prop_snapshots backtesting now available)"
            )

    async def _requeue_stale_signal_rejections(self) -> None:
        """Requeue hypotheses rejected with '0 signals' that actually have signals.

        Race condition: retroactive signal update runs after backtest but before
        evaluate. The evaluate phase sees stale signals_generated=0 in backtest_runs
        and rejects, even though backtest_events now has signals. Fix: requeue these
        to backtesting so they get a fresh evaluation with correct stats.
        """
        db = self.hypothesis_manager._db
        if db is None:
            return

        # Find hypotheses rejected for "0 signals" that actually have signals in events.
        # Two-step approach to avoid slow correlated subquery on 3000+ rejected hyps.
        cursor = await db.execute(
            "SELECT hypothesis_id, name, promoted_by FROM hypotheses "
            "WHERE status = 'rejected' AND promoted_by LIKE '%0 signals%'"
        )
        candidates = await cursor.fetchall()
        rows = []
        for hid, name, reason in candidates:
            sig_row = await (await db.execute(
                "SELECT COUNT(*) FROM backtest_events "
                "WHERE hypothesis_id = ? AND signal_generated = 1",
                (hid,),
            )).fetchone()
            actual_signals = sig_row[0] if sig_row else 0
            if actual_signals > 0:
                rows.append((hid, name, actual_signals))

        if not rows:
            logger.info(f"Stale signal requeue: checked {len(candidates)} candidates, none had actual signals")
            return

        count = 0
        for hid, name, actual_signals in rows:
            await self.hypothesis_manager.update_status(
                hid, "backtesting",
                f"auto:requeued_stale_signal_rejection — rejected with '0 signals' "
                f"but backtest_events has {actual_signals} signals. Race condition fix."
            )
            count += 1
            logger.info(
                f"Requeued {hid} ({name}): rejected for '0 signals' but has "
                f"{actual_signals} actual signals in backtest_events"
            )

        if count:
            await db.commit()
            logger.info(f"Stale signal rejection requeue: restored {count} hypotheses")

    async def _reject_anti_predictive(self) -> None:
        """Reject hypotheses with strongly negative IC on sufficient sample size.

        Uses the same thresholds as hypothesis.py auto-rejection:
        - IC < -0.15 with 15+ signals (standard)
        - IC < -0.25 with 10+ signals (strong anti-prediction)
        Previous threshold of IC < -0.10 with NO sample minimum was rejecting
        hypotheses based on noise (e.g. IC=-0.13 on 11 paper trades is meaningless).
        """
        db = self.hypothesis_manager._db
        if db is None:
            return

        cursor = await db.execute(
            "SELECT h.hypothesis_id, h.name, h.status, "
            "hs.information_coefficient, hs.brier_score, hs.signals_n "
            "FROM hypotheses h "
            "JOIN hypothesis_stats hs ON h.hypothesis_id = hs.hypothesis_id "
            "WHERE h.status IN ('backtesting', 'paper_trading') "
            "AND hs.information_coefficient < -0.15"
        )
        rows = await cursor.fetchall()

        count = 0
        for hid, name, status, ic, brier, signals_n in rows:
            signals_n = signals_n or 0
            # Require minimum sample size for IC to be meaningful
            if ic < -0.25 and signals_n >= 10:
                pass  # strong anti-prediction — reject
            elif ic < -0.15 and signals_n >= 15:
                pass  # standard anti-prediction — reject
            else:
                continue  # insufficient evidence
            try:
                brier_str = f"{brier:.3f}" if brier is not None else "N/A"
                ic_str = f"{ic:.3f}" if ic is not None else "N/A"
                await self.hypothesis_manager.update_status(
                    hid, "rejected",
                    f"auto:anti_predictive — IC={ic_str}, brier={brier_str}, n={signals_n}. "
                    f"Strongly anti-predictive, worse than random."
                )
                count += 1
                logger.info(
                    f"Rejected anti-predictive {hid} ({name}): IC={ic_str}, brier={brier_str}, n={signals_n}"
                )
            except Exception as e:
                logger.warning(f"Failed to reject anti-predictive {hid} ({name}): {e}")

        if count:
            logger.info(f"Anti-predictive sweep: rejected {count} hypotheses")

    async def _reject_low_signal_rate(self) -> None:
        """Reject backtesting hypotheses with 100+ events but <2% signal rate.

        These hypotheses target edge conditions that don't exist at detectable
        frequency. All p-value and IC rejection tiers gate on signal count,
        so near-zero-signal hypotheses slip through indefinitely.
        """
        db = self.hypothesis_manager._db
        if db is None:
            return

        cursor = await db.execute(
            "SELECT h.hypothesis_id, h.name, hs.total_n, hs.signals_n "
            "FROM hypotheses h "
            "JOIN hypothesis_stats hs ON h.hypothesis_id = hs.hypothesis_id "
            "WHERE h.status = 'backtesting' "
            "AND hs.total_n >= 100 "
            "AND (CAST(hs.signals_n AS REAL) / hs.total_n) < 0.02"
        )
        rows = await cursor.fetchall()

        count = 0
        for hid, name, total_n, signals_n in rows:
            signals_n = signals_n or 0
            rate = signals_n / total_n if total_n > 0 else 0
            try:
                await self.hypothesis_manager.update_status(
                    hid, "rejected",
                    f"auto:low_signal_rate — {signals_n}/{total_n} events = "
                    f"{rate:.1%} signal rate < 2%. Edge condition too rare."
                )
                count += 1
                logger.info(
                    f"Rejected low-signal-rate {hid} ({name}): "
                    f"{signals_n}/{total_n} = {rate:.1%}"
                )
            except Exception as e:
                logger.warning(f"Failed to reject low-signal-rate {hid} ({name}): {e}")

        if count:
            logger.info(f"Low-signal-rate sweep: rejected {count} hypotheses")

    async def stop(self) -> None:
        """Stop the research loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        # Cancel the quant scanner alongside the main loop.
        qt = getattr(self, "_quant_scan_task", None)
        if qt is not None and not qt.done():
            qt.cancel()
            try:
                await qt
            except (asyncio.CancelledError, Exception):
                pass
        # Unsubscribe from event bus to prevent leaked references on restart
        try:
            from tools.event_bus import get_event_bus, EVENT_GAME_COMPLETED, EVENT_GAME_LINEUP_WINDOW
            bus = get_event_bus()
            bus.unsubscribe(EVENT_GAME_COMPLETED, self._on_game_completed)
            bus.unsubscribe(EVENT_GAME_LINEUP_WINDOW, self._on_game_lineup_window)
        except Exception:
            pass
        # Record final downtime stats
        await self._downtime_tracker.record_to_hermes()
        logger.info(
            f"Research loop stopped — {self._cycles} cycles, "
            f"{self._hypotheses_generated} hypotheses generated, "
            f"{self._backtests_run} backtests run, "
            f"{self._promotions} promoted, {self._rejections} rejected"
        )

    async def pause(self) -> dict:
        """Pause the research loop (keeps running but skips all phases)."""
        self._paused = True
        logger.info("Research loop PAUSED")
        return {"status": "paused", "cycles_completed": self._cycles}

    async def resume(self) -> dict:
        """Resume the research loop."""
        self._paused = False
        logger.info("Research loop RESUMED")
        return {"status": "running", "cycles_completed": self._cycles}

    def set_local_only(self, enabled: bool) -> dict:
        """Toggle local-only mode (no Claude Code calls)."""
        self._local_only = enabled
        mode = "local_only" if enabled else "full"
        logger.info(f"Research loop mode: {mode}")
        return {"mode": mode, "local_only": enabled}

    def _claude_ok(self) -> bool:
        """Check if Claude Code calls are allowed."""
        if self._local_only:
            return False
        from tools.claude_code import is_available as claude_available
        return claude_available()

    async def _drain_deferred_queue(self) -> None:
        """If Claude is available and we have queued work, drain it first.

        This is the critical path: when Claude comes back online after a
        rate-limit window, all deferred hypothesis generation, interpretation,
        and deep work gets executed immediately before the normal cycle.
        """
        from tools.claude_code import is_available as claude_available, claude_code_query

        claude_up = claude_available() and not self._local_only

        # Track Claude availability transitions
        if claude_up and not self._was_claude_available:
            self._downtime_tracker.mark_available()
        elif not claude_up and self._was_claude_available:
            self._downtime_tracker.mark_unavailable()
        self._was_claude_available = claude_up

        if not claude_up:
            return

        pending = await self._work_queue.size()
        if pending == 0:
            return

        logger.info(f"Claude available -- draining {pending} deferred items")
        drained = await self._work_queue.drain(max_items=5)

        for item in drained:
            if not self._running:
                break
            try:
                result = await claude_code_query(
                    item["prompt"], hermes_caller=item["work_type"]
                )
                self._last_claude_call = time.time()
                self._claude_escalations += 1

                if result.get("content") and not result.get("error"):
                    # Process based on work type
                    await self._process_drained_item(item, result["content"])
                    await self._work_queue.mark_done(item["id"], result["content"][:500])
                    logger.info(
                        f"Drained item {item['id']} ({item['work_type']}): success"
                    )
                elif result.get("rate_limited"):
                    # Claude went away again -- put item back
                    await self._work_queue.mark_failed(item["id"], "rate_limited_during_drain")
                    logger.info("Claude rate-limited during drain -- stopping drain")
                    break
                else:
                    await self._work_queue.mark_done(
                        item["id"], f"error: {result.get('error', 'unknown')}"
                    )
            except Exception as e:
                await self._work_queue.mark_failed(item["id"], str(e))
                logger.warning(f"Drain item {item['id']} failed: {e}")

        # Record downtime pattern every 10 cycles
        if self._cycles % 10 == 0:
            await self._downtime_tracker.record_to_hermes()

    async def _process_drained_item(self, item: dict, content: str) -> None:
        """Process the result of a drained deferred work item."""
        work_type = item["work_type"]
        try:
            # Extract JSON from response
            json_str = content
            if "```" in json_str:
                parts = json_str.split("```")
                for part in parts:
                    stripped = part.strip()
                    if stripped.startswith("json"):
                        stripped = stripped[4:].strip()
                    if stripped.startswith("{"):
                        json_str = stripped
                        break
            elif "{" in json_str:
                start = json_str.index("{")
                end = json_str.rindex("}") + 1
                json_str = json_str[start:end]

            parsed = json.loads(json_str)

            if work_type == "hypothesis_gen":
                created = 0
                for nh in parsed.get("hypotheses", []):
                    try:
                        _dq_config = {
                                "source": "deferred_queue_claude",
                                "cycle": self._cycles,
                                "training_period_start": "2023-01-01",
                                "training_period_end": str(
                                    datetime.now(timezone.utc).date()
                                    - timedelta(days=DEFAULT_TRAINING_WINDOW_DAYS)
                                ),
                                "forward_test_start": str(
                                    datetime.now(timezone.utc).date()
                                    - timedelta(days=DEFAULT_TRAINING_WINDOW_DAYS)
                                    + timedelta(days=BACKTEST_GAP_DAYS)
                                ),
                        }
                        if nh.get("game_filters"):
                            _dq_config["game_filters"] = nh["game_filters"]
                        if nh.get("line_filters"):
                            _dq_config["line_filters"] = nh["line_filters"]
                        await self.hypothesis_manager.create_hypothesis(
                            name=nh.get("name", f"deferred_gen_{self._cycles}"),
                            thesis=nh.get("thesis", ""),
                            sport=nh.get("sport", "basketball_nba"),
                            market_type=nh.get("market_type", "spreads"),
                            edge_threshold=nh.get("edge_threshold", 0.015),
                            model_config=_dq_config,
                        )
                        created += 1
                    except Exception as e:
                        logger.warning(f"Failed to create deferred hypothesis: {e}")
                if created:
                    self._hypotheses_generated += created
                    logger.info(f"Deferred drain: created {created} hypotheses")

            elif work_type == "deep_work":
                # Same processing as _phase_claude_deep_work
                rejected = 0
                for hid in parsed.get("reject_ids", []):
                    try:
                        await self.hypothesis_manager.update_status(
                            hid, "rejected", "deferred_claude_deep_work"
                        )
                        rejected += 1
                        self._rejections += 1
                    except Exception:
                        pass
                created = 0
                for nh in parsed.get("new_hypotheses", []):
                    try:
                        _ddw_config = {
                                "source": "deferred_deep_work",
                                "cycle": self._cycles,
                                "training_period_start": "2023-01-01",
                                "training_period_end": str(
                                    datetime.now(timezone.utc).date()
                                    - timedelta(days=DEFAULT_TRAINING_WINDOW_DAYS)
                                ),
                                "forward_test_start": str(
                                    datetime.now(timezone.utc).date()
                                    - timedelta(days=DEFAULT_TRAINING_WINDOW_DAYS)
                                    + timedelta(days=BACKTEST_GAP_DAYS)
                                ),
                        }
                        if nh.get("game_filters"):
                            _ddw_config["game_filters"] = nh["game_filters"]
                        if nh.get("line_filters"):
                            _ddw_config["line_filters"] = nh["line_filters"]
                        await self.hypothesis_manager.create_hypothesis(
                            name=nh.get("name", "deferred_deep"),
                            thesis=nh.get("thesis", ""),
                            sport=nh.get("sport", "basketball_nba"),
                            market_type=nh.get("market_type", "spreads"),
                            edge_threshold=nh.get("edge_threshold", 0.015),
                            model_config=_ddw_config,
                        )
                        created += 1
                    except Exception:
                        pass
                if rejected or created:
                    self._hypotheses_generated += created
                    logger.info(
                        f"Deferred drain deep_work: rejected {rejected}, created {created}"
                    )

                # Route pipeline_issues to self-repair (same as _phase_claude_deep_work)
                pipeline_issues = parsed.get("pipeline_issues", [])
                if pipeline_issues:
                    findings = []
                    for issue in pipeline_issues:
                        issue_lower = issue.lower() if isinstance(issue, str) else ""
                        if any(kw in issue_lower for kw in ["identical", "same games", "filtering bug", "broken"]):
                            severity = "CRITICAL"
                        elif any(kw in issue_lower for kw in ["prioritize", "threshold", "zero promotion", "low sample"]):
                            severity = "HIGH"
                        else:
                            severity = "LOW"
                        findings.append({"severity": severity, "description": issue})
                    try:
                        from tools.self_repair import get_repair_engine
                        engine = get_repair_engine()
                        repair_results = await engine.handle_claude_findings(findings)
                        for r in repair_results:
                            if r["fixed"]:
                                logger.info(f"Deferred deep work auto-fix: {r['action']} — {r['detail']}")
                            else:
                                logger.warning(f"Deferred deep work unfixed: {r['action']} — {r['detail']}")
                    except Exception as e:
                        logger.warning(f"Deferred drain: failed to route findings to self-repair: {e}")

            elif work_type == "interpret_backtests":
                # Same processing as _phase_interpret_backtests
                db = self.data_collector._db
                rejected = 0
                for hid in parsed.get("reject", []):
                    try:
                        await self.hypothesis_manager.update_status(
                            hid, "rejected", "deferred_interpret"
                        )
                        rejected += 1
                        self._rejections += 1
                    except Exception:
                        pass
                modified = 0
                for mod in parsed.get("modify", []):
                    try:
                        hid = mod.get("id")
                        new_thresh = mod.get("new_threshold")
                        if hid and new_thresh is not None and db:
                            await db.execute(
                                "UPDATE hypotheses SET edge_threshold = ? WHERE hypothesis_id = ?",
                                (new_thresh, hid),
                            )
                            await db.commit()
                            modified += 1
                    except Exception:
                        pass
                if rejected or modified:
                    logger.info(
                        f"Deferred drain interpret: rejected {rejected}, modified {modified}"
                    )

            elif work_type == "system_improvement":
                db = self.data_collector._db
                stored = 0
                for imp in parsed.get("improvements", []):
                    try:
                        if db:
                            await db.execute(
                                "INSERT INTO system_improvements "
                                "(cycle, category, suggestion, priority) VALUES (?, ?, ?, ?)",
                                (self._cycles, imp.get("category", "general"),
                                 imp.get("suggestion", ""), imp.get("priority", "medium")),
                            )
                            stored += 1
                    except Exception:
                        pass
                if stored and db:
                    await db.commit()
                    logger.info(f"Deferred drain: stored {stored} system improvements")

            elif work_type == "diagnostic_escalation":
                logger.info(f"Deferred diagnostic processed: {content[:200]}")

        except (json.JSONDecodeError, ValueError) as e:
            logger.debug(f"Deferred item {work_type} response not valid JSON: {e}")

    async def _quant_scan_loop(self) -> None:
        """Continuously refresh the live edge surface.

        Every ``QUANT_SCAN_INTERVAL_S`` seconds, pull current odds for
        every research sport, build per-market snapshots across all
        available books, run the ranker, and persist the output. The
        resulting table (``live_edge_surface``) is what the /edges/live
        API endpoint reads, what the Telegram alerting can consume, and
        what the bet_executor will read once it's enabled.

        Runs independently of the main research cycle so the two
        cadences don't fight each other. Research cycle is human-scale
        (5 min, statistical work). Quant scan is market-scale (60s,
        line movement and soft-book divergence).
        """
        import os as _os
        interval = float(_os.getenv("CALLISTO_QUANT_SCAN_INTERVAL_S", "60"))
        # Brief startup delay so the main loop wins initial DB contention
        # and telemetry collectors have a chance to populate.
        await asyncio.sleep(30)

        from tools.quant import scan_all_sports
        while self._running:
            if self._paused:
                await asyncio.sleep(min(interval, 15))
                continue
            try:
                db = self.data_collector._db if self.data_collector else None
                if db is None:
                    await asyncio.sleep(interval)
                    continue
                result = await scan_all_sports(
                    list(RESEARCH_SPORTS),
                    db,
                    placement_books={"draftkings", "fanatics"},
                    min_recommend_edge=0.02,
                    top_n_per_sport=25,
                )
                total = result.get("total_recommended", 0)
                if total:
                    logger.info(
                        f"Quant scan: {total} recommended edges across "
                        f"{sum(1 for r in result['per_sport'].values() if r.get('recommended'))} "
                        f"sports"
                    )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Quant scan loop iteration failed: {e}")
            await asyncio.sleep(interval)

    async def _loop(self) -> None:
        """Main research cycle."""
        # Brief delay to let other systems start
        await asyncio.sleep(15)

        while self._running:
            try:
                self._cycles += 1
                self._reactive_collected.clear()
                _cycle_start = time.monotonic()
                logger.info(f"Research cycle #{self._cycles} starting")

                # Pause check — sleep and skip cycle
                if self._paused:
                    logger.info(f"Research cycle #{self._cycles} skipped (PAUSED)")
                    await asyncio.sleep(RESEARCH_CYCLE_INTERVAL)
                    continue

                # ── Pause line_monitor for ENTIRE cycle to prevent SQLite lock cascade.
                # All phases do DB writes; concurrent line_monitor snapshots cause
                # deadlocks even with 120s busy_timeout. Snapshots catch up between cycles.
                # wait_for_drain() sets _paused, waits for loop ack AND in-flight DB
                # ops to complete — no more fire-and-forget WAL contention.
                if self.line_monitor:
                    drained = await self.line_monitor.wait_for_drain(timeout=30)
                    if drained:
                        logger.debug("line_monitor paused and drained for research cycle")
                    else:
                        logger.warning("line_monitor drain incomplete — proceeding (may contend on WAL)")

                # ── Queue drain: if Claude just became available, burn through deferred work ──
                try:
                    await asyncio.wait_for(self._drain_deferred_queue(), timeout=120)
                except asyncio.TimeoutError:
                    logger.warning("Queue drain timed out after 120s — skipping")
                except Exception as e:
                    logger.warning(f"Queue drain failed (non-fatal): {e}")

                # Phase 0: Self-repair (detect, fix, verify, record)
                try:
                    await asyncio.wait_for(self._phase_self_repair(), timeout=120)
                except asyncio.TimeoutError:
                    logger.warning("Phase self_repair timed out after 120s — skipping")
                except Exception as e:
                    logger.warning(f"Self-repair failed (non-fatal): {e}")

                if not self._running:
                    break

                # Phase 0a: Self-diagnose pipeline health
                try:
                    await asyncio.wait_for(self._phase_self_diagnose(), timeout=120)
                except asyncio.TimeoutError:
                    logger.warning("Phase self_diagnose timed out after 120s — skipping")
                except Exception as e:
                    logger.warning(f"Self-diagnose failed (non-fatal): {e}")

                if not self._running:
                    break

                # Phase 0b: Refresh signals (retroactive threshold updates)
                try:
                    await asyncio.wait_for(self._phase_refresh_signals(), timeout=120)
                except asyncio.TimeoutError:
                    logger.warning("Phase refresh_signals timed out after 120s — skipping")
                except Exception as e:
                    logger.warning(f"Signal refresh failed (non-fatal): {e}")

                if not self._running:
                    break

                # Phase 1: Backtest pending hypotheses (FIRST — highest priority)
                try:
                    await asyncio.wait_for(self._phase_backtest(), timeout=600)
                except asyncio.TimeoutError:
                    logger.warning("Phase backtest timed out after 600s — skipping")
                except Exception as e:
                    logger.warning(f"Phase backtest failed (non-fatal): {e}")

                if not self._running:
                    break

                # Phase 1b: Validate backtest output (sanity checks every cycle)
                try:
                    await self._phase_validate()
                except Exception as e:
                    logger.warning(f"Phase validate failed (non-fatal): {e}")

                if not self._running:
                    break

                # Phase 2: Generate hypotheses (if due)
                try:
                    await asyncio.wait_for(self._phase_generate_hypotheses(), timeout=300)
                except asyncio.TimeoutError:
                    logger.warning("Phase generate_hypotheses timed out after 300s — skipping")
                except Exception as e:
                    logger.warning(f"Phase generate_hypotheses failed (non-fatal): {e}")

                if not self._running:
                    break

                # Phase 2b: Injury-driven prop hypotheses (every 4 cycles — coprime with regime/integrity)
                if self._cycles % 4 == 0:
                    try:
                        await asyncio.wait_for(
                            self._phase_injury_prop_hypotheses(), timeout=120,
                        )
                    except asyncio.TimeoutError:
                        logger.warning("Phase injury_prop_hypotheses timed out — skipping")
                    except Exception as e:
                        logger.warning(f"Injury prop hypothesis phase failed (non-fatal): {e}")

                if not self._running:
                    break

                # Phase 3: Collect data (if due — runs every 5 min)
                try:
                    await asyncio.wait_for(self._phase_collect_data(), timeout=120)
                except asyncio.TimeoutError:
                    logger.warning("Phase collect_data timed out after 120s — skipping")
                except Exception as e:
                    logger.warning(f"Data collection failed (non-fatal): {e}")

                if not self._running:
                    break

                # Phase 4: Embed new data
                try:
                    await asyncio.wait_for(self._phase_embed_data(), timeout=120)
                except asyncio.TimeoutError:
                    logger.warning("Phase embed_data timed out after 120s — skipping")
                except Exception as e:
                    logger.warning(f"Embedding failed (non-fatal): {e}")

                if not self._running:
                    break

                # Phase 5: Evaluate and promote/reject
                try:
                    await asyncio.wait_for(self._phase_evaluate(), timeout=600)
                except asyncio.TimeoutError:
                    logger.warning("Phase evaluate timed out after 600s — skipping")
                except Exception as e:
                    logger.warning(f"Phase evaluate failed (non-fatal): {e}")

                if not self._running:
                    break

                # Phase 5b: Claude interprets backtest results (signal vs noise)
                try:
                    await asyncio.wait_for(self._phase_interpret_backtests(), timeout=300)
                except asyncio.TimeoutError:
                    logger.warning("Phase interpret_backtests timed out after 300s — skipping")
                except Exception as e:
                    logger.warning(f"Phase interpret_backtests failed (non-fatal): {e}")

                if not self._running:
                    break

                # Phase 6: Paper trade active hypotheses
                # 300s timeout — paper trading needs live odds fetches (DK scraper +
                # devig), and 120s was causing 100% timeout rate (0 paper trades ever)
                try:
                    await asyncio.wait_for(self._phase_paper_trade(), timeout=300)
                except asyncio.TimeoutError:
                    logger.warning("Phase paper_trade timed out after 300s — skipping")
                except Exception as e:
                    logger.warning(f"Phase paper_trade failed (non-fatal): {e}")

                if not self._running:
                    break

                # Phase 6b: Execute live bets on proven hypotheses
                try:
                    await asyncio.wait_for(self._phase_live_execute(), timeout=120)
                except asyncio.TimeoutError:
                    logger.warning("Phase live_execute timed out after 120s — skipping")
                except Exception as e:
                    logger.warning(f"Phase live_execute failed (non-fatal): {e}")

                if not self._running:
                    break

                # Phase 6c: Narrative edge detection (milestones, role changes, revenge)
                try:
                    await asyncio.wait_for(self._phase_narrative_edges(), timeout=120)
                except asyncio.TimeoutError:
                    logger.warning("Phase narrative_edges timed out after 120s — skipping")
                except Exception as e:
                    logger.warning(f"Phase narrative_edges failed (non-fatal): {e}")

                if not self._running:
                    break

                # Phase 7: Claude deep analysis — use remaining budget
                try:
                    await asyncio.wait_for(self._phase_claude_deep_work(), timeout=300)
                except asyncio.TimeoutError:
                    logger.warning("Phase claude_deep_work timed out after 300s — skipping")
                except Exception as e:
                    logger.warning(f"Phase claude_deep_work failed (non-fatal): {e}")

                if not self._running:
                    break

                # ── Periodic phases: defer if core phases already consumed >5 min ──
                # This prevents phase collision from stacking 10+ min cycles
                # (was causing stalls at cycles 6, 10, 15, 16, 20).
                _cycle_elapsed = time.monotonic() - _cycle_start
                _CYCLE_TIME_BUDGET = 300  # 5 min — if core phases took this long, skip periodic
                if _cycle_elapsed > _CYCLE_TIME_BUDGET:
                    logger.info(
                        f"Cycle #{self._cycles} core phases took {_cycle_elapsed:.0f}s "
                        f"(>{_CYCLE_TIME_BUDGET}s) — deferring periodic phases"
                    )
                else:
                    # Phase 8: System self-improvement (every N cycles)
                    try:
                        await asyncio.wait_for(self._phase_system_improvement(), timeout=120)
                    except asyncio.TimeoutError:
                        logger.warning("Phase system_improvement timed out after 120s — skipping")
                    except Exception as e:
                        logger.warning(f"Phase system_improvement failed (non-fatal): {e}")

                    if not self._running:
                        break

                    # Phase 9: Pipeline integrity check (every N cycles)
                    try:
                        await asyncio.wait_for(self._phase_integrity_check(), timeout=120)
                    except asyncio.TimeoutError:
                        logger.warning("Phase integrity_check timed out after 120s — skipping")
                    except Exception as e:
                        logger.warning(f"Phase integrity_check failed (non-fatal): {e}")

                    # Phase 10: System watchdog (every 13 cycles — coprime with regime/improvement)
                    if self._cycles % 13 == 0 and self._cycles > 0:
                        try:
                            await asyncio.wait_for(self._phase_system_watchdog(), timeout=60)
                        except asyncio.TimeoutError:
                            logger.warning("Phase system_watchdog timed out")
                        except Exception as e:
                            logger.warning(f"Phase system_watchdog failed: {e}")

                    if not self._running:
                        break

                    # Phase 10b: Granger temporal prediction — identify sharp book leaders (weekly)
                    try:
                        await asyncio.wait_for(self._phase_granger_analysis(), timeout=300)
                    except asyncio.TimeoutError:
                        logger.warning("Phase granger_analysis timed out after 300s — skipping")
                    except Exception as e:
                        logger.warning(f"Phase granger_analysis failed (non-fatal): {e}")

                    if not self._running:
                        break

                    # Phase 11: Regime analysis — detect regime changes, recency bias, mean reversion
                    try:
                        await asyncio.wait_for(self._phase_regime_analysis(), timeout=180)
                    except asyncio.TimeoutError:
                        logger.warning("Phase regime_analysis timed out after 180s — skipping")
                    except Exception as e:
                        logger.warning(f"Phase regime_analysis failed (non-fatal): {e}")

                    # Phase 12: Knowledge wiki compilation (every 7 cycles)
                    try:
                        await asyncio.wait_for(self._phase_knowledge_compile(), timeout=180)
                    except asyncio.TimeoutError:
                        logger.warning("Phase knowledge_compile timed out after 180s — skipping")
                    except Exception as e:
                        logger.warning(f"Phase knowledge_compile failed (non-fatal): {e}")

                    # Phase 13: Knowledge wiki lint (every 11 cycles — coprime with compile)
                    try:
                        await asyncio.wait_for(self._phase_knowledge_lint(), timeout=120)
                    except asyncio.TimeoutError:
                        logger.warning("Phase knowledge_lint timed out after 120s — skipping")
                    except Exception as e:
                        logger.warning(f"Phase knowledge_lint failed (non-fatal): {e}")

                # ── Progress tracking: detect spinning ──
                await self._check_progress()

                _cycle_total = time.monotonic() - _cycle_start

                # Force garbage collection after each cycle — large numpy arrays
                # and JSON dicts from backtest processing don't always get freed promptly.
                # Also clear linecache (tracemalloc causes it to grow ~1.5 MB/session).
                gc.collect()
                gc.collect()  # Second pass catches reference cycles
                import linecache
                linecache.clearcache()

                # ── Memory telemetry: track RSS per cycle to detect leaks ──
                try:
                    import psutil
                    _rss_mb = psutil.Process().memory_info().rss / (1024 * 1024)
                    logger.info(
                        f"Research cycle #{self._cycles} completed in {_cycle_total:.0f}s | "
                        f"RSS={_rss_mb:.0f}MB | KL_cache={len(self.line_monitor._kl_cache) if self.line_monitor else '?'}"
                    )
                except Exception:
                    logger.info(f"Research cycle #{self._cycles} completed in {_cycle_total:.0f}s")

                # Proactive DB prune — prop_snapshots grows 15K rows/hr,
                # backtest_events from rejected hypotheses bloat DB indefinitely
                try:
                    import aiosqlite
                    _prune_db = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")
                    _prune_cutoff = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
                    async with aiosqlite.connect(_prune_db) as _pdb:
                        await _pdb.execute("PRAGMA busy_timeout = 60000")
                        await _pdb.execute(
                            "DELETE FROM prop_snapshots WHERE snapshot_time < ?",
                            (_prune_cutoff,)
                        )
                        await _pdb.execute(
                            "DELETE FROM deferred_work_queue WHERE status = 'done' AND created_at < ?",
                            (_prune_cutoff,)
                        )
                        # Prune backtest_events for rejected hypotheses (>2 days old)
                        # With 3192 rejected hyps, this recovers massive DB space
                        _pruned = await _pdb.execute(
                            "DELETE FROM backtest_events WHERE hypothesis_id IN ("
                            "  SELECT hypothesis_id FROM hypotheses "
                            "  WHERE status = 'rejected' AND updated_at < ?"
                            ")",
                            (_prune_cutoff,)
                        )
                        _pruned_count = _pruned.rowcount
                        # Also prune backtest_runs for rejected hypotheses
                        await _pdb.execute(
                            "DELETE FROM backtest_runs WHERE hypothesis_id IN ("
                            "  SELECT hypothesis_id FROM hypotheses "
                            "  WHERE status = 'rejected' AND updated_at < ?"
                            ")",
                            (_prune_cutoff,)
                        )
                        await _pdb.commit()
                        if _pruned_count > 0:
                            logger.info(
                                f"DB prune: deleted {_pruned_count} backtest_events "
                                f"from rejected hypotheses"
                            )
                        # WAL checkpoint — prevents unbounded WAL growth (was 1.4GB).
                        # Persistent connections block wal_autocheckpoint; this fresh
                        # connection after commit can checkpoint freed pages.
                        try:
                            wal_result = await (await _pdb.execute(
                                "PRAGMA wal_checkpoint(TRUNCATE)"
                            )).fetchone()
                            if wal_result:
                                busy, log, ckpt = wal_result
                                if log > 0:
                                    logger.info(
                                        f"WAL checkpoint: {ckpt}/{log} pages "
                                        f"(busy={busy})"
                                    )
                        except Exception as wal_e:
                            logger.debug(f"WAL checkpoint: {wal_e}")
                except Exception:
                    pass  # Non-critical — self_repair will catch it

                # Force GC to reclaim large transient allocations from backtest/resolve
                # phases. CPython's pymalloc holds freed blocks; gc.collect() nudges
                # the allocator to release pages back to the OS.
                gc.collect()

                # ── Unpause line_monitor BEFORE sleeping so it can take snapshots
                # during the inter-cycle window. Previously this was in the finally
                # block which ran after the sleep, giving the monitor ~0ms to run.
                if self.line_monitor:
                    self.line_monitor.resume()  # Releases snapshot lock atomically
                    self.line_monitor._pause_ack.clear()
                    logger.info("line_monitor unpaused for inter-cycle snapshot window")

                logger.info(
                    f"Research cycle #{self._cycles} complete — "
                    f"sleeping {RESEARCH_CYCLE_INTERVAL}s"
                )
                await asyncio.sleep(RESEARCH_CYCLE_INTERVAL)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Research loop error: {e}", exc_info=True)
                await asyncio.sleep(120)
            finally:
                # ── Safety net: always unpause on exception/cancel too ──
                if self.line_monitor:
                    self.line_monitor.resume()  # Releases snapshot lock if held
                    self.line_monitor._pause_ack.clear()

    async def _phase_self_repair(self) -> None:
        """
        Self-repair phase — detect issues, fix them autonomously, verify,
        and record to Hermes. Runs every 5 cycles to avoid overhead.
        Also runs cache rotation to maintain operational hygiene.
        """
        if self._cycles % 5 != 1:
            return  # Only run every 5 cycles (cycle 1, 6, 11, ...)

        # Cache rotation — rebuild hot cache, archive stale data
        try:
            from tools.cache_manager import rotate_caches
            await rotate_caches()
        except Exception as e:
            logger.debug(f"Cache rotation failed (non-fatal): {e}")

        try:
            from tools.self_repair import get_repair_engine
            engine = get_repair_engine()
            result = await engine.run_repair_cycle()

            if result["fixed"] > 0:
                logger.info(
                    f"Self-repair: fixed {result['fixed']}/{result['issues_found']} issues"
                )

            # Record phase success for pipeline integrity tracking
            try:
                from tools.pipeline_integrity import get_checker
                get_checker().record_phase_result("self_repair", True)
            except Exception:
                pass

        except Exception as e:
            logger.error(f"Self-repair phase failed: {e}", exc_info=True)
            try:
                from tools.pipeline_integrity import get_checker
                get_checker().record_phase_result("self_repair", False)
            except Exception:
                pass

    async def _phase_self_diagnose(self) -> None:
        """
        Self-diagnostic phase — detects broken pipelines BEFORE wasting cycles.

        Checks data quality, pipeline throughput, and data freshness.
        Escalates critical issues to Claude Code exactly once per issue.
        """
        from datetime import datetime, timedelta, timezone

        db = self.data_collector._db
        if db is None:
            logger.warning("DIAG: data_collector DB not initialized, skipping diagnostics")
            return

        issues: list[dict] = []  # {"key": str, "severity": str, "message": str}

        # ── 1. Data quality: avg books per record per sport ──
        try:
            cursor = await db.execute(
                "SELECT sport, COUNT(*) as cnt "
                "FROM historical_odds_cache GROUP BY sport"
            )
            rows = await cursor.fetchall()
            for row in rows:
                sport, cnt = row[0], row[1]
                # Sample up to 50 records to estimate avg books
                sample_cursor = await db.execute(
                    "SELECT response_json FROM historical_odds_cache "
                    "WHERE sport = ? ORDER BY RANDOM() LIMIT 50",
                    (sport,),
                )
                samples = await sample_cursor.fetchall()
                total_books = 0
                parsed = 0
                scores_only = 0
                usable = 0
                for (rj,) in samples:
                    try:
                        data = json.loads(rj) if isinstance(rj, str) else rj
                        # Cached format: {"games": [...], "sport": "...", ...}
                        # Each game has a "bookmakers" list
                        games = []
                        if isinstance(data, dict) and "games" in data:
                            games = data["games"]
                        elif isinstance(data, list):
                            games = data
                        elif isinstance(data, dict) and "bookmakers" in data:
                            games = [data]

                        record_books = 0
                        for game in games:
                            bm_count = len(game.get("bookmakers", []))
                            total_books += bm_count
                            record_books = max(record_books, bm_count)
                            parsed += 1
                        if record_books == 0:
                            scores_only += 1
                        elif record_books >= 2:
                            usable += 1
                    except (json.JSONDecodeError, TypeError):
                        continue
                avg_books = total_books / max(parsed, 1)
                if scores_only > 0 and usable == 0:
                    issue_key = f"scores_only_{sport}"
                    msg = (
                        f"DIAG: {sport} has {cnt} cached records but ALL are "
                        f"scores-only (0 bookmakers) — no odds data for backtesting"
                    )
                    logger.warning(msg)
                    issues.append({"key": issue_key, "severity": "CRITICAL", "message": msg})
                elif avg_books < 2:
                    issue_key = f"low_books_{sport}"
                    msg = (
                        f"DIAG: {sport} has avg {avg_books:.1f} books/game "
                        f"({cnt} records, {usable}/{len(samples)} usable) — "
                        f"backtests against <2 books are unreliable"
                    )
                    logger.warning(msg)
                    issues.append({"key": issue_key, "severity": "WARNING", "message": msg})
                else:
                    logger.info(
                        f"DIAG: {sport} data quality OK — avg {avg_books:.1f} books/game "
                        f"({cnt} records, {usable}/{len(samples)} usable)"
                    )
        except Exception as e:
            logger.warning(f"DIAG: data quality check failed: {e}")

        # ── 1b. Check game_results overlap with odds data ──
        try:
            cursor = await db.execute(
                "SELECT h.sport, COUNT(DISTINCT h.snapshot_date) as odds_dates, "
                "COUNT(DISTINCT g.game_date) as result_dates "
                "FROM historical_odds_cache h "
                "LEFT JOIN game_results g ON h.sport = g.sport "
                "AND h.snapshot_date = g.game_date "
                "GROUP BY h.sport"
            )
            for row in await cursor.fetchall():
                sport, odds_dates, result_dates = row[0], row[1], row[2]
                if odds_dates > 0 and result_dates == 0:
                    issue_key = f"no_results_overlap_{sport}"
                    msg = (
                        f"DIAG: {sport} has {odds_dates} odds dates but 0 matching "
                        f"game_results dates — backtest resolution will fail"
                    )
                    logger.warning(msg)
                    issues.append({"key": issue_key, "severity": "WARNING", "message": msg})
        except Exception as e:
            logger.warning(f"DIAG: date overlap check failed: {e}")

        # ── 2. Pipeline throughput ──
        try:
            if self._hypotheses_generated > 0 and self._backtests_run > 0:
                # Check total signals across all backtest events
                cursor = await db.execute(
                    "SELECT COUNT(DISTINCT event_id) as total, "
                    "COUNT(DISTINCT CASE WHEN signal_generated = 1 THEN event_id END) as signals "
                    "FROM backtest_events"
                )
                row = await cursor.fetchone()
                if row:
                    total_events, total_signals = row[0] or 0, row[1] or 0
                    if total_events > 0:
                        signal_rate = total_signals / total_events
                        if signal_rate < 0.01:
                            issue_key = "low_signal_rate"
                            msg = (
                                f"DIAG: signal rate {signal_rate:.2%} "
                                f"({total_signals}/{total_events} events) — "
                                f"<1% signal generation indicates broken hypothesis logic"
                            )
                            logger.warning(msg)
                            issues.append({"key": issue_key, "severity": "WARNING", "message": msg})

            if self._backtests_run >= 100 and self._promotions == 0:
                issue_key = "zero_promotions"
                msg = (
                    f"DIAG: 0 promotions after {self._backtests_run} backtests — "
                    f"promotion gates may be too strict or data insufficient"
                )
                logger.warning(msg)
                issues.append({"key": issue_key, "severity": "WARNING", "message": msg})
        except Exception as e:
            logger.warning(f"DIAG: throughput check failed: {e}")

        # ── 3. Data freshness ──
        try:
            now = datetime.now(timezone.utc)

            # Latest game_context
            cursor = await db.execute(
                "SELECT MAX(game_date) FROM game_contexts WHERE sport != 'meta_research'"
            )
            row = await cursor.fetchone()
            if row and row[0]:
                try:
                    latest_ctx = datetime.strptime(str(row[0]), "%Y-%m-%d").replace(
                        tzinfo=timezone.utc
                    )
                    age_days = (now - latest_ctx).days
                    if age_days > 2:
                        issue_key = "stale_game_contexts"
                        msg = (
                            f"DIAG: latest game_context is {age_days} days old "
                            f"({row[0]}) — data collection may be broken"
                        )
                        logger.warning(msg)
                        issues.append({"key": issue_key, "severity": "WARNING", "message": msg})
                except ValueError:
                    pass
            else:
                issue_key = "no_game_contexts"
                msg = "DIAG: no game_contexts found at all — data collection has never succeeded"
                logger.warning(msg)
                issues.append({"key": issue_key, "severity": "CRITICAL", "message": msg})

            # Latest odds_snapshot (from line_monitor's table)
            try:
                cursor = await db.execute(
                    "SELECT MAX(timestamp) FROM odds_snapshots"
                )
                row = await cursor.fetchone()
                if row and row[0]:
                    try:
                        latest_snap = datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))
                        if latest_snap.tzinfo is None:
                            latest_snap = latest_snap.replace(tzinfo=timezone.utc)
                        age_hours = (now - latest_snap).total_seconds() / 3600
                        if age_hours > 1:
                            issue_key = "stale_odds_snapshots"
                            msg = (
                                f"DIAG: latest odds_snapshot is {age_hours:.1f}h old — "
                                f"snapshot collection may be failing"
                            )
                            logger.warning(msg)
                            issues.append({"key": issue_key, "severity": "WARNING", "message": msg})
                    except (ValueError, TypeError):
                        pass
            except Exception as e:
                # odds_snapshots table may be in a different DB (line_monitor's)
                logger.info(f"DIAG: odds_snapshots freshness check skipped: {e}")

        except Exception as e:
            logger.warning(f"DIAG: freshness check failed: {e}")

        # ── 4. Escalate critical issues to Claude Code (once per issue) ──
        new_critical = [
            i for i in issues
            if i["severity"] == "CRITICAL" and i["key"] not in self._diagnostic_issues
        ]
        if new_critical:
            from tools.claude_code import claude_code_query

            now = time.time()
            if now - self._last_claude_call < CLAUDE_ESCALATION_COOLDOWN:
                logger.debug("DIAG: skipping Claude escalation — cooldown active")
            elif self._claude_ok():
                # Load error patterns for institutional memory
                _error_patterns = ""
                try:
                    with open("memory/error_patterns.md", "r") as f:
                        _error_patterns = f.read()[:1500]
                except Exception:
                    pass

                diag_report = (
                    "CALLISTO SELF-DIAGNOSTIC — CRITICAL ISSUES DETECTED\n\n"
                    + "\n".join(
                        f"[{i['severity']}] {i['message']}" for i in issues
                    )
                    + "\n\nPipeline state:\n"
                    f"- Cycles: {self._cycles}\n"
                    f"- Hypotheses generated: {self._hypotheses_generated}\n"
                    f"- Backtests run: {self._backtests_run}\n"
                    f"- Promotions: {self._promotions}\n"
                    f"- Rejections: {self._rejections}\n\n"
                    + (f"KNOWN ERROR PATTERNS (do not repeat):\n{_error_patterns}\n\n" if _error_patterns else "")
                    + f"Analyze these diagnostics and suggest specific fixes. "
                    f"Focus on: which data is missing, what to collect, "
                    f"and whether the pipeline should pause or adjust parameters."
                )
                try:
                    result = await claude_code_query(diag_report)
                    self._last_claude_call = time.time()
                    if result.get("content") and not result.get("error"):
                        logger.info(
                            f"DIAG: Claude analysis received — "
                            f"{len(result['content'])} chars"
                        )
                    # Mark all critical issues as escalated regardless of response
                    for i in new_critical:
                        self._diagnostic_issues.add(i["key"])
                except Exception as e:
                    logger.warning(f"DIAG: Claude escalation failed: {e}")
            else:
                # Defer diagnostic escalation to queue for when Claude returns
                diag_report = (
                    "CALLISTO SELF-DIAGNOSTIC — CRITICAL ISSUES DETECTED\n\n"
                    + "\n".join(
                        f"[{i['severity']}] {i['message']}" for i in issues
                    )
                    + "\n\nPipeline state:\n"
                    f"- Cycles: {self._cycles}\n"
                    f"- Hypotheses generated: {self._hypotheses_generated}\n"
                    f"- Backtests run: {self._backtests_run}\n"
                    f"- Promotions: {self._promotions}\n"
                    f"- Rejections: {self._rejections}\n\n"
                    f"Analyze these diagnostics and suggest specific fixes. "
                    f"Focus on: which data is missing, what to collect, "
                    f"and whether the pipeline should pause or adjust parameters."
                )
                await self._work_queue.enqueue("diagnostic_escalation", diag_report, priority=1)
                self._downtime_tracker.item_queued()
                logger.warning(
                    f"DIAG: {len(new_critical)} critical issues deferred to work queue "
                    f"(Claude unavailable)"
                )

        # Mark non-critical issues as seen too (no re-escalation)
        for i in issues:
            if i["severity"] != "CRITICAL":
                self._diagnostic_issues.add(i["key"])

        # Evict oldest entries if set exceeds cap (prevents unbounded growth)
        if len(self._diagnostic_issues) > self._DIAGNOSTIC_ISSUES_MAX:
            # Sets are unordered; drop arbitrary entries to get back under limit
            excess = len(self._diagnostic_issues) - self._DIAGNOSTIC_ISSUES_MAX
            for _ in range(excess):
                self._diagnostic_issues.pop()

        if not issues:
            logger.info("DIAG: all pipeline health checks passed")

    async def _phase_refresh_signals(self) -> None:
        """Retroactively update signal_generated when thresholds change.

        Claude deep work can lower edge_threshold on hypotheses AFTER backtests
        have already run and stored signal_generated=0. This phase catches those
        events and upgrades them to signal=1 so the pipeline sees them.
        """
        import aiosqlite

        db_path = self.backtest_engine.db_path
        try:
            async with aiosqlite.connect(db_path) as db:
                await db.execute("PRAGMA busy_timeout = 60000")
                # Find events where edge now exceeds threshold but signal=0
                updated = await db.execute(
                    """UPDATE backtest_events SET signal_generated = 1
                       WHERE id IN (
                           SELECT be.id FROM backtest_events be
                           JOIN hypotheses h ON be.hypothesis_id = h.hypothesis_id
                           WHERE be.edge >= h.edge_threshold AND be.edge > 0
                           AND be.signal_generated = 0
                       )"""
                )
                if updated.rowcount > 0:
                    await db.commit()
                    logger.info(
                        f"Signal refresh: upgraded {updated.rowcount} events "
                        f"to signal=1 (threshold lowered after backtest)"
                    )
                    # Sync backtest_runs.signals_generated from backtest_events
                    # so monitoring/display data reflects retroactive updates.
                    await db.execute(
                        """UPDATE backtest_runs SET signals_generated = (
                               SELECT COUNT(DISTINCT event_id)
                               FROM backtest_events
                               WHERE backtest_events.run_id = backtest_runs.run_id
                               AND signal_generated = 1
                           )
                           WHERE run_id IN (
                               SELECT DISTINCT run_id FROM backtest_events
                               WHERE signal_generated = 1
                           )"""
                    )
                    await db.commit()
                    # Recalculate full stats for affected runs
                    affected_runs = await db.execute(
                        "SELECT DISTINCT run_id FROM backtest_events "
                        "WHERE signal_generated = 1"
                    )
                    run_ids = [r[0] for r in await affected_runs.fetchall()]
                    for rid in run_ids:
                        try:
                            await self.backtest_engine.recalculate_run_stats(rid)
                        except Exception as rc_e:
                            logger.warning(f"Signal refresh: recalculate_run_stats({rid[:8]}) failed: {rc_e}")
        except Exception as e:
            logger.warning(f"Signal refresh failed: {e}")

    async def _phase_collect_data(self) -> None:
        """Collect post-game data from ESPN (free).

        Normal cadence: last 7 days every DATA_COLLECTION_INTERVAL.
        Bulk backfill: if game_contexts < 100, one-time 30-day pull to seed the system.
        """
        from datetime import datetime, timedelta, timezone

        now = time.time()
        if now - self._last_data_collect < DATA_COLLECTION_INTERVAL:
            return

        self._last_data_collect = now

        # Determine how far back to collect
        # First collection: 7-day window. Subsequent: 2-day window (today + yesterday)
        lookback_days = 7 if self._data_collections == 0 else 2

        # One-time bulk backfill when data is thin
        if not self._bulk_backfill_done:
            try:
                stats = await self.data_collector.get_collection_stats()
                total_contexts = sum(
                    row.get("count", 0)
                    for row in stats.get("game_contexts", [])
                )
                if total_contexts < 100:
                    lookback_days = 30
                    logger.info(
                        f"Research: bulk backfill triggered — only {total_contexts} "
                        f"game contexts, collecting last 30 days"
                    )
                else:
                    logger.info(
                        f"Research: {total_contexts} game contexts already present, "
                        f"skipping bulk backfill"
                    )
            except Exception as e:
                logger.warning(f"Could not check collection stats for backfill: {e}")
            self._bulk_backfill_done = True

            # Also trigger historical odds backfill from odds-api.io Pro
            try:
                from tools.odds_api_io import get_usage_status as _io_usage
                usage = _io_usage()
                remaining = usage.get("remaining", 0)
                if remaining > 1000:
                    logger.info(
                        f"Research: triggering historical odds backfill "
                        f"(odds-api.io budget: {remaining} remaining)"
                    )
                    # Use the HistoricalOddsFetcher to backfill all core sports
                    from api import historical_fetcher as _hf
                    if _hf:
                        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                        thirty_ago = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
                        backfill_sports = [
                            "basketball_nba", "icehockey_nhl",
                            "americanfootball_nfl", "baseball_mlb",
                            "basketball_ncaab",
                        ]
                        for bs in backfill_sports:
                            if not self._running:
                                break
                            try:
                                result = await _hf.bulk_fetch_date_range(
                                    sport=bs,
                                    start_date=thirty_ago,
                                    end_date=today_str,
                                )
                                fetched = result.get("dates_fetched", 0)
                                cached = result.get("dates_cached_already", 0)
                                if fetched > 0:
                                    logger.info(
                                        f"Historical backfill {bs}: "
                                        f"{fetched} new dates, {cached} cached"
                                    )
                            except Exception as e:
                                logger.debug(f"Historical backfill {bs}: {e}")
                else:
                    logger.info(
                        f"Research: skipping historical backfill — "
                        f"odds-api.io budget low ({remaining})"
                    )
            except Exception as e:
                logger.debug(f"Historical odds backfill: {e}")

        logger.info(f"Research: collecting post-game data (last {lookback_days} days)")

        today = datetime.now(timezone.utc)
        dates = [today - timedelta(days=d) for d in range(lookback_days)]

        for sport in RESEARCH_SPORTS:
            try:
                for dt in dates:
                    date_str = dt.strftime("%Y%m%d")
                    scores = await self.data_collector.collect_scores(sport, date_str)
                    if scores.get("completed", 0) > 0:
                        await self.data_collector.collect_box_scores(sport, date_str)
                        # Enrich with play-by-play and win probability data
                        await self.data_collector.collect_play_by_play(sport, date_str)

                # Resolve pending paper trades for the same window
                for dt in dates:
                    date_fmt = dt.strftime("%Y-%m-%d")
                    await self.data_collector.resolve_prop_outcomes(sport, date_fmt)
                    await self.data_collector.resolve_game_level_outcomes(sport, date_fmt)

                # Update learned correlations from completed game data
                try:
                    from tools.correlation import get_learned_store
                    lcs = get_learned_store()
                    if lcs is not None and self.data_collector._db is not None:
                        for dt in dates:
                            date_fmt = dt.strftime("%Y-%m-%d")
                            await lcs.update_from_game_data(
                                self.data_collector._db, sport, date_fmt,
                            )
                except Exception as e:
                    logger.debug(f"Learned correlation update failed for {sport}: {e}")

                # TCI enrichment for women's basketball (identity/cohesion thesis)
                if sport in ("basketball_ncaaw", "basketball_wnba"):
                    try:
                        from tools.tci_scraper import build_tci_for_tournament
                        tci_data = await build_tci_for_tournament(sport=sport)
                        if tci_data:
                            db = self.data_collector._db
                            for team_name, tci in tci_data.items():
                                await db.execute(
                                    "INSERT OR REPLACE INTO tci_scores "
                                    "(team, sport, tci_score, task_cohesion, social_cohesion, "
                                    "experience_ratio, coaching_stability, computed_at) "
                                    "VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))",
                                    (
                                        team_name, sport,
                                        tci.get("tci_score", 0),
                                        tci.get("task_cohesion", 0),
                                        tci.get("social_cohesion", 0),
                                        tci.get("experience_ratio", 0),
                                        tci.get("coaching_stability", 0),
                                    ),
                                )
                            await db.commit()
                            logger.info(f"TCI: enriched {len(tci_data)} teams for {sport}")
                    except Exception as e:
                        logger.debug(f"TCI enrichment failed for {sport}: {e}")

                self._data_collections += 1
            except Exception as e:
                logger.warning(f"Data collection failed for {sport}: {e}")

        # Statcast pitch-level data for MLB (free from Baseball Savant).
        # Each call stores the full pitch timeline in statcast_pitches
        # (one row per pitch, 40 fields of physics + location + outcome).
        if "baseball_mlb" in RESEARCH_SPORTS:
            try:
                for dt in dates[:3]:  # Last 3 days only (Statcast is dense)
                    date_fmt = dt.strftime("%Y-%m-%d")
                    await self.data_collector.collect_statcast(date_fmt)
            except Exception as e:
                logger.warning(f"Statcast collection failed: {e}")

            # MLB player metadata (height, weight, bats, throws, debut, team).
            # Refresh at most once per day — roster moves are sparse, and the
            # endpoint takes ~30 HTTP calls. Anchored on a module-level ts.
            try:
                import time as _t
                last = getattr(self, "_last_mlb_player_refresh", 0.0)
                if _t.time() - last > 86400:  # 24h
                    await self.data_collector.collect_mlb_players()
                    self._last_mlb_player_refresh = _t.time()
            except Exception as e:
                logger.warning(f"MLB player metadata refresh failed: {e}")

        # ── NHL: shot-level play-by-play + player metadata ──
        # Per-shot events land in nhl_shot_events (coords, shot type, situation,
        # shooter/goalie); player metadata lands in nhl_players (height,
        # weight, shoots, position, birth, draft). Free api-web.nhle.com.
        if "icehockey_nhl" in RESEARCH_SPORTS:
            try:
                for dt in dates[:3]:
                    await self.data_collector.collect_nhl_shots(dt.strftime("%Y-%m-%d"))
            except Exception as e:
                logger.warning(f"NHL shot collection failed: {e}")
            try:
                import time as _t
                last = getattr(self, "_last_nhl_player_refresh", 0.0)
                if _t.time() - last > 86400:
                    await self.data_collector.collect_nhl_players()
                    self._last_nhl_player_refresh = _t.time()
            except Exception as e:
                logger.warning(f"NHL player metadata refresh failed: {e}")

        # ── NFL: play-by-play + roster + combine ──
        # Per-season CSV fetches from nflverse. Season-active cadence: PBP
        # refreshes daily during season (new plays land as weekly games
        # complete); rosters refresh daily; combine is yearly so we gate on
        # 7d cadence to stay polite to GitHub.
        if "americanfootball_nfl" in RESEARCH_SPORTS:
            try:
                import time as _t
                last_pbp = getattr(self, "_last_nfl_pbp_refresh", 0.0)
                if _t.time() - last_pbp > 86400:
                    await self.data_collector.collect_nfl_plays()
                    self._last_nfl_pbp_refresh = _t.time()
            except Exception as e:
                logger.warning(f"NFL PBP collection failed: {e}")
            try:
                import time as _t
                last_roster = getattr(self, "_last_nfl_roster_refresh", 0.0)
                if _t.time() - last_roster > 86400:
                    await self.data_collector.collect_nfl_players()
                    self._last_nfl_roster_refresh = _t.time()
            except Exception as e:
                logger.warning(f"NFL roster refresh failed: {e}")
            try:
                import time as _t
                last_combine = getattr(self, "_last_nfl_combine_refresh", 0.0)
                if _t.time() - last_combine > 7 * 86400:
                    await self.data_collector.collect_nfl_combine()
                    self._last_nfl_combine_refresh = _t.time()
            except Exception as e:
                logger.warning(f"NFL combine refresh failed: {e}")

        # ── NBA: shot chart + player metadata ──
        # stats.nba.com throttles hard under burst load, so we pace with a
        # 0.6s inter-request delay inside the collector and only fetch the
        # last 3 days' shots. Player metadata refresh once per day.
        if "basketball_nba" in RESEARCH_SPORTS:
            try:
                for dt in dates[:3]:
                    await self.data_collector.collect_nba_shots(dt.strftime("%Y-%m-%d"))
            except Exception as e:
                logger.warning(f"NBA shot collection failed: {e}")
            try:
                import time as _t
                last = getattr(self, "_last_nba_player_refresh", 0.0)
                if _t.time() - last > 86400:
                    await self.data_collector.collect_nba_players()
                    self._last_nba_player_refresh = _t.time()
            except Exception as e:
                logger.warning(f"NBA player metadata refresh failed: {e}")

        # ── NCAA MBB + WBB: player metadata + per-game box stats ──
        for ncaa_sport in ("basketball_ncaab", "basketball_ncaaw"):
            if ncaa_sport not in RESEARCH_SPORTS:
                continue
            try:
                for dt in dates[:3]:
                    await self.data_collector.collect_ncaa_basketball_game_stats(
                        ncaa_sport, dt.strftime("%Y%m%d")
                    )
            except Exception as e:
                logger.warning(f"{ncaa_sport} box stats failed: {e}")
            try:
                import time as _t
                last_key = f"_last_{ncaa_sport}_player_refresh"
                last = getattr(self, last_key, 0.0)
                if _t.time() - last > 7 * 86400:  # rosters rarely change mid-season
                    await self.data_collector.collect_ncaa_basketball_players(ncaa_sport)
                    setattr(self, last_key, _t.time())
            except Exception as e:
                logger.warning(f"{ncaa_sport} roster refresh failed: {e}")

        # ── PGA GOLF: per-round strokes-gained + core stats ──
        if "golf_pga" in RESEARCH_SPORTS:
            try:
                import time as _t
                last = getattr(self, "_last_golf_rounds_refresh", 0.0)
                if _t.time() - last > 86400:
                    await self.data_collector.collect_golf_player_rounds()
                    self._last_golf_rounds_refresh = _t.time()
            except Exception as e:
                logger.warning(f"Golf rounds collection failed: {e}")

        # Collect pre-calculated value bets from Odds-API.io Pro
        # These are updated every 5 seconds with EV computed from consensus
        try:
            from tools.odds_api_io import get_value_bets
            for book in ["DraftKings", "Fanatics"]:
                vb = await get_value_bets(book)
                if vb.get("count", 0) > 0:
                    logger.info(
                        f"Research: {vb['count']} value bets from {book} "
                        f"(top EV: {max(b['ev_pct'] for b in vb['bets']):.1%})"
                    )
                    # Store in ev_opportunities table for edge scanner.
                    # NOTE 2026-04-18: column names map onto line_monitor's canonical
                    # schema (game_id/bookmaker/team/edge/detected_at). `source` is
                    # 'odds_api_io_pro' so downstream consumers can distinguish
                    # provider-fed value bets from on-box line-movement EV scans.
                    try:
                        db = self.data_collector._db
                        if db:
                            for bet in vb["bets"]:
                                if bet["ev_pct"] >= 0.01:  # Only store 1%+ EV
                                    await db.execute(
                                        "INSERT INTO ev_opportunities "
                                        "(detected_at, sport, game_id, team, market, "
                                        "bookmaker, edge, expected_value, source) "
                                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'odds_api_io_pro')",
                                        (
                                            bet.get("updated_at", ""),
                                            bet.get("sport", ""),
                                            bet.get("event_id", ""),
                                            bet.get("side", ""),
                                            bet.get("market", ""),
                                            bet.get("bookmaker", ""),
                                            bet.get("ev_pct", 0.0),
                                            bet.get("ev_pct", 0.0),
                                        ),
                                    )
                            await db.commit()
                    except Exception as e:
                        logger.debug(f"Value bet storage: {e}")
        except Exception as e:
            logger.warning(f"Value bets collection failed: {e}")

        # Collect pre-calculated arbitrage opportunities from Odds-API.io Pro
        try:
            from tools.odds_api_io import get_arbitrage_bets
            arb = await get_arbitrage_bets()
            if arb.get("count", 0) > 0:
                logger.info(
                    f"Research: {arb['count']} arbitrage opportunities found "
                    f"(guaranteed profit regardless of outcome)"
                )
                # Store for analysis — arbs indicate book disagreement.
                # Same canonical-schema mapping as value-bet path above; source
                # 'arbitrage' lets downstream consumers filter arb signals.
                try:
                    db = self.data_collector._db
                    if db:
                        for bet in arb.get("bets", []):
                            await db.execute(
                                "INSERT INTO ev_opportunities "
                                "(detected_at, sport, game_id, team, market, "
                                "bookmaker, edge, expected_value, source) "
                                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'arbitrage')",
                                (
                                    bet.get("updated_at", ""),
                                    bet.get("sport", ""),
                                    bet.get("event_id", ""),
                                    bet.get("side", "arb"),
                                    bet.get("market", ""),
                                    bet.get("bookmakers", "multi"),
                                    bet.get("profit_pct", 0),
                                    bet.get("profit_pct", 0),
                                ),
                            )
                        await db.commit()
                except Exception as e:
                    logger.debug(f"Arbitrage storage: {e}")
        except Exception as e:
            logger.debug(f"Arbitrage collection: {e}")

    async def _phase_embed_data(self) -> None:
        """Embed new game contexts into the vector store."""
        from tools.embeddings import embed_game_context

        contexts = await self.data_collector.get_unembedded_contexts(limit=50)
        if not contexts:
            return

        logger.info(f"Research: embedding {len(contexts)} game contexts")

        for ctx in contexts:
            try:
                await embed_game_context(
                    store=self.vector_store,
                    sport=ctx["sport"],
                    game_date=ctx["game_date"],
                    home_team=ctx["home_team"],
                    away_team=ctx["away_team"],
                    context=ctx["context"],
                )
                await self.data_collector.mark_embedded(ctx["id"])
            except Exception as e:
                logger.warning(f"Embedding failed for context {ctx['id']}: {e}")

    async def _phase_injury_prop_hypotheses(self) -> None:
        """Generate prop hypotheses from current injury data.

        When a key player is out, redistribute_usage() predicts which
        teammates absorb the production. This directly feeds into player
        prop edges: if Tatum is out, Jaylen Brown's usage increases and
        his over on points has value.

        Creates draft hypotheses for each high-confidence prop opportunity.
        """
        from tools.contextual_data import get_injuries as _get_injuries
        from tools.injury_model import redistribute_usage as _redistribute

        _sport_map = {
            "basketball_nba": "NBA",
            "americanfootball_nfl": "NFL",
            "baseball_mlb": "MLB",
        }

        active_sports = list(self.line_monitor._snapshots.keys()) if self.line_monitor else []
        if not active_sports:
            active_sports = ["basketball_nba"]

        total_created = 0
        for sport_key in active_sports:
            model_sport = _sport_map.get(sport_key)
            if not model_sport:
                continue

            try:
                inj_data = await _get_injuries(sport_key)
            except Exception as e:
                logger.warning(f"Injury fetch failed for {sport_key}: {e}")
                continue

            injuries = inj_data.get("injuries", [])
            # Only process players who are OUT (not questionable)
            out_players = [i for i in injuries if (i.get("status") or "").lower() == "out"]
            if not out_players:
                continue

            for inj in out_players[:10]:  # cap at 10 per sport
                player = inj.get("player", "")
                team = inj.get("team", "")
                position = inj.get("position", "")
                if not player or not team:
                    continue

                # Build minimal absent player stats from position heuristics
                absent_stats = {}
                if model_sport == "NBA":
                    # Default to a starter-level stat line; real data would be better
                    absent_stats = {"ppg": 18.0, "rpg": 5.0, "apg": 4.0, "usage_rate": 25.0}
                elif model_sport == "NFL":
                    absent_stats = {"role": position or "WR1"}

                try:
                    redist = _redistribute(
                        absent_player=player,
                        team_roster=[],  # empty roster triggers generic redistribution
                        sport=model_sport,
                        absent_player_stats=absent_stats or None,
                    )
                except Exception as e:
                    logger.debug(f"Redistribution failed for {player}: {e}")
                    continue

                if not redist:
                    continue

                # Create draft hypotheses for top beneficiaries
                for r in redist[:3]:
                    beneficiary = r.player if hasattr(r, "player") else "Unknown"
                    usage_inc = r.usage_increase if hasattr(r, "usage_increase") else 0
                    stat_chg = r.projected_stat_change if hasattr(r, "projected_stat_change") else {}

                    if usage_inc < 2.0:
                        continue  # too small to be actionable

                    hypo_name = (
                        f"injury_prop_{team}_{beneficiary}_{player}_out"
                    ).replace(" ", "_").lower()

                    # Check if hypothesis already exists
                    try:
                        existing_names = await self.hypothesis_manager.get_all_names()
                        if hypo_name in existing_names:
                            continue
                    except Exception:
                        pass

                    ppg_inc = stat_chg.get("ppg_increase", stat_chg.get("projected_ppg_increase", 0))
                    description = (
                        f"With {player} OUT for {team}, {beneficiary} absorbs "
                        f"+{usage_inc:.1f}% usage (projected +{ppg_inc:.1f} PPG). "
                        f"Player prop overs for {beneficiary} have value when "
                        f"{player} is confirmed out."
                    )

                    try:
                        await self.hypothesis_manager.create_hypothesis(
                            name=hypo_name,
                            description=description,
                            sport=sport_key,
                            tags=["injury", "prop", "usage_redistribution", "auto_generated"],
                        )
                        total_created += 1
                        logger.info(
                            f"Injury prop hypothesis created: {beneficiary} "
                            f"benefits from {player} OUT (+{usage_inc:.1f}% usage)"
                        )
                    except Exception as e:
                        logger.debug(f"Failed to create injury prop hypothesis: {e}")

        if total_created:
            logger.info(f"Injury prop phase: created {total_created} hypotheses")

    async def _phase_generate_hypotheses(self) -> None:
        """Generate new hypotheses — Claude Code PRIMARY, templates FALLBACK.

        Claude Code is the primary hypothesis generator. Every cycle where
        Claude is available, we ask it to generate hypotheses based on current
        pipeline state, data stats, and what hasn't been tried. Template
        generation is the fallback when Claude is rate-limited.
        """
        now = time.time()
        if now - self._last_hypothesis_gen < HYPOTHESIS_GEN_INTERVAL:
            return

        # When spinning, generate hypotheses biased toward TESTABLE patterns.
        # Previously this disabled generation entirely, creating a permanent
        # deadlock: all existing drafts exhausted → 0 testable → spinning →
        # generation disabled → no new testable drafts → spinning forever.
        spinning_mode = self._spinning_detected
        if spinning_mode:
            logger.info(
                "Research: generating hypotheses in SPINNING RECOVERY mode — "
                "biasing toward pure line-based patterns (no context factors)"
            )
        else:
            logger.info("Research: generating hypotheses (Claude-primary)")
        self._last_hypothesis_gen = now

        total_created = 0
        used_claude = False

        # ── Temporal isolation: compute training cutoff ──
        # All hypotheses generated this cycle train on data before the cutoff.
        # Backtests will only use data AFTER cutoff + gap.
        today = datetime.now(timezone.utc).date()
        training_cutoff = today - timedelta(days=DEFAULT_TRAINING_WINDOW_DAYS)
        training_period_start = "2023-01-01"  # earliest cached data

        # Try to use temporal_analysis module for walk-forward windows
        try:
            from tools.temporal_analysis import get_training_window
            window = get_training_window()
            if window:
                training_period_start = window.get("start", training_period_start)
                training_cutoff = datetime.strptime(
                    window.get("end", str(training_cutoff)), "%Y-%m-%d"
                ).date()
                logger.info(f"Research: using temporal_analysis window {training_period_start} to {training_cutoff}")
        except (ImportError, Exception) as e:
            logger.debug(f"Research: temporal_analysis not available, using default window: {e}")

        training_period_end = str(training_cutoff)
        forward_test_start = str(training_cutoff + timedelta(days=BACKTEST_GAP_DAYS))
        logger.info(
            f"Research: temporal isolation — train [{training_period_start} .. {training_period_end}], "
            f"forward-test from {forward_test_start}"
        )

        # ── PRIMARY: Claude Code hypothesis generation ──
        from tools.claude_code import claude_code_query

        if (now - self._last_claude_call > CLAUDE_ESCALATION_COOLDOWN
                and self._claude_ok()):
            try:
                # Gather context for Claude — use lightweight queries instead of loading all rows
                existing_names = list(await self.hypothesis_manager.get_all_names())
                draft_count = await self.hypothesis_manager.count_by_status("draft")
                active_count = await self.hypothesis_manager.count_by_status("backtesting", "paper_trading", "live")
                rejected_count = await self.hypothesis_manager.count_by_status("rejected")

                data_stats = await self.data_collector.get_collection_stats()

                # Get date ranges per sport from DB
                date_ranges = {}
                db = self.data_collector._db
                if db:
                    try:
                        cursor = await db.execute(
                            "SELECT sport, MIN(snapshot_date), MAX(snapshot_date), COUNT(*) "
                            "FROM historical_odds_cache GROUP BY sport"
                        )
                        for row in await cursor.fetchall():
                            date_ranges[row[0]] = {
                                "from": row[1], "to": row[2], "records": row[3]
                            }
                    except Exception as e:
                        logger.warning(f"Failed to query historical_odds_cache date ranges: {e}")


                # ── Filter sports by data availability ──
                game_counts_by_sport = {}
                if db:
                    try:
                        cursor = await db.execute(
                            "SELECT sport, COUNT(*) FROM game_contexts GROUP BY sport"
                        )
                        for row in await cursor.fetchall():
                            game_counts_by_sport[row[0]] = row[1]
                    except Exception:
                        pass  # Fall back to unfiltered if query fails

                # Gate on BOTH game count AND odds data availability
                sports_with_odds = {s for s, dr in date_ranges.items() if dr.get("records", 0) > 0}
                eligible_sports = [
                    s for s in RESEARCH_SPORTS
                    if game_counts_by_sport.get(s, 0) >= MIN_GAMES_FOR_HYPOTHESIS
                    and s in sports_with_odds
                ]
                ineligible_sports = []
                for s in RESEARCH_SPORTS:
                    gc = game_counts_by_sport.get(s, 0)
                    if gc < MIN_GAMES_FOR_HYPOTHESIS:
                        ineligible_sports.append(f"{s} ({gc} games)")
                    elif s not in sports_with_odds:
                        ineligible_sports.append(f"{s} ({gc} games, NO odds data)")
                if ineligible_sports:
                    logger.info(
                        f"Research: sports excluded from hypothesis gen "
                        f"(need >={MIN_GAMES_FOR_HYPOTHESIS} games AND odds data): {ineligible_sports}"
                    )

                # Build regime analysis context — highlight teams with actionable signals
                regime_context = ""
                if _regime_cache:
                    regime_lines = []
                    for cache_key, regime in _regime_cache.items():
                        if regime.get("has_edge_signal"):
                            signals = regime.get("actionable_signals", [])
                            team = regime.get("team", cache_key)
                            pr = regime.get("power_rating", {})
                            regime_label = pr.get("regime", "stable") if isinstance(pr, dict) else "stable"
                            recency = regime.get("recency_bias", {})
                            bias_dir = recency.get("bias_direction", "neutral") if isinstance(recency, dict) else "neutral"
                            bias_mag = recency.get("bias_magnitude", 0) if isinstance(recency, dict) else 0
                            mr = regime.get("mean_reversion", {})
                            mr_expected = mr.get("reversion_expected", False) if isinstance(mr, dict) else False
                            mr_z = mr.get("current_zscore", 0) if isinstance(mr, dict) else 0
                            regime_lines.append(
                                f"  {team}: regime={regime_label}, "
                                f"bias={bias_dir}({bias_mag:.2f}), "
                                f"mean_reversion={'yes' if mr_expected else 'no'}(z={mr_z:.1f}), "
                                f"signals={signals}"
                            )
                    if regime_lines:
                        regime_context = (
                            "REGIME ANALYSIS (teams with actionable signals — "
                            "prioritize hypotheses around these):\n"
                            + "\n".join(regime_lines[:20]) + "\n\n"
                        )

                # Build correlation context — strongest market pairs per focus sport
                correlation_context = ""
                try:
                    from tools.correlation import list_correlated_markets
                    corr_lines = []
                    sports_to_check = RESEARCH_SPORTS[:4]
                    key_markets = [
                        "team_total", "game_total", "team_spread", "player_points",
                    ]
                    for fs in sports_to_check:
                        sport_pairs = []
                        for km in key_markets:
                            related = list_correlated_markets(km, fs, min_abs_rho=0.35)
                            for r in related[:3]:
                                pair_str = (
                                    f"{km}<->{r['market']}"
                                    f"(rho={r['correlation']:.2f})"
                                )
                                if pair_str not in sport_pairs:
                                    sport_pairs.append(pair_str)
                        if sport_pairs:
                            corr_lines.append(
                                f"  {fs}: {', '.join(sport_pairs[:6])}"
                            )
                    if corr_lines:
                        correlation_context = (
                            "CROSS-MARKET CORRELATIONS (strongest pairs — "
                            "use for SGP/parlay hypotheses):\n"
                            + "\n".join(corr_lines) + "\n\n"
                        )
                except Exception as e:
                    logger.debug(f"Correlation context generation failed: {e}")

                spinning_preamble = ""
                if spinning_mode:
                    spinning_preamble = (
                        "** SPINNING RECOVERY MODE **\n"
                        "The research loop has been spinning with 0 progress. "
                        "ALL existing 2000+ drafts are untestable — they require context "
                        "factors (weather, pitcher, travel, venue, etc.) that can't be filtered.\n"
                        "You MUST generate hypotheses that are PURELY LINE-BASED — "
                        "using ONLY game_filters and line_filters from the AVAILABLE FILTERS list below.\n"
                        "DO NOT reference weather, pitchers, venue type, travel, altitude, "
                        "bullpen, spring training, roster changes, or any factor not in the filters list.\n"
                        "Focus on: schedule spots (B2B, rest mismatch, road streaks), "
                        "win% ranges, spread ranges, underdog/favorite dynamics, "
                        "and cross-book consensus divergence.\n\n"
                    )

                prompt = (
                    f"CALLISTO HYPOTHESIS GENERATION — Cycle #{self._cycles}\n\n"
                    f"{spinning_preamble}"
                    f"You are a skeptical quantitative researcher. Your default stance: "
                    f"most hypotheses are noise. Your job is to find the rare ones that aren't.\n\n"
                    f"BEFORE GENERATING: scrutinize the pipeline state below. If something "
                    f"is broken or data quality is insufficient, say so in a 'pipeline_warning' "
                    f"field instead of generating garbage hypotheses.\n\n"
                    f"PIPELINE STATE:\n"
                    f"  Total hypotheses: {draft_count + active_count + rejected_count} "
                    f"({draft_count} draft, {active_count} active, {rejected_count} rejected)\n"
                    f"  Rejection rate: {rejected_count}/{max(1, rejected_count + active_count)}"
                    f" — if this is >90%, challenge whether the pipeline can test ANY hypothesis\n"
                    f"  Eligible sports (>={MIN_GAMES_FOR_HYPOTHESIS} games): {', '.join(eligible_sports)}\n"
                    f"  Ineligible (insufficient data): {', '.join(ineligible_sports) if ineligible_sports else 'none'}\n"
                    f"  Data ranges: {json.dumps(date_ranges)}\n"
                    f"  Collection stats: {json.dumps(data_stats)}\n"
                    f"  Model: consensus devig (power method) — needs 3+ books to be reliable. "
                    f"If most events show books_used=1, the devig is meaningless.\n\n"
                    f"EXISTING HYPOTHESIS NAMES (avoid duplicates):\n"
                    f"  {json.dumps(existing_names[:50])}\n\n"
                    f"ELIGIBLE SPORTS ONLY: {eligible_sports}\n"
                    f"DO NOT generate hypotheses for ineligible sports — they will be auto-rejected.\n\n"
                    f"{regime_context}"
                    f"{correlation_context}"
                    f"EDGE PHILOSOPHY — READ THIS CAREFULLY:\n"
                    f"Vegas prices rest, travel, B2Bs, weather, and schedule spots CORRECTLY.\n"
                    f"Every model already has those columns. Do NOT generate more of these.\n"
                    f"We need edges in dimensions that models DON'T HAVE COLUMNS FOR:\n\n"
                    f"  UNCONVENTIONAL FACTORS (the kind of thing no model prices):\n"
                    f"  - Team identity/cohesion: racial composition, regional identity, religious\n"
                    f"    institutional values (e.g. BYU, Notre Dame, Liberty), coaching culture\n"
                    f"  - Roster sociology: age variance, draft capital distribution, contract year\n"
                    f"    clusters, language barriers, shared alma mater connections\n"
                    f"  - Referee/umpire biases: specific officials' tendencies with specific teams,\n"
                    f"    foul call patterns by game context, home-whistle strength by ref\n"
                    f"  - Psychological momentum: post-trade deadline chemistry disruption, coaching\n"
                    f"    hire/fire bounce, rivalry game emotional overperformance, clinch letdown\n"
                    f"    dynamics in specific roster age profiles\n"
                    f"  - Structural market inefficiencies: SGP correlation mispricing (correlated\n"
                    f"    legs priced as independent), alt-line vs main-line gaps, live betting\n"
                    f"    overreaction to early scores, cross-book consensus divergence\n"
                    f"  - Scheme/matchup geometry: specific offensive system vs specific defensive\n"
                    f"    scheme interactions, pace-forcing mismatches, platoon advantages\n"
                    f"  - Media/narrative mispricing: nationally televised game line inflation,\n"
                    f"    star player absence overreaction, preseason ranking anchor bias\n"
                    f"  - Venue-specific micro-factors: altitude, turf vs grass transitions,\n"
                    f"    dome-to-outdoor, timezone-specific circadian effects\n"
                    f"  - Calendar/scheduling quirks: exam week in college sports, holiday games,\n"
                    f"    conference tournament motivation asymmetry\n\n"
                    f"  BANNED (already priced, stop generating these):\n"
                    f"  - Generic rest/B2B/travel advantages\n"
                    f"  - Home underdog ATS\n"
                    f"  - Eliminated team fades\n"
                    f"  - Basic weather totals\n"
                    f"  - Blowout-loss bounce-back (63 variants tested, 0 promoted, 3 anti-predictive at p<0.02 — structurally dead)\n"
                    f"  - Any hypothesis that is just 'situational factor X is underpriced'\n"
                    f"    without specifying WHY models can't capture it\n\n"
                    f"RESPOND WITH EXACTLY THIS JSON (no other text):\n"
                    f'{{"hypotheses": [\n'
                    f'  {{"name": "unique_snake_case_name", '
                    f'"thesis": "Clear testable statement", '
                    f'"sport": "<sport_key>", '
                    f'"market_type": "spreads|totals|h2h|player_props", '
                    f'"edge_threshold": 0.015, '
                    f'"game_filters": {{"STRUCTURED filters on game context — see AVAILABLE FILTERS below"}}, '
                    f'"line_filters": {{"STRUCTURED filters on bet lines — see AVAILABLE FILTERS below"}}'
                    f'}}\n'
                    f'], "pipeline_warning": "optional — flag if data quality makes testing pointless"}}\n\n'
                    f"AVAILABLE GAME FILTERS (applied per-game BEFORE edge calculation):\n"
                    f"  These filter which GAMES are tested. Use ONLY keys listed here:\n"
                    f"  - require_b2b: true — at least one team on back-to-back\n"
                    f"  - min_rest_mismatch: N — abs(home_rest - away_rest) >= N days\n"
                    f"  - max_rest_days: N — at least one team with rest <= N days\n"
                    f"  - min_games_in_4: N — at least one team played N+ games in 4 days\n"
                    f"  - require_road_streak: N — at least one team on N+ consecutive road games\n"
                    f"  - require_sandwich: true — at least one team in sandwich game spot\n"
                    f"  - require_revenge: true — rematch within 30 days\n"
                    f"  - min_win_pct: 0.65 — at least one team above this win%\n"
                    f"  - max_win_pct: 0.35 — at least one team below this win%\n"
                    f"  - win_pct_range: [0.43, 0.57] — at least one team in this range (bubble)\n"
                    f"  - max_prev_margin: -10 — at least one team lost prev game by 10+ pts\n"
                    f"  - min_prev_margin: 15 — at least one team won prev game by 15+ pts\n"
                    f"  - side: 'home'|'away' — apply conditions to THIS side specifically\n"
                    f"  If your hypothesis doesn't need game filtering, use {{}}\n\n"
                    f"AVAILABLE LINE FILTERS (applied per-line DURING edge calculation):\n"
                    f"  These filter which BET LINES are evaluated within matching games:\n"
                    f"  - home_away: 'home'|'away' — only evaluate this team's line\n"
                    f"  - dog_fav: 'underdog'|'favorite' — only evaluate this role\n"
                    f"  - side: 'Over'|'Under' — for totals, only evaluate this side\n"
                    f"  - spread_range: [3, 7] — only test spreads in this point range\n"
                    f"  - spread_min: 3 — only test spreads >= this value\n"
                    f"  If your hypothesis doesn't need line filtering, use {{}}\n\n"
                    f"AVAILABLE CONTEXT DATA per game (what game_contexts actually stores):\n"
                    f"  ALL SPORTS: scores (home/away), rest_days, b2b (boolean), records,\n"
                    f"    attendance, venue (name, dome, altitude_ft), tz_offset, national_tv,\n"
                    f"    officials (refs/umps), broadcasts, spread, total,\n"
                    f"    play_by_play (period-level scoring summaries)\n"
                    f"  MLB ONLY: park_factor (venue-specific run environment multiplier)\n"
                    f"  NOT AVAILABLE (do NOT reference): umpire strike zones, ref crew tendencies,\n"
                    f"    goalie workloads, pitch-level data, weather, public betting %,\n"
                    f"    player prop lines, lineup data, advanced team stats\n\n"
                    f"CRITICAL: Every hypothesis MUST include game_filters and line_filters.\n"
                    f"If a hypothesis can't be expressed with these filters, it CANNOT be tested.\n"
                    f"Do NOT generate hypotheses requiring data NOT in the available context list above.\n\n"
                    f"RULES:\n"
                    f"- Generate 3-5 hypotheses per call\n"
                    f"- Spread across multiple sports — do NOT cluster on one sport\n"
                    f"- Each hypothesis MUST explain WHY this edge would survive (why can't\n"
                    f"  Vegas or sharp models capture this factor?)\n"
                    f"- Each must be testable with the ACTUAL data we have (check collection stats)\n"
                    f"- Names must be unique (not in existing list)\n"
                    f"- Thesis must be specific and falsifiable\n"
                    f"- If the pipeline state shows systemic issues (high rejection rate, "
                    f"thin data, broken resolution), flag them — do NOT just generate more "
                    f"hypotheses into a broken funnel\n"
                )

                result = await claude_code_query(prompt, hermes_caller="hypothesis_gen")
                self._last_claude_call = time.time()
                self._claude_escalations += 1

                if result.get("content") and not result.get("error"):
                    content = result["content"]
                    try:
                        # Extract JSON from response
                        json_str = content
                        if "```" in json_str:
                            parts = json_str.split("```")
                            for part in parts:
                                stripped = part.strip()
                                if stripped.startswith("json"):
                                    stripped = stripped[4:].strip()
                                if stripped.startswith("{"):
                                    json_str = stripped
                                    break
                        elif "{" in json_str:
                            start = json_str.index("{")
                            end = json_str.rindex("}") + 1
                            json_str = json_str[start:end]

                        parsed = json.loads(json_str)
                        for nh in parsed.get("hypotheses", []):
                            try:
                                h_sport = nh.get("sport", "basketball_nba")
                                # Hard gate: reject hypotheses for sports with insufficient data
                                if eligible_sports and h_sport not in eligible_sports:
                                    logger.info(
                                        f"Research: rejected '{nh.get('name')}' — "
                                        f"sport '{h_sport}' has insufficient data "
                                        f"({game_counts_by_sport.get(h_sport, 0)} games < {MIN_GAMES_FOR_HYPOTHESIS})"
                                    )
                                    continue
                                h_config = {
                                    "source": "claude_primary_gen",
                                    "cycle": self._cycles,
                                    "training_period_start": training_period_start,
                                    "training_period_end": training_period_end,
                                    "forward_test_start": forward_test_start,
                                }
                                # Pass structured filters from Claude to model_config
                                if nh.get("game_filters"):
                                    h_config["game_filters"] = nh["game_filters"]
                                if nh.get("line_filters"):
                                    h_config["line_filters"] = nh["line_filters"]
                                # Enrich with regime data if available for this sport
                                if _regime_cache:
                                    sport_regimes = {
                                        k: v for k, v in _regime_cache.items()
                                        if k.startswith(h_sport + ":")
                                        and v.get("has_edge_signal")
                                    }
                                    if sport_regimes:
                                        # Attach summary of regime signals for backtester
                                        regime_summary = {}
                                        for rk, rv in list(sport_regimes.items())[:5]:
                                            team = rv.get("team", rk)
                                            rb = rv.get("recency_bias", {})
                                            regime_summary[team] = {
                                                "regime": rv.get("power_rating", {}).get("regime", "stable") if isinstance(rv.get("power_rating"), dict) else "stable",
                                                "recency_bias_score": rb.get("bias_magnitude", 0) if isinstance(rb, dict) else 0,
                                                "signals": rv.get("actionable_signals", []),
                                            }
                                        h_config["regime_signals"] = regime_summary
                                await self.hypothesis_manager.create_hypothesis(
                                    name=nh.get("name", f"claude_gen_{self._cycles}"),
                                    thesis=nh.get("thesis", ""),
                                    sport=h_sport,
                                    market_type=nh.get("market_type", "spreads"),
                                    edge_threshold=nh.get("edge_threshold", 0.015),
                                    model_config=h_config,
                                )
                                total_created += 1
                            except Exception as e:
                                logger.warning(f"Failed to create Claude hypothesis: {e}")

                        used_claude = True
                        logger.info(
                            f"Research: Claude generated {total_created} hypotheses"
                        )
                    except (json.JSONDecodeError, ValueError) as e:
                        logger.warning(
                            f"Claude hypothesis response not valid JSON: {e}"
                        )
                elif result.get("rate_limited"):
                    logger.info(
                        "Research: Claude rate-limited during hypothesis gen — "
                        "falling back to templates"
                    )
            except Exception as e:
                logger.warning(f"Claude hypothesis generation failed: {e}")

        # ── FALLBACK: Template + local model generation when Claude unavailable ──
        if not used_claude:
            # Defer the Claude prompt so it runs when Claude comes back
            from tools.claude_code import is_available as _ca
            if not _ca():
                try:
                    existing_names = await self.hypothesis_manager.get_names()
                    deferred_prompt = (
                        f"CALLISTO HYPOTHESIS GENERATION — Deferred from Cycle #{self._cycles}\n\n"
                        f"Generate 3-5 UNCONVENTIONAL sports betting hypotheses across: {RESEARCH_SPORTS}\n\n"
                        f"BANNED: rest/B2B, home underdog ATS, eliminated fades, basic weather. "
                        f"Vegas prices these. Find edges in dimensions models lack columns for: "
                        f"team identity/cohesion, roster sociology, ref biases, scheme geometry, "
                        f"SGP correlation mispricing, media narrative inflation, calendar quirks.\n\n"
                        f"AVAILABLE game_filters: require_b2b, min_rest_mismatch, max_rest_days, "
                        f"min_games_in_4, require_road_streak, require_sandwich, require_revenge, "
                        f"min_win_pct, max_win_pct, win_pct_range, max_prev_margin, min_prev_margin, side\n"
                        f"AVAILABLE line_filters: home_away, dog_fav, side, spread_range, spread_min\n\n"
                        f"EXISTING NAMES (avoid duplicates): {json.dumps(existing_names[:30])}\n\n"
                        f"RESPOND WITH JSON: {{\"hypotheses\": [{{\"name\": \"...\", \"thesis\": \"...\", "
                        f"\"sport\": \"...\", \"market_type\": \"...\", \"edge_threshold\": 0.015, "
                        f"\"game_filters\": {{}}, \"line_filters\": {{}}}}]}}"
                    )
                    await self._work_queue.enqueue("hypothesis_gen", deferred_prompt, priority=2)
                    self._downtime_tracker.item_queued()
                    logger.info("Research: hypothesis gen deferred to work queue (Claude unavailable)")
                except Exception as e:
                    logger.warning(f"Failed to enqueue deferred hypothesis gen: {e}")

                # Try local model via escalation ladder (Apriel > Qwen3 > DeepSeek)
                try:
                    from inference import escalate_with_ladder
                    existing_l = (await self.hypothesis_manager.get_names())[:30]
                    ladder_prompt = (
                        f"Generate 3 testable sports betting hypotheses.\n"
                        f"Sports: {RESEARCH_SPORTS}\n"
                        f"Market types: h2h, spreads, totals, player_points, player_strikeouts\n"
                        f"EXISTING (avoid duplicates): {json.dumps(existing_l)}\n\n"
                        f"AVAILABLE game_filters: require_b2b, min_rest_mismatch, max_rest_days, "
                        f"min_games_in_4, require_road_streak, require_sandwich, require_revenge, "
                        f"min_win_pct, max_win_pct, win_pct_range, max_prev_margin, min_prev_margin, side\n"
                        f"AVAILABLE line_filters: home_away, dog_fav, side, spread_range, spread_min\n\n"
                        f"RESPOND WITH JSON ONLY:\n"
                        f'{{"hypotheses": [{{"name": "sport_descriptive_name", '
                        f'"thesis": "Testable claim", "sport": "basketball_nba", '
                        f'"market_type": "spreads", "edge_threshold": 0.003, '
                        f'"game_filters": {{}}, "line_filters": {{}}}}]}}'
                    )
                    ladder_result = await escalate_with_ladder(
                        ladder_prompt, task_type="hypothesis_gen", timeout=120,
                    )
                    ladder_content = ladder_result.get("content", "")
                    if ladder_content:
                        from inference import _parse_json_response
                        parsed = _parse_json_response(ladder_content)
                        if parsed and isinstance(parsed, dict):
                            for nh in parsed.get("hypotheses", []):
                                try:
                                    _ladder_config = {
                                            "source": f"ladder_{ladder_result.get('model_used', 'unknown')}",
                                            "cycle": self._cycles,
                                            "training_period_start": training_period_start,
                                            "training_period_end": training_period_end,
                                            "forward_test_start": forward_test_start,
                                        }
                                    if nh.get("game_filters"):
                                        _ladder_config["game_filters"] = nh["game_filters"]
                                    if nh.get("line_filters"):
                                        _ladder_config["line_filters"] = nh["line_filters"]
                                    await self.hypothesis_manager.create_hypothesis(
                                        name=nh.get("name", f"ladder_gen_{self._cycles}"),
                                        thesis=nh.get("thesis", ""),
                                        sport=nh.get("sport", "basketball_nba"),
                                        market_type=nh.get("market_type", "spreads"),
                                        edge_threshold=nh.get("edge_threshold", 0.003),
                                        model_config=_ladder_config,
                                    )
                                    total_created += 1
                                    self._hypotheses_generated += 1
                                except Exception as e:
                                    logger.debug(f"Ladder hypothesis creation failed: {e}")
                            logger.info(
                                f"Research: ladder model ({ladder_result.get('model_used')}) "
                                f"generated {total_created} hypotheses"
                            )
                except Exception as e:
                    logger.warning(f"Ladder hypothesis generation failed: {e}")

                # Also try template-based local fallback for quick hypothesis ideas
                try:
                    from tools.work_queue import local_fallback_hypothesis_gen
                    pipeline_state = (
                        f"Cycles: {self._cycles}, Hypotheses: {self._hypotheses_generated}, "
                        f"Backtests: {self._backtests_run}"
                    )
                    existing_names = await self.hypothesis_manager.get_names()
                    local_hypos = await local_fallback_hypothesis_gen(
                        pipeline_state, existing_names, ""
                    )
                    for nh in local_hypos:
                        try:
                            _local_config = {
                                    "source": "local_fallback_gen",
                                    "cycle": self._cycles,
                                    "training_period_start": training_period_start,
                                    "training_period_end": training_period_end,
                                    "forward_test_start": forward_test_start,
                                }
                            if nh.get("game_filters"):
                                _local_config["game_filters"] = nh["game_filters"]
                            if nh.get("line_filters"):
                                _local_config["line_filters"] = nh["line_filters"]
                            await self.hypothesis_manager.create_hypothesis(
                                name=nh.get("name", f"local_gen_{self._cycles}"),
                                thesis=nh.get("thesis", ""),
                                sport=nh.get("sport", "basketball_nba"),
                                market_type=nh.get("market_type", "spreads"),
                                edge_threshold=nh.get("edge_threshold", 0.015),
                                model_config=_local_config,
                            )
                            total_created += 1
                        except Exception as e:
                            logger.debug(f"Local fallback hypothesis creation failed: {e}")
                    if local_hypos:
                        logger.info(f"Research: local model generated {len(local_hypos)} hypotheses")
                except Exception as e:
                    logger.debug(f"Local fallback hypothesis gen failed: {e}")

            # Template fallback always runs when Claude didn't
            # Re-check data availability for template path
            _template_eligible = RESEARCH_SPORTS
            if hasattr(self, 'data_collector') and self.data_collector._db:
                try:
                    _gc = {}
                    cursor = await self.data_collector._db.execute(
                        "SELECT sport, COUNT(*) FROM game_contexts GROUP BY sport"
                    )
                    for row in await cursor.fetchall():
                        _gc[row[0]] = row[1]
                    _template_eligible = [
                        s for s in RESEARCH_SPORTS
                        if _gc.get(s, 0) >= MIN_GAMES_FOR_HYPOTHESIS
                    ]
                    _skipped = [s for s in RESEARCH_SPORTS if s not in _template_eligible]
                    if _skipped:
                        logger.info(
                            f"Research: template gen skipping sports with <{MIN_GAMES_FOR_HYPOTHESIS} games: {_skipped}"
                        )
                except Exception:
                    pass
            logger.info("Research: using template fallback for hypothesis generation")
            for sport in _template_eligible:
                try:
                    quota = 20
                    created = await self.hypothesis_generator.generate_from_templates(
                        sport=sport,
                        max_hypotheses=quota,
                        training_cutoff_date=training_period_end,
                    )
                    total_created += len(created)
                except Exception as e:
                    logger.warning(f"Template generation failed for {sport}: {e}")

        # ── DATA-DRIVEN PATTERN DISCOVERY ──
        # Pure computation — no LLM needed. Discovers statistical anomalies
        # from historical data using temporal splits. Runs EVERY cycle
        # regardless of Claude availability because data-driven hypotheses
        # are grounded in actual patterns, not LLM-plausible theses.
        if total_created < 3:  # Always try unless we already have enough
            try:
                from tools.temporal_analysis import generate_hypotheses_from_analysis
                import asyncio

                pattern_hypotheses = await asyncio.get_event_loop().run_in_executor(
                    None,
                    generate_hypotheses_from_analysis,
                    os.getenv("CALLISTO_DB_PATH", "memory/callisto.db"),
                    None,  # all sports
                    training_period_end,
                    20,  # min_sample
                    3.0,  # min_edge %
                    0.10,  # max p-value
                )
                for h_def in pattern_hypotheses[:5]:  # cap at 5 per cycle
                    try:
                        await self.hypothesis_manager.create_hypothesis(
                            name=h_def["name"],
                            thesis=h_def["thesis"],
                            sport=h_def["sport"],
                            market_type=h_def["market_type"],
                            model_config=h_def.get("model_config", {}),
                        )
                        total_created += 1
                    except Exception as e:
                        logger.debug(f"Pattern hypothesis creation failed: {e}")
                if pattern_hypotheses:
                    logger.info(
                        f"Research: data-driven pattern discovery generated "
                        f"{min(len(pattern_hypotheses), 5)} hypotheses"
                    )
            except Exception as e:
                logger.debug(f"Pattern discovery failed (non-fatal): {e}")

        self._hypotheses_generated += total_created
        logger.info(f"Research: generated {total_created} new hypotheses")

    async def _phase_validate(self) -> None:
        """Per-cycle sanity validation — catches data quality issues immediately.

        Runs after every backtest phase. Checks:
        1. Phantom edges (>15% or impossibly uniform signal rates)
        2. Context enrichment coverage
        3. Books_used distribution (devig quality)
        4. Orphaned tables that should have data
        """
        db = self.hypothesis_manager._db
        if not db:
            return

        issues = []

        try:
            # 1. Phantom edge detection: flag backtest events with >15% edge
            cursor = await db.execute(
                "SELECT COUNT(*) FROM backtest_events WHERE ABS(edge) > 0.15"
            )
            phantom_count = (await cursor.fetchone())[0]
            if phantom_count > 0:
                issues.append(
                    f"PHANTOM: {phantom_count} backtest events with |edge| > 15% "
                    f"— likely data contamination"
                )
                # Auto-purge phantoms
                await db.execute("DELETE FROM backtest_events WHERE ABS(edge) > 0.15")
                await db.commit()
                logger.warning(f"Purged {phantom_count} phantom backtest events (|edge| > 15%)")

            # 2. Context enrichment coverage (last 7 days)
            cursor = await db.execute(
                "SELECT COUNT(*) FROM game_contexts "
                "WHERE game_date >= date('now', '-7 days')"
            )
            total_recent = (await cursor.fetchone())[0]
            cursor = await db.execute(
                "SELECT COUNT(*) FROM game_contexts "
                "WHERE game_date >= date('now', '-7 days') "
                "AND context_json LIKE '%rest_days%'"
            )
            enriched_recent = (await cursor.fetchone())[0]
            if total_recent > 0:
                enrich_rate = enriched_recent / total_recent
                if enrich_rate < 0.5:
                    issues.append(
                        f"ENRICHMENT: Only {enrich_rate:.0%} of last 7 days' games "
                        f"have rest_days ({enriched_recent}/{total_recent})"
                    )

            # 3. Orphaned table detection
            orphan_checks = [
                ("market_microstructure", "odds_snapshots", 100),
                ("learned_correlations", "game_results", 1000),
            ]
            from tools.db_utils import safe_ident
            for target_table, source_table, source_min in orphan_checks:
                cursor = await db.execute(f"SELECT COUNT(*) FROM {safe_ident(target_table)}")
                target_count = (await cursor.fetchone())[0]
                cursor = await db.execute(f"SELECT COUNT(*) FROM {safe_ident(source_table)}")
                source_count = (await cursor.fetchone())[0]
                if target_count == 0 and source_count >= source_min:
                    issues.append(
                        f"ORPHAN: {target_table} has 0 rows but {source_table} "
                        f"has {source_count} — pipeline not connected"
                    )

            # 4. Stale data detection (hot tables)
            for table, ts_col, max_hours in [
                ("odds_snapshots", "timestamp", 2),
                ("game_contexts", "created_at", 24),
            ]:
                cursor = await db.execute(
                    f"SELECT MAX({safe_ident(ts_col)}) FROM {safe_ident(table)}"
                )
                row = await cursor.fetchone()
                if row and row[0]:
                    from datetime import datetime, timezone
                    try:
                        last_ts = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
                        age_hours = (datetime.now(timezone.utc) - last_ts).total_seconds() / 3600
                        if age_hours > max_hours:
                            issues.append(
                                f"STALE: {table} last update {age_hours:.1f}h ago "
                                f"(threshold: {max_hours}h)"
                            )
                    except (ValueError, TypeError):
                        pass

        except Exception as e:
            logger.debug(f"Validation phase error: {e}")

        if issues:
            logger.warning(
                f"Pipeline validation: {len(issues)} issues found:\n"
                + "\n".join(f"  - {i}" for i in issues)
            )
            # Record to Hermes for cross-session awareness
            try:
                from tools.hermes_memory import get_hermes_memory
                hm = await get_hermes_memory()
                if hm:
                    await hm.record_learning(
                        key="pipeline_validation_issues",
                        value="; ".join(issues),
                        confidence=0.9,
                        source="pipeline_validator",
                    )
            except Exception:
                pass

            # Record sentinel flags for anomaly tracking
            try:
                from tools.cache_manager import record_sentinel_flag
                for issue in issues:
                    severity = "critical" if "PHANTOM" in issue else "warning"
                    await record_sentinel_flag(
                        flag_type="pipeline_validation",
                        description=issue,
                        severity=severity,
                    )
            except Exception:
                pass

    async def _phase_backtest(self) -> None:
        """Backtest draft hypotheses — enforcing temporal isolation.

        The correct lifecycle:
          1. Hypothesis was generated using data from [training_period_start .. training_period_end]
          2. Backtest MUST only use data AFTER training_period_end + gap
          3. This prevents circular testing (training and testing on same data)

        Legacy hypotheses without temporal metadata get a conservative default:
        backtest only the last 30 days (assumed to be unseen).
        """
        # Bridge live odds_snapshots into historical_odds_cache so backtests
        # can use recently-collected multi-book data
        try:
            bridge_result = await self.backtest_engine.historical_fetcher.bridge_snapshots_to_cache()
            if bridge_result.get("bridged", 0) > 0:
                logger.info(f"Research: bridged {bridge_result['bridged']} snapshot-days into historical cache")
        except Exception as e:
            logger.warning(f"Research: snapshot bridge failed: {e}")

        # Get draft hypotheses that haven't been backtested
        drafts = await self.hypothesis_manager.list_hypotheses(status="draft")

        if not drafts:
            return

        # ── Pre-filter: skip drafts that already have 0-event backtest runs ──
        # Hypotheses with prior 0-event runs are likely untestable with current
        # data. The circuit breaker will reject after 2, but skipping here avoids
        # wasting one more cycle re-running them before the breaker fires.
        already_zero = set()
        try:
            db = self.data_collector._db
            if db:
                cursor = await db.execute(
                    "SELECT DISTINCT hypothesis_id FROM backtest_runs "
                    "WHERE total_events = 0"
                )
                already_zero = {row[0] for row in await cursor.fetchall()}
                if already_zero:
                    before = len(drafts)
                    drafts = [h for h in drafts if h.get("hypothesis_id") not in already_zero]
                    skipped_zero = before - len(drafts)
                    if skipped_zero > 0:
                        logger.info(
                            f"Research: skipped {skipped_zero} drafts with prior "
                            f"0-event backtest runs (awaiting circuit breaker)"
                        )
        except Exception as e:
            logger.warning(f"Pre-filter for 0-event drafts failed: {e}")

        # Pre-check which sports have usable odds (>=2 books)
        sports_with_odds = set()
        try:
            db = self.data_collector._db
            if db:
                cursor = await db.execute(
                    "SELECT DISTINCT sport FROM historical_odds_cache"
                )
                for (sport,) in await cursor.fetchall():
                    # Quick sample: does this sport have any multi-book records?
                    check = await db.execute(
                        "SELECT response_json FROM historical_odds_cache "
                        "WHERE sport = ? ORDER BY RANDOM() LIMIT 5",
                        (sport,),
                    )
                    for (rj,) in await check.fetchall():
                        try:
                            data = json.loads(rj) if isinstance(rj, str) else rj
                            games = data.get("games", []) if isinstance(data, dict) else data
                            for g in games:
                                if len(g.get("bookmakers", [])) >= 2:
                                    sports_with_odds.add(sport)
                                    break
                        except (json.JSONDecodeError, TypeError):
                            continue
                        if sport in sports_with_odds:
                            break
        except Exception as e:
            logger.warning(f"Data quality pre-check failed: {e}")

        # Pre-filter: remove hypotheses that will definitely be skipped
        # (context_coverage < 0.5). Without this, the same 20 untestable
        # hypotheses clog the batch every cycle and nothing testable runs.
        from tools.backtest import BacktestEngine
        testable = []
        for h in drafts:
            mc = h.get("model_config", {})
            if isinstance(mc, str):
                try:
                    mc = json.loads(mc)
                except (json.JSONDecodeError, TypeError):
                    mc = {}
            ctx_coverage = BacktestEngine.compute_context_coverage(mc)
            has_struct = BacktestEngine.has_structured_filters(mc)
            if ctx_coverage >= 0.5 and not mc.get("context_factors"):
                h_thesis = h.get("thesis", "")
                h_name = h.get("name", "")
                inferred = BacktestEngine._infer_context_needs(h_thesis, h_name)
                if inferred and not has_struct:
                    continue  # Skip — will fail context check anyway
            elif ctx_coverage < 0.5 and not has_struct:
                continue  # Skip — insufficient context coverage
            testable.append(h)

        # Sport-balanced batching: round-robin across sports instead of
        # pure priority sort. This prevents NBA from saturating the queue
        # and starving all other sports (root cause of 0 non-NBA backtests).
        from collections import defaultdict
        by_sport = defaultdict(list)
        for h in testable:
            sport = h.get("sport", "unknown")
            by_sport[sport].append(h)

        # Sort sports by data availability (SPORT_PRIORITY) — all sports equal
        sport_order = sorted(by_sport.keys(), key=lambda x: SPORT_PRIORITY.get(x, 99))

        # Round-robin: take hypotheses from each sport in turns
        to_test = []
        sport_idx = {s: 0 for s in sport_order}
        while len(to_test) < BACKTEST_BATCH_SIZE:
            added_any = False
            for sport in sport_order:
                if len(to_test) >= BACKTEST_BATCH_SIZE:
                    break
                idx = sport_idx[sport]
                if idx < len(by_sport[sport]):
                    to_test.append(by_sport[sport][idx])
                    sport_idx[sport] = idx + 1
                    added_any = True
            if not added_any:
                break

        skipped = len(drafts) - len(testable)
        sports_in_batch = set(h.get("sport", "?") for h in to_test)
        logger.info(
            f"Research: backtesting {len(to_test)} hypotheses across {len(sports_in_batch)} sports "
            f"({skipped} skipped as untestable, {len(testable)} testable, "
            f"sports: {sorted(sports_in_batch)})"
        )

        for h in to_test:
            if not self._running:
                break

            sport = h.get("sport", "")
            market = h.get("market_type", "")

            # Player prop hypotheses now backtested via prop_snapshots table.
            # The backtest engine fetches multi-book prop data and applies
            # consensus devig with MIN_BOOKS=2 (thinner markets than game-level).

            # Skip hypotheses where most context conditions are unfilterable.
            # These produce identical event sets across different hypotheses
            # because game-level conditions (pitcher stats, weather, etc.) can't
            # be applied — the backtest just tests ALL games in the sport/market.
            model_cfg = h.get("model_config", {})
            if isinstance(model_cfg, str):
                try:
                    model_cfg = json.loads(model_cfg)
                except (json.JSONDecodeError, TypeError):
                    model_cfg = {}
            from tools.backtest import BacktestEngine
            ctx_coverage = BacktestEngine.compute_context_coverage(model_cfg)
            has_struct = BacktestEngine.has_structured_filters(model_cfg)
            # Also infer context needs from thesis/name BEFORE running backtest
            # (same inference run_backtest does internally). This prevents wasting
            # a backtest cycle on hypotheses that will just return "untestable".
            if ctx_coverage >= 0.5 and not model_cfg.get("context_factors"):
                h_thesis = h.get("thesis", "")
                h_name_for_ctx = h.get("name", "")
                inferred_pre = BacktestEngine._infer_context_needs(h_thesis, h_name_for_ctx)
                if inferred_pre and not has_struct:
                    ctx_coverage = 0.0
                    logger.info(
                        f"Research: pre-backtest inference for {h['hypothesis_id']} "
                        f"({h_name_for_ctx}) detected unfilterable needs: {inferred_pre}"
                    )
                elif inferred_pre and has_struct:
                    logger.info(
                        f"Research: {h['hypothesis_id']} ({h_name_for_ctx}) has inferred "
                        f"unfilterable needs {inferred_pre} but structured filters present — proceeding"
                    )
            if ctx_coverage < 0.5 and not has_struct:
                ctx_factors = model_cfg.get("context_factors", [])
                logger.info(
                    f"Research: skipping backtest for {h['hypothesis_id']} — "
                    f"context_coverage={ctx_coverage:.0%}. Needs game context enrichment."
                )
                continue

            # Skip hypotheses for sports with no usable multi-book data
            if sports_with_odds and sport not in sports_with_odds:
                logger.info(
                    f"Research: skipping backtest for {h['hypothesis_id']} — "
                    f"{sport} has no multi-book odds data yet"
                )
                continue

            try:
                # ── Temporal isolation: determine forward-test date range ──
                model_config = h.get("model_config", {})
                if isinstance(model_config, str):
                    try:
                        model_config = json.loads(model_config)
                    except (json.JSONDecodeError, TypeError):
                        model_config = {}

                # Pre-check: fix stale contaminated temporal metadata from
                # before BACKTEST_GAP_DAYS was corrected (1→7). Recompute
                # backtest_period_start rather than rejecting fixable drafts.
                overlap_err = self._check_temporal_overlap(model_config)
                if overlap_err:
                    te = model_config.get("training_period_end", "")
                    if te:
                        try:
                            te_date = datetime.strptime(te, "%Y-%m-%d").date()
                            correct_start = str(te_date + timedelta(days=BACKTEST_GAP_DAYS))
                            model_config["backtest_period_start"] = correct_start
                            db = self.data_collector._db
                            await db.execute(
                                "UPDATE hypotheses SET model_config = ? WHERE hypothesis_id = ?",
                                (json.dumps(model_config), h["hypothesis_id"]),
                            )
                            await db.commit()
                            logger.info(
                                f"Research: fixed stale temporal metadata for "
                                f"{h['hypothesis_id']} — backtest_period_start → {correct_start}"
                            )
                        except Exception:
                            await self.hypothesis_manager.update_status(
                                h["hypothesis_id"], "rejected",
                                f"auto:temporal_overlap — {overlap_err}"
                            )
                            self._rejections += 1
                            continue

                has_temporal = (
                    "training_period_end" in model_config
                    and model_config["training_period_end"]
                )

                if has_temporal:
                    # Forward-only backtest: start AFTER training period + gap
                    training_end = model_config["training_period_end"]
                    try:
                        te_date = datetime.strptime(training_end, "%Y-%m-%d").date()
                    except ValueError:
                        te_date = datetime.now(timezone.utc).date() - timedelta(days=DEFAULT_TRAINING_WINDOW_DAYS)
                    start_date = str(te_date + timedelta(days=BACKTEST_GAP_DAYS))
                    end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                    logger.info(
                        f"Research: backtest {h['hypothesis_id']} forward-only "
                        f"[{start_date} .. {end_date}] (trained up to {training_end})"
                    )
                else:
                    # Legacy hypothesis without temporal metadata — backfill it
                    # to enforce temporal isolation (prevents circular testing).
                    today_d = datetime.now(timezone.utc).date()
                    training_cutoff = today_d - timedelta(days=DEFAULT_TRAINING_WINDOW_DAYS)
                    model_config["training_period_start"] = "2023-01-01"
                    model_config["training_period_end"] = str(training_cutoff)
                    model_config["forward_test_start"] = str(training_cutoff + timedelta(days=1))
                    start_date = str(training_cutoff + timedelta(days=BACKTEST_GAP_DAYS))
                    end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                    has_temporal = True  # Now it does
                    logger.info(
                        f"Research: backfilled temporal metadata for {h['hypothesis_id']} — "
                        f"training ends {training_cutoff}, backtest [{start_date} .. {end_date}]"
                    )

                # Never backtest against today — games haven't finished
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                if end_date >= today:
                    end_date = str(datetime.now(timezone.utc).date() - timedelta(days=1))

                # ── Constrain to date range where historical data EXISTS ──
                # Without this, backtests target dates with no cached odds
                # and produce 0 events every time.
                cached_range = await self.backtest_engine.historical_fetcher.get_cached_date_range(sport)
                if cached_range and cached_range[0] and cached_range[1]:
                    cache_start, cache_end = cached_range
                    # Clamp start_date and end_date to the cached range
                    if start_date < cache_start:
                        start_date = cache_start
                    if end_date > cache_end:
                        end_date = cache_end
                    logger.info(
                        f"Research: backtest {h['hypothesis_id']} date range "
                        f"clamped to cached data [{start_date} .. {end_date}]"
                    )
                else:
                    logger.info(
                        f"Research: skipping backtest for {h['hypothesis_id']} — "
                        f"no historical odds cached for {sport}"
                    )
                    continue

                if start_date > end_date:
                    logger.info(
                        f"Research: skipping backtest for {h['hypothesis_id']} — "
                        f"no historical date range available (start={start_date} > end={end_date})"
                    )
                    continue

                # ── Flush any dangling transactions before backtest writes ──
                # Phase timeouts (self_repair, etc.) can leave uncommitted
                # transactions on shared connections, holding the WAL write lock.
                # Check all accessible DB connections.
                _flush_conns = {
                    "data_collector": getattr(self.data_collector, "_db", None),
                    "backtest_engine": getattr(self.backtest_engine, "_db", None),
                    "line_monitor": getattr(self.line_monitor, "_db", None) if self.line_monitor else None,
                    "hypothesis_mgr": getattr(self.hypothesis_manager, "_db", None),
                }
                _tx_state = []
                for _fn, _fdb in _flush_conns.items():
                    if _fdb and hasattr(_fdb, "_conn") and _fdb._conn:
                        try:
                            _in_tx = _fdb._conn.in_transaction
                            _tx_state.append(f"{_fn}={_in_tx}")
                            if _in_tx:
                                await _fdb.rollback()
                                logger.warning(f"Flushed dangling transaction on {_fn}")
                        except Exception:
                            _tx_state.append(f"{_fn}=err")
                if _tx_state:
                    logger.info(f"Pre-backtest tx state: {', '.join(_tx_state)}")

                _bt_t0 = time.time()
                # Retry on database lock — other subsystems (line_monitor,
                # self_repair) occasionally hold the WAL write lock.
                _max_retries = 3
                result = None
                for _attempt in range(_max_retries):
                    try:
                        result = await self.backtest_engine.run_backtest(
                            hypothesis_id=h["hypothesis_id"],
                            start_date=start_date,
                            end_date=end_date,
                            credit_budget=30,
                        )
                        break  # Success
                    except Exception as _bt_err:
                        if "database is locked" in str(_bt_err) and _attempt < _max_retries - 1:
                            _wait = 5 * (2 ** _attempt)  # 5s, 10s
                            logger.warning(
                                f"Backtest {h['hypothesis_id']} hit DB lock "
                                f"(attempt {_attempt + 1}/{_max_retries}), "
                                f"retrying in {_wait}s"
                            )
                            await asyncio.sleep(_wait)
                        else:
                            raise  # Re-raise for outer except handler
                if result is None:
                    continue  # All retries exhausted

                _bt_elapsed = time.time() - _bt_t0
                if _bt_elapsed > 30:
                    logger.warning(
                        f"Slow backtest: {h.get('name', h['hypothesis_id'])} "
                        f"took {_bt_elapsed:.1f}s"
                    )

                # Handle untestable hypotheses — context filtering not available
                if result.get("error") == "untestable":
                    logger.warning(
                        f"Research: hypothesis {h['hypothesis_id']} ({h.get('name', '?')}) "
                        f"is UNTESTABLE — {result.get('detail', 'no context data')}. "
                        f"Moving back to draft."
                    )
                    try:
                        await self.hypothesis_manager.update_status(
                            h["hypothesis_id"], "draft", "auto:untestable"
                        )
                    except Exception as e:
                        logger.warning(f"Failed to revert {h['hypothesis_id']} to draft: {e}")
                    continue

                # Handle duplicate backtests — same events as another hypothesis
                if result.get("error") == "duplicate_backtest":
                    logger.warning(
                        f"Research: {h['hypothesis_id']} ({h.get('name', '?')}) "
                        f"is a DUPLICATE backtest of {result.get('duplicate_of', '?')}. "
                        f"Moving back to draft — needs unique filtering to be testable."
                    )
                    try:
                        await self.hypothesis_manager.update_status(
                            h["hypothesis_id"], "draft", "auto:duplicate_backtest"
                        )
                    except Exception:
                        pass
                    continue

                # Handle spring training — don't penalize, just skip until season starts
                if result.get("error") == "spring_training":
                    logger.info(
                        f"Research: skipping {h['hypothesis_id']} ({h.get('name', '?')}) — "
                        f"MLB spring training, will retry after season start"
                    )
                    continue

                # Store temporal metadata in backtest result for integrity checking
                self._backtests_run += 1
                signals = result.get("signals_generated", 0)

                # Update model_config with actual backtest range for audit trail
                # Use actual_start_date from backtest result (may be auto-adjusted
                # for temporal isolation) instead of the original start_date
                actual_start = result.get("actual_start_date", start_date)
                actual_end = result.get("actual_end_date", end_date)
                if has_temporal:
                    model_config["backtest_period_start"] = actual_start
                    model_config["backtest_period_end"] = actual_end
                    model_config["temporal_isolation"] = True
                else:
                    model_config["backtest_period_start"] = actual_start
                    model_config["backtest_period_end"] = actual_end
                    model_config["temporal_isolation"] = False
                    model_config["temporal_isolation_note"] = "legacy_hypothesis_conservative_default"

                # Persist updated model_config
                try:
                    db = self.data_collector._db
                    if db:
                        await db.execute(
                            "UPDATE hypotheses SET model_config = ? WHERE hypothesis_id = ?",
                            (json.dumps(model_config), h["hypothesis_id"]),
                        )
                        await db.commit()
                except Exception as e:
                    logger.warning(f"Failed to update temporal metadata for {h['hypothesis_id']}: {e}")

                total_events = result.get("total_events", 0)
                if total_events == 0:
                    # ── Circuit breaker: reject after 2 consecutive 0-event runs ──
                    # Without this, hypotheses like nhl_playoff_clinch_letdown_total_over
                    # get re-run 5-6 times with 0 events each, wasting backtest cycles.
                    try:
                        db = self.data_collector._db
                        if db:
                            prev_runs = await db.execute(
                                "SELECT COUNT(*) FROM backtest_runs "
                                "WHERE hypothesis_id = ? AND total_events = 0",
                                (h["hypothesis_id"],),
                            )
                            zero_count = (await prev_runs.fetchone())[0]
                            if zero_count >= 2:
                                await self.hypothesis_manager.update_status(
                                    h["hypothesis_id"], "rejected",
                                    f"auto:zero_events_circuit_breaker — {zero_count} consecutive "
                                    f"backtest runs with 0 events. Context filters may be too "
                                    f"restrictive or insufficient historical data for {sport}."
                                )
                                self._rejections += 1
                                logger.info(
                                    f"Research: CIRCUIT BREAKER — rejected {h['hypothesis_id']} "
                                    f"({h.get('name', '?')}) after {zero_count} zero-event runs"
                                )
                                continue
                    except Exception as e:
                        logger.warning(f"Circuit breaker check failed for {h['hypothesis_id']}: {e}")

                    logger.warning(
                        f"Research: backtest {h['hypothesis_id']} produced 0 events "
                        f"({start_date} to {end_date}) — no historical odds data for {sport}?"
                    )
                else:
                    # ── Gate: reject hypotheses that need context filtering but lack game_filters ──
                    # Without structured game_filters, these hypotheses test ALL games for the sport,
                    # producing identical event sets (the "149 identical events" bug).
                    _mc = h.get("model_config", {})
                    if isinstance(_mc, str):
                        try:
                            _mc = json.loads(_mc)
                        except (json.JSONDecodeError, TypeError):
                            _mc = {}
                    _has_gf = bool(_mc.get("game_filters"))
                    _needs_cf = BacktestEngine._needs_context_filter(
                        h.get("name", ""), h.get("thesis", ""), _mc
                    )
                    if _needs_cf and not _has_gf:
                        await self.hypothesis_manager.update_status(
                            h["hypothesis_id"], "rejected",
                            "auto:missing_game_filters — name implies contextual conditions "
                            "but no structured game_filters defined. Recreate with game_filters."
                        )
                        self._rejections += 1
                        logger.info(
                            f"Research: GATE REJECT {h['hypothesis_id']} ({h.get('name', '?')}) — "
                            f"needs context filter but has no game_filters"
                        )
                        continue

                    # ── CRITICAL: Move hypothesis from draft → backtesting ──
                    # Without this, _phase_evaluate() never sees these hypotheses
                    # (it queries status='backtesting' only). This was the root cause
                    # of 0 promotions with 577+ backtest events.
                    try:
                        await self.hypothesis_manager.update_status(
                            h["hypothesis_id"], "backtesting",
                            f"auto:backtest_completed — {total_events} events, {signals} signals"
                        )
                    except Exception as e:
                        logger.warning(
                            f"Failed to promote {h['hypothesis_id']} to backtesting: {e}"
                        )
                    logger.info(
                        f"Research: backtest {h['hypothesis_id']} — "
                        f"{total_events} events, {signals} signals → status=backtesting"
                    )
            except Exception as e:
                logger.warning(
                    f"Backtest failed for {h['hypothesis_id']}: {e}"
                )

    @staticmethod
    def _check_temporal_overlap(model_config: dict) -> Optional[str]:
        """Check if training and backtest periods overlap. Returns error message or None."""
        if isinstance(model_config, str):
            try:
                model_config = json.loads(model_config)
            except (json.JSONDecodeError, TypeError):
                return None
        if not isinstance(model_config, dict):
            return None

        training_end = model_config.get("training_period_end")
        backtest_start = model_config.get("backtest_period_start")

        if not training_end or not backtest_start:
            return None  # Can't check without both dates

        try:
            te = datetime.strptime(str(training_end), "%Y-%m-%d").date()
            bs = datetime.strptime(str(backtest_start), "%Y-%m-%d").date()
            if bs <= te:
                return (
                    f"TEMPORAL OVERLAP: backtest starts {bs} but training ends {te}. "
                    f"Backtest results are contaminated by training data."
                )
        except ValueError:
            pass

        return None

    async def _phase_evaluate(self) -> None:
        """Evaluate backtesting hypotheses for promotion or rejection.

        Enforces temporal isolation: a hypothesis can only be promoted if
        its backtest period does NOT overlap its training period. This
        prevents circular testing from ever reaching paper trading or live.
        """
        # First, resolve unresolved backtest events from game_results.
        # MEMORY FIX: resolve per-sport for active hypotheses only, not the
        # entire 38K+ backtest_events table. The unbounded query was loading
        # all rows every 60s → 1643 MB/hr leak (CPython pymalloc never frees).
        try:
            active_sports = set()
            cursor = await self.backtest_engine._db.execute(
                "SELECT DISTINCT sport FROM hypotheses WHERE status IN ('backtesting', 'paper_trading')"
            )
            for row in await cursor.fetchall():
                active_sports.add(row[0])
            total_resolved = 0
            for sport in active_sports:
                resolution = await self.backtest_engine.resolve_from_game_results(sport=sport)
                total_resolved += resolution.get("resolved", 0)
            if total_resolved > 0:
                logger.info(
                    f"Research: resolved {total_resolved} backtest events "
                    f"from game_results ({len(active_sports)} sports)"
                )
        except Exception as e:
            logger.warning(f"Backtest resolution failed: {e}")

        # ── Paper trading evaluation FIRST ──
        # Paper_trading hypotheses are closest to live and there are only a handful.
        # Evaluate them before backtesting so they always get processed even if the
        # backtesting loop (which can have 15+ hypotheses × 60s each) times out the
        # phase. Previously this block was at the END of _phase_evaluate and never
        # ran because backtesting evaluation consumed the entire 600s budget.
        paper = await self.hypothesis_manager.list_hypotheses(status="paper_trading")
        for h in paper:
            try:
                model_config = h.get("model_config", {})
                if isinstance(model_config, str):
                    try:
                        model_config = json.loads(model_config)
                    except (json.JSONDecodeError, TypeError):
                        model_config = {}

                has_temporal = bool(model_config.get("training_period_end"))
                has_backtest = bool(model_config.get("temporal_isolation"))

                if not has_temporal and not has_backtest:
                    logger.warning(
                        f"Research: hypothesis {h['hypothesis_id']} lacks temporal "
                        f"isolation metadata — allowing paper trade eval but flagging"
                    )

                result = await self.hypothesis_manager.auto_promote(h["hypothesis_id"])
                action = result.get("action", "held")
                if action == "promoted":
                    self._promotions += 1
                    logger.info(
                        f"Research: hypothesis {h['hypothesis_id']} PROMOTED TO LIVE"
                    )
                    try:
                        await telegram.alert_system(
                            f"HYPOTHESIS PROVEN: {h['name']}\n"
                            f"Thesis: {h['thesis'][:200]}\n"
                            f"Status: LIVE — ready for real money\n"
                            f"Temporal isolation: {'YES' if has_temporal else 'LEGACY (no metadata)'}"
                        )
                    except Exception as e:
                        logger.warning(f"Telegram notification failed for proven hypothesis {h['name']}: {e}")
                else:
                    checks = result.get("checks", [])
                    reason = result.get("reason", "")
                    logger.info(
                        f"Research: paper_trading {h.get('name', h['hypothesis_id'])} "
                        f"{action.upper()} — reason={reason[:200] if reason else 'N/A'}, "
                        f"gates={checks}"
                    )
            except Exception as e:
                logger.warning(f"Paper trade eval failed for {h['hypothesis_id']}: {e}")

        backtesting = await self.hypothesis_manager.list_hypotheses(status="backtesting")

        # ── Recovery: promote stuck drafts with completed backtests ──
        # If the system restarts after a backtest completes but before the
        # draft→backtesting promotion, the hypothesis stays in draft forever.
        # This sweep catches those orphans and promotes them.
        try:
            db = self.hypothesis_manager._db
            cursor = await db.execute(
                "SELECT DISTINCT h.hypothesis_id, h.name "
                "FROM hypotheses h "
                "JOIN backtest_runs br ON h.hypothesis_id = br.hypothesis_id "
                "WHERE h.status = 'draft' "
                "AND br.total_events > 0 "
                "AND br.completed_at IS NOT NULL"
            )
            stuck_drafts = await cursor.fetchall()
            for hid, hname in stuck_drafts:
                await self.hypothesis_manager.update_status(
                    hid, "backtesting",
                    "auto:recovery — draft had completed backtests, promoting"
                )
                logger.info(
                    f"Research: recovered stuck draft {hname} → backtesting"
                )
                # Add to current evaluation batch
                h_data = await self.hypothesis_manager.get_hypothesis(hid)
                if h_data:
                    backtesting.append(h_data)
        except Exception as e:
            logger.warning(f"Stuck draft recovery failed: {e}")

        # ── Batch-limit: evaluate top N by signal count per cycle ──
        # IMPORTANT: batch selection happens BEFORE stats recalculation so we
        # only recalculate the hypotheses we're actually evaluating (not all 40+).
        # With 60s/hyp timeout and 600s phase timeout, 8 fits safely
        # (8 × 60s = 480s worst-case, leaves 120s margin).
        MAX_EVALUATE_PER_CYCLE = 8
        if len(backtesting) > MAX_EVALUATE_PER_CYCLE:
            try:
                db = self.hypothesis_manager._db
                cursor = await db.execute(
                    "SELECT hypothesis_id, "
                    "SUM(CASE WHEN signal_generated = 1 THEN 1 ELSE 0 END) as signals "
                    "FROM backtest_events "
                    "WHERE hypothesis_id IN ({}) "
                    "GROUP BY hypothesis_id "
                    "ORDER BY signals DESC "
                    "LIMIT ?".format(
                        ",".join("?" for _ in backtesting)
                    ),
                    [h["hypothesis_id"] for h in backtesting] + [MAX_EVALUATE_PER_CYCLE],
                )
                top_ids = {row[0] for row in await cursor.fetchall()}
                # Always include hypotheses with no backtest events (need initial eval)
                no_data_ids = {
                    h["hypothesis_id"] for h in backtesting
                    if h["hypothesis_id"] not in top_ids
                }
                # Limit no-data to 5 per cycle
                no_data_sample = set(list(no_data_ids)[:5])
                priority_ids = top_ids | no_data_sample
                backtesting = [h for h in backtesting if h["hypothesis_id"] in priority_ids]
                logger.info(
                    f"Research: evaluating {len(backtesting)} hypotheses "
                    f"(top {MAX_EVALUATE_PER_CYCLE} by signals + {len(no_data_sample)} new)"
                )
            except Exception as e:
                logger.warning(f"Batch-limit query failed, evaluating all: {e}")

        # Recompute backtest_runs stats from backtest_events — scoped to the
        # batch being evaluated. This fixes the stale stats problem: retroactive
        # signal updates and game resolution change backtest_events AFTER the run
        # completes, but backtest_runs keeps the original stats. The promotion
        # gate checks backtest_runs, so stale data blocks promotion.
        # Previously recalculated ALL runs in the batch every cycle (even unchanged
        # ones), causing 10-15 min stalls. Now uses a lightweight fingerprint cache
        # inside recalculate_all_active_runs: only runs with new/changed
        # backtest_events (new events, signal flips, result resolution) get the
        # expensive scipy/numpy recompute. Unchanged runs are skipped in O(1).
        try:
            batch_ids = [h["hypothesis_id"] for h in backtesting]
            paper_ids = [
                h["hypothesis_id"]
                for h in await self.hypothesis_manager.list_hypotheses(status="paper_trading")
            ]
            all_recompute_ids = batch_ids + paper_ids
            # Expensive recalculation (scipy/numpy) only for the batch
            updated = await self.backtest_engine.recalculate_all_active_runs(
                hypothesis_ids=all_recompute_ids
            )
            # Sync hypothesis_stats for ALL backtesting hypotheses (not just
            # the batch). The sync itself is cheap (reads from backtest_runs),
            # only the recalculation above is expensive. Without this, hypotheses
            # outside the top-8 batch have perpetually stale hypothesis_stats,
            # which breaks auto-reject tiers and promotion gate evaluation.
            all_backtesting_ids = [
                h["hypothesis_id"]
                for h in await self.hypothesis_manager.list_hypotheses(status="backtesting")
            ]
            all_sync_ids = list(set(all_backtesting_ids + paper_ids))
            if updated > 0:
                logger.info(f"Research: recomputed stats for {updated} backtest runs (batch of {len(all_recompute_ids)}, incl {len(paper_ids)} paper_trading)")
            # ── Always sync hypothesis_stats from backtest_runs ──
            # Must run even when updated==0: after a restart the fingerprint
            # cache is rebuilt but backtest_runs may already be correct, so
            # recalculate returns 0.  Meanwhile hypothesis_stats can be stale
            # from the previous session (e.g. paper_trading hypothesis promoted
            # but stats still show old stage/p_value).  The sync is cheap
            # (one query + N deletes + N inserts) so always running it is safe.
            if all_sync_ids:
                try:
                    from tools.db_utils import execute_with_retry, commit_with_retry
                    db = self.backtest_engine._db
                    now = datetime.now(timezone.utc).isoformat()
                    hs_placeholders = ",".join("?" for _ in all_sync_ids)
                    # Get the latest run per hypothesis (most recent run_id)
                    hs_cursor = await db.execute(
                        f"SELECT br.hypothesis_id, "
                        f"  br.total_events, br.signals_generated, "
                        f"  br.actual_win, br.actual_loss, br.actual_push, "
                        f"  br.hit_rate, br.avg_edge, br.avg_ev, br.avg_clv, "
                        f"  br.roi_pct, br.sharpe_ratio, br.p_value_binomial, "
                        f"  br.sortino_ratio_val, br.brier_score, br.information_coefficient, "
                        f"  h.significance_level, h.min_sample_size, h.status "
                        f"FROM backtest_runs br "
                        f"JOIN hypotheses h ON br.hypothesis_id = h.hypothesis_id "
                        f"WHERE br.hypothesis_id IN ({hs_placeholders}) "
                        f"ORDER BY br.run_id DESC",
                        all_sync_ids,
                    )
                    rows = await hs_cursor.fetchall()
                    # Keep only the latest run per hypothesis
                    seen = set()
                    synced = 0
                    for row in rows:
                        hid = row[0]
                        if hid in seen:
                            continue
                        seen.add(hid)
                        (total_n, signals_n, wins, losses, pushes,
                         hit_rate, avg_edge, avg_ev, avg_clv,
                         roi_pct, sharpe, p_value,
                         sortino, brier, ic,
                         sig_level, min_sample, status) = row[1:]
                        # Determine stage from hypothesis status
                        stage = "paper_trade" if status == "paper_trading" else "backtest"
                        decided = (wins or 0) + (losses or 0)
                        sig_level = sig_level or 0.05
                        min_sample = min_sample or 50
                        is_significant = (
                            p_value is not None
                            and p_value < sig_level
                            and decided >= min_sample
                        )
                        # Delete ALL stages for this hypothesis — when promoted
                        # from backtesting→paper_trading the old row has
                        # stage='backtest' but we'd be inserting stage='paper_trade'.
                        # Without clearing all stages the stale row persists and
                        # the promotion gate reads the wrong p_value.
                        await execute_with_retry(
                            db,
                            "DELETE FROM hypothesis_stats "
                            "WHERE hypothesis_id = ?",
                            (hid,),
                            operation="sync hypothesis_stats delete",
                        )
                        await execute_with_retry(
                            db,
                            "INSERT INTO hypothesis_stats "
                            "(hypothesis_id, stage, computed_at, total_n, signals_n, "
                            "win, loss, push_, hit_rate, avg_edge, avg_ev, avg_clv, "
                            "positive_clv_rate, roi_pct, sharpe, max_drawdown, p_value, "
                            "is_significant, sortino, brier_score, information_coefficient) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (hid, stage, now, total_n or 0, signals_n or 0,
                             wins or 0, losses or 0, pushes or 0,
                             hit_rate, avg_edge, avg_ev, avg_clv,
                             None, roi_pct, sharpe, None, p_value,
                             is_significant,
                             sortino, brier, ic),
                            operation="sync hypothesis_stats insert",
                        )
                        synced += 1
                    if synced > 0:
                        await commit_with_retry(db, operation="sync hypothesis_stats")
                        logger.info(f"Research: synced hypothesis_stats for {synced} hypotheses from backtest_runs")
                except Exception as e:
                    logger.warning(f"hypothesis_stats sync from backtest_runs failed: {e}")
        except Exception as e:
            logger.warning(f"Backtest stats recompute failed: {e}")

        for h in backtesting:
            try:
                # ── Temporal isolation gate ──
                model_config = h.get("model_config", {})
                if isinstance(model_config, str):
                    try:
                        model_config = json.loads(model_config)
                    except (json.JSONDecodeError, TypeError):
                        model_config = {}

                overlap_err = self._check_temporal_overlap(model_config)
                if overlap_err:
                    logger.error(
                        f"Research: REJECTING {h['hypothesis_id']} — {overlap_err}"
                    )
                    await self.hypothesis_manager.update_status(
                        h["hypothesis_id"], "rejected",
                        f"auto:temporal_overlap — {overlap_err}"
                    )
                    self._rejections += 1
                    continue

                # ── Context coverage gate ──
                # If a hypothesis was backtested before the context coverage check
                # was added, its results are noise. Move back to draft so it can
                # be properly evaluated when game context enrichment is available.
                from tools.backtest import BacktestEngine
                ctx_coverage = BacktestEngine.compute_context_coverage(model_config)
                has_struct = BacktestEngine.has_structured_filters(model_config)

                # Also infer context needs from thesis/name (same logic as
                # run_backtest). Without this, hypotheses with empty
                # context_factors appear "fully filterable" (coverage=1.0)
                # even when their name implies unfilterable conditions.
                if ctx_coverage >= 0.5 and not model_config.get("context_factors"):
                    thesis = h.get("thesis", "")
                    h_name = h.get("name", "")
                    inferred = BacktestEngine._infer_context_needs(thesis, h_name)
                    if inferred and not has_struct:
                        ctx_coverage = 0.0
                        logger.info(
                            f"Research: {h['hypothesis_id']} ({h_name}) — inferred "
                            f"unfilterable context needs: {inferred}"
                        )
                    elif inferred and has_struct:
                        logger.info(
                            f"Research: {h['hypothesis_id']} ({h_name}) — inferred "
                            f"unfilterable needs {inferred} but structured filters present — proceeding"
                        )

                # Also check needs_unique_data flag from self-repair
                if model_config.get("needs_unique_data"):
                    logger.warning(
                        f"Research: demoting {h['hypothesis_id']} to draft — "
                        f"flagged as needs_unique_data (duplicate event set)"
                    )
                    await self.hypothesis_manager.update_status(
                        h["hypothesis_id"], "draft",
                        "auto:needs_unique_data — stale backtest with duplicate event set"
                    )
                    continue

                if ctx_coverage < 0.5:
                    ctx_factors = model_config.get("context_factors", [])
                    # Count how many times this hypothesis has been demoted.
                    # After 2 demotions, reject instead of creating a circular loop.
                    demotion_count = model_config.get("demotion_count", 0) + 1
                    model_config["demotion_count"] = demotion_count
                    await self.hypothesis_manager._db.execute(
                        "UPDATE hypotheses SET model_config = ? WHERE hypothesis_id = ?",
                        (json.dumps(model_config), h["hypothesis_id"]),
                    )
                    await self.hypothesis_manager._db.commit()

                    if demotion_count >= 2:
                        logger.info(
                            f"Research: rejecting {h['hypothesis_id']} — demoted "
                            f"{demotion_count}x for ctx_coverage={ctx_coverage:.0%}. "
                            f"Hypothesis is untestable with available data."
                        )
                        await self.hypothesis_manager.update_status(
                            h["hypothesis_id"], "rejected",
                            f"auto:untestable_context — demoted {demotion_count}x, "
                            f"ctx_coverage={ctx_coverage:.0%}"
                        )
                        self._rejections += 1
                    else:
                        logger.warning(
                            f"Research: demoting {h['hypothesis_id']} to draft — "
                            f"context_coverage={ctx_coverage:.0%} ({len(ctx_factors)} "
                            f"factors, most unfilterable). Attempt {demotion_count}/2."
                        )
                        await self.hypothesis_manager.update_status(
                            h["hypothesis_id"], "draft",
                            f"auto:low_context_coverage ({ctx_coverage:.0%}) — "
                            f"needs game context enrichment (demotion {demotion_count}/2)"
                        )
                    continue

                # Per-hypothesis timeout: prevent a single slow auto_promote
                # from consuming the entire 600s phase budget.
                _eval_t0 = time.time()
                try:
                    result = await asyncio.wait_for(
                        self.hypothesis_manager.auto_promote(h["hypothesis_id"]),
                        timeout=60,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        f"Evaluation TIMEOUT (60s) for {h['hypothesis_id']} "
                        f"({h.get('name', '?')})"
                    )
                    continue
                _eval_elapsed = time.time() - _eval_t0
                if _eval_elapsed > 10:
                    logger.warning(
                        f"Slow eval: {h.get('name', h['hypothesis_id'])} "
                        f"took {_eval_elapsed:.1f}s"
                    )
                action = result.get("action", "held")

                if action == "promoted":
                    self._promotions += 1
                    logger.info(
                        f"Research: hypothesis {h['hypothesis_id']} PROMOTED to "
                        f"{result.get('new_status')}"
                    )
                elif action == "rejected":
                    self._rejections += 1
                    logger.info(
                        f"Research: hypothesis {h['hypothesis_id']} REJECTED — "
                        f"data disproves thesis"
                    )
                else:
                    # Log gate checks for "held" hypotheses so we can diagnose
                    # why promotion isn't happening.
                    checks = result.get("checks", [])
                    reason = result.get("reason", "")
                    if checks or reason:
                        logger.info(
                            f"Research: {h.get('name', h['hypothesis_id'])} HELD — "
                            f"reason={reason[:120] if reason else 'N/A'}, "
                            f"gates={checks}"
                        )
            except Exception as e:
                logger.warning(
                    f"Evaluation failed for {h['hypothesis_id']}: {e}"
                )

        # ── Draft-level auto-rejection ──
        # Hypotheses that were backtested but reverted to draft (or never left it)
        # may have definitive negative-edge data. Reject them instead of letting
        # them clog the queue forever.
        #
        # CRITICAL: Only consider SIGNAL events for edge quality. Non-signal events
        # having negative edge is EXPECTED — the hypothesis correctly didn't fire on
        # those. A hypothesis with 16W-1L signals but negative all-event edge is GOOD.
        MIN_EVENTS_FOR_REJECTION = 30
        MAX_SIGNAL_EDGE_FOR_REJECTION = -0.005  # -0.5% avg edge on SIGNAL events
        MIN_SIGNAL_WIN_RATE_PROTECT = 0.60  # Never reject if signals win 60%+
        try:
            db = self.hypothesis_manager._db
            cursor = await db.execute(
                "SELECT h.hypothesis_id, h.name, h.market_type, "
                "COUNT(DISTINCT be.event_id) as events, "
                "COALESCE(AVG(CASE WHEN be.signal_generated = 1 THEN be.edge END), 0) as signal_avg_edge, "
                "COUNT(DISTINCT CASE WHEN be.signal_generated = 1 THEN be.event_id END) as signals, "
                "SUM(CASE WHEN be.signal_generated = 1 AND be.actual_result = 'won' THEN 1 ELSE 0 END) as wins, "
                "SUM(CASE WHEN be.signal_generated = 1 AND be.actual_result = 'lost' THEN 1 ELSE 0 END) as losses "
                "FROM hypotheses h "
                "JOIN backtest_events be ON h.hypothesis_id = be.hypothesis_id "
                "WHERE h.status IN ('draft', 'backtesting') "
                "GROUP BY h.hypothesis_id "
                "HAVING events >= ? AND signal_avg_edge < ?",
                (MIN_EVENTS_FOR_REJECTION, MAX_SIGNAL_EDGE_FOR_REJECTION),
            )
            draft_rejects = await cursor.fetchall()
            for row in draft_rejects:
                hid, hname, mtype, events, signal_edge, signals, wins, losses = row
                total_decided = (wins or 0) + (losses or 0)
                win_rate = (wins or 0) / max(total_decided, 1)

                # PROTECT: never reject hypotheses with strong signal win rate
                if total_decided >= 5 and win_rate >= MIN_SIGNAL_WIN_RATE_PROTECT:
                    logger.info(
                        f"Research: PROTECTED {hid[:12]} ({hname}) from rejection — "
                        f"signal WR={win_rate:.0%} ({wins}W-{losses}L) despite "
                        f"signal_edge={signal_edge:.2%}"
                    )
                    continue

                reason = (
                    f"auto:negative_edge_disproven — {events} events, "
                    f"signal_avg_edge={signal_edge:.2%}, signals={signals}. "
                    f"Signal data disproves thesis."
                )
                await self.hypothesis_manager.update_status(hid, "rejected", reason)
                self._rejections += 1
                logger.info(
                    f"Research: REJECTED zombie {hid[:12]} ({hname}) — "
                    f"{events} events, signal_edge={signal_edge:.2%}, "
                    f"{signals} signals, {wins}W-{losses}L"
                )
            if draft_rejects:
                logger.info(
                    f"Research: processed {len(draft_rejects)} zombie candidates"
                )
        except Exception as e:
            logger.warning(f"Zombie auto-rejection failed: {e}")

        # ── Untestable draft sweep ──
        # Drafts with ctx_coverage < 0.5 are skipped during backtesting selection
        # (lines 3655-3676) but never rejected — they accumulate forever and
        # trigger spinning detection. Bulk-reject drafts older than 48h that
        # are provably untestable with available data.
        try:
            from tools.backtest import BacktestEngine
            db = self.hypothesis_manager._db
            cursor = await db.execute(
                "SELECT hypothesis_id, name, thesis, model_config, created_at "
                "FROM hypotheses WHERE status = 'draft' "
                "AND created_at < datetime('now', '-48 hours')"
            )
            old_drafts = await cursor.fetchall()
            untestable_rejected = 0
            for row in old_drafts:
                hid, hname, thesis, mc_raw, created = row
                try:
                    mc = json.loads(mc_raw) if isinstance(mc_raw, str) else (mc_raw or {})
                except (json.JSONDecodeError, TypeError):
                    mc = {}
                ctx_cov = BacktestEngine.compute_context_coverage(mc)
                has_struct = BacktestEngine.has_structured_filters(mc)
                # Also check inferred context needs
                if ctx_cov >= 0.5 and not mc.get("context_factors"):
                    inferred = BacktestEngine._infer_context_needs(thesis or "", hname or "")
                    if inferred and not has_struct:
                        ctx_cov = 0.0
                if ctx_cov < 0.5 and not has_struct:
                    await self.hypothesis_manager.update_status(
                        hid, "rejected",
                        f"auto:untestable_draft — ctx_coverage={ctx_cov:.0%}, "
                        f"stuck in draft >48h. Untestable with available context data."
                    )
                    untestable_rejected += 1
            if untestable_rejected:
                self._rejections += untestable_rejected
                logger.info(
                    f"Research: auto-rejected {untestable_rejected} untestable drafts "
                    f"(ctx_coverage < 0.5, >48h old)"
                )
        except Exception as e:
            logger.warning(f"Untestable draft sweep failed: {e}")

        # Anti-predictive sweep: reject hypotheses with strongly negative IC
        # (runs each cycle, not just at startup, to catch newly anti-predictive ones)
        try:
            await self._reject_anti_predictive()
        except Exception as e:
            logger.warning(f"Anti-predictive sweep failed: {e}")
        # Low signal rate sweep: reject hypotheses with 100+ events but <2% signal rate
        try:
            await self._reject_low_signal_rate()
        except Exception as e:
            logger.warning(f"Low-signal-rate sweep failed: {e}")

    async def _phase_narrative_edges(self) -> None:
        """Detect player-level narrative edges that models can't price.

        Runs every cycle to find: usage surges, role changes, milestone
        proximity, revenge games. Logs actionable findings and stores
        them for the deep_work phase to incorporate into analysis.
        """
        try:
            from tools.narrative_edge import full_narrative_scan
        except ImportError as e:
            logger.debug(f"Narrative edge module not available: {e}")
            return

        # Run for active sports with player data
        for sport in ["basketball_nba", "icehockey_nhl", "baseball_mlb"]:
            try:
                results = await full_narrative_scan(sport)

                # Log actionable edges
                actionable = [
                    e for e in results.get("usage_surges", [])
                    if e.get("actionable")
                ]
                if actionable:
                    for edge in actionable[:5]:
                        logger.info(
                            f"NARRATIVE EDGE: {edge['player']} {edge['stat_type']} "
                            f"surge {edge['surge_ratio']:.2f}x "
                            f"(recent {edge['recent_avg']:.1f} vs season {edge['season_avg']:.1f}) "
                            f"| line={edge.get('current_line','?')} gap={edge.get('line_gap','?')} "
                            f"| {edge.get('book','?')} [{sport}]"
                        )

                role_changes = results.get("role_changes", [])
                if role_changes:
                    for rc in role_changes[:3]:
                        logger.info(
                            f"ROLE CHANGE: {rc['player']} "
                            f"+{rc['minute_increase']:.0f} min/game "
                            f"({rc['season_avg_minutes']:.0f} -> {rc['recent_avg_minutes']:.0f}) [{sport}]"
                        )

                milestones = results.get("milestones", [])
                if milestones:
                    for m in milestones[:3]:
                        logger.info(
                            f"MILESTONE: {m['player']} "
                            f"{m['edge_type']} — {m.get('note','')} [{sport}]"
                        )

                revenge = results.get("revenge_games", [])
                if revenge:
                    for r in revenge[:3]:
                        logger.info(
                            f"REVENGE GAME: {r['player']} "
                            f"(now {r['current_team']}, ex-{', '.join(r['former_teams'])}) [{sport}]"
                        )

            except Exception as e:
                logger.debug(f"Narrative scan failed for {sport}: {e}")

    async def _phase_live_execute(self) -> None:
        """Execute bets on live (proven) hypotheses using the bet executor.

        Scans live odds for signals matching live hypotheses, then places
        real bets via Playwright browser automation on DraftKings.
        Only runs if the executor is enabled and logged in.
        """
        try:
            from tools.bet_executor import BetExecutor
        except ImportError:
            return

        # Check if executor is available (initialized externally)
        executor = getattr(self, "_bet_executor", None)
        if not executor or not executor.is_enabled:
            return

        live = await self.hypothesis_manager.list_hypotheses(status="live")
        if not live:
            return

        logger.info(f"Research: scanning {len(live)} live hypotheses for bet signals")

        # Cache live odds per sport
        odds_cache: dict[str, dict] = {}

        for h in live:
            if not self._running:
                break

            try:
                sport = h["sport"]
                market = h.get("market_type", "")

                # Get live odds (DK scraper for game-level, Odds API for props)
                if sport not in odds_cache:
                    if market.startswith("player_"):
                        from tools.odds_api_io import get_odds
                        odds_data = await get_odds(sport=sport, regions="us", markets="h2h,spreads,totals")
                    else:
                        from tools.dk_scraper import scrape_dk_odds
                        odds_data = await scrape_dk_odds(sport)
                        if odds_data.get("error") or not odds_data.get("games"):
                            from tools.odds_api_io import get_odds
                            odds_data = await get_odds(sport=sport, regions="us", markets="h2h,spreads,totals")

                    if not odds_data.get("error"):
                        odds_cache[sport] = odds_data

                odds_data = odds_cache.get(sport)
                if not odds_data:
                    continue

                # Generate signals using the backtest engine's paper trade logic
                signals = await self.backtest_engine.generate_paper_trade_signal(
                    hypothesis_id=h["hypothesis_id"],
                    live_odds=odds_data,
                )

                if not signals:
                    continue

                # Execute each signal
                for signal in signals:
                    if not self._running:
                        break

                    result = await executor.execute_bet(
                        sport=sport,
                        team=signal.get("team", ""),
                        market=signal.get("market", market),
                        side=signal.get("side", ""),
                        odds=signal.get("book_odds_american", 0),
                        fair_prob=signal.get("model_fair_prob", 0.5),
                        edge=signal.get("edge", 0),
                        hypothesis_id=h["hypothesis_id"],
                        event_id=signal.get("event_id", ""),
                        game_description=signal.get("game_description", ""),
                    )

                    if result.get("success"):
                        logger.info(
                            f"LIVE BET PLACED: {signal.get('team')} "
                            f"${result.get('stake', 0):.2f} @ {signal.get('book_odds_american')}"
                        )
                    else:
                        logger.warning(
                            f"Live bet failed: {result.get('reason', 'unknown')}"
                        )

            except Exception as e:
                logger.warning(f"Live execution failed for {h['hypothesis_id']}: {e}")

    async def _phase_interpret_backtests(self) -> None:
        """Claude interprets backtest results — signal vs noise, modifications.

        Sends the top 10 hypotheses by signal count with their win/loss/edge
        stats to Claude for interpretation. Claude identifies genuine signals,
        rejects noise, and suggests threshold modifications.

        When Claude is unavailable: defers the prompt to the work queue AND
        runs a local rules-based interpretation as fallback.
        """
        from tools.claude_code import claude_code_query

        db = self.data_collector._db
        if not db:
            return

        # Get top 10 hypotheses by signal count with stats
        try:
            cursor = await db.execute("""
                SELECT h.hypothesis_id, h.name, h.thesis, h.sport, h.market_type,
                       h.edge_threshold, h.status,
                       COUNT(CASE WHEN be.signal_generated=1 THEN 1 END) as sigs,
                       COUNT(*) as events,
                       SUM(CASE WHEN be.signal_generated=1 AND be.actual_result='won' THEN 1 ELSE 0 END) as wins,
                       SUM(CASE WHEN be.signal_generated=1 AND be.actual_result='lost' THEN 1 ELSE 0 END) as losses,
                       SUM(CASE WHEN be.signal_generated=1 AND be.actual_result='push' THEN 1 ELSE 0 END) as pushes,
                       AVG(CASE WHEN be.signal_generated=1 THEN be.edge END) as avg_edge,
                       AVG(CASE WHEN be.signal_generated=1 THEN be.ev_pct END) as avg_ev
                FROM hypotheses h
                LEFT JOIN backtest_events be ON be.hypothesis_id = h.hypothesis_id
                WHERE h.status IN ('backtesting', 'paper_trading')
                GROUP BY h.hypothesis_id
                HAVING events > 0
                ORDER BY sigs DESC, events DESC
                LIMIT 10
            """)
            rows = await cursor.fetchall()
        except Exception as e:
            logger.warning(f"Failed to query backtest stats for interpretation: {e}")
            return

        if not rows:
            logger.info("Research: no hypotheses with backtest data for interpretation")
            return

        # Format hypothesis data for Claude — pre-compute significance locally
        # using local_significance_test to save Claude tokens on basic math
        hypo_data = []
        for r in rows:
            h_id, name, thesis, sport, mkt, thresh, status = r[0], r[1], r[2], r[3], r[4], r[5], r[6]
            sigs, events, wins, losses, pushes = r[7] or 0, r[8] or 0, r[9] or 0, r[10] or 0, r[11] or 0
            avg_edge, avg_ev = r[12] or 0, r[13] or 0
            resolved = wins + losses + pushes
            hit_rate = wins / max(resolved, 1)

            entry = {
                "id": h_id, "name": name, "thesis": thesis[:200],
                "sport": sport, "market": mkt, "threshold": thresh,
                "status": status, "signals": sigs, "events": events,
                "wins": wins, "losses": losses, "pushes": pushes,
                "hit_rate": round(hit_rate, 4),
                "avg_edge": round(avg_edge, 5),
                "avg_ev": round(avg_ev, 5),
            }

            # Local significance test — pre-compute p-value and z-score
            # so Claude can focus on interpretation, not basic math
            if resolved >= 2:
                try:
                    from tools.local_compute import local_significance_test
                    sig_events = [
                        {"edge": avg_edge, "won": i < wins}
                        for i in range(resolved)
                    ]
                    sig_result = await local_significance_test(sig_events)
                    entry["z_score"] = sig_result.get("z_score", 0)
                    entry["p_value"] = sig_result.get("p_value", 1.0)
                    entry["significant"] = sig_result.get("significant", False)
                except Exception:
                    pass

            hypo_data.append(entry)

        # Load error patterns for institutional memory
        error_patterns = ""
        try:
            with open("memory/error_patterns.md", "r") as f:
                error_patterns = f.read()[:1500]  # Cap at 1500 chars to save context
        except Exception:
            pass

        prompt = (
            f"CALLISTO BACKTEST INTERPRETATION — Cycle #{self._cycles}\n\n"
            f"You are a statistician reviewing backtest results. Your bias is toward "
            f"skepticism: most patterns are noise, and you must prove otherwise.\n\n"
            + (f"KNOWN ERROR PATTERNS (avoid repeating these mistakes):\n{error_patterns}\n\n" if error_patterns else "")
            + f"Before evaluating any hypothesis, ask: was this a FAIR test?\n"
            f"- If events=15 and signals=0, that is NOT enough data to reject — hold it.\n"
            f"- If avg_edge is computed from 1 book, the entire edge is an artifact.\n"
            f"- If all hypotheses show similar event counts, the backtest filter is broken.\n\n"
            f"HYPOTHESIS BACKTEST RESULTS (top 10 by signal count):\n"
            f"{json.dumps(hypo_data, indent=2)}\n\n"
            f"STATISTICAL CONTEXT:\n"
            f"- A fair coin has ~50% hit rate. Signal needs to beat that consistently.\n"
            f"- With <30 resolved bets, results are noise. DO NOT reject on thin data.\n"
            f"- avg_edge > 0.03 with hit_rate > 0.53 over 50+ resolved is promising.\n"
            f"- 0 signals after 50+ events means the hypothesis never fires — reject it.\n"
            f"- Low signal rate (<5%) with poor hit rate: lower the threshold, don't kill it.\n"
            f"- Before rejecting: steelman the hypothesis. What is the strongest case it's real?\n"
            f"  Only reject if you can refute that case with the data.\n\n"
            f"RESPOND WITH EXACTLY THIS JSON (no other text):\n"
            f'{{"data_quality_assessment": "honest 1-sentence verdict on whether these backtests are reliable", '
            f'"reject": ["hypothesis_id — ONLY with 50+ events AND fair test conditions"], '
            f'"modify": [{{"id": "hypothesis_id", "new_threshold": 0.025, "reason": "..."}}], '
            f'"insights": "What patterns are working, what isn\'t, and what the pipeline should change"}}\n\n'
            f"RULES:\n"
            f"- data_quality_assessment FIRST: are these results trustworthy?\n"
            f"- reject: ONLY hypotheses with clear disproof (0 signals after 50+ events with 3+ books)\n"
            f"- modify: lower thresholds on promising hypotheses rather than killing them\n"
            f"- If data quality is poor, say so and recommend holding rather than rejecting\n"
        )

        if not self._claude_ok():
            await self._work_queue.enqueue("interpret_backtests", prompt, priority=2)
            self._downtime_tracker.item_queued()
            logger.info("Research: backtest interpretation deferred to work queue (Claude unavailable)")

            # Run local rules-based interpretation as fallback
            try:
                from tools.work_queue import local_fallback_interpret
                local_actions = await local_fallback_interpret(hypo_data)
                rejected = 0
                for hid in local_actions.get("reject", []):
                    try:
                        await self.hypothesis_manager.update_status(
                            hid, "rejected", "local_fallback_interpret"
                        )
                        rejected += 1
                        self._rejections += 1
                    except Exception:
                        pass
                if rejected:
                    logger.info(
                        f"Research: local fallback interpretation rejected {rejected} "
                        f"noise hypotheses"
                    )
                insights = local_actions.get("insights", "")
                if insights:
                    logger.info(f"Research: local interpretation — {insights[:300]}")
            except Exception as e:
                logger.debug(f"Local fallback interpretation failed: {e}")
            return

        remaining = CLAUDE_ESCALATION_COOLDOWN - (time.time() - self._last_claude_call)
        if remaining > 0:
            logger.debug(f"Interpret backtests: cooldown active ({remaining:.0f}s left), deferring to next cycle")
            return

        try:
            result = await claude_code_query(prompt, hermes_caller="deep_work")
            self._last_claude_call = time.time()
            self._claude_escalations += 1

            if result.get("content") and not result.get("error"):
                content = result["content"]
                try:
                    json_str = content
                    if "```" in json_str:
                        parts = json_str.split("```")
                        for part in parts:
                            stripped = part.strip()
                            if stripped.startswith("json"):
                                stripped = stripped[4:].strip()
                            if stripped.startswith("{"):
                                json_str = stripped
                                break
                    elif "{" in json_str:
                        start = json_str.index("{")
                        end = json_str.rindex("}") + 1
                        json_str = json_str[start:end]

                    actions = json.loads(json_str)

                    # Act: Reject noise hypotheses
                    rejected = 0
                    for hid in actions.get("reject", []):
                        try:
                            await self.hypothesis_manager.update_status(
                                hid, "rejected", "claude_interpret_backtests"
                            )
                            rejected += 1
                            self._rejections += 1
                        except Exception as e:
                            logger.warning(f"Failed to reject hypothesis {hid}: {e}")
                    if rejected:
                        logger.info(
                            f"Research: Claude interpretation rejected {rejected} "
                            f"noise hypotheses"
                        )

                    # Act: Modify thresholds for promising hypotheses
                    modified = 0
                    for mod in actions.get("modify", []):
                        try:
                            hid = mod.get("id")
                            new_thresh = mod.get("new_threshold")
                            reason = mod.get("reason", "claude_threshold_adjust")
                            if hid and new_thresh is not None:
                                await db.execute(
                                    "UPDATE hypotheses SET edge_threshold = ?, "
                                    "notes = COALESCE(notes, '') || ? "
                                    "WHERE hypothesis_id = ?",
                                    (
                                        new_thresh,
                                        f"\n[cycle {self._cycles}] threshold adjusted "
                                        f"to {new_thresh}: {reason}",
                                        hid,
                                    ),
                                )
                                await db.commit()
                                modified += 1
                        except Exception as e:
                            logger.warning(f"Failed to modify threshold for hypothesis {mod.get('id', '?')}: {e}")
                    if modified:
                        logger.info(
                            f"Research: Claude modified thresholds on {modified} hypotheses"
                        )

                    # Log insights
                    insights = actions.get("insights", "")
                    if insights:
                        logger.info(f"Research: Claude backtest insights — {insights[:300]}")

                except (json.JSONDecodeError, ValueError) as e:
                    logger.warning(f"Claude interpretation response not valid JSON: {e}")

            elif result.get("rate_limited"):
                logger.info("Research: Claude rate-limited during backtest interpretation")
        except Exception as e:
            logger.warning(f"Claude backtest interpretation failed: {e}")

    async def _phase_paper_trade(self) -> None:
        """Generate paper trade signals for promoted hypotheses.

        Uses DK scraper (free) as primary source for the target book's
        current lines, with Odds API as enrichment for cross-book data.
        This saves API credits while keeping paper trades accurate.
        """
        from datetime import datetime, timezone

        paper = await self.hypothesis_manager.list_hypotheses(status="paper_trading")

        if not paper:
            return

        # ── Auto-reject anti-predictive paper_trading hypotheses ──
        # IC < -0.10 means the model is inversely correlated with outcomes.
        # Don't waste paper trading cycles on these.
        # BUT: at n<20 IC is statistically meaningless (variance ~1/sqrt(n-3)),
        # so waive the gate for small samples — same logic as promotion gate.
        clean_paper = []
        for h in paper:
            try:
                db = self.data_collector._db
                cursor = await db.execute(
                    "SELECT information_coefficient, signals_n FROM hypothesis_stats "
                    "WHERE hypothesis_id = ?",
                    (h["hypothesis_id"],),
                )
                row = await cursor.fetchone()
                ic = row[0] if row else None
                n_signals = row[1] if row else 0
            except Exception:
                ic = None
                n_signals = 0
            if ic is not None and ic < -0.10 and n_signals >= 20:
                logger.warning(
                    f"Paper trade: rejecting {h['name']} (IC={ic:.3f}, n={n_signals}, anti-predictive)"
                )
                await self.hypothesis_manager.update_status(
                    h["hypothesis_id"], "rejected",
                    f"auto:anti_predictive_paper_trading — IC={ic:.3f} < -0.10 (n={n_signals})"
                )
                self._rejections += 1
            elif ic is not None and ic < -0.10 and n_signals < 20:
                logger.info(
                    f"Paper trade: waiving anti-predictive gate for {h['name']} "
                    f"(IC={ic:.3f}, n={n_signals}<20, statistically unreliable)"
                )
                clean_paper.append(h)
            else:
                clean_paper.append(h)
        paper = clean_paper

        if not paper:
            return

        logger.info(f"Research: paper trading {len(paper)} hypotheses")

        # Cache live odds per sport to avoid redundant API calls
        odds_cache: dict[str, dict] = {}

        for h in paper:
            if not self._running:
                break

            try:
                sport = h["sport"]
                market = h.get("market_type", "")

                # For player props: use Odds API prop scanner (DK scraper has no props)
                if market.startswith("player_"):
                    from tools.prop_scanner import scan_props_ev
                    from tools.odds_api_io import get_odds
                    import uuid as _uuid
                    # Get upcoming games for this sport
                    if sport not in odds_cache:
                        live_odds = await get_odds(
                            sport=sport, regions="us", markets="h2h",
                        )
                        if live_odds.get("error"):
                            logger.warning(
                                f"Paper trade: Odds API failed for {sport} props: "
                                f"{live_odds.get('error')} — skipping prop hypotheses"
                            )
                        elif not live_odds.get("games"):
                            logger.warning(
                                f"Paper trade: Odds API returned 0 games for {sport}"
                            )
                        else:
                            odds_cache[sport] = live_odds
                    games = odds_cache.get(sport, {}).get("games", [])
                    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                    now_iso = datetime.now(timezone.utc).isoformat()
                    for game in games[:3]:  # Limit to 3 games to conserve credits
                        event_id = game.get("id")
                        if not event_id:
                            continue
                        try:
                            result = await scan_props_ev(
                                sport=sport,
                                event_id=event_id,
                                target_book="draftkings",
                                edge_threshold=h["edge_threshold"],
                                prop_markets=market,
                            )
                            edges = result.get("edges", [])
                            if edges:
                                logger.info(
                                    f"Research: {len(edges)} prop edges for "
                                    f"{h['hypothesis_id']} in game {event_id}"
                                )
                                # Record each edge as a paper trade
                                db = self.data_collector._db
                                for edge_info in edges:
                                    trade_id = str(_uuid.uuid4())[:12]
                                    await db.execute(
                                        "INSERT OR IGNORE INTO paper_trades "
                                        "(trade_id, hypothesis_id, event_id, sport, player, market, "
                                        "line, side, book, signal_time, signal_odds_american, "
                                        "signal_implied_prob, model_fair_prob, edge, ev_pct, "
                                        "kelly_fraction, game_date) "
                                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                        (
                                            trade_id,
                                            h["hypothesis_id"],
                                            event_id,
                                            sport,
                                            edge_info.get("player"),
                                            market,
                                            edge_info.get("line"),
                                            edge_info.get("side", ""),
                                            "draftkings",
                                            now_iso,
                                            edge_info.get("target_price", 0),
                                            edge_info.get("target_implied", 0),
                                            edge_info.get("fair_probability", 0),
                                            round(edge_info.get("edge_pct", 0) / 100, 6),
                                            round(edge_info.get("ev_per_100", 0) / 100, 6),
                                            edge_info.get("kelly_fraction", 0),
                                            today,
                                        ),
                                    )
                                    # Also insert into signals table
                                    edge_val = round(edge_info.get("edge_pct", 0) / 100, 6)
                                    sig_confidence = _signal_confidence(edge_val)
                                    await db.execute(
                                        "INSERT INTO signals "
                                        "(event_id, sport, signal_type, team, market, book, "
                                        "odds_american, fair_probability, fair_prob_source, "
                                        "edge_pct, ev_pct, confidence, kelly_fraction, "
                                        "recommended_stake, status, notes) "
                                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                        (
                                            event_id,
                                            sport,
                                            "paper_trade",
                                            edge_info.get("side", ""),
                                            market,
                                            "draftkings",
                                            edge_info.get("target_price", 0),
                                            edge_info.get("fair_probability", 0),
                                            "cross_book_devig",
                                            edge_val,
                                            round(edge_info.get("ev_per_100", 0) / 100, 6),
                                            sig_confidence,
                                            edge_info.get("kelly_fraction", 0),
                                            None,
                                            "paper",
                                            f"hypothesis_id={h['hypothesis_id']}, trade_id={trade_id}",
                                        ),
                                    )
                                await db.commit()
                        except Exception as e:
                            logger.warning(f"Prop scan failed for {event_id}: {e}", exc_info=True)
                    continue

                # For game-level markets: line_monitor cache (instant, free) first,
                # then DK scraper (free but slow), then Odds API (costs credits)
                if sport not in odds_cache:
                    live_odds = {}

                    # Try line_monitor cache first — instant, no network call
                    if self.line_monitor:
                        snap = self.line_monitor._snapshots.get(sport, {})
                        if snap and not snap.get("error") and snap.get("games"):
                            live_odds = snap
                            logger.info(
                                f"Paper trade: using line_monitor cache for {sport} "
                                f"({len(snap.get('games', []))} games)"
                            )

                    # Fallback: DK scraper (free but slow — was causing 120s timeouts)
                    if not live_odds.get("games"):
                        from tools.dk_scraper import scrape_dk_odds
                        live_odds = await scrape_dk_odds(sport)

                    # DK scraper returns only 1 book (draftkings). Paper trading
                    # needs multi-book data for devigging to compute fair probs.
                    # Check if we have sufficient books, otherwise fall through.
                    _needs_multibook = True
                    if live_odds.get("games") and not live_odds.get("error"):
                        _sample_books = len(live_odds["games"][0].get("bookmakers", []))
                        if _sample_books < 2:
                            logger.info(
                                f"Paper trade: {sport} has only {_sample_books} book(s) "
                                f"(need ≥2 for devig) — enriching with Odds API"
                            )
                            _needs_multibook = True
                        else:
                            _needs_multibook = False

                    # Odds API: needed when no games OR single-book data
                    if live_odds.get("error") or not live_odds.get("games") or _needs_multibook:
                        from tools.odds_api_io import get_odds
                        _fallback_odds = live_odds
                        live_odds = await get_odds(
                            sport=sport,
                            regions="us",
                            markets="h2h,spreads,totals",
                        )
                        # If Odds API failed but we had line_monitor data, keep it
                        if (live_odds.get("error") or not live_odds.get("games")) and _fallback_odds.get("games"):
                            live_odds = _fallback_odds
                            logger.info(
                                f"Paper trade: Odds API failed for {sport}, "
                                f"using line_monitor data ({len(_fallback_odds.get('games', []))} games, single-book)"
                            )

                    if live_odds.get("error") or not live_odds.get("games"):
                        logger.warning(
                            f"Paper trade: no odds available for {sport} — "
                            f"line_monitor, DK scraper, and Odds API all failed"
                        )
                    else:
                        odds_cache[sport] = live_odds

                live_odds = odds_cache.get(sport)
                if not live_odds:
                    continue

                signals = await self.backtest_engine.generate_paper_trade_signal(
                    hypothesis_id=h["hypothesis_id"],
                    live_odds=live_odds,
                )

                if signals:
                    logger.info(
                        f"Research: {len(signals)} paper trade signals for "
                        f"hypothesis {h['hypothesis_id']}"
                    )
            except Exception as e:
                logger.warning(
                    f"Paper trading failed for {h['hypothesis_id']}: {e}"
                )

    async def _phase_claude_deep_work(self) -> None:
        """
        Karpathy-style: maximize Claude Code throughput.

        Claude is NOT used for generic analysis text. Every call must produce
        ACTIONABLE output: hypotheses to create, hypotheses to reject, or
        specific pipeline fixes. If it can't act, it shouldn't call.

        Cooldown-gated to prevent burst stalls — deep work is valuable but
        firing it 10s after interpret_backtests was causing 5x/day rate limit hits.

        When Claude is unavailable: defers the prompt to work queue AND
        runs local model fallback for basic maintenance (reject zero-signal
        hypotheses, gather pipeline metrics).
        """
        from tools.claude_code import claude_code_query
        import time as _time

        if not self._claude_ok():
            # Local model deep work via model ladder (Qwen3-14B primary)
            # Much better than rule-based: structured diagnosis, JSON output
            try:
                from inference import escalate_with_ladder
                # Build a condensed pipeline summary for the local model
                db = self.data_collector._db
                bt_total = 0
                bt_signals = 0
                try:
                    cursor = await db.execute("SELECT COUNT(*), SUM(CASE WHEN signals_generated > 0 THEN 1 ELSE 0 END) FROM backtest_runs")
                    row = await cursor.fetchone()
                    bt_total, bt_signals = row[0] or 0, row[1] or 0
                except Exception:
                    pass

                local_prompt = (
                    f"You are diagnosing a sports betting research pipeline.\n"
                    f"Backtest runs: {bt_total}, with signals: {bt_signals}\n"
                    f"Signal rate: {bt_signals*100//max(bt_total,1)}%\n"
                    f"Respond ONLY with JSON:\n"
                    f'{{"pipeline_issues": ["specific problem"], "reject_ids": [], "actions": ["specific fix"]}}'
                )
                result = await escalate_with_ladder(
                    local_prompt, task_type="deep_work", timeout=30,
                )
                if result.get("content") and result.get("model_used") != "none":
                    logger.info(
                        f"Research: local deep work via {result['model_used']} "
                        f"({len(result['content'])} chars)"
                    )
            except Exception as e:
                logger.debug(f"Local model deep work failed: {e}")

            # Also run rule-based fallback for guaranteed maintenance
            try:
                from tools.work_queue import local_fallback_deep_work
                actions = await local_fallback_deep_work(db)
                rejected = 0
                for hid in actions.get("reject_ids", []):
                    try:
                        await self.hypothesis_manager.update_status(
                            hid, "rejected", "local_fallback_deep_work"
                        )
                        rejected += 1
                        self._rejections += 1
                    except Exception:
                        pass
                if rejected:
                    logger.info(f"Research: local fallback rejected {rejected} hypotheses")
            except Exception as e:
                logger.debug(f"Rule-based fallback failed: {e}")

            _defer_deep_work = True
        else:
            _defer_deep_work = False

        logger.info("Research: Claude deep work phase — actionable output only")

        # Gather pipeline metrics for Claude
        db = self.data_collector._db
        metrics = {}
        try:
            # Backtest signal rate (deduplicated by event_id — each game
            # produces multiple rows across books, COUNT(*) overcounts)
            row = await db.execute_fetchone(
                "SELECT COUNT(DISTINCT event_id) total, "
                "COUNT(DISTINCT CASE WHEN signal_generated=1 THEN event_id END) signals "
                "FROM backtest_events"
            ) if hasattr(db, 'execute_fetchone') else None
            if not row:
                cursor = await db.execute(
                    "SELECT COUNT(DISTINCT event_id) total, "
                    "COUNT(DISTINCT CASE WHEN signal_generated=1 THEN event_id END) signals "
                    "FROM backtest_events"
                )
                row = await cursor.fetchone()
            metrics["bt_events"] = (row[0] or 0) if row else 0
            metrics["bt_signals"] = (row[1] or 0) if row else 0

            # Avg books in historical odds
            cursor = await db.execute(
                "SELECT sport, COUNT(*) FROM historical_odds_cache GROUP BY sport"
            )
            metrics["odds_cache"] = {r[0]: r[1] for r in await cursor.fetchall()}

            # Hypothesis stats
            cursor = await db.execute(
                "SELECT status, COUNT(*) FROM hypotheses GROUP BY status"
            )
            metrics["hypo_status"] = {r[0]: r[1] for r in await cursor.fetchall()}
        except Exception as e:
            logger.warning(f"Failed to gather metrics for deep work: {e}")

        # Get top backtesting hypotheses by UNIQUE signal count
        # (dedup by event_id to match evaluate_significance, which keeps
        # best-edge row per event — raw row counts inflate signal/event totals)
        top_hypos = []
        try:
            cursor = await db.execute("""
                SELECT h.hypothesis_id, h.name, h.thesis, h.sport, h.status,
                       COUNT(DISTINCT CASE WHEN be.signal_generated=1 THEN be.event_id END) as sigs,
                       COUNT(DISTINCT be.event_id) as events,
                       AVG(CASE WHEN be.signal_generated=1 THEN be.edge END) as avg_edge,
                       br.actual_win, br.actual_loss, br.p_value_binomial
                FROM hypotheses h
                LEFT JOIN backtest_events be ON be.hypothesis_id = h.hypothesis_id
                LEFT JOIN backtest_runs br ON br.hypothesis_id = h.hypothesis_id
                WHERE h.status IN ('backtesting', 'paper_trading')
                GROUP BY h.hypothesis_id
                ORDER BY sigs DESC, events DESC
                LIMIT 15
            """)
            for r in await cursor.fetchall():
                w = r[8] or 0
                l = r[9] or 0
                p = r[10] or 1.0
                wr = w / (w + l) * 100 if (w + l) > 0 else 0
                status_tag = f"[{r[4]}]" if r[4] == "paper_trading" else ""
                top_hypos.append(
                    f"  {r[1]} [{r[3]}]{status_tag}: {r[5]} signals / {r[6]} events, "
                    f"avg_edge={r[7] or 0:.4f}, {w}W-{l}L ({wr:.0f}%), p={p:.4f}"
                )
        except Exception as e:
            logger.warning(f"Failed to query top hypotheses for deep work prompt: {e}")

        # Self-scrutiny: check if hypotheses are testing the same games
        scrutiny_info = ""
        try:
            cursor = await db.execute("""
                SELECT h.hypothesis_id, h.name,
                       COUNT(DISTINCT be.event_id) as unique_events,
                       COUNT(DISTINCT CASE WHEN be.signal_generated=1 THEN be.event_id END) as unique_signals
                FROM hypotheses h
                JOIN backtest_events be ON be.hypothesis_id = h.hypothesis_id
                WHERE h.status IN ('backtesting', 'paper_trading')
                GROUP BY h.hypothesis_id
                HAVING unique_events > 0
                ORDER BY unique_events DESC
                LIMIT 10
            """)
            game_sets = []
            for r in await cursor.fetchall():
                game_sets.append(f"  {r[1]}: {r[2]} unique events, {r[3]} signals")

            # Check for duplicate game sets (different hypotheses testing identical events)
            cursor2 = await db.execute("""
                SELECT GROUP_CONCAT(DISTINCT h.name) as hypo_names,
                       COUNT(DISTINCT be.event_id) as unique_events,
                       COUNT(DISTINCT CASE WHEN be.signal_generated=1 THEN be.event_id END) as unique_signals
                FROM hypotheses h
                JOIN backtest_events be ON be.hypothesis_id = h.hypothesis_id
                WHERE h.status IN ('backtesting', 'paper_trading')
                GROUP BY h.hypothesis_id
                HAVING unique_events > 10
            """)
            event_counts = {}
            for r in await cursor2.fetchall():
                key = f"{r[1]}e_{r[2]}s"
                if key not in event_counts:
                    event_counts[key] = []
                event_counts[key].append(r[0])
            duplicates = {k: v for k, v in event_counts.items() if len(v) > 1}

            if game_sets or duplicates:
                scrutiny_info = "\nBACKTEST SCRUTINY:\n"
                if game_sets:
                    scrutiny_info += "  Event counts per hypothesis:\n" + "\n".join(game_sets) + "\n"
                if duplicates:
                    scrutiny_info += (
                        "  WARNING: These hypotheses tested IDENTICAL game sets "
                        "(same unique_games and total_events):\n"
                    )
                    for k, names in duplicates.items():
                        scrutiny_info += f"    {k}: {', '.join(names)}\n"
                    scrutiny_info += (
                        "  This suggests backtests are NOT filtering for hypothesis-specific conditions.\n"
                        "  Different hypotheses should produce DIFFERENT event sets.\n"
                    )
        except Exception as e:
            logger.warning(f"Failed to gather scrutiny metrics: {e}")

        prompt = (
            f"CALLISTO AUTONOMOUS SYSTEM — DEEP WORK CYCLE #{self._cycles}\n"
            f"You are a critical analyst auditing an autonomous research pipeline.\n"
            f"Your primary obligation is honesty about what is working and what is broken.\n"
            f"pipeline_issues takes PRIORITY over new_hypotheses — fix the funnel before "
            f"pouring more hypotheses into it.\n\n"
            f"PIPELINE STATE:\n"
            f"  Backtest events: {metrics.get('bt_events') or 0} total, "
            f"{metrics.get('bt_signals') or 0} signals ({((metrics.get('bt_signals') or 0) / max(1,(metrics.get('bt_events') or 1))) * 100:.1f}%)\n"
            f"  Hypothesis status: {json.dumps(metrics.get('hypo_status', {}))}\n"
            f"  Odds cache: {json.dumps(metrics.get('odds_cache', {}))}\n"
            f"  Promotions: {self._promotions} | Rejections: {self._rejections}\n"
            f"  Cycles: {self._cycles} | Data collections: {self._data_collections}\n\n"
            f"CRITICAL QUESTIONS (answer these honestly before generating anything):\n"
            f"  1. What is the promotion rate? If 0%, why — bad hypotheses or broken pipeline?\n"
            f"  2. Signal rate {((metrics.get('bt_signals') or 0) / max(1,(metrics.get('bt_events') or 1))) * 100:.1f}% — "
            f"is this because edges don't exist, or because devig uses too few books?\n"
            f"  3. Are hypotheses getting a fair trial, or dying before results are collected?\n\n"
            f"ALL SPORTS RESEARCHED EQUALLY: {RESEARCH_SPORTS}\n\n"
            f"TOP HYPOTHESES BY SIGNALS:\n"
            + ("\n".join(top_hypos) if top_hypos else "  (none with signals)") + "\n\n"
            f"RESPOND WITH EXACTLY THIS JSON STRUCTURE (no other text):\n"
            f'{{"pipeline_issues": ["MOST IMPORTANT — specific, actionable problems"], '
            f'"reject_ids": ["hypothesis_id1", ...], '
            f'"promising_sports": ["sport1", ...], '
            f'"new_hypotheses": [{{"name": "...", "thesis": "...", "sport": "...", '
            f'"market_type": "...", "edge_threshold": 0.015}}]}}\n\n'
            f"{scrutiny_info}\n"
            f"RULES:\n"
            f"- pipeline_issues FIRST: what is structurally preventing any hypothesis from succeeding?\n"
            f"  - If multiple hypotheses tested the EXACT SAME events, that is a filtering bug\n"
            f"  - If 0 promotions after {self._cycles} cycles, diagnose the bottleneck explicitly\n"
            f"  - If devig uses <3 books on most events, the edge detection is unreliable\n"
            f"- reject_ids: ONLY hypotheses with 50+ events, 0 signals, AND adequate data quality\n"
            f"- new_hypotheses: 3-5 NOVEL, testable — but ONLY if the pipeline can actually test them.\n"
            f"  If the funnel is broken, say so and generate 0.\n"
            f"  BANNED TOPICS: generic rest/B2B, home underdog ATS, eliminated team fades, basic weather.\n"
            f"  Vegas already prices these. Focus on dimensions models DON'T have columns for:\n"
            f"  team identity/cohesion, roster sociology, ref biases, scheme geometry,\n"
            f"  SGP correlation mispricing, media narrative inflation, calendar quirks.\n"
        )

        if _defer_deep_work:
            await self._work_queue.enqueue("deep_work", prompt, priority=3)
            self._downtime_tracker.item_queued()
            logger.info("Research: deep work prompt deferred to work queue (Claude unavailable)")
            return

        remaining = CLAUDE_ESCALATION_COOLDOWN - (_time.time() - self._last_claude_call)
        if remaining > 0:
            logger.debug(f"Deep work: cooldown active ({remaining:.0f}s left), deferring to next cycle")
            return

        try:
            result = await claude_code_query(prompt, hermes_caller="deep_work")
            self._last_claude_call = _time.time()
            self._claude_escalations += 1

            if result.get("content") and not result.get("error"):
                content = result["content"]
                logger.info(f"Research: Claude deep work response — {len(content)} chars")

                # Write learnings back to Hermes from the deep work output
                try:
                    from tools.hermes_memory import get_hermes_memory
                    hermes = get_hermes_memory()
                    # Store a summary learning from this deep work cycle
                    await hermes.record_learning(
                        key=f"deep_work_cycle_{self._cycles}",
                        value=content[:500],
                        confidence=0.6,
                        source="deep_work",
                    )
                except Exception as e:
                    logger.debug(f"Failed to record deep work learning: {e}")

                # Parse and ACT on the structured response
                try:
                    # Extract JSON from response (may have markdown fences)
                    json_str = content
                    if "```" in json_str:
                        parts = json_str.split("```")
                        for part in parts:
                            stripped = part.strip()
                            if stripped.startswith("json"):
                                stripped = stripped[4:].strip()
                            if stripped.startswith("{"):
                                json_str = stripped
                                break
                    elif "{" in json_str:
                        start = json_str.index("{")
                        end = json_str.rindex("}") + 1
                        json_str = json_str[start:end]

                    actions = json.loads(json_str)

                    # Act 1: Reject hopeless hypotheses
                    rejected = 0
                    for hid in actions.get("reject_ids", []):
                        try:
                            await self.hypothesis_manager.update_status(
                                hid, "rejected", "claude_deep_work"
                            )
                            rejected += 1
                            self._rejections += 1
                        except Exception as e:
                            logger.warning(f"Failed to reject hypothesis {hid} in deep work: {e}")
                    if rejected:
                        logger.info(f"Research: Claude rejected {rejected} hopeless hypotheses")

                    # Act 2: Create new hypotheses
                    created = 0
                    for nh in actions.get("new_hypotheses", []):
                        try:
                            _dw_config = {
                                    "source": "claude_deep_work",
                                    "cycle": self._cycles,
                                    "training_period_start": "2023-01-01",
                                    "training_period_end": str(
                                        datetime.now(timezone.utc).date()
                                        - timedelta(days=DEFAULT_TRAINING_WINDOW_DAYS)
                                    ),
                                    "forward_test_start": str(
                                        datetime.now(timezone.utc).date()
                                        - timedelta(days=DEFAULT_TRAINING_WINDOW_DAYS)
                                        + timedelta(days=BACKTEST_GAP_DAYS)
                                    ),
                            }
                            if nh.get("game_filters"):
                                _dw_config["game_filters"] = nh["game_filters"]
                            if nh.get("line_filters"):
                                _dw_config["line_filters"] = nh["line_filters"]
                            await self.hypothesis_manager.create_hypothesis(
                                name=nh.get("name", "claude_generated"),
                                thesis=nh.get("thesis", ""),
                                sport=nh.get("sport", "basketball_nba"),
                                market_type=nh.get("market_type", "spreads"),
                                edge_threshold=nh.get("edge_threshold", 0.015),
                                model_config=_dw_config,
                            )
                            created += 1
                        except Exception as e:
                            logger.warning(f"Failed to create hypothesis '{nh.get('name', '?')}' in deep work: {e}")
                    if created:
                        self._hypotheses_generated += created
                        logger.info(f"Research: Claude created {created} new hypotheses")

                    # Act 3: Convert pipeline issues into structured findings
                    # and route them to self-repair for automatic fixes
                    pipeline_issues = actions.get("pipeline_issues", [])
                    if pipeline_issues:
                        findings = []
                        for issue in pipeline_issues:
                            logger.warning(f"Research: Claude identified issue — {issue}")
                            # Classify severity based on keywords
                            issue_lower = issue.lower() if isinstance(issue, str) else ""
                            if any(kw in issue_lower for kw in ["identical", "same games", "filtering bug", "broken"]):
                                severity = "CRITICAL"
                            elif any(kw in issue_lower for kw in ["prioritize", "threshold", "zero promotion", "low sample"]):
                                severity = "HIGH"
                            else:
                                severity = "LOW"
                            findings.append({"severity": severity, "description": issue})

                        # Route to self-repair engine
                        try:
                            from tools.self_repair import get_repair_engine
                            engine = get_repair_engine()
                            repair_results = await engine.handle_claude_findings(findings)
                            for r in repair_results:
                                if r["fixed"]:
                                    logger.info(f"Deep work auto-fix: {r['action']} — {r['detail']}")
                                else:
                                    logger.warning(f"Deep work unfixed: {r['action']} — {r['detail']}")
                        except Exception as e:
                            logger.warning(f"Failed to route findings to self-repair: {e}")

                except (json.JSONDecodeError, ValueError) as e:
                    logger.warning(f"Claude deep work returned non-JSON: {e}")
                    # Still store the raw analysis as fallback
                    try:
                        await db.execute(
                            "INSERT INTO game_contexts "
                            "(sport, game_date, home_team, away_team, context_json, embedded) "
                            "VALUES (?, ?, ?, ?, ?, 1)",
                            (
                                "meta_research",
                                _time.strftime("%Y-%m-%d"),
                                "callisto", "self_analysis",
                                json.dumps({
                                    "type": "claude_deep_analysis",
                                    "cycle": self._cycles,
                                    "raw": content[:5000],
                                }),
                            ),
                        )
                        await db.commit()
                    except Exception as e:
                        logger.warning(f"Failed to store raw deep analysis fallback: {e}")

            elif result.get("rate_limited"):
                logger.info("Research: Claude rate limited — will retry next cycle")
        except Exception as e:
            logger.warning(f"Claude deep work failed: {e}", exc_info=True)

    async def _phase_granger_analysis(self) -> None:
        """Granger temporal prediction phase — identify which books lead each sport.

        Runs weekly (every ~100 cycles at 1-min intervals). Checks the most
        recent computed_at timestamp in granger_results and skips if the last
        analysis is less than 7 days old.

        Results feed into edge_scanner's dynamic sharp book classification:
        when a book is identified as the temporal leader for a sport, it is
        added to the sharp set for edge detection in that sport.
        """
        import aiosqlite
        from tools.granger_causality import analyze_book_leadership, store_results

        db_path = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")

        # Check if we ran recently (within 7 days) — skip if so
        try:
            async with aiosqlite.connect(db_path) as db:
                await db.execute("PRAGMA busy_timeout = 60000")
                cursor = await db.execute(
                    "SELECT MAX(computed_at) FROM granger_results"
                )
                row = await cursor.fetchone()
                if row and row[0]:
                    last_computed = datetime.fromisoformat(row[0])
                    age_days = (datetime.now(timezone.utc) - last_computed).total_seconds() / 86400
                    if age_days < 7:
                        logger.debug(
                            f"Granger analysis: last run {age_days:.1f} days ago, "
                            f"skipping (< 7 days)"
                        )
                        return
        except Exception as e:
            # Table might not exist yet or be empty — proceed with analysis
            logger.debug(f"Granger recency check failed (will run analysis): {e}")

        # Run analysis for all sports
        total_stored = 0
        for sport in RESEARCH_SPORTS:
            if not self._running:
                break
            try:
                results = await analyze_book_leadership(db_path, sport)
                leader = results.get("leader_book")
                score = results.get("leader_score", 0)
                n_pairs = results.get("n_pairs_tested", 0)

                if results.get("warning"):
                    logger.info(
                        f"Granger {sport}: {results['warning']}"
                    )
                    continue

                if leader:
                    logger.info(
                        f"Granger {sport}: leader={leader} "
                        f"(score={score:.3f}, pairs={n_pairs}, "
                        f"books={results.get('books_tested', [])})"
                    )
                else:
                    logger.info(
                        f"Granger {sport}: no clear leader "
                        f"(pairs={n_pairs}, books={results.get('books_tested', [])})"
                    )

                stored = await store_results(db_path, results)
                total_stored += stored

                # Update edge_scanner's cache immediately
                if leader:
                    from tools.edge_scanner import _granger_sharp_cache
                    _granger_sharp_cache[sport] = (leader, time.time())

            except Exception as e:
                logger.warning(f"Granger analysis failed for {sport}: {e}")

        if total_stored:
            logger.info(
                f"Granger phase complete: {total_stored} results stored "
                f"across {len(focus_sports)} sports"
            )

        # Record phase success for pipeline integrity tracking
        try:
            from tools.pipeline_integrity import get_checker
            get_checker().record_phase_result("granger_analysis", True)
        except Exception:
            pass

    async def _phase_regime_analysis(self) -> None:
        """Regime analysis phase — detect regime changes, recency bias, mean reversion.

        Runs every REGIME_ANALYSIS_INTERVAL cycles (regime changes are slow —
        no point re-analyzing every minute). Results are cached in
        _regime_cache and fed into:
          1. Edge confidence scoring (via regime_data parameter)
          2. Hypothesis generation (regime context in Claude prompt)
          3. Edge candidate enrichment (regime signals on candidates)

        Uses ESPN box score data already collected by data_collector to build
        per-team performance histories, then runs full_regime_analysis() from
        tools/regime.py.
        """
        if self._cycles % REGIME_ANALYSIS_INTERVAL != 0:
            return

        from tools.regime import full_regime_analysis

        logger.info("Research: running regime analysis phase")

        # Map sport keys to the short sport name used by regime.py
        sport_short_map = {
            "basketball_nba": "nba",
            "americanfootball_nfl": "nfl",
            "icehockey_nhl": "nhl",
            "baseball_mlb": "mlb",
            "basketball_ncaab": "ncaab",
            "basketball_ncaaw": "ncaaw",
            "basketball_wnba": "wnba",
        }

        db = self.data_collector._db
        if db is None:
            logger.warning("Regime analysis: data_collector DB not initialized")
            return

        total_analyzed = 0
        total_signals = 0

        for sport in RESEARCH_SPORTS:
            if not self._running:
                break

            sport_short = sport_short_map.get(sport, "nba")

            try:
                # Query box score data for team performance histories.
                # box_scores table has: sport, game_date, team_name, points,
                # opponent_points, plus advanced stats when available.
                cursor = await db.execute(
                    "SELECT team_name, points FROM box_scores "
                    "WHERE sport = ? AND points IS NOT NULL "
                    "ORDER BY game_date ASC",
                    (sport,),
                )
                rows = await cursor.fetchall()

                if not rows:
                    logger.debug(f"Regime analysis: no box score data for {sport}")
                    continue

                # Group by team
                from collections import defaultdict
                team_histories: dict[str, list[float]] = defaultdict(list)
                for team_name, points in rows:
                    if team_name and points is not None:
                        team_histories[team_name].append(float(points))

                if not team_histories:
                    continue

                # Compute league average for this sport
                all_points = []
                for pts_list in team_histories.values():
                    all_points.extend(pts_list)
                league_avg = sum(all_points) / len(all_points) if all_points else 100.0

                # Run regime analysis for each team with enough data
                for team_name, history in team_histories.items():
                    if len(history) < 8:
                        continue  # Need minimum data for meaningful analysis

                    try:
                        team_data = {
                            "name": team_name,
                            "performance_history": history,
                            "league_avg": league_avg,
                        }
                        result = full_regime_analysis(team_data, sport=sport_short)

                        # Cache the result keyed by team name
                        cache_key = f"{sport}:{team_name}"
                        _regime_cache[cache_key] = result
                        total_analyzed += 1

                        if result.get("has_edge_signal"):
                            total_signals += 1
                            logger.info(
                                f"Regime signal: {team_name} ({sport_short}) — "
                                f"{result.get('actionable_signals', [])}"
                            )
                    except Exception as e:
                        logger.debug(f"Regime analysis failed for {team_name}: {e}")

            except Exception as e:
                logger.warning(f"Regime analysis failed for {sport}: {e}")

        self._last_regime_analysis = time.time()

        if total_analyzed > 0:
            logger.info(
                f"Regime analysis complete: {total_analyzed} teams analyzed, "
                f"{total_signals} with actionable signals, "
                f"cache size: {len(_regime_cache)}"
            )

        # Record phase success for pipeline integrity tracking
        try:
            from tools.pipeline_integrity import get_checker
            get_checker().record_phase_result("regime_analysis", True)
        except Exception:
            pass

    def get_regime_for_team(self, sport: str, team_name: str) -> Optional[dict]:
        """Look up cached regime analysis for a team.

        Args:
            sport: Sport key (e.g., "basketball_nba")
            team_name: Team name as it appears in box scores

        Returns:
            Full regime analysis dict or None if not cached.
        """
        cache_key = f"{sport}:{team_name}"
        result = _regime_cache.get(cache_key)
        if result:
            return result
        # Try partial match — team names can vary (e.g., "Boston Celtics" vs "Celtics")
        for key, val in _regime_cache.items():
            if key.startswith(sport + ":") and team_name.lower() in key.lower():
                return val
        return None

    async def _phase_knowledge_compile(self) -> None:
        """Knowledge wiki compilation — LLM Wiki pattern (Karpathy).

        Reads recent sessions/evidence/learnings and compiles them into
        persistent, cross-referenced wiki articles. Knowledge compounds
        instead of being re-discovered each time.

        Runs every COMPILE_INTERVAL_CYCLES (7) — coprime with lint (11).
        Uses Gemma 4 (local, free) for compilation.
        """
        from tools.knowledge_wiki import get_wiki, COMPILE_INTERVAL_CYCLES

        if self._cycles % COMPILE_INTERVAL_CYCLES != 0:
            return

        db = self.data_collector._db
        if not db:
            return

        try:
            wiki = get_wiki()
            stats = await wiki.compile(db, self._cycles)
            created = stats.get("articles_created", 0)
            updated = stats.get("articles_updated", 0)
            if created or updated:
                logger.info(
                    f"Wiki compile: {created} new articles, {updated} updated"
                )
            try:
                from tools.pipeline_integrity import get_checker
                get_checker().record_phase_result("knowledge_compile", True)
            except Exception:
                pass
        except Exception as e:
            logger.warning(f"Knowledge compile phase failed: {e}")
            try:
                from tools.pipeline_integrity import get_checker
                get_checker().record_phase_result("knowledge_compile", False)
            except Exception:
                pass

    async def _phase_knowledge_lint(self) -> None:
        """Knowledge wiki lint — detect contradictions, stale claims, orphans.

        Scans wiki articles for:
          - Contradictions: conflicting claims between articles
          - Stale articles: not updated in >72 hours
          - Orphans: articles with no cross-references

        Runs every LINT_INTERVAL_CYCLES (11) — coprime with compile (7).
        Uses Qwen 3.5 4B (ultra-fast classifier) for contradiction detection.
        """
        from tools.knowledge_wiki import get_wiki, LINT_INTERVAL_CYCLES

        if self._cycles % LINT_INTERVAL_CYCLES != 0:
            return

        db = self.data_collector._db
        if not db:
            return

        try:
            wiki = get_wiki()
            stats = await wiki.lint(db, self._cycles)

            # Alert on high-severity contradictions
            contradictions = stats.get("contradictions_found", 0)
            if contradictions > 0:
                try:
                    from tools import telegram
                    await telegram.alert_system(
                        f"Wiki lint: {contradictions} contradictions detected. "
                        f"Stale: {stats.get('stale_articles', 0)}, "
                        f"Orphans: {stats.get('orphan_articles', 0)}"
                    )
                except Exception:
                    pass

            try:
                from tools.pipeline_integrity import get_checker
                get_checker().record_phase_result("knowledge_lint", True)
            except Exception:
                pass
        except Exception as e:
            logger.warning(f"Knowledge lint phase failed: {e}")
            try:
                from tools.pipeline_integrity import get_checker
                get_checker().record_phase_result("knowledge_lint", False)
            except Exception:
                pass

    async def _phase_system_improvement(self) -> None:
        """Self-improvement phase — runs every SYSTEM_IMPROVEMENT_INTERVAL cycles.

        Asks Claude to review pipeline metrics and suggest specific code
        improvements. Stores suggestions in a system_improvements table.
        This is how the system learns to improve itself over time.
        """
        if self._cycles % SYSTEM_IMPROVEMENT_INTERVAL != 0:
            return

        from tools.claude_code import claude_code_query

        now = time.time()
        remaining = CLAUDE_ESCALATION_COOLDOWN - (now - self._last_claude_call)
        if remaining > 0:
            logger.debug(f"System improvement: cooldown active ({remaining:.0f}s left), deferring to next interval")
            return

        db = self.data_collector._db
        if not db:
            return

        # Ensure system_improvements table exists
        try:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS system_improvements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cycle INTEGER NOT NULL,
                    category TEXT NOT NULL,
                    suggestion TEXT NOT NULL,
                    priority TEXT NOT NULL DEFAULT 'medium',
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    implemented_at DATETIME
                )
            """)
            await db.commit()
        except Exception as e:
            logger.warning(f"Failed to create system_improvements table: {e}")
            return

        # Gather comprehensive pipeline metrics
        metrics = {}
        try:
            # Hypothesis pipeline funnel
            cursor = await db.execute(
                "SELECT status, COUNT(*) FROM hypotheses GROUP BY status"
            )
            metrics["hypothesis_funnel"] = {r[0]: r[1] for r in await cursor.fetchall()}

            # Backtest throughput
            cursor = await db.execute(
                "SELECT COUNT(*) total, "
                "SUM(CASE WHEN signal_generated=1 THEN 1 ELSE 0 END) signals, "
                "SUM(CASE WHEN signal_generated=1 AND actual_result='won' THEN 1 ELSE 0 END) wins, "
                "SUM(CASE WHEN signal_generated=1 AND actual_result='lost' THEN 1 ELSE 0 END) losses, "
                "SUM(CASE WHEN actual_result IS NULL THEN 1 ELSE 0 END) unresolved "
                "FROM backtest_events"
            )
            row = await cursor.fetchone()
            if row:
                metrics["backtest_totals"] = {
                    "events": row[0] or 0, "signals": row[1] or 0,
                    "wins": row[2] or 0, "losses": row[3] or 0,
                    "unresolved": row[4] or 0,
                }

            # Data coverage
            cursor = await db.execute(
                "SELECT sport, COUNT(*), MIN(snapshot_date), MAX(snapshot_date) "
                "FROM historical_odds_cache GROUP BY sport"
            )
            metrics["data_coverage"] = {
                r[0]: {"records": r[1], "from": r[2], "to": r[3]}
                for r in await cursor.fetchall()
            }

            # Loop performance
            metrics["loop_stats"] = {
                "cycles": self._cycles,
                "data_collections": self._data_collections,
                "hypotheses_generated": self._hypotheses_generated,
                "backtests_run": self._backtests_run,
                "claude_escalations": self._claude_escalations,
                "promotions": self._promotions,
                "rejections": self._rejections,
            }

            # Previous improvements (to avoid repetition)
            cursor = await db.execute(
                "SELECT suggestion FROM system_improvements "
                "ORDER BY created_at DESC LIMIT 20"
            )
            metrics["recent_suggestions"] = [r[0] for r in await cursor.fetchall()]

        except Exception as e:
            logger.warning(f"Failed to gather metrics for system improvement: {e}")

        prompt = (
            f"CALLISTO SYSTEM IMPROVEMENT REVIEW — Cycle #{self._cycles}\n\n"
            f"You are an adversarial auditor of this pipeline. Your job is to find "
            f"the single biggest bottleneck and propose a concrete fix.\n\n"
            f"PIPELINE METRICS:\n{json.dumps(metrics, indent=2)}\n\n"
            f"RECENT SUGGESTIONS (already made, avoid repeating):\n"
            f"{json.dumps(metrics.get('recent_suggestions', []))}\n\n"
            f"RESPOND WITH EXACTLY THIS JSON (no other text):\n"
            f'{{"diagnosis": "1-sentence root cause of why 0 hypotheses have been promoted", '
            f'"improvements": [\n'
            f'  {{"category": "data_collection|hypothesis_gen|backtesting|evaluation|infrastructure", '
            f'"suggestion": "Specific actionable improvement", '
            f'"priority": "high|medium|low", '
            f'"rationale": "Why this would help based on the metrics"}}\n'
            f"]}}\n\n"
            f"RULES:\n"
            f"- diagnosis FIRST: why has the promotion rate been 0%? Be brutally specific.\n"
            f"- 2-4 suggestions, ranked by impact on the BOTTLENECK (not generic improvements)\n"
            f"- Each must be specific and implementable (not vague)\n"
            f"- If the bottleneck is data quality (few books, thin markets), say so\n"
            f"- If the bottleneck is evaluation criteria (too strict), say so\n"
            f"- Do NOT suggest generating more hypotheses if the funnel is broken\n"
            f"- Do NOT repeat recent suggestions\n"
        )

        if not self._claude_ok():
            # Defer system improvement to queue for when Claude returns
            await self._work_queue.enqueue("system_improvement", prompt, priority=4)
            self._downtime_tracker.item_queued()
            logger.info("Research: system improvement deferred to work queue (Claude unavailable)")
            return

        try:
            result = await claude_code_query(prompt, hermes_caller="deep_work")
            self._last_claude_call = time.time()
            self._claude_escalations += 1

            if result.get("content") and not result.get("error"):
                content = result["content"]
                try:
                    json_str = content
                    if "```" in json_str:
                        parts = json_str.split("```")
                        for part in parts:
                            stripped = part.strip()
                            if stripped.startswith("json"):
                                stripped = stripped[4:].strip()
                            if stripped.startswith("{"):
                                json_str = stripped
                                break
                    elif "{" in json_str:
                        start = json_str.index("{")
                        end = json_str.rindex("}") + 1
                        json_str = json_str[start:end]

                    parsed = json.loads(json_str)
                    stored = 0
                    for imp in parsed.get("improvements", []):
                        try:
                            await db.execute(
                                "INSERT INTO system_improvements "
                                "(cycle, category, suggestion, priority) "
                                "VALUES (?, ?, ?, ?)",
                                (
                                    self._cycles,
                                    imp.get("category", "general"),
                                    imp.get("suggestion", ""),
                                    imp.get("priority", "medium"),
                                ),
                            )
                            stored += 1
                        except Exception as e:
                            logger.warning(f"Failed to store system improvement suggestion: {e}")
                    if stored:
                        await db.commit()
                        logger.info(
                            f"Research: system improvement stored {stored} suggestions "
                            f"at cycle #{self._cycles}"
                        )

                except (json.JSONDecodeError, ValueError) as e:
                    logger.warning(f"System improvement response not valid JSON: {e}")

            elif result.get("rate_limited"):
                logger.info("Research: Claude rate-limited during system improvement")
        except Exception as e:
            logger.warning(f"System improvement phase failed: {e}")

    async def _phase_system_watchdog(self) -> None:
        """Hermes-powered system watchdog — detects orphaned features and stale pipelines.

        Runs every 10 cycles. Checks that committed features actually produce data,
        hot tables are receiving writes, and enrichment coverage is adequate.
        Records findings to Hermes for cross-session awareness and escalates
        critical issues to Claude deep work.
        """
        from tools.hermes_memory import get_hermes_memory

        db = self.hypothesis_manager._db
        if not db:
            return

        findings = []

        try:
            # 1. Orphaned table detection — tables that SHOULD have data but don't
            orphan_checks = {
                "clv_log": ("bets WHERE result != 'pending'", "Resolved bets exist but CLV not logged"),
                "paper_trades": ("hypotheses WHERE status = 'paper_trading'", "Paper trading hypotheses exist but 0 trades"),
                "market_microstructure": ("odds_snapshots", "Snapshots exist but microstructure never computed"),
                "closing_lines": ("odds_snapshots", "Snapshots exist but closing lines never captured"),
            }
            from tools.db_utils import safe_ident
            # source_query is a literal SQL fragment (e.g., "bets WHERE result != 'pending'");
            # validate only its leading identifier. The remainder is hard-coded above and
            # is NOT user-derived — but reject any source_query whose first token isn't a
            # plain table name to defeat future careless edits.
            for target, (source_query, msg) in orphan_checks.items():
                try:
                    safe_ident(target)
                    first_tok = source_query.split()[0] if source_query else ""
                    safe_ident(first_tok)
                    target_cnt = (await (await db.execute(f"SELECT COUNT(*) FROM {target}")).fetchone())[0]
                    source_cnt = (await (await db.execute(f"SELECT COUNT(*) FROM {source_query}")).fetchone())[0]
                    if target_cnt == 0 and source_cnt > 0:
                        findings.append(f"ORPHAN: {target} empty — {msg}")
                except Exception:
                    pass

            # 2. Feature coverage audit — enrichment rate
            try:
                total = (await (await db.execute(
                    "SELECT COUNT(*) FROM game_contexts WHERE game_date >= date('now', '-7 days')"
                )).fetchone())[0]
                enriched = (await (await db.execute(
                    "SELECT COUNT(*) FROM game_contexts "
                    "WHERE game_date >= date('now', '-7 days') "
                    "AND context_json LIKE '%rest_days%'"
                )).fetchone())[0]
                if total > 10 and enriched / total < 0.5:
                    findings.append(
                        f"COVERAGE: Only {enriched}/{total} ({enriched/total:.0%}) "
                        f"recent games enriched with rest_days"
                    )
            except Exception:
                pass

            # 3. Signal quality audit — check for phantom signals
            try:
                phantom = (await (await db.execute(
                    "SELECT COUNT(*) FROM signals WHERE edge_pct > 0.20"
                )).fetchone())[0]
                if phantom > 0:
                    findings.append(f"PHANTOM: {phantom} signals with >20% edge still in DB")
            except Exception:
                pass

        except Exception as e:
            logger.debug(f"System watchdog error: {e}")

        if findings:
            logger.warning(
                f"System watchdog ({len(findings)} findings):\n"
                + "\n".join(f"  - {f}" for f in findings)
            )
            # Record to Hermes
            try:
                hm = await get_hermes_memory()
                if hm:
                    await hm.record_learning(
                        key="system_watchdog_findings",
                        value="; ".join(findings),
                        confidence=0.9,
                        source="system_watchdog",
                    )
            except Exception:
                pass
        else:
            logger.info("System watchdog: all checks passed")

    async def _phase_integrity_check(self) -> None:
        """
        Pipeline integrity check — detects silent failures that the
        standard health check misses.

        Runs every INTEGRITY_CHECK_INTERVAL_CYCLES cycles. Checks that
        pipelines are not just running but producing valid, changing,
        non-zero output.
        """
        from tools.pipeline_integrity import get_checker, INTEGRITY_CHECK_INTERVAL_CYCLES

        # Always run on cycle 1 (first cycle), then every N cycles
        if self._cycles > 1 and self._cycles % INTEGRITY_CHECK_INTERVAL_CYCLES != 0:
            return

        checker = get_checker()

        try:
            result = await checker.run_all_checks()

            # If critical issues found, alert via Telegram
            critical = result.get("issues", {}).get("critical", 0)
            if critical > 0:
                issue_details = result.get("issue_details", [])
                critical_msgs = [
                    i["message"] for i in issue_details
                    if i.get("severity") == "CRITICAL"
                ]
                alert_text = (
                    f"PIPELINE INTEGRITY ALERT: {critical} critical issues\n\n"
                    + "\n\n".join(f"- {m}" for m in critical_msgs[:3])
                )
                try:
                    await telegram.alert_system(alert_text)
                except Exception as e:
                    logger.warning(f"Failed to send integrity alert via Telegram: {e}", exc_info=True)

            # Also add phase error rate issues
            phase_issues = checker.check_phase_error_rates()
            if phase_issues:
                logger.warning(
                    f"PIPELINE INTEGRITY: {len(phase_issues)} phases with high error rates"
                )

        except Exception as e:
            logger.error(f"Pipeline integrity check failed: {e}", exc_info=True)

    async def _check_progress(self) -> None:
        """Ralph loop pattern: detect spinning vs making progress.

        Every 10 cycles, snapshot key metrics and compare to previous window.
        If no meaningful progress (0 new signals, 0 promotions, same rejection
        count), the loop is spinning — shift to diagnostic mode.
        """
        PROGRESS_CHECK_INTERVAL = 10

        if self._cycles % PROGRESS_CHECK_INTERVAL != 0:
            return

        # Take snapshot of current progress
        snapshot = {
            "cycle": self._cycles,
            "promotions": self._promotions,
            "rejections": self._rejections,
            "backtests": self._backtests_run,
            "hypotheses": self._hypotheses_generated,
            "claude_calls": self._claude_escalations,
        }

        # Also query signal count from DB
        try:
            db = self.hypothesis_manager._db
            cursor = await db.execute(
                "SELECT COUNT(*) FROM backtest_events WHERE signal_generated = 1"
            )
            row = await cursor.fetchone()
            snapshot["total_signals"] = row[0] if row else 0

            cursor = await db.execute(
                "SELECT COUNT(*) FROM hypotheses WHERE status = 'backtesting'"
            )
            row = await cursor.fetchone()
            snapshot["active_backtesting"] = row[0] if row else 0
        except Exception:
            snapshot["total_signals"] = 0
            snapshot["active_backtesting"] = 0

        self._progress_window.append(snapshot)
        if len(self._progress_window) > 5:
            self._progress_window = self._progress_window[-5:]

        # Need at least 2 snapshots to compare
        if len(self._progress_window) < 2:
            return

        prev = self._progress_window[-2]
        curr = self._progress_window[-1]

        # Measure actual progress
        new_promotions = curr["promotions"] - prev["promotions"]
        new_signals = curr["total_signals"] - prev.get("total_signals", 0)
        new_backtests = curr["backtests"] - prev["backtests"]
        cycles_elapsed = curr["cycle"] - prev["cycle"]

        is_progressing = (new_promotions > 0 or new_signals > 0)

        if is_progressing:
            self._consecutive_no_progress = 0
            self._spinning_detected = False
            logger.info(
                f"Progress check: +{new_signals} signals, +{new_promotions} promotions "
                f"over {cycles_elapsed} cycles — loop is productive"
            )
        else:
            self._consecutive_no_progress += 1
            logger.warning(
                f"Progress check: 0 new signals, 0 promotions over {cycles_elapsed} "
                f"cycles ({new_backtests} backtests ran). "
                f"No-progress streak: {self._consecutive_no_progress}"
            )

            if self._consecutive_no_progress >= 3:
                self._spinning_detected = True
                logger.warning(
                    f"SPINNING DETECTED: {self._consecutive_no_progress * PROGRESS_CHECK_INTERVAL} "
                    f"cycles with no new signals or promotions. "
                    f"Triggering diagnostic mode."
                )
                await self._run_spinning_diagnosis()

    async def _run_spinning_diagnosis(self) -> None:
        """When spinning is detected, gather real data instead of re-theorizing.

        Queries the DB for concrete evidence of what's failing, then
        escalates to Claude with actionable diagnostics — not vague prompts.
        """
        from tools.claude_code import claude_code_query

        diag = {}
        try:
            db = self.hypothesis_manager._db

            # 1. Why are backtests producing 0 signals?
            cursor = await db.execute(
                "SELECT COUNT(*) as total, "
                "SUM(CASE WHEN signal_generated = 1 THEN 1 ELSE 0 END) as signals, "
                "AVG(CASE WHEN ev_pct IS NOT NULL THEN ev_pct ELSE 0 END) as avg_ev "
                "FROM backtest_events"
            )
            row = await cursor.fetchone()
            diag["events"] = {"total": row[0], "signals": row[1], "avg_ev": round(row[2] or 0, 5)}

            # 2. What edge thresholds are hypotheses using?
            cursor = await db.execute(
                "SELECT MIN(edge_threshold), MAX(edge_threshold), AVG(edge_threshold) "
                "FROM hypotheses WHERE status IN ('draft', 'backtesting')"
            )
            row = await cursor.fetchone()
            diag["thresholds"] = {"min": row[0], "max": row[1], "avg": round(row[2] or 0, 4)}

            # 3. What's the max observed edge in events?
            cursor = await db.execute(
                "SELECT MAX(ev_pct), AVG(ev_pct), "
                "COUNT(CASE WHEN ev_pct > 0.01 THEN 1 END), "
                "COUNT(CASE WHEN ev_pct > 0.02 THEN 1 END) "
                "FROM backtest_events WHERE ev_pct IS NOT NULL"
            )
            row = await cursor.fetchone()
            diag["edge_distribution"] = {
                "max_edge": round(row[0] or 0, 5),
                "avg_edge": round(row[1] or 0, 5),
                "above_1pct": row[2],
                "above_2pct": row[3],
            }

            # 4. How many books per event?
            cursor = await db.execute(
                "SELECT AVG(json_extract(model_factors, '$.books_used')) "
                "FROM backtest_events WHERE model_factors IS NOT NULL "
                "LIMIT 100"
            )
            row = await cursor.fetchone()
            diag["avg_books_used"] = round(row[0] or 0, 1)

            # 5. Hypothesis status breakdown
            cursor = await db.execute(
                "SELECT status, COUNT(*) FROM hypotheses GROUP BY status"
            )
            diag["hypothesis_status"] = {r[0]: r[1] for r in await cursor.fetchall()}

        except Exception as e:
            logger.warning(f"Spinning diagnosis DB query failed: {e}")
            diag["error"] = str(e)

        logger.info(f"Spinning diagnosis results: {json.dumps(diag, indent=2)}")

        # If thresholds are higher than max observed edge, that's the bottleneck
        max_edge = diag.get("edge_distribution", {}).get("max_edge", 0)
        avg_threshold = diag.get("thresholds", {}).get("avg", 0)
        if avg_threshold > 0 and max_edge > 0 and avg_threshold > max_edge:
            logger.warning(
                f"DIAGNOSIS: avg edge_threshold ({avg_threshold:.3f}) exceeds "
                f"max observed edge ({max_edge:.3f}). No hypothesis can EVER "
                f"generate a signal. Thresholds need to be lowered."
            )

        # Escalate to Claude with hard data, not theory
        if self._claude_ok():
            prompt = (
                f"CALLISTO SPINNING DIAGNOSIS — EMERGENCY\n\n"
                f"The research loop has run {self._consecutive_no_progress * 10}+ cycles "
                f"with ZERO new signals and ZERO promotions. This is not working.\n\n"
                f"HARD DATA (from actual database queries, not estimates):\n"
                f"{json.dumps(diag, indent=2)}\n\n"
                f"CRITICAL QUESTION: Why is the loop producing zero value?\n"
                f"Your answer must be ONE specific, actionable root cause based "
                f"on the data above — not a list of possibilities.\n\n"
                f"RESPOND WITH JSON:\n"
                f'{{"root_cause": "single sentence", '
                f'"evidence": "which numbers above prove it", '
                f'"fix": "exact change needed"}}'
            )
            try:
                result = await claude_code_query(prompt, hermes_caller="deep_work")
                if result.get("content"):
                    logger.warning(f"Spinning diagnosis from Claude: {result['content'][:500]}")
            except Exception as e:
                logger.warning(f"Claude spinning diagnosis failed: {e}")

    def get_status(self) -> dict:
        """Return research loop status."""
        from tools.claude_code import get_usage_stats as claude_stats
        from tools.pipeline_integrity import get_checker

        # Include pipeline integrity info
        integrity_report = get_checker().get_latest_report()

        # Include work queue status (async call — best-effort)
        work_queue_status = {}
        try:
            import asyncio
            work_queue_status = asyncio.get_event_loop().run_until_complete(
                self._work_queue.get_status()
            ) if not asyncio.get_event_loop().is_running() else {}
        except Exception:
            pass

        return {
            "running": self._running,
            "paused": self._paused,
            "local_only": self._local_only,
            "mode": "paused" if self._paused else ("local_only" if self._local_only else "full"),
            "cycles_completed": self._cycles,
            "data_collections": self._data_collections,
            "hypotheses_generated": self._hypotheses_generated,
            "backtests_run": self._backtests_run,
            "claude_escalations": self._claude_escalations,
            "promotions": self._promotions,
            "rejections": self._rejections,
            "research_sports": RESEARCH_SPORTS,
            "claude_code": claude_stats(),
            "pipeline_integrity": integrity_report,
            "work_queue": work_queue_status,
            "claude_downtime": self._downtime_tracker.get_status(),
            "progress": {
                "spinning_detected": self._spinning_detected,
                "consecutive_no_progress": self._consecutive_no_progress,
                "window": self._progress_window[-3:] if self._progress_window else [],
            },
            "regime_analysis": {
                "teams_cached": len(_regime_cache),
                "teams_with_signals": sum(
                    1 for v in _regime_cache.values()
                    if v.get("has_edge_signal")
                ),
                "last_run": self._last_regime_analysis,
                "interval_cycles": REGIME_ANALYSIS_INTERVAL,
            },
            "intervals": {
                "research_cycle_seconds": RESEARCH_CYCLE_INTERVAL,
                "data_collection_seconds": DATA_COLLECTION_INTERVAL,
                "hypothesis_gen_seconds": HYPOTHESIS_GEN_INTERVAL,
                "claude_cooldown_seconds": CLAUDE_ESCALATION_COOLDOWN,
            },
        }
