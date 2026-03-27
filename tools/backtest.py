"""
Backtest engine — replay historical odds through a model and evaluate predictions.

This is the core of the scientific method applied to betting theses:
  1. Load hypothesis config (model params, factors, thresholds)
  2. Fetch historical odds for date range (cached after first fetch)
  3. For each event: run model, compare to book, record prediction
  4. Resolve outcomes against actual results
  5. Compute aggregate statistics and significance

The engine dispatches to existing sim functions (player_prop_sim, nba_game_sim, etc.)
based on the hypothesis's model_config. No new simulation code — reuse everything.
"""

import hashlib
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Optional

import aiosqlite
from dotenv import load_dotenv

from tools.historical_odds import HistoricalOddsFetcher
from tools.hypothesis import HypothesisManager
from tools.math_utils import american_to_decimal, american_to_implied
from tools.devig import devig_market, power_devig, multiplicative_devig
from tools.ev import ev_binary, evaluate_edge
from tools.sizing import kelly_binary
from datetime import timedelta
from tools.temporal_analysis import validate_temporal_isolation

load_dotenv()

logger = logging.getLogger("callisto.backtest")

DB_PATH = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")


def _signal_confidence(edge: float) -> str:
    """Categorize edge into confidence tiers based on realistic market edges.

    Real cross-book edges cap at ~2.5%. Old thresholds (5%/3%) were impossible
    to hit, making every signal "low". These thresholds reflect actual edge
    distribution: top-decile edges are ~2%+, median is ~1%.
    """
    if edge >= 0.02:
        return "high"
    elif edge >= 0.012:
        return "medium"
    return "low"


