"""Collect / embed / evaluate ResearchLoop phases, extracted from phases_impl.

Callers still import these names from tools.loop.phases_impl.
This module must never import tools.autonomous (circular).
phase_live_execute stays in phases_impl with CALLISTO_ALLOW_LIVE_EXECUTE.
"""
from __future__ import annotations

import json
import time

from tools.loop import phases_impl as _impl

logger = _impl.logger
DATA_COLLECTION_INTERVAL = _impl.DATA_COLLECTION_INTERVAL
RESEARCH_SPORTS = _impl.RESEARCH_SPORTS


async def phase_collect_data(loop) -> None:
    self = loop
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


async def phase_embed_data(loop) -> None:
    self = loop
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


async def phase_evaluate(loop) -> None:
    self = loop
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
                    f"auto:temporal_overlap — {overlap_err}",
                    expected_status=h.get("status", "backtesting"),
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
                    "auto:needs_unique_data — stale backtest with duplicate event set",
                    expected_status=h.get("status", "backtesting"),
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
                        f"ctx_coverage={ctx_coverage:.0%}",
                        expected_status=h.get("status", "backtesting"),
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
                        f"needs game context enrichment (demotion {demotion_count}/2)",
                        expected_status=h.get("status", "backtesting"),
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
