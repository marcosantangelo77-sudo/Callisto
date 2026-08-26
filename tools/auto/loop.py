"""
AutonomousLoop — real-time edge detection loop (extracted from autonomous.py).

The facade at tools/autonomous.py re-exports this class so existing
``from tools.autonomous import AutonomousLoop`` callers keep working.
"""

import asyncio
import logging
import time
from typing import Optional

from tools import telegram
from tools.edge_confidence import score_edge
from tools.market_psychology import (
    attention_arbitrage,
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
    SPORT_ALIASES as _DEAD_NUM_SPORT_ALIASES,
)
from tools.injury_model import full_injury_analysis

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

# Cooldown between full analysis cycles (seconds)
ANALYSIS_COOLDOWN = 120  # 2 min between analysis runs

# Don't re-analyze the same edge within this window
EDGE_DEDUP_WINDOW = 1800  # 30 minutes


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