class BacktestEngine:
    """Replay historical odds through a model and evaluate predictions."""

    def __init__(
        self,
        hypothesis_manager: HypothesisManager,
        historical_fetcher: HistoricalOddsFetcher,
        db_path: str = DB_PATH,
    ):
        self.hypothesis_manager = hypothesis_manager
        self.historical_fetcher = historical_fetcher
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def initialize(self) -> None:
        from tools.schema import open_db
        self._db = await open_db(self.db_path)
        # Override with even longer timeout for bulk backtest writes
        await self._db.execute("PRAGMA busy_timeout = 120000")
        logger.info("Backtest engine initialized")

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    async def _enrich_snapshot_with_multibook(
        self,
        sport: str,
        date_str: str,
        snapshot: dict,
        target_book: str,
    ) -> dict:
        """Enrich a snapshot with multi-book data from odds_snapshots.

        When the historical_odds_cache has only single-book "consensus" data
        (common for older dates), check if odds_snapshots has a richer
        multi-book snapshot for the same date and sport. If so, use that
        instead — it has the target book + comparison books needed for
        cross-book edge detection.

        Returns the original snapshot if already multi-book or no better
        data is available.
        """
        games = snapshot.get("games", [])
        if not games:
            return snapshot

        # Check if snapshot already has multi-book data with the target book
        max_books = 0
        has_target = False
        for g in games:
            book_keys = {bm.get("key", "").lower() for bm in g.get("bookmakers", [])}
            max_books = max(max_books, len(book_keys))
            if target_book in book_keys:
                has_target = True

        if has_target and max_books >= 2:
            # Already have multi-book data with target — use as-is
            return snapshot

        # Try to find a better snapshot in odds_snapshots for this date
        # Look for snapshots on this date with the most games
        try:
            cursor = await self._db.execute(
                "SELECT snapshot_json FROM odds_snapshots "
                "WHERE sport = ? AND timestamp LIKE ? AND game_count > 0 "
                "ORDER BY game_count DESC LIMIT 1",
                (sport, f"{date_str}%"),
            )
            row = await cursor.fetchone()
            if not row:
                return snapshot

            better_snapshot = json.loads(row[0])
            better_games = better_snapshot.get("games", [])

            # Verify the better snapshot actually has multi-book data
            better_max_books = 0
            better_has_target = False
            for g in better_games:
                book_keys = {bm.get("key", "").lower() for bm in g.get("bookmakers", [])}
                better_max_books = max(better_max_books, len(book_keys))
                if target_book in book_keys:
                    better_has_target = True

            if better_has_target and better_max_books > max_books:
                logger.info(
                    f"Enriched {sport} {date_str}: upgraded from {max_books} to "
                    f"{better_max_books} books (from odds_snapshots)"
                )
                return better_snapshot

        except Exception as e:
            logger.warning(f"Snapshot enrichment failed for {sport} {date_str}: {e}", exc_info=True)

        return snapshot

    async def run_backtest(
        self,
        hypothesis_id: str,
        start_date: str,
        end_date: str,
        credit_budget: int = 50,
    ) -> dict:
        """
        Full backtest pipeline for a hypothesis.

        1. Load hypothesis config
        2. Fetch/load historical odds for date range
        3. For each game: devig all books, compute consensus fair value,
           find edges on target book, record predictions
        4. Resolve outcomes (if results available)
        5. Run statistical evaluation

        Returns the run summary.
        """
        h = await self.hypothesis_manager.get_hypothesis(hypothesis_id)
        if not h:
            return {"error": "Hypothesis not found"}

        config = h["model_config"]
        sport = h["sport"]
        market_type = h["market_type"]
        edge_threshold = h["edge_threshold"]
        thesis = h.get("thesis", "")
        h_name = h.get("name", hypothesis_id)  # Readable name for filter parsing
        target_book = config.get("target_book", "draftkings")
        devig_method = config.get("devig_method", "power")
        min_books = config.get("consensus_min_books", 3)

        # ── SPRING TRAINING GATE ──
        # MLB spring training (Feb-late March) uses split-squad rosters, shortened
        # starts, and matchups that don't appear in any results source. Backtesting
        # against spring training produces unresolvable events. Skip it.
        if sport == "baseball_mlb":
            from datetime import datetime as _dt_check
            try:
                end_dt = _dt_check.strptime(end_date, "%Y-%m-%d")
                # MLB Opening Day varies by year. Use dynamic detection: check if
                # game_results has MLB games before falling back to a conservative
                # date. 2026 season started March 20.
                mlb_season_start = _dt_check(end_dt.year, 3, 20)
                try:
                    if self.db:
                        import asyncio
                        # Check for actual MLB regular season games in game_results
                        cursor = await self.db.execute(
                            "SELECT MIN(game_date) FROM game_results "
                            "WHERE sport = 'baseball_mlb' AND game_date >= ? "
                            "AND game_date LIKE ?",
                            (f"{end_dt.year}-03-01", f"{end_dt.year}-%"),
                        )
                        row = await cursor.fetchone()
                        if row and row[0]:
                            actual_start = _dt_check.strptime(row[0][:10], "%Y-%m-%d")
                            mlb_season_start = actual_start
                except Exception:
                    pass  # Fall back to March 20 default
                if end_dt < mlb_season_start:
                    return {
                        "hypothesis_id": hypothesis_id,
                        "hypothesis_name": h_name,
                        "error": "spring_training",
                        "detail": (
                            f"Date range [{start_date} .. {end_date}] falls entirely "
                            f"in MLB spring training. Spring training matchups from "
                            f"odds sources don't match actual results — skipping."
                        ),
                        "total_events": 0,
                        "signals_generated": 0,
                    }
                # Clamp start_date to season start if range spans the boundary
                start_dt = _dt_check.strptime(start_date, "%Y-%m-%d")
                if start_dt < mlb_season_start:
                    start_date = mlb_season_start.strftime("%Y-%m-%d")
                    logger.info(
                        f"Backtest {hypothesis_id}: clamped MLB start_date to "
                        f"{start_date} (skip spring training)"
                    )
            except ValueError:
                pass

        # ── HYPOTHESIS-AWARE FILTERING ──
        # Parse line-based filters from thesis text, model_config, and name (Tier 1)
        # NOTE: pass h_name (readable name like "mlb_ace_first_start_over_total")
        # not hypothesis_id (UUID like "756c5b28-093") so name-based fallbacks work
        filters = self._parse_hypothesis_filters(thesis, config, h_name)
        if filters:
            logger.info(
                f"Backtest {hypothesis_id}: applying hypothesis filters: {filters}"
            )
        else:
            logger.info(
                f"Backtest {hypothesis_id}: no line-based filters parsed — "
                f"processing all lines (generic cross-book edge detection)"
            )

        # Warn specifically when a totals hypothesis couldn't determine a side
        if market_type == "totals" and "side_filter" not in filters:
            logger.warning(
                f"Backtest {hypothesis_id}: totals hypothesis without side filter — "
                f"BOTH Over and Under will be evaluated (2x events, diluted signal). "
                f"Fix: add side_filter to model_config or include 'over'/'under' in "
                f"thesis/name."
            )

        # Log unfilterable context factors (Tier 2)
        unfilterable = self._log_unfilterable_context_factors(hypothesis_id, config)
        context_coverage = self.compute_context_coverage(config)

        # ── INFER MISSING CONTEXT FACTORS ──
        # Many hypotheses have empty context_factors despite clearly needing
        # game-level filtering (dome teams, weather, pitcher stats, etc.).
        # Detect this from thesis/name keywords and compute effective coverage.
        inferred_unfilterable = self._infer_context_needs(thesis, h_name)
        if inferred_unfilterable:
            # Merge inferred unfilterable needs into coverage assessment.
            # Previously only fired when context_factors was empty — MLB hypotheses
            # with non-empty context_factors (e.g. ["season_week", "park_type"])
            # bypassed this check entirely, causing identical event sets.
            existing = set(
                f.lower().replace(" ", "_") for f in config.get("context_factors", [])
            )
            merged = existing | set(inferred_unfilterable)
            filterable_in_merged = sum(
                1 for f in merged
                if f in BacktestEngine.FILTERABLE_CONTEXT_FACTORS
            )
            context_coverage = filterable_in_merged / len(merged) if merged else 1.0
            unfilterable = list(set(unfilterable or []) | set(inferred_unfilterable))
            logger.warning(
                f"Backtest {hypothesis_id} ({h_name}): inferred unfilterable "
                f"context needs from thesis/name: {inferred_unfilterable}. "
                f"Effective coverage after merge: {context_coverage:.0%}."
            )

        if context_coverage < 0.5:
            logger.warning(
                f"Backtest {hypothesis_id}: context_coverage={context_coverage:.0%} — "
                f"most game-selection conditions are unfilterable. Results will be "
                f"indistinguishable from testing ALL games in the sport/market."
            )
            # ── HARD GATE: skip backtests that can't filter meaningfully ──
            # Without game-level context data, all hypotheses test the same games.
            # Running anyway produces identical results that waste cycles and
            # mislead the pipeline into thinking hypotheses were tested.
            return {
                "hypothesis_id": hypothesis_id,
                "hypothesis_name": h_name,
                "error": "untestable",
                "detail": (
                    f"Context coverage is {context_coverage:.0%}. "
                    f"Unfilterable factors: {unfilterable}. "
                    f"Without game-level context data, this hypothesis tests "
                    f"ALL {sport} games indistinguishably from other hypotheses."
                ),
                "context_coverage": context_coverage,
                "unfilterable_context_factors": unfilterable,
                "hypothesis_filters": filters if filters else {},
                "suggestion": (
                    "Either: (1) add venue/weather/player enrichment data to enable "
                    "filtering, or (2) restructure as a pure line-based hypothesis "
                    "(home underdog, spread range, etc.), or (3) add context_factors "
                    "to model_config and populate the corresponding data."
                ),
            }

        # ── DATE RANGE SAFETY ──
        # Never backtest against today or future — games haven't finished,
        # resolution will always fail. Cap at yesterday.
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if end_date >= today_str:
            end_date = str(datetime.now(timezone.utc).date() - timedelta(days=1))
            logger.info(f"Backtest {hypothesis_id}: capped end_date to {end_date} (today's games unfinished)")
        if start_date > end_date:
            return {
                "error": "No valid date range",
                "detail": f"start_date {start_date} > end_date {end_date} after capping at yesterday",
                "hypothesis_id": hypothesis_id,
            }

        # ── TEMPORAL ISOLATION ENFORCEMENT ──
        # If the hypothesis was generated from data analysis, ensure the backtest
        # date range doesn't overlap with the training period.
        temporal_check = validate_temporal_isolation(config, start_date, end_date)
        if not temporal_check.get("has_temporal_metadata", False):
            logger.warning(
                f"Hypothesis {hypothesis_id} has no temporal metadata — "
                "legacy hypothesis, temporal isolation NOT enforced. "
                "Re-generate this hypothesis with temporal_analysis to fix."
            )
        elif not temporal_check["valid"]:
            adjusted = temporal_check.get("adjusted_start")
            if adjusted:
                logger.warning(
                    f"Temporal overlap detected for {hypothesis_id}: "
                    f"{temporal_check['reason']} Auto-adjusting start to {adjusted}."
                )
                start_date = adjusted
            else:
                return {
                    "error": "Temporal isolation violated",
                    "detail": temporal_check["reason"],
                    "hypothesis_id": hypothesis_id,
                }
        else:
            logger.info(
                f"Temporal isolation verified for {hypothesis_id}: "
                f"training ended {temporal_check.get('training_period_end')}, "
                f"backtest starts {start_date} "
                f"(gap: {temporal_check.get('gap_days_actual', '?')} days)"
            )

        # ── DUPLICATE BACKTEST DETECTION ──
        # Multiple hypotheses with different names but same sport/market/filters
        # produce identical event sets. Detect and skip duplicates to save cycles.
        # IMPORTANT: include context_factors AND context-filter flag in fingerprint
        # so hypotheses with different game-level conditions (b2b, road trip, rest,
        # etc.) produce different fingerprints even when line-level filters are
        # identical and context_factors is empty.
        context_factors_sorted = sorted(config.get("context_factors", []))
        uses_context = self._needs_context_filter(h_name, thesis, config)
        fp_parts = json.dumps(
            {"sport": sport, "market": market_type, "start": start_date,
             "end": end_date, "filters": filters, "target": target_book,
             "threshold": edge_threshold, "devig": devig_method, "min_books": min_books,
             "context_factors": context_factors_sorted,
             "uses_context": uses_context},
            sort_keys=True,
        )
        fingerprint = hashlib.md5(fp_parts.encode()).hexdigest()[:16]

        # Primary check: exact fingerprint match (includes filters, threshold, etc.)
        existing = await self._db.execute_fetchall(
            """SELECT br.hypothesis_id, br.run_id, br.total_events, br.signals_generated,
                      br.hit_rate, br.avg_edge, br.is_significant, h.name
               FROM backtest_runs br
               JOIN hypotheses h ON h.hypothesis_id = br.hypothesis_id
               WHERE json_extract(br.run_config, '$.backtest_fingerprint') = ?
                 AND br.hypothesis_id != ?
                 AND br.total_events > 0
               LIMIT 1""",
            (fingerprint, hypothesis_id),
        )
        if existing:
            dup = existing[0]
            logger.warning(
                f"Backtest {hypothesis_id} ({h_name}): DUPLICATE of {dup[7]} "
                f"({dup[0]}) — same sport/market/dates/filters. "
                f"Prior run had {dup[2]} events, {dup[3]} signals. Skipping."
            )
            return {
                "hypothesis_id": hypothesis_id,
                "hypothesis_name": h_name,
                "error": "duplicate_backtest",
                "detail": (
                    f"Identical backtest already ran for hypothesis '{dup[7]}' "
                    f"(run {dup[1]}): {dup[2]} events, {dup[3]} signals, "
                    f"avg_edge={dup[5]}. Same sport/market/dates/filters."
                ),
                "duplicate_of": dup[0],
                "duplicate_run": dup[1],
                "fingerprint": fingerprint,
            }

        # Embed fingerprint in config for future detection
        config["backtest_fingerprint"] = fingerprint

        run_id = str(uuid.uuid4())[:12]
        now = datetime.now(timezone.utc).isoformat()

        # Record run start
        await self._db.execute(
            "INSERT INTO backtest_runs "
            "(run_id, hypothesis_id, date_range_start, date_range_end, "
            "started_at, run_config) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, hypothesis_id, start_date, end_date, now, json.dumps(config)),
        )
        await self._db.commit()

        # Update hypothesis status if still draft
        if h["status"] == "draft":
            await self.hypothesis_manager.update_status(hypothesis_id, "backtesting", "auto")

        # Fetch historical data
        logger.info(f"Backtest {run_id}: fetching {sport} odds {start_date} to {end_date}")

        # Determine which markets to fetch based on hypothesis type
        if market_type.startswith("player_"):
            # For player props, we need the main odds for game-level context
            # and then per-event prop odds
            fetch_markets = "h2h,spreads,totals"
        else:
            fetch_markets = "h2h,spreads,totals"

        fetch_result = await self.historical_fetcher.bulk_fetch_date_range(
            sport=sport,
            start_date=start_date,
            end_date=end_date,
            markets=fetch_markets,
            credit_budget=credit_budget,
        )

        logger.info(
            f"Backtest {run_id}: fetched {fetch_result.get('dates_fetched', 0)} dates, "
            f"{fetch_result.get('dates_cached_already', 0)} cached"
        )

        # Process each cached date
        all_dates = await self.historical_fetcher.get_cached_dates(sport)
        from datetime import datetime as dt
        start_dt = dt.strptime(start_date, "%Y-%m-%d")
        end_dt = dt.strptime(end_date, "%Y-%m-%d")
        dates_in_range = [
            d for d in all_dates
            if start_dt <= dt.strptime(d, "%Y-%m-%d") <= end_dt
        ]

        total_events = 0
        total_signals = 0
        multibook_dates = 0
        singlebook_skipped = 0
        context_filtered = 0

        # ── Pre-compute schedule context for game-level filtering ──
        use_context_filter = self._needs_context_filter(h_name, thesis, config)
        schedule_context = {}
        if use_context_filter:
            schedule_context = await self._build_schedule_context(
                sport, start_date, end_date,
            )
            if schedule_context:
                logger.info(
                    f"Backtest {hypothesis_id}: context filter ENABLED — "
                    f"{len(schedule_context)} games have schedule context"
                )
            else:
                logger.warning(
                    f"Backtest {hypothesis_id}: context filter ENABLED but "
                    f"schedule_context is EMPTY — all games will be rejected (fail-closed)"
                )

        for date_str in dates_in_range:
            snapshot = await self.historical_fetcher.fetch_historical_odds(
                sport=sport, date=date_str, markets=fetch_markets,
            )

            # Check if snapshot has multi-book data or only single-book "consensus"
            snapshot = await self._enrich_snapshot_with_multibook(
                sport, date_str, snapshot, target_book,
            )
            games = snapshot.get("games", [])
            snapshot_time = snapshot.get("timestamp", date_str)
            # Release the top-level snapshot dict early — games list is all we need
            del snapshot

            # Track data quality
            has_target = False
            for g in games:
                book_keys = {bm.get("key", "").lower() for bm in g.get("bookmakers", [])}
                if target_book in book_keys and len(book_keys) >= 2:
                    has_target = True
                    break
            if has_target:
                multibook_dates += 1
            else:
                singlebook_skipped += 1

            for game in games:
                # ── Game-level context filter ──
                # Apply schedule-derived filters BEFORE processing lines.
                # This is where b2b, road_trip, clinched, sandwich, etc. take effect.
                if use_context_filter:
                    if not schedule_context:
                        # No schedule data available — fail CLOSED.
                        # Cannot verify context conditions, so skip all games.
                        context_filtered += 1
                        continue
                    home = game.get("home_team", "")
                    away = game.get("away_team", "")
                    game_ctx = schedule_context.get((date_str, home, away), {})
                    if not self._game_matches_context_filter(
                        game_ctx, h_name, thesis, config,
                    ):
                        context_filtered += 1
                        continue

                events, signals = await self._process_game(
                    run_id=run_id,
                    hypothesis_id=hypothesis_id,
                    game=game,
                    game_date=date_str,
                    snapshot_time=snapshot_time,
                    market_type=market_type,
                    target_book=target_book,
                    edge_threshold=edge_threshold,
                    devig_method=devig_method,
                    min_books=min_books,
                    config=config,
                    h_sport=sport,
                    thesis=thesis,
                    filters=filters,
                )
                total_events += events
                total_signals += signals

        if context_filtered > 0:
            logger.info(
                f"Backtest {run_id}: context filter removed {context_filtered} games "
                f"that didn't match schedule requirements"
            )

        # ── Compound filter fallback ──
        # When ALL games are filtered out (total_events == 0 but we had games),
        # the compound context filter is too restrictive. Log prominently so
        # the circuit breaker in autonomous.py can act on it.
        if total_events == 0 and context_filtered > 0:
            logger.warning(
                f"Backtest {run_id}: COMPOUND FILTER KILLED ALL EVENTS — "
                f"{context_filtered} games filtered, 0 survived. "
                f"Hypothesis may need simpler context requirements."
            )

        logger.info(
            f"Backtest {run_id}: {multibook_dates} dates with multi-book data, "
            f"{singlebook_skipped} dates with single-book only (no cross-book edges)"
        )

        # Update run with totals
        completed = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            "UPDATE backtest_runs SET total_events = ?, signals_generated = ?, "
            "completed_at = ? WHERE run_id = ?",
            (total_events, total_signals, completed, run_id),
        )
        await self._db.commit()

        logger.info(
            f"Backtest {run_id} complete: {total_events} events, {total_signals} signals"
        )

        # ── Copy signal events to signals table ──
        # Bridge the gap: backtest_events with signal_generated=1 need to
        # appear in the signals table so the system has a unified view of
        # all detected edges, not just paper-trade ones.
        if total_signals > 0:
            await self._populate_signals_from_backtest(run_id, hypothesis_id)

        # Resolve outcomes using local game_results table
        resolution = await self.resolve_from_game_results(run_id=run_id, sport=sport)
        logger.info(
            f"Backtest {run_id}: resolved {resolution['resolved']} events "
            f"({resolution['unresolved']} unresolved)"
        )

        # Run significance evaluation
        sig_report = await self.hypothesis_manager.evaluate_significance(
            hypothesis_id, "backtest"
        )

        # Update run with statistical results from signal evaluation
        if sig_report.get("sample_size", 0) > 0:
            sig = sig_report.get("significance", {})
            risk = sig_report.get("risk", {})
            edge = sig_report.get("edge_metrics", {})
            clv = sig_report.get("clv", {})
            results = sig_report.get("results", {})
            cal_score = sig_report.get("calibration_score", {})

            await self._db.execute(
                "UPDATE backtest_runs SET "
                "actual_win = ?, actual_loss = ?, actual_push = ?, "
                "hit_rate = ?, avg_edge = ?, avg_ev = ?, avg_clv = ?, "
                "roi_pct = ?, p_value_binomial = ?, p_value_ttest = ?, "
                "z_score = ?, sharpe_ratio = ?, max_drawdown = ?, "
                "is_significant = ?, "
                "sortino_ratio_val = ?, brier_score = ?, information_coefficient = ? "
                "WHERE run_id = ?",
                (
                    results.get("wins", 0), results.get("losses", 0),
                    results.get("pushes", 0), results.get("hit_rate"),
                    edge.get("avg_edge"), edge.get("avg_ev"),
                    clv.get("avg_clv"), edge.get("roi_pct"),
                    sig.get("p_value_binomial"), sig.get("p_value_ttest"),
                    sig.get("z_score"), risk.get("sharpe_ratio"),
                    risk.get("max_drawdown"), sig.get("is_significant", False),
                    risk.get("sortino_ratio"),
                    cal_score.get("brier_score"),
                    cal_score.get("information_coefficient"),
                    run_id,
                ),
            )
            await self._db.commit()
        else:
            # No signal events — still populate run stats from ALL resolved events
            # so the run isn't stuck with null win/loss/hit_rate
            updated = await self.recalculate_run_stats(run_id)
            if updated:
                logger.info(
                    f"Backtest {run_id}: 0 signal events but populated run stats "
                    f"from resolved events"
                )

        return {
            "run_id": run_id,
            "hypothesis_id": hypothesis_id,
            "date_range": f"{start_date} to {end_date}",
            "actual_start_date": start_date,
            "actual_end_date": end_date,
            "total_events": total_events,
            "signals_generated": total_signals,
            "fetch_summary": fetch_result,
            "significance": sig_report,
            "hypothesis_filters": filters if filters else {},
            "unfilterable_context_factors": unfilterable,
            "unfiltered_totals_side": (
                market_type == "totals" and "side_filter" not in (filters or {})
            ),
            "filter_quality": (
                "fully_filtered" if filters and not unfilterable
                else "partially_filtered" if filters
                else "unfiltered_noisy" if unfilterable
                else "generic"
            ),
            "context_coverage": context_coverage,
        }

    async def _populate_signals_from_backtest(
        self, run_id: str, hypothesis_id: str
    ) -> int:
        """Copy backtest events with signal_generated=1 into the signals table.

        Returns the number of signals inserted.
        """
        rows = await self._db.execute_fetchall(
            "SELECT event_id, sport, side, market, book, book_odds_american, "
            "model_fair_prob, edge, ev_pct, kelly_fraction "
            "FROM backtest_events "
            "WHERE run_id = ? AND hypothesis_id = ? AND signal_generated = 1",
            (run_id, hypothesis_id),
        )
        if not rows:
            return 0

        inserted = 0
        for r in rows:
            edge_val = r[7] or 0  # edge column
            confidence = _signal_confidence(edge_val)
            await self._db.execute(
                "INSERT OR IGNORE INTO signals "
                "(event_id, sport, signal_type, team, market, book, "
                "odds_american, fair_probability, fair_prob_source, "
                "edge_pct, ev_pct, confidence, kelly_fraction, "
                "recommended_stake, status, notes) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    r[0],        # event_id
                    r[1],        # sport
                    "backtest",  # signal_type — distinguishes from paper_trade
                    r[2],        # side/team
                    r[3],        # market
                    r[4],        # book
                    r[5] or 0,   # odds_american
                    r[6] or 0,   # fair_probability
                    "cross_book_devig",
                    edge_val,
                    r[8] or 0,   # ev_pct
                    confidence,
                    r[9],        # kelly_fraction
                    None,        # recommended_stake
                    "historical", # status — these are resolved, not actionable
                    f"hypothesis_id={hypothesis_id}, run_id={run_id}",
                ),
            )
            inserted += 1

        await self._db.commit()
        logger.info(
            f"Backtest {run_id}: populated {inserted} signals from backtest events"
        )
        return inserted

    # ── HYPOTHESIS-AWARE FILTERING ──
    # Tier 1: Line-based filters (spread range, side, home/away)
    # Tier 2: Contextual filters (weather, travel, etc.) — logged as unavailable

    # Factors that CANNOT be applied as game-level filters during backtesting.
    # Split into two groups:
    #   - No data source exists (weather, pitcher, etc.)
    #   - Data exists but filter code is NOT yet implemented (rest, pace, etc.)
    # Both groups are treated as unfilterable. When filter code is written for
    # a factor, remove it from this set and add to _matches_hypothesis_conditions.
    UNFILTERABLE_CONTEXT_FACTORS = {
        # ── Derivable but NOT YET IMPLEMENTED ──
        # These have data in game_contexts / game_results / player_stats,
        # but no code maps them to event-level filters yet.
        # NOTE: days_rest, back_to_back, playoff_standing, revenge_game_flag,
        # schedule_context, etc. are NOW implemented — see FILTERABLE_CONTEXT_FACTORS
        # and _build_schedule_context() / _game_matches_context_filter().
        "starter_4q_minutes_prev",
        "home_pace_rank", "away_pace_rank", "pace_differential",
        "home_team_pace_rank", "away_team_pace_rank",
        "head_to_head_record", "opponent_record",
        "conference_tier",
        "team_identity", "school_identity",
        "seed_number",
        "hours_before_tip",
        "foul_rates", "foul_rate", "personal_fouls_per_game",
        "defensive_efficiency", "adjusted_defensive_efficiency",
        "tempo", "pace", "offensive_efficiency",
        "overtime_history", "prior_game_overtime",
        # schedule_context (sandwich/trap/letdown) — NOW FILTERABLE via _game_matches_context_filter
        "tournament_round",   # Sweet 16, Elite 8, etc. — no round detection from dates alone
        # ── No data source ──
        "weather", "temperature", "wind", "wind_speed", "wind_direction",
        "travel_distance", "timezone_crossing", "altitude",
        "venue_type",  # dome, indoor, outdoor, retractable roof
        "pitcher_history", "pitcher_velocity", "pitcher_workload",
        "pitcher_pitch_type",  # sinkerball, breaking ball, pitch mix
        "player_trade_recency", "player_impact_rating",
        "bullpen_status", "battery_composition",
        "spring_training_stats", "roster_composition",
        "first_inning_stats",
        "umpire_tendencies",  # hp umpire, zone width
        "coaching_staff",  # manager, coach, scheme changes
        "referee_crew", "referee_foul_tendency", "public_betting_pct",
        "handle_estimate", "line_movement_velocity", "line_movement_direction",
        "bye_week_flag", "bye_week_return", "primetime_flag", "game_slot",
        "thursday_game", "national_tv_flag",
        "last_10_possessions_per_game", "defensive_rating_slow_team",
        "season_avg_total", "pre_bye_scoring_trend_last_3",
        "first_half_total", "defensive_rank_both_teams",
        "postseason_stage",  # playoff round, series length, elimination game
        "pitcher_identity",  # starting pitcher name/matchup
        "schedule_type",  # interleague, opening day, etc.
    }

    # Keywords in thesis/name that imply game-level context filtering is needed.
    # Maps keyword patterns → the unfilterable factor they represent.
    _CONTEXT_KEYWORD_MAP = {
        r"\bdome\b": "venue_type",
        r"\bretractable.roof\b": "venue_type",
        r"\bindoor\b": "venue_type",
        r"\boutdoor\b": "venue_type",
        r"\bweather\b": "weather",
        r"\btemperature\b": "temperature",
        r"\bcold.weather\b": "temperature",
        r"\bwind\b": "wind",
        r"\bfastball.velo": "pitcher_velocity",
        r"\bvelo(city)?\b.*(drop|gain)": "pitcher_velocity",
        r"\bmph\b": "pitcher_velocity",
        r"\bpitch.count": "pitcher_workload",
        r"\bbullpen\b": "bullpen_status",
        r"\bthin.bullpen\b": "bullpen_status",
        r"\bopener\b.*inning": "bullpen_status",
        r"\bcatcher\b": "battery_composition",
        r"\bbattery\b": "battery_composition",
        r"\btravel\b": "travel_distance",
        r"\bwest.coast.*east|east.*west.coast": "timezone_crossing",
        r"\btimezone\b": "timezone_crossing",
        r"\bspring.training\b": "spring_training_stats",
        r"\bspring.era\b": "spring_training_stats",
        r"\bspring.*k/9\b": "spring_training_stats",
        r"\bspring.*\bip\b": "spring_training_stats",
        r"\bwhiff.rate\b": "spring_training_stats",
        r"\broster.turnover\b": "roster_composition",
        r"\bnew.lineup\b": "roster_composition",
        r"\bnew.team\b": "roster_composition",
        r"\b\d\+.new\b.*starter": "roster_composition",
        r"\boffseason\b.*acqui": "roster_composition",
        r"\bfree.agency\b": "roster_composition",
        r"\btrade\b": "roster_composition",
        r"\bnrfi\b": "first_inning_stats",
        r"\bfirst.inning\b": "first_inning_stats",
        r"\bdivision\b": "head_to_head_record",
        r"familiarity\b": "head_to_head_record",
        r"\brevenge\b": "head_to_head_record",
        r"\bformer.team\b": "head_to_head_record",
        r"\bpitcher\b": "pitcher_identity",
        r"\baces?\b.*first.*start": "pitcher_history",
        r"\bace\b.*starter": "pitcher_history",
        r"\bseason.debut\b": "pitcher_history",
        r"\bfirst.*career.*start": "pitcher_history",
        r"\bcareer.*debut": "pitcher_history",
        r"\bk/9\b": "pitcher_history",
        r"\bera\b.*under|under.*\bera\b": "pitcher_history",
        r"\bstrikeout": "pitcher_history",
        r"\bsinkerball\b": "pitcher_pitch_type",
        r"\bbreaking.ball\b": "pitcher_pitch_type",
        r"\bpitch.mix\b": "pitcher_pitch_type",
        r"\bcurveball\b": "pitcher_pitch_type",
        r"\bslider\b": "pitcher_pitch_type",
        r"\bchangeup\b": "pitcher_pitch_type",
        r"\bumpire\b": "umpire_tendencies",
        r"\bhp.umpire\b": "umpire_tendencies",
        r"\bwide.zone\b": "umpire_tendencies",
        r"\bstrike.zone\b": "umpire_tendencies",
        r"\bmanager\b": "coaching_staff",
        r"\bcoach\b": "coaching_staff",
        r"\bscheme\b": "coaching_staff",
        r"\bhbcu\b": "school_identity",
        r"\breligious\b": "school_identity",
        r"\bcohesion\b": "team_identity",
        r"\bidentity\b": "team_identity",
        r"\bcultural\b": "team_identity",
        # Playoff standing / motivation factors (NBA, NHL, MLB)
        r"\beliminated\b": "playoff_standing",
        r"\btanking\b": "playoff_standing",
        r"\bclinch": "playoff_standing",
        r"\bplayoff.race\b": "playoff_standing",
        r"\bplay.in\b": "playoff_standing",
        r"\bseed.locked\b": "playoff_standing",
        r"\bmagic.number\b": "playoff_standing",
        r"\bdesperate\b": "playoff_standing",
        r"\bmust.win\b": "playoff_standing",
        r"\bletdown\b": "schedule_context",
        r"\bsandwich\b": "schedule_context",
        r"\btrap.game\b": "schedule_context",
        r"\blook.ahead\b": "schedule_context",
        r"\boverlay\b": "schedule_context",
        # Rest / schedule factors
        r"\brest\b": "days_rest",
        r"\bb2b\b": "days_rest",
        r"\bback.to.back\b": "days_rest",
        r"\bdays.rest\b": "days_rest",
        r"\brest.mismatch\b": "days_rest",
        r"\bshort.rest\b": "days_rest",
        r"\bextra.rest\b": "extra_rest_days",
        r"\bbye\b": "bye_week_flag",
        # Closer / reliever patterns
        r"\bcloser\b": "bullpen_status",
        r"\breliever\b": "bullpen_status",
        r"\bsetup.man\b": "bullpen_status",
        # Venue / park factors
        r"\bpark.dim": "venue_type",
        r"\bpark.factor": "venue_type",
        r"\bfence.distance\b": "venue_type",
        # Pace / tempo factors
        r"\bpace\b": "pace",
        r"\btempo\b": "tempo",
        # Foul rate / defensive efficiency factors
        r"\bfoul.rat": "foul_rates",
        r"\bpersonal.foul": "foul_rates",
        r"\bfoul.prone\b": "foul_rates",
        r"\bhigh.foul": "foul_rates",
        r"\bdefensive.efficiency\b": "defensive_efficiency",
        r"\badjusted.defensive\b": "adjusted_defensive_efficiency",
        r"\bdefensive.rating\b": "defensive_efficiency",
        # Overtime / fatigue from prior game
        r"\bovertime\b.*fatigue\b": "overtime_history",
        r"\bovertime\b.*prior\b": "prior_game_overtime",
        r"\bovertime\b.*previous\b": "prior_game_overtime",
        r"\bclose.game.*intensity\b": "overtime_history",
        # Schedule / matchup type factors — season timing, date-based filters
        r"\binterleague\b": "schedule_type",
        r"\bopening.day\b": "schedule_type",
        r"\bopener\b": "schedule_type",
        r"\bopening.week\b": "schedule_type",
        r"\bopening.series\b": "schedule_type",
        r"\bearly.season\b": "schedule_type",
        r"\bfirst.week\b": "schedule_type",
        r"\bfirst.series\b": "schedule_type",
        r"\bfirst.\d+.games?\b": "schedule_type",
        r"\bseason.open": "schedule_type",
        r"\bday.game.*night|night.*day.game\b": "schedule_type",
        r"\bday.after.night\b": "schedule_type",
        r"\bapril\b": "schedule_type",
        r"\bsp.rust\b": "schedule_type",
        r"\bpitcher.rust\b": "schedule_type",
        # Specific venue names
        r"\bcoors\b": "venue_type",
        r"\bfenway\b": "venue_type",
        r"\bwrigley\b": "venue_type",
        r"\byankee.stadium\b": "venue_type",
        # Lineup / starter identity
        r"\bstarting.pitcher\b": "pitcher_identity",
        r"\b(?:sp|ace)\b.*\bpitcher\b": "pitcher_identity",
        # Tournament round / stage (NCAA, playoffs)
        r"\bsweet.16\b": "tournament_round",
        r"\belite.8\b": "tournament_round",
        r"\bfinal.four\b": "tournament_round",
        r"\bround.of.\d+\b": "tournament_round",
        r"\btournament\b": "tournament_round",
        r"\bmarch.madness\b": "tournament_round",
        r"\bncaa.*round\b": "tournament_round",
        r"\bsurviv\w*\b.*seed": "tournament_round",
        # Seed matchups
        r"\b\d+.seed\b": "seed_number",
        r"\blower.seed\b": "seed_number",
        r"\bhigher.seed\b": "seed_number",
        r"\bseed.matchup\b": "seed_number",
        r"\bseed.*vs\b": "seed_number",
        r"\bcinderella\b": "seed_number",
        # Postseason / playoff stage
        r"\bplayoff.round\b": "postseason_stage",
        r"\bfirst.round.*playoff": "postseason_stage",
        r"\bsecond.round.*playoff": "postseason_stage",
        r"\belimination.game\b": "postseason_stage",
        r"\bgame.[567]\b": "postseason_stage",
        r"\bseries.length\b": "postseason_stage",
    }

    @staticmethod
    def _infer_context_needs(thesis: str, name: str) -> list[str]:
        """Infer unfilterable context factors from thesis/name when context_factors is empty.

        Returns list of inferred context needs, or empty list if the hypothesis
        appears to be purely line-based (no game-context filtering needed).

        Only returns factors that are in UNFILTERABLE_CONTEXT_FACTORS — keywords
        that map to derivable/filterable factors should not block backtesting.
        """
        # Replace underscores/hyphens with spaces so \b word boundaries match
        # hypothesis names like "sp_dome_to_cold" (where _ is a word char in regex)
        text = f"{thesis} {name}".lower().replace("_", " ").replace("-", " ")
        inferred = set()
        for pattern, factor in BacktestEngine._CONTEXT_KEYWORD_MAP.items():
            if re.search(pattern, text):
                # Only block if the factor is truly unfilterable
                if factor in BacktestEngine.UNFILTERABLE_CONTEXT_FACTORS:
                    inferred.add(factor)
        return sorted(inferred)

    @staticmethod
    def _parse_hypothesis_filters(thesis: str, config: dict, hypothesis_id: str = "") -> dict:
        """Parse hypothesis thesis text, model_config, and name to extract line-based filters.

        Returns a dict of filters that can be applied to game lines:
            side_filter: "Over", "Under", or None — only evaluate this side
            spread_range: (min, max) or None — only test spreads in this range
            spread_min: float or None — only test spreads >= this value
            home_away_filter: "home", "away", or None — only test this side
            dog_fav_filter: "underdog", "favorite", or None — only test this role
        """
        filters = {}
        thesis_lower = thesis.lower() if thesis else ""
        h_id_lower = hypothesis_id.lower() if hypothesis_id else ""

        # 0. STRUCTURED LINE FILTERS from model_config (highest priority)
        # These are machine-readable specs generated alongside the hypothesis.
        # When present, use them directly and skip all regex parsing.
        lf = config.get("line_filters") or {}
        if lf:
            if lf.get("home_away") in ("home", "away"):
                filters["home_away_filter"] = lf["home_away"]
            if lf.get("dog_fav") in ("underdog", "favorite"):
                filters["dog_fav_filter"] = lf["dog_fav"]
            if lf.get("side") in ("Over", "Under"):
                filters["side_filter"] = lf["side"]
            if lf.get("spread_range") and isinstance(lf["spread_range"], (list, tuple)):
                lo, hi = float(lf["spread_range"][0]), float(lf["spread_range"][1])
                if lo > hi:
                    lo, hi = hi, lo
                filters["spread_range"] = (lo, hi)
            if lf.get("spread_min") is not None:
                filters["spread_min"] = float(lf["spread_min"])
            if filters:
                logger.info(
                    f"Line filters from structured spec for {hypothesis_id}: {filters}"
                )
                return filters  # Structured filters are authoritative

        # 1. Side filter from model_config (most reliable — explicitly set)
        side_filter = config.get("side_filter")
        if side_filter:
            filters["side_filter"] = side_filter  # "Over" or "Under"
        elif config.get("market_type") == "totals" or "total" in thesis_lower:
            # Parse from thesis: if thesis is about "under" or "over" specifically
            # Be careful: "underdog" contains "under" but is not a side filter
            # Look for "under" as a side-of-total context, not "underdog"
            thesis_words = re.split(r'[\s,;.!?()]+', thesis_lower)
            has_under_side = ("under" in thesis_words or "unders" in thesis_words
                             or "under-" in thesis_lower
                             or re.search(r'\bunder\b(?!dog)', thesis_lower))
            has_over_side = ("over" in thesis_words or "overs" in thesis_words
                            or "over-" in thesis_lower
                            or re.search(r'\bover\b(?!reaction|priced|weight|all|val)', thesis_lower))
            # Only set side filter if thesis is clearly about ONE side
            if has_under_side and not has_over_side:
                filters["side_filter"] = "Under"
            elif has_over_side and not has_under_side:
                filters["side_filter"] = "Over"

        # 1b. Fallback: extract side from hypothesis NAME if thesis parsing missed it.
        # Names like "mlb_opener_bullpen_game_total_over" or "mlb_new_manager_total_under"
        # encode the predicted direction. Only use as fallback when thesis didn't yield a side.
        if "side_filter" not in filters and h_id_lower:
            is_totals = config.get("market_type") == "totals" or "total" in h_id_lower
            if is_totals:
                # Check name suffix/segments for over/under direction
                name_parts = h_id_lower.replace("-", "_").split("_")
                name_has_over = "over" in name_parts or "overs" in name_parts
                name_has_under = "under" in name_parts or "unders" in name_parts
                if name_has_over and not name_has_under:
                    filters["side_filter"] = "Over"
                    logger.info(f"Side filter 'Over' inferred from hypothesis name: {hypothesis_id}")
                elif name_has_under and not name_has_over:
                    filters["side_filter"] = "Under"
                    logger.info(f"Side filter 'Under' inferred from hypothesis name: {hypothesis_id}")

        # 2. Spread range from model_config or thesis
        spread_range = config.get("spread_range")
        if spread_range and isinstance(spread_range, (list, tuple)) and len(spread_range) == 2:
            filters["spread_range"] = (float(spread_range[0]), float(spread_range[1]))
        else:
            # Parse "X-Y points" from thesis — only when describing game selection criteria
            # e.g., "underdogs of 3-7 points" or "favorites by 3-7 points"
            # NOT "moving the line 0.5-1.5 points past fair value" (line movement context)
            range_match = re.search(
                r'(?:of|by|between|within|spread(?:s)?[\s:]+)'
                r'\s*(\d+(?:\.\d+)?)\s*[-–to]+\s*(\d+(?:\.\d+)?)\s*point',
                thesis_lower,
            )
            if range_match:
                low = float(range_match.group(1))
                high = float(range_match.group(2))
                if low > high:
                    low, high = high, low
                # Sanity: spread ranges should be in a reasonable range (1-20)
                if 1 <= low and high <= 25:
                    filters["spread_range"] = (low, high)

        # 3. Spread minimum from thesis ("X+ points", "of X+ points")
        if "spread_range" not in filters:
            # Only match when in a game-selection context, not line movement
            min_match = re.search(
                r'(?:of|by|at least|minimum|spread(?:s)?[\s:]+)'
                r'\s*(\d+(?:\.\d+)?)\+?\s*point',
                thesis_lower,
            )
            if min_match:
                val = float(min_match.group(1))
                # Sanity: only treat as spread minimum if it looks like a spread value (1-20)
                if 1 <= val <= 20:
                    filters["spread_min"] = val

        # 4. Home/away filter — from thesis text
        if re.search(r'\bhome\s+(underdog|dog|team|favorite|side|advantage)', thesis_lower):
            filters["home_away_filter"] = "home"
        elif re.search(r'\b(?:road|away|visitor|visiting)\s+(underdog|dog|team|favorite|side|value)', thesis_lower):
            filters["home_away_filter"] = "away"
        elif re.search(r'\baway\s+(underdog|dog|team|favorite)', thesis_lower):
            filters["home_away_filter"] = "away"

        # 4b. Fallback: extract home/away from hypothesis NAME.
        # Names like "mlb_opening_week_road_favorites_h2h" or "nba_home_underdog_ats"
        if "home_away_filter" not in filters and h_id_lower:
            name_parts = h_id_lower.replace("-", "_").split("_")
            if any(kw in name_parts for kw in ("road", "away", "visitor", "visiting")):
                filters["home_away_filter"] = "away"
                logger.info(f"home_away_filter 'away' inferred from hypothesis name: {hypothesis_id}")
            elif "home" in name_parts:
                filters["home_away_filter"] = "home"
                logger.info(f"home_away_filter 'home' inferred from hypothesis name: {hypothesis_id}")

        # 5. Underdog/favorite filter — from thesis text
        if re.search(r'\bunderdog', thesis_lower) and not re.search(r'\bfavorite', thesis_lower):
            filters["dog_fav_filter"] = "underdog"
        elif re.search(r'\bfavorite', thesis_lower) and not re.search(r'\bunderdog', thesis_lower):
            filters["dog_fav_filter"] = "favorite"

        # 5b. Fallback: extract dog/fav from hypothesis NAME if thesis didn't yield it.
        # Names like "mlb_opening_week_underdog_ml" or "mlb_road_favorite_mispricing"
        # encode the predicted direction.
        if "dog_fav_filter" not in filters and h_id_lower:
            name_parts = set(h_id_lower.replace("-", "_").split("_"))
            has_dog = bool(name_parts & {
                "underdog", "dog", "underdogs", "undervalued", "upset",
            })
            has_fav = bool(name_parts & {
                "favorite", "favorites", "fav", "chalk",
            })
            if has_dog and not has_fav:
                filters["dog_fav_filter"] = "underdog"
                logger.info(f"dog_fav_filter 'underdog' inferred from hypothesis name: {hypothesis_id}")
            elif has_fav and not has_dog:
                filters["dog_fav_filter"] = "favorite"
                logger.info(f"dog_fav_filter 'favorite' inferred from hypothesis name: {hypothesis_id}")

        # 6. Thesis direction — detect bearish hypotheses (bet AGAINST filtered side)
        # E.g., "nba_heavy_favorite_ml_overpriced" → bet the underdog, not the favorite.
        # When bearish + a directional filter exists, flip the filter so we
        # evaluate and record the CORRECT bet side.
        #
        # IMPORTANT: Only use the hypothesis NAME for bearish detection, not the
        # thesis text. The name encodes intent (what we bet on), while the thesis
        # explains the phenomenon. A thesis like "underdogs have value because
        # favorites are overpriced" is BULLISH on underdogs — detecting "overpriced"
        # in the thesis would falsely trigger a bearish flip.
        bearish_name = False
        if h_id_lower:
            name_parts = set(h_id_lower.replace("-", "_").split("_"))
            bearish_name = bool(name_parts & {
                "overpriced", "overvalued", "inflated",
                "fade", "fading",
            })

        if bearish_name:
            flipped = False
            if "dog_fav_filter" in filters:
                old = filters["dog_fav_filter"]
                filters["dog_fav_filter"] = (
                    "underdog" if old == "favorite" else "favorite"
                )
                flipped = True
                logger.info(
                    "Bearish thesis detected for %s: flipped dog_fav_filter "
                    "%s → %s", hypothesis_id, old, filters["dog_fav_filter"],
                )
            if "home_away_filter" in filters:
                old = filters["home_away_filter"]
                filters["home_away_filter"] = (
                    "away" if old == "home" else "home"
                )
                flipped = True
                logger.info(
                    "Bearish thesis detected for %s: flipped home_away_filter "
                    "%s → %s", hypothesis_id, old, filters["home_away_filter"],
                )
            if flipped:
                filters["_bearish_flip"] = True

        return filters

    def _matches_hypothesis_conditions(
        self,
        side_name: str,
        market_type: str,
        point: Optional[float],
        home_team: str,
        away_team: str,
        filters: dict,
        fair_prob: Optional[float] = None,
    ) -> bool:
        """Check if a specific line/side matches the hypothesis conditions.

        Args:
            side_name: The side being evaluated (team name, "Over", "Under")
            market_type: "spreads", "totals", "h2h"
            point: The line value (spread number, total number)
            home_team: Home team name
            away_team: Away team name
            filters: Pre-parsed filters from _parse_hypothesis_filters()
            fair_prob: Devigged fair probability for this side (used for
                h2h favorite/underdog detection where no spread line exists)

        Returns True if this line should be processed, False to skip.
        """
        # 1. Side filter (Over/Under for totals) — check even when filters dict
        # is empty, because totals should default to single-side if possible
        side_filter = filters.get("side_filter") if filters else None
        if market_type == "totals":
            if side_filter:
                if side_name.lower() != side_filter.lower():
                    return False
            # No side filter on a totals hypothesis = both sides processed.
            # This is acceptable for generic edge detection but will be flagged
            # in backtest metadata as "unfiltered_totals_side".

        if not filters:
            # No line-based filters parsed — process all lines for this game.
            # WARNING: This means the hypothesis has no directional filtering.
            # For generic cross-book edge detection this is acceptable, but for
            # directional hypotheses (favorite/underdog/home/away) this is a bug
            # in _parse_hypothesis_filters that causes identical event sets.
            # We still return True here but callers should check filter coverage.
            return True

        # 2. Spread range filter
        spread_range = filters.get("spread_range")
        if spread_range and market_type == "spreads" and point is not None:
            abs_spread = abs(point)
            low, high = spread_range
            if abs_spread < low or abs_spread > high:
                return False

        # 3. Spread minimum filter
        spread_min = filters.get("spread_min")
        if spread_min is not None and market_type == "spreads" and point is not None:
            if abs(point) < spread_min:
                return False

        # 4. Home/away filter
        home_away = filters.get("home_away_filter")
        if home_away and market_type in ("spreads", "h2h"):
            is_home_side = self._team_matches(side_name, home_team)
            is_away_side = self._team_matches(side_name, away_team)
            if home_away == "home" and not is_home_side:
                return False
            if home_away == "away" and not is_away_side:
                return False

        # 5. Underdog/favorite filter
        # For spreads: negative line = favorite, positive = underdog
        # For h2h: fair_prob > 0.5 = favorite, < 0.5 = underdog
        dog_fav = filters.get("dog_fav_filter")
        if dog_fav:
            if market_type == "spreads" and point is not None:
                is_underdog = point > 0
                is_favorite = point < 0
                if dog_fav == "underdog" and not is_underdog:
                    return False
                if dog_fav == "favorite" and not is_favorite:
                    return False
            elif market_type == "h2h" and fair_prob is not None:
                is_favorite = fair_prob > 0.5
                is_underdog = fair_prob < 0.5
                if dog_fav == "underdog" and not is_underdog:
                    return False
                if dog_fav == "favorite" and not is_favorite:
                    return False

        return True

    @staticmethod
    def _log_unfilterable_context_factors(hypothesis_id: str, config: dict) -> list[str]:
        """Check for context factors we cannot filter on and log them.

        Returns list of unfilterable factors for inclusion in backtest metadata.
        """
        context_factors = config.get("context_factors", [])
        if not context_factors:
            return []

        unfilterable = [
            f for f in context_factors
            if f.lower().replace(" ", "_") in BacktestEngine.UNFILTERABLE_CONTEXT_FACTORS
            or f.lower() in BacktestEngine.UNFILTERABLE_CONTEXT_FACTORS
        ]

        if unfilterable:
            logger.warning(
                f"Hypothesis {hypothesis_id} requires context_factors "
                f"{unfilterable} which are not yet available — running "
                f"unfiltered backtest for those conditions (results will be noisy)."
            )

        return unfilterable

    @staticmethod
    def compute_context_coverage(config: dict) -> float:
        """Calculate what fraction of a hypothesis's context conditions can be filtered.

        Returns 1.0 if no context factors needed (pure line-based hypothesis),
        0.0 if ALL context factors are unfilterable (backtest is meaningless),
        or a value between 0-1 indicating partial coverage.

        Hypotheses with context_coverage < 0.5 should NOT be backtested — the
        results will be indistinguishable from random since most game-selection
        conditions cannot be applied.
        """
        context_factors = config.get("context_factors", [])
        if not context_factors:
            return 1.0  # No context needed — pure line-based, fully filterable

        # WHITELIST logic: only factors with actual filtering code count as filterable.
        # Previously used blacklist (UNFILTERABLE), but unknown factors like
        # "season_week", "park_type" slipped through as falsely "filterable".
        filterable_count = sum(
            1 for f in context_factors
            if f.lower().replace(" ", "_") in BacktestEngine.FILTERABLE_CONTEXT_FACTORS
            or f.lower() in BacktestEngine.FILTERABLE_CONTEXT_FACTORS
        )

        return filterable_count / len(context_factors)

    # ── SCHEDULE CONTEXT COMPUTATION ──
    # Derive game-level context from game_results so contextual filters
    # (b2b, days_rest, road_trip, sandwich, clinched, revenge) can actually
    # filter games instead of being no-ops.

    # Factors that ARE now filterable via schedule context.
    # When adding a new derivable factor: implement it in _build_schedule_context,
    # add matching logic in _game_matches_context_filter, and list it here.
    FILTERABLE_CONTEXT_FACTORS = {
        "days_rest", "days_since_last_game", "extra_rest_days",
        "back_to_back", "is_b2b_second_night", "back_to_back_second_night",
        "both_teams_short_rest", "opponent_days_rest",
        "consecutive_road_games", "road_trip_game_number",
        "schedule_density", "games_in_last_4_days", "schedule_context",
        "revenge_game_flag", "is_revenge_game",
        "prev_game_margin", "divisional_matchup",
        "playoff_standing",
    }

    async def _build_schedule_context(
        self, sport: str, start_date: str, end_date: str,
    ) -> dict:
        """Pre-compute schedule context for all games in a date range.

        Returns dict keyed by (game_date, home_team, away_team) with context:
            home_days_rest / away_days_rest: int
            home_b2b / away_b2b: bool — team played yesterday
            home_road_streak / away_road_streak: int — consecutive away games
            home_games_in_4 / away_games_in_4: int — schedule density
            home_prev_margin / away_prev_margin: float
            is_revenge: bool — teams played recently
            home_sandwich / away_sandwich: bool — game squeezed between two others
            home_win_pct / away_win_pct: float — season record approximation
        """
        from datetime import datetime as dt

        buffer_start = dt.strptime(start_date, "%Y-%m-%d") - timedelta(days=30)
        buffer_start_str = buffer_start.strftime("%Y-%m-%d")

        rows = await self._db.execute_fetchall(
            """SELECT game_date, home_team, away_team, home_score, away_score,
                      total_score, spread_result, winner
               FROM game_results
               WHERE sport = ? AND game_date >= ? AND game_date <= ?
               ORDER BY game_date""",
            (sport, buffer_start_str, end_date),
        )

        if not rows:
            return {}

        # Build per-team game lists
        team_games: dict[str, list] = {}
        for r in rows:
            gd, home, away, hs, as_, ts, sr, winner = r
            hs = hs or 0
            as_ = as_ or 0
            home_margin = hs - as_
            team_games.setdefault(home, []).append(
                (gd, away, True, home_margin, winner)
            )
            team_games.setdefault(away, []).append(
                (gd, home, False, -home_margin, winner)
            )

        for t in team_games:
            team_games[t].sort(key=lambda x: x[0])

        context = {}
        for r in rows:
            gd, home, away = r[0], r[1], r[2]
            if gd < start_date:
                continue

            ctx: dict = {}
            for team, prefix in [(home, "home"), (away, "away")]:
                tg = team_games.get(team, [])
                opp = away if prefix == "home" else home
                is_home_side = prefix == "home"
                idx = None
                for i, g in enumerate(tg):
                    if g[0] == gd and g[2] == is_home_side and g[1] == opp:
                        idx = i
                        break
                if idx is None:
                    ctx[f"{prefix}_days_rest"] = 99
                    ctx[f"{prefix}_b2b"] = False
                    ctx[f"{prefix}_road_streak"] = 0
                    ctx[f"{prefix}_games_in_4"] = 1
                    ctx[f"{prefix}_prev_margin"] = 0.0
                    continue

                # Days rest
                if idx > 0:
                    prev_date = tg[idx - 1][0]
                    d1 = dt.strptime(gd, "%Y-%m-%d")
                    d0 = dt.strptime(prev_date, "%Y-%m-%d")
                    days_rest = (d1 - d0).days
                    prev_margin = tg[idx - 1][3]
                else:
                    days_rest = 99
                    prev_margin = 0.0

                ctx[f"{prefix}_days_rest"] = days_rest
                ctx[f"{prefix}_b2b"] = (days_rest == 1)
                ctx[f"{prefix}_prev_margin"] = prev_margin

                # Road streak
                road_streak = 0
                if not is_home_side:
                    for j in range(idx, -1, -1):
                        if not tg[j][2]:
                            road_streak += 1
                        else:
                            break
                else:
                    for j in range(idx - 1, -1, -1):
                        if not tg[j][2]:
                            road_streak += 1
                        else:
                            break
                ctx[f"{prefix}_road_streak"] = road_streak

                # Games in last 4 days (schedule density)
                game_dt = dt.strptime(gd, "%Y-%m-%d")
                four_days_ago = (game_dt - timedelta(days=4)).strftime("%Y-%m-%d")
                games_in_4 = sum(1 for g in tg if four_days_ago < g[0] <= gd)
                ctx[f"{prefix}_games_in_4"] = games_in_4

            # Revenge game: teams played in last 30 days
            home_games = team_games.get(home, [])
            ctx["is_revenge"] = any(
                g[1] == away and g[0] < gd and g[0] >= buffer_start_str
                for g in home_games
            )

            # Sandwich game: game within 2 days before AND within 2 days after
            for team, prefix in [(home, "home"), (away, "away")]:
                tg = team_games.get(team, [])
                game_dt = dt.strptime(gd, "%Y-%m-%d")
                has_prev_close = any(
                    0 < (game_dt - dt.strptime(g[0], "%Y-%m-%d")).days <= 2
                    for g in tg if g[0] < gd
                )
                has_next_close = any(
                    0 < (dt.strptime(g[0], "%Y-%m-%d") - game_dt).days <= 2
                    for g in tg if g[0] > gd
                )
                ctx[f"{prefix}_sandwich"] = has_prev_close and has_next_close

            # Team records for playoff standing approximation
            for team, prefix in [(home, "home"), (away, "away")]:
                tg = team_games.get(team, [])
                wins = sum(1 for g in tg if g[0] < gd and g[4] == team)
                losses = sum(1 for g in tg if g[0] < gd and g[4] and g[4] != team)
                ctx[f"{prefix}_wins"] = wins
                ctx[f"{prefix}_losses"] = losses
                total = wins + losses
                ctx[f"{prefix}_win_pct"] = wins / total if total > 0 else 0.5

            context[(gd, home, away)] = ctx

        logger.info(
            f"Schedule context: computed for {len(context)} games "
            f"({sport}, {start_date} to {end_date})"
        )
        return context

    @staticmethod
    def _game_matches_context_filter(
        game_context: dict,
        hypothesis_name: str,
        thesis: str,
        config: dict,
    ) -> bool:
        """Check if a game matches the hypothesis's contextual requirements.

        Uses hypothesis name, thesis text, and config.context_factors to determine
        what context conditions are needed, then checks them against the pre-computed
        game context.

        Returns True if the game should be processed, False to skip.
        """
        name_lower = hypothesis_name.lower().replace("-", " ").replace("_", " ")
        thesis_lower = (thesis or "").lower()
        text = f"{name_lower} {thesis_lower}"
        context_factors = config.get("context_factors", [])
        cf_set = {f.lower().replace(" ", "_") for f in context_factors}

        if not game_context:
            return False  # Context filtering expected but no data — fail closed

        # ── STRUCTURED GAME FILTERS (from model_config — highest priority) ──
        # These are machine-readable specs generated alongside the hypothesis,
        # not reverse-engineered from natural language.  When present they are
        # authoritative; the regex fallbacks below only fire for legacy
        # hypotheses that lack structured filters.
        gf = config.get("game_filters") or {}
        if gf:
            # Helper: get a value for the specified side or either side
            gf_side = gf.get("side")  # "home", "away", or None (either)

            def _val(field_prefix):
                """Return the context value for the filter-specified side,
                or the more extreme value if no side is specified."""
                h = game_context.get(f"home_{field_prefix}")
                a = game_context.get(f"away_{field_prefix}")
                if gf_side == "home":
                    return h
                elif gf_side == "away":
                    return a
                return h, a  # caller decides how to use both

            if gf.get("require_b2b"):
                if gf_side:
                    if not game_context.get(f"{gf_side}_b2b"):
                        return False
                else:
                    if not game_context.get("home_b2b") and not game_context.get("away_b2b"):
                        return False

            if "min_rest_mismatch" in gf:
                hr = game_context.get("home_days_rest", 1)
                ar = game_context.get("away_days_rest", 1)
                if abs(hr - ar) < gf["min_rest_mismatch"]:
                    return False

            if "max_rest_days" in gf:
                hr = game_context.get("home_days_rest", 99)
                ar = game_context.get("away_days_rest", 99)
                if gf_side:
                    target = game_context.get(f"{gf_side}_days_rest", 99)
                    if target > gf["max_rest_days"]:
                        return False
                else:
                    if hr > gf["max_rest_days"] and ar > gf["max_rest_days"]:
                        return False

            if "min_games_in_4" in gf:
                hg = game_context.get("home_games_in_4", 1)
                ag = game_context.get("away_games_in_4", 1)
                if gf_side:
                    target = game_context.get(f"{gf_side}_games_in_4", 1)
                    if target < gf["min_games_in_4"]:
                        return False
                else:
                    if hg < gf["min_games_in_4"] and ag < gf["min_games_in_4"]:
                        return False

            if "require_road_streak" in gf:
                threshold = gf["require_road_streak"]
                hs = game_context.get("home_road_streak", 0)
                aws = game_context.get("away_road_streak", 0)
                if hs < threshold and aws < threshold:
                    return False

            if gf.get("require_sandwich"):
                if not game_context.get("home_sandwich") and not game_context.get("away_sandwich"):
                    return False

            if gf.get("require_revenge"):
                if not game_context.get("is_revenge"):
                    return False

            if "min_win_pct" in gf:
                hwp = game_context.get("home_win_pct", 0.5)
                awp = game_context.get("away_win_pct", 0.5)
                if gf_side:
                    target = game_context.get(f"{gf_side}_win_pct", 0.5)
                    if target < gf["min_win_pct"]:
                        return False
                else:
                    if hwp < gf["min_win_pct"] and awp < gf["min_win_pct"]:
                        return False

            if "max_win_pct" in gf:
                hwp = game_context.get("home_win_pct", 0.5)
                awp = game_context.get("away_win_pct", 0.5)
                if gf_side:
                    target = game_context.get(f"{gf_side}_win_pct", 0.5)
                    if target > gf["max_win_pct"]:
                        return False
                else:
                    if hwp > gf["max_win_pct"] and awp > gf["max_win_pct"]:
                        return False

            if "win_pct_range" in gf:
                lo, hi = gf["win_pct_range"]
                hwp = game_context.get("home_win_pct", 0.5)
                awp = game_context.get("away_win_pct", 0.5)
                if not (lo <= hwp <= hi or lo <= awp <= hi):
                    return False

            if "max_prev_margin" in gf:
                # max_prev_margin is NEGATIVE — "lost by at least X"
                threshold = gf["max_prev_margin"]
                hpm = game_context.get("home_prev_margin", 0)
                apm = game_context.get("away_prev_margin", 0)
                if gf_side:
                    target = game_context.get(f"{gf_side}_prev_margin", 0)
                    if target > threshold:  # margin > -10 means didn't lose badly enough
                        return False
                else:
                    if hpm > threshold and apm > threshold:
                        return False

            if "min_prev_margin" in gf:
                # min_prev_margin is POSITIVE — "won by at least X"
                threshold = gf["min_prev_margin"]
                hpm = game_context.get("home_prev_margin", 0)
                apm = game_context.get("away_prev_margin", 0)
                if gf_side:
                    target = game_context.get(f"{gf_side}_prev_margin", 0)
                    if target < threshold:
                        return False
                else:
                    if hpm < threshold and apm < threshold:
                        return False

            # Structured filters are authoritative — skip regex fallbacks
            return True

        # ── LEGACY REGEX FALLBACKS (for hypotheses without structured filters) ──
        # Track whether ANY filter pattern matched.  If none match, the
        # hypothesis text is too vague to derive filters from → fail closed.
        _any_filter_matched = False

        # ── Back-to-back filter ──
        if ("back_to_back" in cf_set or "is_b2b_second_night" in cf_set
                or "back_to_back_second_night" in cf_set
                or re.search(r"\bb2b\b|\bback.to.back\b", text)):
            _any_filter_matched = True
            if not game_context.get("home_b2b") and not game_context.get("away_b2b"):
                return False

        # ── Days rest filter ──
        if ("days_rest" in cf_set or "days_since_last_game" in cf_set
                or re.search(r"\bshort.rest\b|\brest.mismatch\b|\bdays?.rest\b", text)):
            _any_filter_matched = True
            home_rest = game_context.get("home_days_rest", 99)
            away_rest = game_context.get("away_days_rest", 99)
            if home_rest > 2 and away_rest > 2:
                return False

        # ── Extra rest filter ──
        if "extra_rest_days" in cf_set or re.search(r"\bextra.rest\b", text):
            _any_filter_matched = True
            home_rest = game_context.get("home_days_rest", 1)
            away_rest = game_context.get("away_days_rest", 1)
            if home_rest < 3 and away_rest < 3:
                return False

        # ── Road trip filter ──
        if ("consecutive_road_games" in cf_set or "road_trip_game_number" in cf_set
                or re.search(r"\broad.trip\b|\b\d\+?\s*(?:road|away)\b|\bconsecutive.(?:road|away)\b", text)):
            _any_filter_matched = True
            threshold = 3
            m = re.search(r"(\d)\+?\s*(?:road|away)", text)
            if m:
                threshold = int(m.group(1))
            away_streak = game_context.get("away_road_streak", 0)
            home_road_before = game_context.get("home_road_streak", 0)
            if away_streak < threshold and home_road_before < threshold:
                return False

        # ── Schedule density (3in4, 4in5) filter ──
        if ("schedule_density" in cf_set or "games_in_last_4_days" in cf_set
                or re.search(r"\b3.?in.?4\b|\b4.?in.?5\b|\bschedule.compress\b|\bschedule.density\b", text)):
            _any_filter_matched = True
            home_g4 = game_context.get("home_games_in_4", 1)
            away_g4 = game_context.get("away_games_in_4", 1)
            if home_g4 < 3 and away_g4 < 3:
                return False

        # ── Sandwich game filter ──
        if ("schedule_context" in cf_set
                or re.search(r"\bsandwich\b|\btrap.game\b|\bletdown\b", text)):
            _any_filter_matched = True
            if not game_context.get("home_sandwich") and not game_context.get("away_sandwich"):
                return False

        # ── Revenge game filter ──
        if ("revenge_game_flag" in cf_set or "is_revenge_game" in cf_set
                or re.search(r"\brevenge\b|\bformer.team\b", text)):
            _any_filter_matched = True
            if not game_context.get("is_revenge"):
                return False

        # ── Playoff standing / clinched / eliminated / bubble filter ──
        if ("playoff_standing" in cf_set
                or re.search(r"\bclinch|\beliminated\b|\btanking\b|\bplayoff.(?:race|bubble)\b|\bdesperate\b|\bbubble\b|\bmust.win\b", text)):
            _any_filter_matched = True
            home_wp = game_context.get("home_win_pct", 0.5)
            away_wp = game_context.get("away_win_pct", 0.5)

            if re.search(r"\bclinch", text):
                # 65%+ win pct = likely clinched (top ~6 teams per conference)
                # Previous 60% was too loose — captured mid-tier teams
                if home_wp < 0.65 and away_wp < 0.65:
                    return False
            elif re.search(r"\beliminated\b|\btanking\b", text):
                # 35%- win pct = likely eliminated/tanking
                # Previous 40% was too loose — captured mediocre teams
                if home_wp > 0.35 and away_wp > 0.35:
                    return False
            elif re.search(r"\bbubble\b|\bdesperate\b|\bmust.win\b|\bplayoff.race\b", text):
                # Bubble/desperate = at least one team in tight playoff fight
                # Narrowed from 40-60% to 43-57% to exclude comfortable mid-table
                if not (0.43 <= home_wp <= 0.57 or 0.43 <= away_wp <= 0.57):
                    return False

        # ── Both teams short rest filter ──
        if "both_teams_short_rest" in cf_set:
            _any_filter_matched = True
            home_rest = game_context.get("home_days_rest", 99)
            away_rest = game_context.get("away_days_rest", 99)
            if home_rest > 1 or away_rest > 1:
                return False

        # ── Rest mismatch filter ──
        if ("rest_mismatch" in cf_set
                or re.search(r"\brest.(?:mismatch|differential|advantage|edge)\b|\bfresh.vs.tired\b", text)):
            _any_filter_matched = True
            home_rest = game_context.get("home_days_rest", 1)
            away_rest = game_context.get("away_days_rest", 1)
            # Extract mismatch threshold from text (e.g., "2+ day rest mismatch")
            mm = re.search(r"(\d)\+?\s*(?:day)?\s*rest", text)
            threshold = int(mm.group(1)) if mm else 2
            if abs(home_rest - away_rest) < threshold:
                return False

        # ── Bad loss / blowout / bounce filter (using prev_margin) ──
        if (re.search(r"\bbad.loss\b|\bblowout(?!.win)\b|\bblown.(?:out|lead)\b|\bbounce\b"
                       r"|\bhangover\b|\bafter.(?:bad|ugly|blowout)", text)):
            _any_filter_matched = True
            hpm = game_context.get("home_prev_margin", 0)
            apm = game_context.get("away_prev_margin", 0)
            # At least one team lost their previous game badly (margin < -10)
            if hpm > -10 and apm > -10:
                return False

        # ── Winning streak / dominant win filter (using prev_margin) ──
        if re.search(r"\bwinning.streak\b|\bblowout.win\b|\bdomin\w+.win\b|\bmomentum\b", text):
            _any_filter_matched = True
            hpm = game_context.get("home_prev_margin", 0)
            apm = game_context.get("away_prev_margin", 0)
            # At least one team won their previous game convincingly (margin > 10)
            if hpm < 10 and apm < 10:
                return False

        # ── Losing team / struggling team filter ──
        if re.search(r"\blosing.streak\b|\bstruggling\b|\bslumping\b|\bskid\b", text):
            _any_filter_matched = True
            hwp = game_context.get("home_win_pct", 0.5)
            awp = game_context.get("away_win_pct", 0.5)
            hpm = game_context.get("home_prev_margin", 0)
            apm = game_context.get("away_prev_margin", 0)
            # At least one team is losing AND lost their previous game
            if not ((hwp < 0.45 and hpm < 0) or (awp < 0.45 and apm < 0)):
                return False

        # ── Generic streak filter (bare "streak" without winning/losing qualifier) ──
        if not _any_filter_matched and re.search(r"\bstreak\b", text):
            _any_filter_matched = True
            hwp = game_context.get("home_win_pct", 0.5)
            awp = game_context.get("away_win_pct", 0.5)
            # At least one team has a notably non-average record (on a streak)
            if not (hwp >= 0.58 or hwp <= 0.42 or awp >= 0.58 or awp <= 0.42):
                return False

        # ── Home stand filter ──
        if not _any_filter_matched and re.search(r"\bhome.stand\b", text):
            _any_filter_matched = True
            # Home stand = home team not on road trip + playing frequently
            home_road = game_context.get("home_road_streak", 0)
            home_g4 = game_context.get("home_games_in_4", 1)
            # Home team must have 0 consecutive road games and 2+ games in 4 days
            if home_road > 0 or home_g4 < 2:
                return False

        if not _any_filter_matched:
            # No regex pattern matched the hypothesis text — we can't verify the
            # hypothesis condition for this game.  Fail closed to prevent all
            # games leaking through unfiltered (the "149 identical events" bug).
            return False

        return True

    @staticmethod
    def _needs_context_filter(hypothesis_name: str, thesis: str, config: dict) -> bool:
        """Quick check: does this hypothesis need game-level context filtering?

        Returns True if the hypothesis references any schedule-derivable context
        factor in its name, thesis, or context_factors config.
        """
        context_factors = config.get("context_factors", [])
        cf_set = {f.lower().replace(" ", "_") for f in context_factors}
        if cf_set & BacktestEngine.FILTERABLE_CONTEXT_FACTORS:
            return True

        text = f"{hypothesis_name} {thesis or ''}".lower().replace("_", " ").replace("-", " ")
        schedule_patterns = [
            r"\bb2b\b", r"\bback.to.back\b", r"\bdays?.rest\b", r"\bshort.rest\b",
            r"\broad.trip\b", r"\bconsecutive.(?:road|away)\b",
            r"\b3.?in.?4\b", r"\b4.?in.?5\b", r"\bschedule.(?:compress|density)\b",
            r"\bsandwich\b", r"\btrap.game\b", r"\bletdown\b",
            r"\brevenge\b", r"\bformer.team\b",
            r"\bclinch", r"\beliminated\b", r"\btanking\b", r"\bplayoff.(?:race|bubble)\b",
            r"\bbubble\b", r"\bdesperate\b", r"\bmust.win\b",
            r"\bextra.rest\b", r"\brest.mismatch\b",
            r"\bblowout\b", r"\bstreak\b", r"\bbounce\b",
            r"\bhome.stand\b", r"\bwinning.streak\b", r"\blosing.streak\b",
        ]
        return any(re.search(p, text) for p in schedule_patterns)

    async def _process_game(
        self,
        run_id: str,
        hypothesis_id: str,
        game: dict,
        game_date: str,
        snapshot_time: str,
        market_type: str,
        target_book: str,
        edge_threshold: float,
        devig_method: str,
        min_books: int,
        config: dict,
        h_sport: str = "",
        thesis: str = "",
        filters: Optional[dict] = None,
    ) -> tuple[int, int]:
        """
        Process a single game: devig, compare, record predictions.
        Returns (events_processed, signals_generated).

        Cross-book edge detection requires the target book AND at least one
        other book in the data. When only a "consensus" book exists (old
        historical data without the target book), we skip — there's no
        cross-book edge to detect without pricing from both sides.

        Falls back to game-level markets (spreads/h2h/totals) when
        player prop data isn't available, since our free historical
        data is consensus game lines, not per-player props.
        """
        # Determine available markets in this game
        available_markets = set()
        for bm in game.get("bookmakers", []):
            for mkt in bm.get("markets", []):
                available_markets.add(mkt["key"])

        # If hypothesis wants player props but we only have game lines,
        # fall back to the closest game-level market
        effective_market = market_type
        if market_type.startswith("player_") and market_type not in available_markets:
            # Map prop types to game-level equivalents for backtesting
            prop_to_game = {
                "player_points": "totals",
                "player_rebounds": "totals",
                "player_assists": "totals",
                "player_threes": "totals",
                "player_pra": "totals",
            }
            effective_market = prop_to_game.get(market_type, "spreads")
            if effective_market not in available_markets:
                effective_market = next(iter(available_markets), None)
                if not effective_market:
                    return 0, 0

        available_books = {bm.get("key", "").lower() for bm in game.get("bookmakers", [])}
        bookmaker_count = len(available_books)

        # Multi-book edge detection: need at least 3 books total.
        # Need at least min_books+1 total (min_books for consensus + 1 target).
        # For thin markets (NCAAW, NWSL) with consensus_min_books=2, allow 2 total.
        required_total = max(2, min_books + 1)
        if bookmaker_count < required_total:
            return 0, 0

        # target_book is now just a hint — _process_game_lines evaluates
        # ALL soft books against the consensus. No single-book dependency.
        effective_target = target_book
        effective_min_books = max(2, min(min_books, bookmaker_count - 1))

        return await self._process_game_lines(
            run_id, hypothesis_id, game, game_date, snapshot_time,
            effective_market, effective_target, edge_threshold, devig_method,
            effective_min_books, config, h_sport=h_sport, filters=filters,
        )

    async def _process_game_lines(
        self,
        run_id: str,
        hypothesis_id: str,
        game: dict,
        game_date: str,
        snapshot_time: str,
        market_type: str,
        target_book: str,
        edge_threshold: float,
        devig_method: str,
        min_books: int,
        config: dict,
        h_sport: str = "",
        filters: Optional[dict] = None,
    ) -> tuple[int, int]:
        """Process spreads/totals/h2h lines for a game.

        Uses cross-book edge detection when multi-book data is available:
        1. Devig each non-target book to get fair probabilities
        2. Find the BEST (sharpest) devigged line across non-target books
        3. Also compute consensus (average) devigged fair value
        4. Use the best line as the fair value — edges exist BETWEEN books
        5. Fall back to consensus-only when only 1-2 non-target books exist
        """
        home = game.get("home_team", "")
        away = game.get("away_team", "")
        bookmakers = game.get("bookmakers", [])

        events = 0
        signals = 0
        _pending_rows: list[tuple] = []  # Collect rows for batch INSERT

        # Organize lines by (market, outcome_name, point) -> book -> price
        lines_by_key = {}
        for bm in bookmakers:
            bk_key = bm.get("key", "").lower()
            bk_name = bm.get("title", bk_key)
            for mkt in bm.get("markets", []):
                if mkt["key"] != market_type:
                    continue
                for outcome in mkt.get("outcomes", []):
                    name = outcome.get("name", "")
                    point = outcome.get("point")
                    price = outcome.get("price", 0)
                    key = (mkt["key"], name, point)
                    if key not in lines_by_key:
                        lines_by_key[key] = {}
                    lines_by_key[key][bk_key] = {
                        "price": price,
                        "name": bk_name,
                    }

        # For each unique line, find the opposite side and devig
        # Group by (market, point) to get both sides
        # For spreads, sides have opposite-sign points (e.g., -7.5 and +7.5)
        # so group by abs(point) to pair them correctly
        sides_by_line = {}
        signed_points = {}  # (mkt_key, group_point, side_name) -> signed point
        for (mkt_key, name, point), books in lines_by_key.items():
            group_point = abs(point) if point is not None and mkt_key == "spreads" else point
            line_key = (mkt_key, group_point)
            if line_key not in sides_by_line:
                sides_by_line[line_key] = {}
            sides_by_line[line_key][name] = books
            # Track the original signed point for underdog/favorite filtering
            signed_points[(mkt_key, group_point, name)] = point

        for (mkt_key, point), sides in sides_by_line.items():
            side_names = list(sides.keys())
            if len(side_names) != 2:
                continue

            side_a_name, side_b_name = side_names[0], side_names[1]
            side_a_books = sides[side_a_name]
            side_b_books = sides[side_b_name]

            # Find books that have both sides
            common_books = set(side_a_books.keys()) & set(side_b_books.keys())
            if len(common_books) < min_books + 1:  # Need min_books for consensus + 1 target
                continue

            # Devig ALL books to get fair values
            all_fair_a = {}  # book_key -> fair_prob_a
            all_fair_b = {}  # book_key -> fair_prob_b
            for bk in common_books:
                price_a = side_a_books[bk]["price"]
                price_b = side_b_books[bk]["price"]
                try:
                    dec_a = american_to_decimal(price_a)
                    dec_b = american_to_decimal(price_b)
                    if devig_method == "power":
                        fair, _ = power_devig([dec_a, dec_b])
                    else:
                        fair = multiplicative_devig([dec_a, dec_b])
                    all_fair_a[bk] = fair[0]
                    all_fair_b[bk] = fair[1]
                except (ValueError, ZeroDivisionError) as e:
                    logger.warning(
                        f"Devig failed for book={bk}, market={mkt_key}, "
                        f"prices=({price_a}, {price_b}): {e}"
                    )
                    continue

            if len(all_fair_a) < 3:
                continue

            # --- Multi-book edge detection ---
            # For EACH book as potential target, compute consensus from all
            # other books and measure the edge. This finds the best mispricing
            # across ALL books, not just DraftKings.
            #
            # Sharp books (Pinnacle, Circa, etc.) are excluded as targets —
            # they set the true line. Only soft/retail books are tested.
            SHARP_BOOKS = {
                "pinnacle", "lowvig", "lowvig.ag", "circa",
                "bookmaker.eu", "betonline", "betonline.ag",
                "betonlineag",
                "betcris", "betfair_exchange", "sbobet",
            }

            for eval_target in common_books:
                # Only evaluate retail/soft books as targets
                if eval_target in SHARP_BOOKS:
                    continue
                if eval_target not in all_fair_a:
                    continue

                # Build consensus from all books EXCEPT this target
                others_a = [(v, bk) for bk, v in all_fair_a.items() if bk != eval_target]
                others_b = [(v, bk) for bk, v in all_fair_b.items() if bk != eval_target]

                non_target_count = len(others_a)
                if non_target_count < min_books:
                    continue

                consensus_a = sum(v for v, _ in others_a) / non_target_count
                consensus_b = sum(v for v, _ in others_b) / non_target_count

                # Filter outliers before computing best-line
                OUTLIER_THRESHOLD = 0.15
                clean_a = [(v, bk) for v, bk in others_a
                            if abs(v - consensus_a) <= OUTLIER_THRESHOLD]
                clean_b = [(v, bk) for v, bk in others_b
                            if abs(v - consensus_b) <= OUTLIER_THRESHOLD]
                if not clean_a:
                    clean_a = others_a
                if not clean_b:
                    clean_b = others_b

                best_a_val, best_a_book = max(clean_a, key=lambda x: x[0])
                best_b_val, best_b_book = max(clean_b, key=lambda x: x[0])

                use_crossbook = non_target_count >= 3
                if use_crossbook:
                    fair_a = best_a_val
                    fair_b = best_b_val
                    edge_method = "cross_book_best_line"
                else:
                    fair_a = consensus_a
                    fair_b = consensus_b
                    edge_method = "consensus_devig"

                contributing_books_a = [bk for _, bk in others_a]
                contributing_books_b = [bk for _, bk in others_b]

                # Evaluate both sides against this target book
                for side_name, fair_val, consensus_val, best_val, best_book, side_books, contrib_books in [
                    (side_a_name, fair_a, consensus_a, best_a_val, best_a_book, side_a_books, contributing_books_a),
                    (side_b_name, fair_b, consensus_b, best_b_val, best_b_book, side_b_books, contributing_books_b),
                ]:
                    side_signed_point = signed_points.get((mkt_key, point, side_name), point)
                    if not self._matches_hypothesis_conditions(
                        side_name=side_name,
                        market_type=mkt_key,
                        point=side_signed_point,
                        home_team=home,
                        away_team=away,
                        filters=filters or {},
                        fair_prob=fair_val,
                    ):
                        continue

                    if eval_target not in side_books:
                        continue
                    target_price = side_books[eval_target]["price"]
                    target_implied = american_to_implied(target_price)
                    ev = ev_binary(fair_val, american_to_decimal(target_price))
                    kelly = kelly_binary(fair_val, american_to_decimal(target_price))
                    edge = fair_val - target_implied

                    # Sanity check: absurd edges (>15%) are almost always
                    # data quality issues (book has team names swapped vs
                    # consensus). Skip entirely — don't cap and pretend it's real.
                    MAX_EDGE_MAGNITUDE = 0.15
                    if abs(edge) > MAX_EDGE_MAGNITUDE:
                        continue

                    # Direction sanity: if fair_val > 0.5 but book prices this
                    # side as a heavy underdog (or vice versa), the consensus
                    # and book disagree on which team is favored. Data error.
                    if (fair_val > 0.6 and target_implied < 0.3) or \
                       (fair_val < 0.3 and target_implied > 0.6):
                        continue

                    # Require minimum book count for reliable signals —
                    # with <4 non-target books, devig consensus is noisy
                    # and produces spurious edges (3.01 avg books on signals
                    # vs 6.11 on non-signals proved this empirically).
                    MIN_BOOKS_FOR_SIGNAL = 4
                    # Heavy favorite filter (h2h only): signals on lines
                    # with >80% fair probability are noise-dominated.
                    # At -400 odds, a 2% edge yields $0.50 EV per $100
                    # risked, and one loss erases 4 wins. Devig consensus
                    # is also least reliable at probability extremes.
                    MAX_FAIR_PROB_FOR_SIGNAL = 0.80
                    heavy_fav = (mkt_key == "h2h"
                                 and fair_val > MAX_FAIR_PROB_FOR_SIGNAL)
                    is_signal = (edge >= edge_threshold
                                 and non_target_count >= MIN_BOOKS_FOR_SIGNAL
                                 and not heavy_fav)

                    events += 1
                    if is_signal:
                        signals += 1

                    team = side_name
                    event_id = game.get("id") or f"{game_date}|{home}|{away}"
                    event_sport = game.get("sport_key") or h_sport

                    _pending_rows.append((
                            run_id, event_id, hypothesis_id, event_sport,
                            None, mkt_key, side_signed_point, team, eval_target,
                            target_price, round(target_implied, 6),
                            round(fair_val, 6),
                            json.dumps({
                                "edge_method": edge_method,
                                "books_used": non_target_count,
                                "target_excluded": True,
                                "devig_method": devig_method,
                                "target_book": eval_target,
                                "best_line_book": best_book,
                                "best_line_fair_prob": round(best_val, 6),
                                "consensus_fair_prob": round(consensus_val, 6),
                                "contributing_books": contrib_books,
                                "home_team": home,
                                "away_team": away,
                            }),
                            round(edge, 6), round(ev, 6), round(kelly, 6),
                            is_signal, game_date, snapshot_time,
                    ))

        # Batch INSERT all rows in one transaction — dramatically reduces lock contention
        if _pending_rows:
            await self._db.executemany(
                "INSERT OR IGNORE INTO backtest_events "
                "(run_id, event_id, hypothesis_id, sport, player, market, "
                "line, side, book, book_odds_american, book_implied_prob, "
                "model_fair_prob, model_factors, edge, ev_pct, kelly_fraction, "
                "signal_generated, game_date, snapshot_time) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                _pending_rows,
            )
            await self._db.commit()
        return events, signals

    async def _process_game_props(
        self,
        run_id: str,
        hypothesis_id: str,
        game: dict,
        game_date: str,
        snapshot_time: str,
        market_type: str,
        target_book: str,
        edge_threshold: float,
        devig_method: str,
        min_books: int,
        config: dict,
        filters: Optional[dict] = None,
    ) -> tuple[int, int]:
        """
        Process player props for a game.
        For props, we need per-event prop data which may require separate API calls.
        If prop data is embedded in the game object, process directly.
        """
        bookmakers = game.get("bookmakers", [])
        events = 0
        signals = 0
        _pending_rows: list[tuple] = []  # Collect rows for batch INSERT

        # Organize props: (player, market, line) -> book -> {Over, Under}
        prop_lines = {}
        book_names = {}

        for bm in bookmakers:
            bk_key = bm.get("key", "").lower()
            bk_name = bm.get("title", bk_key)
            book_names[bk_key] = bk_name

            for mkt in bm.get("markets", []):
                if not mkt["key"].startswith("player_"):
                    continue
                # Filter to specific market type if specified
                if market_type != "player_props" and mkt["key"] != market_type:
                    continue

                for outcome in mkt.get("outcomes", []):
                    player = outcome.get("description", "Unknown")
                    line = outcome.get("point")
                    side = outcome.get("name", "")  # Over or Under
                    price = outcome.get("price", 0)

                    if not side or not price:
                        continue

                    key = (player, mkt["key"], line)
                    if key not in prop_lines:
                        prop_lines[key] = {}
                    if bk_key not in prop_lines[key]:
                        prop_lines[key][bk_key] = {}
                    prop_lines[key][bk_key][side] = price

        # Process each prop line
        for (player, mkt_key, line), books in prop_lines.items():
            if target_book not in books:
                continue
            target_data = books[target_book]
            if "Over" not in target_data or "Under" not in target_data:
                continue

            # Devig all books with both sides at this line
            # Track (fair_prob, book_key) for cross-book best-line detection
            fair_overs = []   # (fair_prob, book_key)
            fair_unders = []  # (fair_prob, book_key)
            for bk_key, bk_data in books.items():
                if bk_key == target_book:
                    continue  # exclude target book from consensus
                if "Over" not in bk_data or "Under" not in bk_data:
                    continue
                try:
                    dec_o = american_to_decimal(bk_data["Over"])
                    dec_u = american_to_decimal(bk_data["Under"])
                    if devig_method == "power":
                        fair, _ = power_devig([dec_o, dec_u])
                    else:
                        fair = multiplicative_devig([dec_o, dec_u])
                    fair_overs.append((fair[0], bk_key))
                    fair_unders.append((fair[1], bk_key))
                except (ValueError, ZeroDivisionError) as e:
                    logger.warning(
                        f"Devig failed for book={bk_key}, market={mkt_key}, "
                        f"prices=(Over={bk_data['Over']}, Under={bk_data['Under']}): {e}"
                    )
                    continue

            non_target_count = len(fair_overs)
            if non_target_count < min_books:
                continue

            consensus_over = sum(v[0] for v in fair_overs) / non_target_count
            consensus_under = sum(v[0] for v in fair_unders) / non_target_count

            # ── Outlier filter (same logic as _process_game_lines) ──
            OUTLIER_THRESHOLD = 0.15
            clean_overs = [(v, bk) for v, bk in fair_overs
                           if abs(v - consensus_over) <= OUTLIER_THRESHOLD]
            clean_unders = [(v, bk) for v, bk in fair_unders
                            if abs(v - consensus_under) <= OUTLIER_THRESHOLD]
            if not clean_overs:
                clean_overs = fair_overs
            if not clean_unders:
                clean_unders = fair_unders

            # Cross-book best line: sharpest devigged fair prob for each side
            best_over_val, best_over_book = max(clean_overs, key=lambda x: x[0])
            best_under_val, best_under_book = max(clean_unders, key=lambda x: x[0])

            use_crossbook = non_target_count >= 3
            contributing_books = [bk for _, bk in fair_overs]

            for side, consensus, best_val, best_book, target_price in [
                ("Over", consensus_over, best_over_val, best_over_book, target_data["Over"]),
                ("Under", consensus_under, best_under_val, best_under_book, target_data["Under"]),
            ]:
                # Apply side_filter from hypothesis filters (e.g. "Over" or "Under")
                if filters and "side_filter" in filters:
                    if side.lower() != filters["side_filter"].lower():
                        continue

                fair_val = best_val if use_crossbook else consensus
                edge_method = "cross_book_best_line" if use_crossbook else "consensus_devig"

                target_implied = american_to_implied(target_price)
                ev = ev_binary(fair_val, american_to_decimal(target_price))
                kelly = kelly_binary(fair_val, american_to_decimal(target_price))
                edge = fair_val - target_implied  # Probability edge (not EV)

                # Hard cap: same as _process_game_lines
                MAX_EDGE_MAGNITUDE = 0.15
                if abs(edge) > MAX_EDGE_MAGNITUDE:
                    edge = MAX_EDGE_MAGNITUDE if edge > 0 else -MAX_EDGE_MAGNITUDE

                # Require minimum book count for reliable signals —
                # same gate as _process_game_lines to prevent 1-2 book phantom edges
                MIN_BOOKS_FOR_SIGNAL = 4
                # Heavy favorite filter: same as _process_game_lines
                MAX_FAIR_PROB_FOR_SIGNAL = 0.80
                heavy_fav = (mkt_key == "h2h"
                             and fair_val > MAX_FAIR_PROB_FOR_SIGNAL)
                is_signal = (edge >= edge_threshold
                             and non_target_count >= MIN_BOOKS_FOR_SIGNAL
                             and not heavy_fav)

                events += 1
                if is_signal:
                    signals += 1

                event_id = game.get("id", "")

                _pending_rows.append((
                        run_id, event_id, hypothesis_id, game.get("sport_key", ""),
                        player, mkt_key, line, side, target_book,
                        target_price, round(target_implied, 6),
                        round(fair_val, 6),
                        json.dumps({
                            "edge_method": edge_method,
                            "books_used": non_target_count,
                            "devig_method": devig_method,
                            "best_line_book": best_book,
                            "best_line_fair_prob": round(best_val, 6),
                            "consensus_fair_prob": round(consensus, 6),
                            "contributing_books": contributing_books,
                        }),
                        round(edge, 6), round(ev, 6), round(kelly, 6),
                        is_signal, game_date, snapshot_time,
                ))

        # Batch INSERT all rows in one transaction
        if _pending_rows:
            await self._db.executemany(
                "INSERT OR IGNORE INTO backtest_events "
                "(run_id, event_id, hypothesis_id, sport, player, market, "
                "line, side, book, book_odds_american, book_implied_prob, "
                "model_fair_prob, model_factors, edge, ev_pct, kelly_fraction, "
                "signal_generated, game_date, snapshot_time) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                _pending_rows,
            )
            await self._db.commit()
        return events, signals

    async def resolve_with_scores(
        self,
        run_id: str,
        sport: str,
    ) -> dict:
        """
        Resolve backtest events using actual game results.
        Fetches scores from The Odds API (free endpoint).
        For player props, needs external stats source.

        Returns resolution summary.
        """
        from tools.odds_api import get_scores

        # Get unresolved events for this run
        cursor = await self._db.execute(
            "SELECT DISTINCT event_id, game_date FROM backtest_events "
            "WHERE run_id = ? AND actual_result IS NULL",
            (run_id,),
        )
        unresolved = await cursor.fetchall()

        resolved_count = 0
        for event_id, game_date in unresolved:
            # Get scores (free API call)
            scores_data = await get_scores(sport=sport, days_from=3)
            games = scores_data.get("games", [])

            for game in games:
                if game.get("id") != event_id:
                    continue
                if not game.get("completed"):
                    continue

                scores = game.get("scores", [])
                if not scores or len(scores) < 2:
                    continue

                home_score = None
                away_score = None
                for s in scores:
                    if s.get("name") == game.get("home_team"):
                        home_score = int(s.get("score", 0))
                    elif s.get("name") == game.get("away_team"):
                        away_score = int(s.get("score", 0))

                if home_score is None or away_score is None:
                    continue

                total_score = home_score + away_score
                margin = home_score - away_score

                # Resolve spreads, totals, h2h events
                ev_cursor = await self._db.execute(
                    "SELECT id, market, side, line, book_odds_american FROM backtest_events "
                    "WHERE run_id = ? AND event_id = ? AND actual_result IS NULL",
                    (run_id, event_id),
                )
                ev_rows = await ev_cursor.fetchall()

                for ev_id, market, side, line, odds in ev_rows:
                    result = self._resolve_line(
                        market, side, line, home_score, away_score,
                        game.get("home_team", ""), game.get("away_team", ""),
                    )
                    if result:
                        await self._db.execute(
                            "UPDATE backtest_events SET actual_result = ? WHERE id = ?",
                            (result, ev_id),
                        )
                        resolved_count += 1

        await self._db.commit()
        return {"run_id": run_id, "resolved": resolved_count}

    def _resolve_line(
        self,
        market: str,
        side: str,
        line: Optional[float],
        home_score: int,
        away_score: int,
        home_team: str,
        away_team: str,
    ) -> Optional[str]:
        """Determine if a bet won, lost, or pushed."""
        total = home_score + away_score
        margin = home_score - away_score

        # Use fuzzy matching for side identification — side names from Odds API
        # may differ from game_results team names
        is_home = self._team_matches(side, home_team)
        is_away = self._team_matches(side, away_team)

        if market == "h2h":
            if is_home:
                return "won" if margin > 0 else "lost" if margin < 0 else "push"
            elif is_away:
                return "won" if margin < 0 else "lost" if margin > 0 else "push"
            return None

        if market == "spreads" and line is not None:
            # side is the team name, line is their spread
            if is_home:
                adjusted = margin + line
            else:
                adjusted = -margin + line

            if adjusted > 0:
                return "won"
            elif adjusted < 0:
                return "lost"
            return "push"

        if market == "totals" and line is not None:
            if side.lower() == "over":
                if total > line:
                    return "won"
                elif total < line:
                    return "lost"
                return "push"
            elif side.lower() == "under":
                if total < line:
                    return "won"
                elif total > line:
                    return "lost"
                return "push"

        return None

    # Canonical team alias map — maps any known variation to a single key.
    # Covers MLB, NBA, NFL, NHL. Keys are lowercase.
    _TEAM_ALIASES: dict[str, str] = {}

    @staticmethod
    def _build_alias_map() -> dict[str, str]:
        """Build a comprehensive alias -> canonical name mapping."""
        # Each entry: canonical name -> list of known aliases
        teams = {
            # ── MLB ──
            "arizona diamondbacks": ["az diamondbacks", "ari diamondbacks", "d-backs", "dbacks"],
            "atlanta braves": ["atl braves"],
            "baltimore orioles": ["bal orioles", "balt orioles"],
            "boston red sox": ["bos red sox", "redsox"],
            "chicago cubs": ["chi cubs", "chc cubs"],
            "chicago white sox": ["chi white sox", "chw white sox", "chi sox", "whitesox"],
            "cincinnati reds": ["cin reds", "cincy reds"],
            "cleveland guardians": ["cle guardians", "cleveland indians", "cle indians"],
            "colorado rockies": ["col rockies", "colo rockies"],
            "detroit tigers": ["det tigers"],
            "houston astros": ["hou astros"],
            "kansas city royals": ["kc royals"],
            "los angeles angels": ["la angels", "anaheim angels", "laa angels", "angels"],
            "los angeles dodgers": ["la dodgers", "lad dodgers"],
            "miami marlins": ["mia marlins", "fla marlins", "florida marlins"],
            "milwaukee brewers": ["mil brewers"],
            "minnesota twins": ["min twins"],
            "new york mets": ["ny mets", "nym mets"],
            "new york yankees": ["ny yankees", "nyy yankees"],
            "athletics": ["oakland athletics", "oakland a's", "oak athletics", "a's", "as"],
            "philadelphia phillies": ["phi phillies", "philly phillies", "phl phillies"],
            "pittsburgh pirates": ["pit pirates", "pitt pirates"],
            "san diego padres": ["sd padres"],
            "san francisco giants": ["sf giants"],
            "seattle mariners": ["sea mariners"],
            "st. louis cardinals": ["stl cardinals", "st louis cardinals", "saint louis cardinals"],
            "tampa bay rays": ["tb rays"],
            "texas rangers": ["tex rangers"],
            "toronto blue jays": ["tor blue jays", "blue jays"],
            "washington nationals": ["was nationals", "wsh nationals", "nats"],
            # ── NBA ──
            "atlanta hawks": ["atl hawks"],
            "boston celtics": ["bos celtics"],
            "brooklyn nets": ["bkn nets", "bk nets"],
            "charlotte hornets": ["cha hornets", "char hornets"],
            "chicago bulls": ["chi bulls"],
            "cleveland cavaliers": ["cle cavaliers", "cle cavs", "cavs"],
            "dallas mavericks": ["dal mavericks", "dal mavs", "mavs"],
            "denver nuggets": ["den nuggets"],
            "detroit pistons": ["det pistons"],
            "golden state warriors": ["gs warriors", "gsw warriors"],
            "houston rockets": ["hou rockets"],
            "indiana pacers": ["ind pacers"],
            "los angeles clippers": ["la clippers", "lac clippers"],
            "los angeles lakers": ["la lakers", "lal lakers"],
            "memphis grizzlies": ["mem grizzlies"],
            "miami heat": ["mia heat"],
            "milwaukee bucks": ["mil bucks"],
            "minnesota timberwolves": ["min timberwolves", "min wolves", "t-wolves"],
            "new orleans pelicans": ["no pelicans", "nop pelicans", "nola pelicans"],
            "new york knicks": ["ny knicks", "nyk knicks"],
            "oklahoma city thunder": ["okc thunder"],
            "orlando magic": ["orl magic"],
            "philadelphia 76ers": ["phi 76ers", "philly 76ers", "philadelphia sixers", "phi sixers", "sixers"],
            "phoenix suns": ["phx suns"],
            "portland trail blazers": ["por trail blazers", "portland blazers", "por blazers", "blazers"],
            "sacramento kings": ["sac kings"],
            "san antonio spurs": ["sa spurs"],
            "toronto raptors": ["tor raptors"],
            "utah jazz": ["uta jazz"],
            "washington wizards": ["was wizards", "wsh wizards"],
            # ── NFL ──
            "arizona cardinals": ["az cardinals", "ari cardinals"],
            "atlanta falcons": ["atl falcons"],
            "baltimore ravens": ["bal ravens", "balt ravens"],
            "buffalo bills": ["buf bills"],
            "carolina panthers": ["car panthers"],
            "chicago bears": ["chi bears"],
            "cincinnati bengals": ["cin bengals", "cincy bengals"],
            "cleveland browns": ["cle browns"],
            "dallas cowboys": ["dal cowboys"],
            "denver broncos": ["den broncos"],
            "detroit lions": ["det lions"],
            "green bay packers": ["gb packers"],
            "houston texans": ["hou texans"],
            "indianapolis colts": ["ind colts", "indy colts"],
            "jacksonville jaguars": ["jax jaguars", "jac jaguars"],
            "kansas city chiefs": ["kc chiefs"],
            "las vegas raiders": ["lv raiders", "oakland raiders", "oak raiders"],
            "los angeles chargers": ["la chargers", "lac chargers", "san diego chargers", "sd chargers"],
            "los angeles rams": ["la rams", "lar rams", "st. louis rams", "stl rams"],
            "miami dolphins": ["mia dolphins"],
            "minnesota vikings": ["min vikings"],
            "new england patriots": ["ne patriots", "nep patriots", "pats"],
            "new orleans saints": ["no saints", "nola saints"],
            "new york giants": ["ny giants", "nyg giants"],
            "new york jets": ["ny jets", "nyj jets"],
            "philadelphia eagles": ["phi eagles", "philly eagles"],
            "pittsburgh steelers": ["pit steelers", "pitt steelers"],
            "san francisco 49ers": ["sf 49ers", "niners"],
            "seattle seahawks": ["sea seahawks"],
            "tampa bay buccaneers": ["tb buccaneers", "tb bucs", "bucs"],
            "tennessee titans": ["ten titans"],
            "washington commanders": ["was commanders", "wsh commanders", "washington football team"],
            # ── NHL ──
            "anaheim ducks": ["ana ducks"],
            "boston bruins": ["bos bruins"],
            "buffalo sabres": ["buf sabres"],
            "calgary flames": ["cgy flames", "cal flames"],
            "carolina hurricanes": ["car hurricanes", "canes"],
            "chicago blackhawks": ["chi blackhawks"],
            "colorado avalanche": ["col avalanche", "avs"],
            "columbus blue jackets": ["cbj blue jackets", "blue jackets"],
            "dallas stars": ["dal stars"],
            "detroit red wings": ["det red wings"],
            "edmonton oilers": ["edm oilers"],
            "florida panthers": ["fla panthers"],
            "los angeles kings": ["la kings", "lak kings"],
            "minnesota wild": ["min wild"],
            "montreal canadiens": ["mtl canadiens", "canadiens", "habs"],
            "nashville predators": ["nsh predators", "nas predators", "preds"],
            "new jersey devils": ["nj devils", "njd devils"],
            "new york islanders": ["ny islanders", "nyi islanders"],
            "new york rangers": ["ny rangers", "nyr rangers"],
            "ottawa senators": ["ott senators", "sens"],
            "philadelphia flyers": ["phi flyers", "philly flyers"],
            "pittsburgh penguins": ["pit penguins", "pitt penguins", "pens"],
            "san jose sharks": ["sj sharks"],
            "seattle kraken": ["sea kraken"],
            "st. louis blues": ["stl blues", "st louis blues", "saint louis blues"],
            "tampa bay lightning": ["tb lightning", "tbl lightning", "bolts"],
            "toronto maple leafs": ["tor maple leafs", "leafs"],
            "utah mammoth": ["uta mammoth", "utah hockey club", "utah hc"],
            "vancouver canucks": ["van canucks"],
            "vegas golden knights": ["vgk golden knights", "vegas knights", "golden knights"],
            "washington capitals": ["was capitals", "wsh capitals", "caps"],
            "winnipeg jets": ["wpg jets"],
        }

        alias_map: dict[str, str] = {}
        for canonical, aliases in teams.items():
            alias_map[canonical] = canonical
            for alias in aliases:
                alias_map[alias] = canonical
        return alias_map

    @staticmethod
    def _normalize_team(name: str) -> str:
        """Normalize team name for fuzzy matching across data sources.

        Uses a canonical alias map for exact lookups, then falls back to
        city-abbreviation replacement for unknown names.

        Handles differences between Odds API names (e.g. "Los Angeles Dodgers")
        and ESPN names (e.g. "LA Dodgers", "Athletics", etc.).
        """
        if not name:
            return ""
        n = name.strip().lower()
        # Remove trailing periods from abbreviations (e.g. "St." -> "st")
        n = " ".join(n.split())

        # Build alias map once (lazy singleton)
        if not BacktestEngine._TEAM_ALIASES:
            BacktestEngine._TEAM_ALIASES = BacktestEngine._build_alias_map()

        # Direct alias lookup
        if n in BacktestEngine._TEAM_ALIASES:
            return BacktestEngine._TEAM_ALIASES[n]

        # Fallback: city abbreviation replacement for unknown names
        city_replacements = {
            "los angeles": "la",
            "new york": "ny",
            "san francisco": "sf",
            "san antonio": "sa",
            "san diego": "sd",
            "golden state": "gs",
            "oklahoma city": "okc",
            "portland trail blazers": "portland blazers",
            "brooklyn": "bkn",
            "saint louis": "st. louis",
            "st louis": "st. louis",
        }
        for full, abbrev in city_replacements.items():
            if n.startswith(full):
                n = abbrev + n[len(full):]
                break
        n = " ".join(n.split())
        return n

    @staticmethod
    def _team_matches(name_a: str, name_b: str) -> bool:
        """Check if two team names refer to the same team.

        Uses canonical alias resolution first, then falls back to
        mascot matching and substring containment.
        """
        if not name_a or not name_b:
            return False
        if name_a == name_b:
            return True

        a = BacktestEngine._normalize_team(name_a)
        b = BacktestEngine._normalize_team(name_b)

        if a == b:
            return True

        # Last word (mascot) match — "LA Dodgers" vs "Los Angeles Dodgers"
        # Only match if mascot has 4+ chars to avoid false positives
        a_last = a.rsplit(None, 1)[-1] if a else ""
        b_last = b.rsplit(None, 1)[-1] if b else ""
        if a_last == b_last and len(a_last) > 3:
            return True

        # Substring: "Athletics" matches "Oakland Athletics" or "Athletics"
        if len(a) > 3 and len(b) > 3 and (a in b or b in a):
            return True

        return False

    async def resolve_from_game_results(
        self,
        run_id: Optional[str] = None,
        sport: Optional[str] = None,
    ) -> dict:
        """
        Resolve backtest events using the local game_results table.
        No API calls needed — matches on game_date + teams with fuzzy name matching.

        If run_id is given, resolves only that run's events.
        If sport is given without run_id, resolves all unresolved events for that sport.
        If neither, resolves everything possible.
        """
        # Build query for unresolved events
        if run_id:
            cursor = await self._db.execute(
                "SELECT id, event_id, sport, market, side, line, game_date, model_factors "
                "FROM backtest_events WHERE run_id = ? AND actual_result IS NULL",
                (run_id,),
            )
        elif sport:
            cursor = await self._db.execute(
                "SELECT id, event_id, sport, market, side, line, game_date, model_factors "
                "FROM backtest_events WHERE sport = ? AND actual_result IS NULL",
                (sport,),
            )
        else:
            cursor = await self._db.execute(
                "SELECT id, event_id, sport, market, side, line, game_date, model_factors "
                "FROM backtest_events WHERE actual_result IS NULL",
            )

        unresolved = await cursor.fetchall()
        if not unresolved:
            return {"resolved": 0, "unresolved": 0}

        # Build a lookup of game results indexed by (sport, date) -> list of games
        # Primary: game_results table. Fallback: game_contexts table (has scores too).
        from collections import defaultdict
        games_by_date = defaultdict(list)
        seen = set()

        result_cursor = await self._db.execute(
            "SELECT sport, game_date, home_team, away_team, home_score, away_score "
            "FROM game_results",
        )
        result_rows = await result_cursor.fetchall()
        for r_sport, r_date, r_home, r_away, r_hscore, r_ascore in result_rows:
            key = (r_sport, r_date, r_home, r_away)
            seen.add(key)
            games_by_date[(r_sport, r_date)].append((r_home, r_away, r_hscore, r_ascore))
            games_by_date[("", r_date)].append((r_home, r_away, r_hscore, r_ascore))

        # Fallback: game_contexts also stores scores from ESPN
        ctx_cursor = await self._db.execute(
            "SELECT sport, game_date, home_team, away_team, home_score, away_score "
            "FROM game_contexts WHERE home_score IS NOT NULL AND away_score IS NOT NULL",
        )
        ctx_rows = await ctx_cursor.fetchall()
        ctx_added = 0
        for r_sport, r_date, r_home, r_away, r_hscore, r_ascore in ctx_rows:
            key = (r_sport, r_date, r_home, r_away)
            if key not in seen:
                seen.add(key)
                games_by_date[(r_sport, r_date)].append((r_home, r_away, r_hscore, r_ascore))
                games_by_date[("", r_date)].append((r_home, r_away, r_hscore, r_ascore))
                ctx_added += 1
        if ctx_added > 0:
            logger.info(f"Resolution: added {ctx_added} games from game_contexts fallback")

        resolved_count = 0
        match_failures = 0
        from datetime import datetime as _dt, timedelta as _td
        for ev_id, event_id, ev_sport, market, side, line, game_date, model_factors in unresolved:
            # Extract home/away from event_id or model_factors
            home_team = ""
            away_team = ""

            if event_id and "|" in event_id:
                parts = event_id.split("|")
                if len(parts) >= 3:
                    home_team = parts[1]
                    away_team = parts[2]
            elif model_factors:
                try:
                    factors = json.loads(model_factors)
                    home_team = factors.get("home_team", "")
                    away_team = factors.get("away_team", "")
                except (json.JSONDecodeError, TypeError):
                    pass

            if not home_team or not away_team:
                continue

            # Fuzzy match: find the game in results for this date
            # Try exact date first, then ±1 day to handle timezone offsets
            # (odds API uses US dates, results sources may use UTC/AU dates)
            scores = None
            try:
                base = _dt.strptime(game_date, "%Y-%m-%d")
                date_candidates = [
                    game_date,
                    (base + _td(days=1)).strftime("%Y-%m-%d"),
                    (base - _td(days=1)).strftime("%Y-%m-%d"),
                ]
            except ValueError:
                date_candidates = [game_date]

            for try_date in date_candidates:
                candidates = games_by_date.get((ev_sport, try_date), [])
                if not candidates:
                    candidates = games_by_date.get(("", try_date), [])

                for r_home, r_away, r_hscore, r_ascore in candidates:
                    if self._team_matches(home_team, r_home) and self._team_matches(away_team, r_away):
                        scores = (r_hscore, r_ascore)
                        break
                    # Also try swapped home/away (data source differences)
                    if self._team_matches(home_team, r_away) and self._team_matches(away_team, r_home):
                        scores = (r_ascore, r_hscore)
                        break
                if scores:
                    break

            if not scores:
                match_failures += 1
                if match_failures <= 5:
                    logger.debug(
                        f"Resolution miss: {ev_sport} {game_date} "
                        f"{home_team} vs {away_team} — "
                        f"no matching game_result found"
                    )
                continue

            home_score, away_score = scores
            if home_score is None or away_score is None:
                continue

            result = self._resolve_line(
                market, side, line, home_score, away_score, home_team, away_team
            )
            if result:
                await self._db.execute(
                    "UPDATE backtest_events SET actual_result = ? WHERE id = ?",
                    (result, ev_id),
                )
                resolved_count += 1

        await self._db.commit()
        if match_failures > 0:
            logger.warning(
                f"Resolution: {match_failures}/{len(unresolved)} events could not match "
                f"to game_results (missing game data or team name mismatch)"
            )
        logger.info(f"Resolved {resolved_count}/{len(unresolved)} backtest events from game_results")

        # Recalculate run-level stats for any runs that had events resolved
        if resolved_count > 0:
            affected_runs = await self._get_affected_run_ids(run_id)
            recalc_count = 0
            for rid in affected_runs:
                updated = await self.recalculate_run_stats(rid)
                if updated:
                    recalc_count += 1
            if recalc_count > 0:
                logger.info(f"Recalculated stats for {recalc_count} backtest runs after resolution")

        return {"resolved": resolved_count, "unresolved": len(unresolved) - resolved_count}

    async def _get_affected_run_ids(self, run_id: Optional[str] = None) -> list[str]:
        """Get run IDs that have resolved events but stale run-level stats."""
        if run_id:
            return [run_id]
        # Find all completed runs that have resolved events but null/zero stats
        cursor = await self._db.execute(
            "SELECT DISTINCT br.run_id FROM backtest_runs br "
            "JOIN backtest_events be ON be.run_id = br.run_id "
            "WHERE br.completed_at IS NOT NULL "
            "AND br.total_events > 0 "
            "AND be.actual_result IS NOT NULL "
            "AND (br.actual_win = 0 AND br.actual_loss = 0 AND br.hit_rate IS NULL)"
        )
        return [r[0] for r in await cursor.fetchall()]

    async def recalculate_run_stats(self, run_id: str) -> bool:
        """Recalculate win/loss/hit_rate for a run from its SIGNALED events only."""
        cursor = await self._db.execute(
            "SELECT actual_result, COUNT(*) FROM backtest_events "
            "WHERE run_id = ? AND actual_result IS NOT NULL "
            "AND signal_generated = 1 "
            "GROUP BY actual_result",
            (run_id,),
        )
        results = {r[0]: r[1] for r in await cursor.fetchall()}

        wins = results.get("won", 0)
        losses = results.get("lost", 0)
        pushes = results.get("push", 0)
        total_decided = wins + losses

        if total_decided == 0:
            return False  # Nothing resolved yet

        # Count unresolved
        cursor = await self._db.execute(
            "SELECT COUNT(*) FROM backtest_events "
            "WHERE run_id = ? AND actual_result IS NULL",
            (run_id,),
        )
        unresolved = (await cursor.fetchone())[0]

        hit_rate = wins / total_decided if total_decided > 0 else None

        # Calculate avg_edge, avg_ev from signal-generated events only
        cursor = await self._db.execute(
            "SELECT AVG(CASE WHEN signal_generated = 1 THEN edge END), "
            "AVG(CASE WHEN signal_generated = 1 THEN ev_pct END), "
            "AVG(CASE WHEN signal_generated = 1 THEN clv_implied END) "
            "FROM backtest_events WHERE run_id = ? AND actual_result IS NOT NULL",
            (run_id,),
        )
        row = await cursor.fetchone()
        avg_edge = row[0]
        avg_ev = row[1]
        avg_clv = row[2]

        await self._db.execute(
            "UPDATE backtest_runs SET "
            "actual_win = ?, actual_loss = ?, actual_push = ?, unresolved = ?, "
            "hit_rate = ?, avg_edge = ?, avg_ev = ?, avg_clv = ? "
            "WHERE run_id = ?",
            (wins, losses, pushes, unresolved, hit_rate, avg_edge, avg_ev, avg_clv, run_id),
        )
        await self._db.commit()
        logger.info(
            f"Run {run_id}: recalculated stats — {wins}W/{losses}L/{pushes}P "
            f"({unresolved} unresolved), hit_rate={hit_rate:.3f}" if hit_rate else
            f"Run {run_id}: recalculated stats — {wins}W/{losses}L/{pushes}P "
            f"({unresolved} unresolved)"
        )
        return True

    async def generate_paper_trade_signal(
        self,
        hypothesis_id: str,
        live_odds: dict,
    ) -> list[dict]:
        """
        For paper trading: apply model to current live odds.
        Returns signals meeting threshold. Does NOT place bets.
        """
        h = await self.hypothesis_manager.get_hypothesis(hypothesis_id)
        if not h or h["status"] != "paper_trading":
            return []

        config = h["model_config"]
        if isinstance(config, str):
            try:
                config = json.loads(config)
            except (json.JSONDecodeError, TypeError):
                config = {}
        target_book = config.get("target_book", "draftkings")
        edge_threshold = h["edge_threshold"]
        devig_method = config.get("devig_method", "power")
        min_books = config.get("consensus_min_books", 3)

        signals = []
        games = live_odds.get("games", [])
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        now = datetime.now(timezone.utc).isoformat()

        # Parse hypothesis-specific filters (same as main backtest path)
        thesis = h.get("thesis", "")
        h_name = h.get("name", "")
        sport = h.get("sport", "")
        filters = self._parse_hypothesis_filters(thesis, config, h_name)

        # ── Build schedule context for game-level filtering (matches backtest path) ──
        # Without this, context-based hypotheses (b2b, road_trip, rest, etc.)
        # will NEVER produce signals because _game_matches_context_filter fails closed.
        use_context_filter = self._needs_context_filter(h_name, thesis, config)
        schedule_context = {}
        context_filtered = 0
        if use_context_filter and sport:
            # Use 30-day lookback so _build_schedule_context can compute
            # days_rest, b2b, road_streak etc. from prior game_results.
            # A 1-day window causes all teams to get defaults (b2b=False,
            # days_rest=99) which then fail context filters → 0 trades.
            context_start = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
            schedule_context = await self._build_schedule_context(
                sport, context_start, today,
            )
            if schedule_context:
                logger.info(
                    f"Paper trade {hypothesis_id}: context filter ENABLED — "
                    f"{len(schedule_context)} games have schedule context"
                )
            else:
                logger.warning(
                    f"Paper trade {hypothesis_id}: context filter ENABLED but "
                    f"schedule_context is EMPTY — all games will be rejected (fail-closed)"
                )

        for game in games:
            # ── Game-level context filter (same as backtest path) ──
            if use_context_filter:
                if not schedule_context:
                    context_filtered += 1
                    continue
                home = game.get("home_team", "")
                away = game.get("away_team", "")
                game_ctx = schedule_context.get((today, home, away), {})
                if not self._game_matches_context_filter(
                    game_ctx, h_name, thesis, config,
                ):
                    context_filtered += 1
                    continue

            # Use same processing logic as backtest
            if h["market_type"].startswith("player_"):
                events, _ = await self._process_game_props(
                    run_id="paper",  # won't be stored via run
                    hypothesis_id=hypothesis_id,
                    game=game,
                    game_date=today,
                    snapshot_time=now,
                    market_type=h["market_type"],
                    target_book=target_book,
                    edge_threshold=edge_threshold,
                    devig_method=devig_method,
                    min_books=min_books,
                    config=config,
                    filters=filters,
                )
            else:
                events, _ = await self._process_game_lines(
                    run_id="paper",
                    hypothesis_id=hypothesis_id,
                    game=game,
                    game_date=today,
                    snapshot_time=now,
                    market_type=h["market_type"],
                    target_book=target_book,
                    edge_threshold=edge_threshold,
                    devig_method=devig_method,
                    min_books=min_books,
                    config=config,
                    h_sport=sport,
                    filters=filters,
                )

        if context_filtered:
            logger.info(
                f"Paper trade {hypothesis_id}: {context_filtered} games "
                f"filtered by context, {len(games) - context_filtered} processed"
            )

        # Retrieve signals that were just generated with run_id="paper"
        cursor = await self._db.execute(
            "SELECT * FROM backtest_events "
            "WHERE run_id = 'paper' AND hypothesis_id = ? AND signal_generated = 1 "
            "AND game_date = ?",
            (hypothesis_id, today),
        )
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]

        for row in rows:
            event = dict(zip(cols, row))
            trade_id = str(uuid.uuid4())[:12]

            # Move to paper_trades table
            await self._db.execute(
                "INSERT OR IGNORE INTO paper_trades "
                "(trade_id, hypothesis_id, event_id, sport, player, market, "
                "line, side, book, signal_time, signal_odds_american, "
                "signal_implied_prob, model_fair_prob, edge, ev_pct, "
                "kelly_fraction, game_date) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    trade_id, hypothesis_id, event["event_id"],
                    event["sport"], event.get("player"), event["market"],
                    event.get("line"), event["side"], event["book"],
                    now, event["book_odds_american"],
                    event["book_implied_prob"], event["model_fair_prob"],
                    event["edge"], event["ev_pct"],
                    event.get("kelly_fraction"), today,
                ),
            )
            # Also insert into signals table
            edge_val = event.get("edge", 0) or 0
            confidence = _signal_confidence(edge_val)
            await self._db.execute(
                "INSERT INTO signals "
                "(event_id, sport, signal_type, team, market, book, "
                "odds_american, fair_probability, fair_prob_source, "
                "edge_pct, ev_pct, confidence, kelly_fraction, "
                "recommended_stake, status, notes) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event.get("event_id"),
                    event["sport"],
                    "paper_trade",
                    event["side"],
                    event["market"],
                    event["book"],
                    event.get("book_odds_american", 0),
                    event.get("model_fair_prob", 0),
                    "cross_book_devig",
                    edge_val,
                    event.get("ev_pct", 0) or 0,
                    confidence,
                    event.get("kelly_fraction"),
                    None,
                    "paper",
                    f"hypothesis_id={hypothesis_id}, trade_id={trade_id}",
                ),
            )

            signals.append({
                "trade_id": trade_id,
                **event,
            })

        # Clean up temporary paper events from backtest_events
        await self._db.execute(
            "DELETE FROM backtest_events WHERE run_id = 'paper' AND hypothesis_id = ?",
            (hypothesis_id,),
        )
        await self._db.commit()

        return signals

    async def get_run_results(self, run_id: str) -> dict:
        """Retrieve full backtest results for a run."""
        # Run metadata
        cursor = await self._db.execute(
            "SELECT * FROM backtest_runs WHERE run_id = ?", (run_id,),
        )
        run_row = await cursor.fetchone()
        if not run_row:
            return {"error": "Run not found"}
        run_cols = [d[0] for d in cursor.description]
        run = dict(zip(run_cols, run_row))

        # Signal events
        ev_cursor = await self._db.execute(
            "SELECT * FROM backtest_events "
            "WHERE run_id = ? AND signal_generated = 1 "
            "ORDER BY edge DESC LIMIT 100",
            (run_id,),
        )
        ev_rows = await ev_cursor.fetchall()
        ev_cols = [d[0] for d in ev_cursor.description]
        signals = [dict(zip(ev_cols, r)) for r in ev_rows]

        # Aggregate stats
        stats_cursor = await self._db.execute(
            "SELECT "
            "COUNT(*) as total, "
            "SUM(CASE WHEN signal_generated = 1 THEN 1 ELSE 0 END) as signals, "
            "AVG(CASE WHEN signal_generated = 1 THEN edge END) as avg_edge, "
            "AVG(CASE WHEN signal_generated = 1 THEN ev_pct END) as avg_ev, "
            "SUM(CASE WHEN signal_generated = 1 AND actual_result = 'won' THEN 1 ELSE 0 END) as wins, "
            "SUM(CASE WHEN signal_generated = 1 AND actual_result = 'lost' THEN 1 ELSE 0 END) as losses, "
            "SUM(CASE WHEN signal_generated = 1 AND actual_result = 'push' THEN 1 ELSE 0 END) as pushes "
            "FROM backtest_events WHERE run_id = ?",
            (run_id,),
        )
        stats_row = await stats_cursor.fetchone()
        stats_cols = [d[0] for d in stats_cursor.description]
        stats = dict(zip(stats_cols, stats_row))

        return {
            "run": run,
            "stats": stats,
            "top_signals": signals,
        }
