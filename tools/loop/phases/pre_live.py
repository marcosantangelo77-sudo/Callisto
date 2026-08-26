"""Pre-live_execute ResearchLoop phases, extracted from phases_impl.

phase_interpret_backtests and phase_paper_trade sit immediately before
phase_live_execute. The live-execute env gate stays in phases_impl.

Callers still import these names from tools.loop.phases_impl.
This module must never import tools.autonomous (circular).
"""
from __future__ import annotations

import json
import time

from tools.backtest import _signal_confidence
from tools.loop import phases_impl as _impl

logger = _impl.logger

CLAUDE_ESCALATION_COOLDOWN = _impl.CLAUDE_ESCALATION_COOLDOWN
MIN_EDGE_THRESHOLD_FLOOR = _impl.MIN_EDGE_THRESHOLD_FLOOR
MAX_EDGE_THRESHOLD_CEILING = _impl.MAX_EDGE_THRESHOLD_CEILING
_wiki_in_loop_enabled = _impl._wiki_in_loop_enabled


async def phase_interpret_backtests(loop) -> None:
    self = loop
    """Claude interprets backtest results — signal vs noise, modifications.

    Sends the top 10 hypotheses by signal count with their win/loss/edge
    stats to Claude for interpretation. Claude identifies genuine signals,
    rejects noise, and suggests threshold modifications.

    When Claude is unavailable: defers the prompt to the work queue AND
    runs a local rules-based interpretation as fallback.
    """
    from inference import escalate_with_ladder

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
        result = await escalate_with_ladder(
            prompt,
            task_type="deep_work",
            hermes_caller="deep_work",
        )
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
                # GATE POLICY (mirrors tools/self_repair.py): an automated
                # actor may STRENGTHEN a gate but never WEAKEN it.
                #   - new_threshold >= current  → applied (gate tightened/unchanged)
                #   - new_threshold <  current  → recorded for human review, NOT applied
                #   - out-of-range values clamped to [MIN_EDGE_THRESHOLD_FLOOR,
                #     MAX_EDGE_THRESHOLD_CEILING] before comparison
                modified = 0
                refused = 0
                for mod in actions.get("modify", []):
                    try:
                        hid = mod.get("id")
                        new_thresh = mod.get("new_threshold")
                        reason = mod.get("reason", "claude_threshold_adjust")
                        if hid and new_thresh is not None:
                            new_thresh = max(MIN_EDGE_THRESHOLD_FLOOR,
                                             min(MAX_EDGE_THRESHOLD_CEILING,
                                                 float(new_thresh)))
                            cur = await db.execute(
                                "SELECT edge_threshold FROM hypotheses WHERE hypothesis_id = ?",
                                (hid,),
                            )
                            row = await cur.fetchone()
                            current = float(row[0]) if row and row[0] is not None else None
                            if current is None:
                                continue
                            if new_thresh < current:
                                refused += 1
                                logger.warning(
                                    "GATE POLICY REFUSED threshold LOWERING hyp=%s "
                                    "%s -> %s (reason=%s) — recorded for human review",
                                    hid, current, new_thresh, str(reason)[:120],
                                )
                                await db.execute(
                                    "UPDATE hypotheses SET "
                                    "notes = COALESCE(notes, '') || ? "
                                    "WHERE hypothesis_id = ?",
                                    (
                                        f"\n[cycle {self._cycles}] REFUSED threshold "
                                        f"lowering {current} -> {new_thresh}: {reason} "
                                        f"(gate policy; human decision required)",
                                        hid,
                                    ),
                                )
                                await db.commit()
                                continue
                            await db.execute(
                                "UPDATE hypotheses SET edge_threshold = ?, "
                                "notes = COALESCE(notes, '') || ? "
                                "WHERE hypothesis_id = ?",
                                (
                                    new_thresh,
                                    f"\n[cycle {self._cycles}] threshold raised "
                                    f"{current} -> {new_thresh}: {reason}",
                                    hid,
                                ),
                            )
                            await db.commit()
                            modified += 1
                    except Exception as e:
                        logger.warning(f"Failed to modify threshold for hypothesis {mod.get('id', '?')}: {e}")
                if modified:
                    logger.info(
                        f"Research: Claude raised thresholds on {modified} hypotheses"
                    )
                if refused:
                    logger.info(
                        f"Research: {refused} threshold-lowering suggestions refused by "
                        f"gate policy and logged to hypothesis notes for human review"
                    )

                # Log insights
                insights = actions.get("insights", "")
                if insights:
                    logger.info(f"Research: Claude backtest insights — {insights[:300]}")

                # ── Wiki write-back: file backtest stats as lessons ──
                # (feat/wiki-in-the-loop 2026-04-22) — replaces the prior
                # "read memory/error_patterns.md only" pattern. For each
                # hypothesis with sufficient data: file success article if
                # significant, null-result article if n>=30 and not
                # significant. Future hypothesis gen retrieves these.
                if _wiki_in_loop_enabled():
                    try:
                        from tools.knowledge_wiki import get_wiki
                        wiki = get_wiki()
                        for entry in hypo_data:
                            try:
                                hid = entry.get("id")
                                n = int(entry.get("events", 0) or 0)
                                is_sig = bool(entry.get("significant"))
                                if is_sig and n >= 15:
                                    topic = f"{hid}_backtest_success"
                                    title = f"Backtest success: {entry.get('name', hid)}"
                                    content = (
                                        f"Hypothesis {entry.get('name', hid)} "
                                        f"({hid}) shows statistically significant "
                                        f"edge in backtest.\n\n"
                                        f"Stats: n={n}, wins={entry.get('wins')}, "
                                        f"losses={entry.get('losses')}, "
                                        f"hit_rate={entry.get('hit_rate')}, "
                                        f"avg_edge={entry.get('avg_edge')}, "
                                        f"avg_ev={entry.get('avg_ev')}, "
                                        f"p_value={entry.get('p_value')}, "
                                        f"z_score={entry.get('z_score')}.\n"
                                        f"Sport: {entry.get('sport')}, "
                                        f"Market: {entry.get('market')}.\n"
                                        f"Thesis: {entry.get('thesis')}"
                                    )
                                    await wiki.write_lesson_article(
                                        db, topic=topic, title=title,
                                        content=content, domain="SIGNAL",
                                        related_topics=[
                                            "backtest_success",
                                            f"sport:{entry.get('sport')}",
                                            f"market:{entry.get('market')}",
                                        ],
                                        confidence=0.75,
                                    )
                                elif (not is_sig) and n >= 30:
                                    topic = f"{hid}_backtest_null_result"
                                    title = f"Backtest null: {entry.get('name', hid)}"
                                    content = (
                                        f"Hypothesis {entry.get('name', hid)} "
                                        f"({hid}) produced no significant edge "
                                        f"after {n} events — treat as dead.\n\n"
                                        f"Stats: wins={entry.get('wins')}, "
                                        f"losses={entry.get('losses')}, "
                                        f"hit_rate={entry.get('hit_rate')}, "
                                        f"avg_edge={entry.get('avg_edge')}, "
                                        f"avg_ev={entry.get('avg_ev')}, "
                                        f"p_value={entry.get('p_value')}.\n"
                                        f"Sport: {entry.get('sport')}, "
                                        f"Market: {entry.get('market')}.\n"
                                        f"Thesis: {entry.get('thesis')}\n\n"
                                        f"Do not re-propose structurally identical "
                                        f"variants — this pattern has been tested."
                                    )
                                    await wiki.write_lesson_article(
                                        db, topic=topic, title=title,
                                        content=content, domain="SIGNAL",
                                        related_topics=[
                                            "backtest_null_result",
                                            "dead_pattern",
                                            f"sport:{entry.get('sport')}",
                                            f"market:{entry.get('market')}",
                                        ],
                                        confidence=0.65,
                                    )
                            except Exception as e:
                                logger.debug(
                                    f"Wiki write-back skipped for "
                                    f"{entry.get('id')}: {e}"
                                )
                    except Exception as e:
                        logger.warning(f"Backtest wiki write-back failed: {e}")

            except (json.JSONDecodeError, ValueError) as e:
                logger.warning(f"Claude interpretation response not valid JSON: {e}")

        elif result.get("rate_limited"):
            logger.info("Research: Claude rate-limited during backtest interpretation")
    except Exception as e:
        logger.warning(f"Claude backtest interpretation failed: {e}")


async def phase_paper_trade(loop) -> None:
    self = loop
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
            # AUDIT FIX 2026-04-21 (autonomous.py:5820 stale read):
            # Previously fetched the FIRST row for a hypothesis with no
            # stage filter and no ORDER BY — non-deterministic, sometimes
            # returning stale backtest stats even when fresh paper_trade
            # stats existed. Pin to latest paper_trade row.
            cursor = await db.execute(
                "SELECT information_coefficient, signals_n "
                "FROM hypothesis_stats "
                "WHERE hypothesis_id = ? AND stage = 'paper_trade' "
                "ORDER BY computed_at DESC LIMIT 1",
                (h["hypothesis_id"],),
            )
            row = await cursor.fetchone()
            if not row:
                # No paper_trade stats yet — fall back to backtest stats so
                # anti-predictive gate still has a signal to work with.
                cursor = await db.execute(
                    "SELECT information_coefficient, signals_n "
                    "FROM hypothesis_stats "
                    "WHERE hypothesis_id = ? AND stage = 'backtest' "
                    "ORDER BY computed_at DESC LIMIT 1",
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
                f"auto:anti_predictive_paper_trading — IC={ic:.3f} < -0.10 (n={n_signals})",
                expected_status=h.get("status", "paper_trading"),
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
