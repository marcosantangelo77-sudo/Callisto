"""AutonomousLoop analysis-candidate scan extracted from tools.auto.loop.

``AutonomousLoop._find_analysis_candidates`` stays defined on the class as a
thin delegate so slice2 ``hasattr`` pins keep passing. The scan body lives
here so tools/auto/loop.py can keep shrinking without changing behaviour.

Psychology / injury / line-analysis helpers stay on AutonomousLoop and are
called through ``self``. Do not import the autonomous facade (no cycles).
Do not arm live betting. Do not add live to paper-signal.
"""
from __future__ import annotations

import asyncio
import logging
import time

from tools import telegram
from tools.edge_confidence import score_edge
from tools.line_analysis import optimal_bet_timing
from tools.loop.phases.shared import get_regime_for_team

from tools.auto.loop import (
    EDGE_DEDUP_WINDOW,
    MIN_CONFIDENCE_TO_ALERT,
    MIN_IMPLIED_RANGE,
    MIN_SOFT_EDGE_VS_SHARP,
)

logger = logging.getLogger("callisto.autonomous")


def find_analysis_candidates(loop) -> list[dict]:
    self = loop
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


async def analyze_edge(loop, candidate: dict) -> None:
    self = loop
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


async def phase_parlay_correlation_scan(loop) -> None:
    self = loop
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
