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

import asyncio
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

# HARD GATE statuses now live in tools.signals.paper (single source of truth).
# This alias keeps existing callers/imports working; do NOT add "live" here —
# update tools/signals/paper.py only after a separately reviewed live path.
from tools.signals.paper import _PAPER_TRADE_SIGNAL_STATUSES
from tools.signals.paper import allowed_paper_statuses, reject_non_paper
from tools.signals.schedule import game_date_from_commence
from tools import backtest_io


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
        # Lightweight fingerprint cache for staleness detection in
        # recalculate_all_active_runs.  Key = run_id, value = (total_events,
        # signals_count, resolved_count).  Only runs whose fingerprint
        # changed since last recalculation get the expensive scipy/numpy
        # recompute.  Keeps 10-15 min stalls down to seconds.
        self._run_fingerprints: dict[str, tuple[int, int, int]] = {}
        self._RUN_FP_MAX = 500  # Cap fingerprint cache — only active runs matter

    async def initialize(self) -> None:
        from tools.schema import open_db
        self._db = await open_db(self.db_path)
        # Override with even longer timeout for bulk backtest writes
        await self._db.execute("PRAGMA busy_timeout = 300000")
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
                # Filter out cross-sport contamination before returning
                better_snapshot["games"] = [
                    g for g in better_snapshot.get("games", [])
                    if not g.get("sport_key") or g["sport_key"] == sport
                ]
                for g in better_snapshot["games"]:
                    if not g.get("sport_key"):
                        g["sport_key"] = sport
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

        # HARD GATE (2026-04-22 FWER audit): binary-both-sides markets
        # (totals O/U, h2h ML) without a side_filter double-count events —
        # each game produces rows for BOTH sides, diluting/selection-biasing
        # the signal population.  Refuse to run unless:
        #   - the hypothesis config sets model_config['legacy']=True  (grandfather), OR
        #   - CALLISTO_ALLOW_BOTH_SIDES=1 is set in the env (emergency override)
        _binary_both = market_type in ("totals", "h2h")
        _has_side = "side_filter" in filters
        _allow_both = os.getenv("CALLISTO_ALLOW_BOTH_SIDES", "0").strip() in ("1", "true", "yes")
        _is_legacy = bool((config or {}).get("legacy") is True)
        if _binary_both and not _has_side and not _allow_both and not _is_legacy:
            logger.error(
                f"Backtest {hypothesis_id}: REJECTED — market_type={market_type} "
                f"requires side_filter.  Add 'side_filter' to model_config or "
                f"encode 'over'/'under'/'home'/'away' in thesis/name, or set "
                f"CALLISTO_ALLOW_BOTH_SIDES=1, or mark model_config['legacy']=True."
            )
            return {
                "hypothesis_id": hypothesis_id,
                "hypothesis_name": h_name,
                "error": "side_filter_required",
                "detail": (
                    f"Binary-both-sides hypothesis ({market_type}) without "
                    f"side_filter — cannot run.  Both sides would be evaluated "
                    f"and double-count events, selection-biasing the signal "
                    f"set.  Fix: set model_config['side_filter']='Over'|'Under' "
                    f"(or 'home'|'away'), or mark legacy=True for grandfather."
                ),
                "total_events": 0,
                "signals_generated": 0,
                "hypothesis_filters": filters if filters else {},
            }
        if _binary_both and not _has_side and (_allow_both or _is_legacy):
            logger.warning(
                f"Backtest {hypothesis_id}: ALLOW_BOTH_SIDES bypass — legacy="
                f"{_is_legacy}, env_override={_allow_both}.  BOTH sides will "
                f"be evaluated (2x events, diluted signal)."
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

        structured = self.has_structured_filters(config)
        if context_coverage < 0.5 and not structured:
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
        elif context_coverage < 0.5 and structured:
            logger.info(
                f"Backtest {hypothesis_id}: context_coverage={context_coverage:.0%} but "
                f"has structured line_filters/game_filters — proceeding with backtest. "
                f"Unfilterable factors (informational): {unfilterable}"
            )

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
        # Include game_filters and line_filters in fingerprint so hypotheses
        # with different game-level conditions get unique fingerprints.
        # Without these, hypotheses differing only in game_filters (b2b, rest,
        # homestand, altitude, etc.) collide and are incorrectly skipped.
        game_filters = config.get("game_filters")
        line_filters = config.get("line_filters")
        fp_parts = json.dumps(
            {"sport": sport, "market": market_type, "start": start_date,
             "end": end_date, "filters": filters, "target": target_book,
             "threshold": edge_threshold, "devig": devig_method, "min_books": min_books,
             "context_factors": context_factors_sorted,
             "uses_context": uses_context,
             "game_filters": game_filters,
             "line_filters": line_filters,
             "hypothesis_name": h_name},
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

        # DEFERRED WRITE: Don't INSERT backtest_runs yet — the write lock
        # contention with line_monitor causes 5-minute blocks. Do all reads
        # first (computing events/signals), then batch-write at the end.
        # The run_id is still generated so _process_game_lines can reference it.
        _deferred_status_update = h["status"] == "draft"

        # Fetch historical data
        logger.info(f"Backtest {run_id}: fetching {sport} odds {start_date} to {end_date}")

        # Determine which markets to fetch based on hypothesis type
        is_prop_hypothesis = market_type.startswith("player_")

        if is_prop_hypothesis:
            # Player props: fetch from prop_snapshots table (multi-book prop data)
            # instead of historical_odds_cache (game-level only).
            fetch_markets = "h2h,spreads,totals"  # Still need game-level for context
            prop_lines = await self.historical_fetcher.fetch_prop_snapshots(
                sport=sport,
                start_date=start_date,
                end_date=end_date,
                market_type=market_type,
            )
            logger.info(
                f"Backtest {run_id}: fetched {len(prop_lines)} prop lines from prop_snapshots"
            )
        else:
            fetch_markets = "h2h,spreads,totals"
            prop_lines = []

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
        all_pending_rows: list[tuple] = []  # Collect ALL event rows for batch INSERT

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

        # Track aggregate snapshot-quality mix across the whole run so the
        # promotion gate downstream can enforce the >=80% pre_commence rule.
        run_quality_mix = {"pre_commence": 0, "closing_fallback": 0, "closing_mode": 0}

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
            # Aggregate the lookahead-mix summary emitted by historical_odds.py
            _snap_mix = snapshot.get("snapshot_quality_mix") or {}
            for k in run_quality_mix:
                run_quality_mix[k] += int(_snap_mix.get(k, 0) or 0)
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
                # ── Sport-level defense-in-depth filter ──
                # Reject games with missing/empty sport_key too — otherwise
                # FanDuel games without competitionId bypass this filter and
                # contaminate backtests across sports (identical event sets).
                game_sport = game.get("sport_key", "")
                if not game_sport or game_sport != sport:
                    continue

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

                events, signals, rows = await self._process_game(
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
                all_pending_rows.extend(rows)

        if context_filtered > 0:
            logger.info(
                f"Backtest {run_id}: context filter removed {context_filtered} games "
                f"that didn't match schedule requirements"
            )

        # ── Player prop backtesting from prop_snapshots ──
        # Process prop lines fetched from prop_snapshots table (separate from
        # game-level odds). Each prop_line has multi-book data for a specific
        # player/market/line combination.
        if is_prop_hypothesis and prop_lines:
            events_from_props, signals_from_props = await self._process_prop_snapshots(
                run_id=run_id,
                hypothesis_id=hypothesis_id,
                prop_lines=prop_lines,
                target_book=target_book,
                edge_threshold=edge_threshold,
                devig_method=devig_method,
                config=config,
                h_sport=sport,
                filters=filters,
            )
            total_events += events_from_props
            total_signals += signals_from_props
            logger.info(
                f"Backtest {run_id}: prop snapshots produced "
                f"{events_from_props} events, {signals_from_props} signals"
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

        # Persist lookahead-mode summary into run_config so the promotion gate
        # and re-eval harness can audit which lead_minutes + snapshot_quality
        # mix produced these stats. Lets downstream reject >=20% closing_fallback.
        try:
            _lead_minutes = int(os.getenv("CALLISTO_BACKTEST_LEAD_MINUTES", "60"))
        except (ValueError, TypeError):
            _lead_minutes = 60
        config = dict(config) if isinstance(config, dict) else {"raw": config}
        config["_lookahead"] = {
            "lead_minutes": _lead_minutes,
            "snapshot_quality_mix": run_quality_mix,
        }

        # ── DEFERRED WRITE: batch ALL writes into one transaction ──
        # Uses a FRESH connection to avoid lock contention from persistent
        # connections that may hold implicit read transactions. The persistent
        # self._db connection shares the event loop with line_monitor, data_collector,
        # etc., causing deadlock-like contention. A fresh connection writes cleanly.
        # Diagnostic: check which connections have open transactions
        _diag_parts = []
        for _dname, _ddb in [
            ("self._db", self._db),
            ("self.db", getattr(self, "db", None)),
        ]:
            if _ddb and hasattr(_ddb, "_conn") and _ddb._conn:
                try:
                    _in_tx = _ddb._conn.in_transaction
                    _diag_parts.append(f"{_dname}.in_transaction={_in_tx}")
                except Exception:
                    _diag_parts.append(f"{_dname}=check_failed")
        logger.info(
            f"Backtest {run_id}: starting deferred write — "
            f"{total_events} events, {len(all_pending_rows)} rows, "
            f"status_update={_deferred_status_update}, "
            f"conn_state=[{', '.join(_diag_parts)}]"
        )
        completed = datetime.now(timezone.utc).isoformat()
        # WriteCoordinator path (single-writer pattern). The whole batch is one
        # logical transaction: the run row, the optional draft→backtesting
        # status flip, and the bulk event insert. Routing through the
        # coordinator means it never competes with line_monitor / hermes /
        # task_queue writers — they share the queue.
        try:
            from tools.db_writer import get_writer_if_running
            coord = get_writer_if_running(self.db_path)
        except Exception:
            coord = None
        if coord is not None:
            ops: list[tuple[str, tuple]] = [
                (
                    "INSERT OR REPLACE INTO backtest_runs "
                    "(run_id, hypothesis_id, date_range_start, date_range_end, "
                    "started_at, run_config, total_events, signals_generated, completed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (run_id, hypothesis_id, start_date, end_date, now,
                     json.dumps(config), total_events, total_signals, completed),
                ),
            ]
            if _deferred_status_update:
                ops.append((
                    "UPDATE hypotheses SET status = 'backtesting', "
                    "updated_at = datetime('now') WHERE hypothesis_id = ? "
                    "AND status = 'draft'",
                    (hypothesis_id,),
                ))
            await coord.transaction(ops)
            if all_pending_rows:
                await coord.executemany(
                    "INSERT OR IGNORE INTO backtest_events "
                    "(run_id, event_id, hypothesis_id, sport, player, market, "
                    "line, side, book, book_odds_american, book_implied_prob, "
                    "model_fair_prob, model_factors, edge, ev_pct, kelly_fraction, "
                    "signal_generated, game_date, snapshot_time) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    all_pending_rows,
                )
        else:
            # Legacy path: open a per-write connection, retry on lock.
            import random as _rnd_bt
            import aiosqlite as _aiosqlite_bt
            for _bt_write_attempt in range(5):
                _write_db = None
                try:
                    _write_db = await _aiosqlite_bt.connect(self.db_path)
                    await _write_db.execute("PRAGMA busy_timeout = 60000")
                    await _write_db.execute("PRAGMA journal_mode = WAL")
                    await _write_db.execute("PRAGMA synchronous = NORMAL")
                    await _write_db.execute(
                        "INSERT OR REPLACE INTO backtest_runs "
                        "(run_id, hypothesis_id, date_range_start, date_range_end, "
                        "started_at, run_config, total_events, signals_generated, completed_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (run_id, hypothesis_id, start_date, end_date, now,
                         json.dumps(config), total_events, total_signals, completed),
                    )
                    if _deferred_status_update:
                        await _write_db.execute(
                            "UPDATE hypotheses SET status = 'backtesting', "
                            "updated_at = datetime('now') WHERE hypothesis_id = ? "
                            "AND status = 'draft'",
                            (hypothesis_id,),
                        )
                    if all_pending_rows:
                        await _write_db.executemany(
                            "INSERT OR IGNORE INTO backtest_events "
                            "(run_id, event_id, hypothesis_id, sport, player, market, "
                            "line, side, book, book_odds_american, book_implied_prob, "
                            "model_fair_prob, model_factors, edge, ev_pct, kelly_fraction, "
                            "signal_generated, game_date, snapshot_time) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            all_pending_rows,
                        )
                    await _write_db.commit()
                    await _write_db.close()
                    break
                except Exception as _bw_e:
                    if _write_db:
                        try:
                            await _write_db.close()
                        except Exception:
                            pass
                    if "locked" in str(_bw_e).lower() and _bt_write_attempt < 4:
                        _wait = min(2 * (2 ** _bt_write_attempt), 15) + _rnd_bt.uniform(0, 1)
                        logger.warning(
                            f"Backtest {run_id} deferred write locked "
                            f"(attempt {_bt_write_attempt + 1}/5), retrying in {_wait:.1f}s"
                        )
                        await asyncio.sleep(_wait)
                    else:
                        raise

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


    # ── EXTRACTED HELPERS (tools/backtest_io.py) ──
    # Filter parsing, context-factor registries, context matching, schedule
    # context, bet resolution, and team-name matching now live in
    # tools/backtest_io.py. Thin delegators keep the historical API stable.

    UNFILTERABLE_CONTEXT_FACTORS = backtest_io.UNFILTERABLE_CONTEXT_FACTORS
    FILTERABLE_CONTEXT_FACTORS = backtest_io.FILTERABLE_CONTEXT_FACTORS
    _CONTEXT_KEYWORD_MAP = backtest_io._CONTEXT_KEYWORD_MAP
    _TEAM_ALIASES = backtest_io._TEAM_ALIASES

    @staticmethod
    def has_structured_filters(config: dict) -> bool:
        return backtest_io.has_structured_filters(config)

    @staticmethod
    def _infer_context_needs(thesis: str, name: str) -> list[str]:
        return backtest_io._infer_context_needs(thesis, name)

    @staticmethod
    def _parse_hypothesis_filters(thesis: str, config: dict, hypothesis_id: str = "") -> dict:
        return backtest_io._parse_hypothesis_filters(thesis, config, hypothesis_id)

    def _matches_hypothesis_conditions(self, *args, **kwargs) -> bool:
        return backtest_io.matches_hypothesis_conditions(*args, **kwargs)

    @staticmethod
    def _log_unfilterable_context_factors(hypothesis_id: str, config: dict) -> list[str]:
        return backtest_io._log_unfilterable_context_factors(hypothesis_id, config)

    @staticmethod
    def compute_context_coverage(config: dict) -> float:
        return backtest_io.compute_context_coverage(config)

    async def _build_schedule_context(
        self, sport: str, start_date: str, end_date: str,
        live_games: list[tuple[str, str, str]] | None = None,
    ) -> dict:
        return await backtest_io.build_schedule_context(
            self._db, sport, start_date, end_date,
            live_games=live_games,
        )

    @staticmethod
    def _game_matches_context_filter(*args, **kwargs) -> bool:
        return backtest_io._game_matches_context_filter(*args, **kwargs)

    @staticmethod
    def _needs_context_filter(hypothesis_name: str, thesis: str, config: dict) -> bool:
        return backtest_io._needs_context_filter(hypothesis_name, thesis, config)

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
        return backtest_io.resolve_line(
            market, side, line, home_score, away_score, home_team, away_team,
        )

    @staticmethod
    def _build_alias_map() -> dict[str, str]:
        return backtest_io._build_alias_map()

    @staticmethod
    def _normalize_team(name: str) -> str:
        return backtest_io._normalize_team(name)

    @staticmethod
    def _team_matches(name_a: str, name_b: str) -> bool:
        return backtest_io._team_matches(name_a, name_b)


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
                    return 0, 0, []

        available_books = {bm.get("key", "").lower() for bm in game.get("bookmakers", [])}
        bookmaker_count = len(available_books)

        # Multi-book edge detection: need at least 3 books total.
        # Need at least min_books+1 total (min_books for consensus + 1 target).
        # For thin markets (NCAAW, NWSL) with consensus_min_books=2, allow 2 total.
        required_total = max(2, min_books + 1)
        if bookmaker_count < required_total:
            return 0, 0, []

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

        # Per-book snapshot_quality: 'pre_commence' | 'closing_fallback' |
        # 'closing_mode'. Gets embedded into model_factors on every event row
        # so the promotion gate can aggregate on the hypothesis level without
        # re-fetching the upstream snapshot. Defaults to 'pre_commence' for
        # books that don't emit it (legacy / synthetic test data) — the
        # promotion gate only rejects when the sample is >=20% fallback.
        book_snapshot_quality: dict[str, str] = {}
        for bm in bookmakers:
            bk_key = bm.get("key", "").lower()
            book_snapshot_quality[bk_key] = bm.get("snapshot_quality", "pre_commence")

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

            # Need at least min_books devigged books for a reliable consensus.
            # Hardcoded 3 blocked paper trading + thin markets (NCAAW, NHL)
            # where only 2-3 books carry the line.
            required_devigged = max(2, min_books)
            if len(all_fair_a) < required_devigged:
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
                                # Lookahead provenance — per-row so the
                                # promotion gate can count fallbacks vs
                                # pre-commence without joining upstream.
                                "snapshot_quality": book_snapshot_quality.get(
                                    eval_target, "pre_commence"
                                ),
                            }),
                            round(edge, 6), round(ev, 6), round(kelly, 6),
                            is_signal, game_date, snapshot_time,
                    ))

        # Return pending rows to caller for batch INSERT at end of backtest.
        # Per-game commits caused 274× write lock contention with line_monitor.
        return events, signals, _pending_rows

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
            import random as _rnd
            for _attempt in range(5):
                try:
                    await self._db.executemany(
                        "INSERT OR IGNORE INTO backtest_events "
                        "(run_id, event_id, hypothesis_id, sport, player, market, "
                        "line, side, book, book_odds_american, book_implied_prob, "
                        "model_fair_prob, model_factors, edge, ev_pct, kelly_fraction, "
                        "signal_generated, game_date, snapshot_time) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        _pending_rows,
                    )
                    break
                except Exception as _e:
                    if "locked" in str(_e).lower() and _attempt < 4:
                        _wait = min(0.5 * (2 ** _attempt), 8) + _rnd.uniform(0, 0.5)
                        logger.warning(f"DB locked on backtest props executemany (attempt {_attempt+1}/5), retrying in {_wait:.1f}s")
                        await asyncio.sleep(_wait)
                    else:
                        raise
            from tools.db_utils import commit_with_retry
            await commit_with_retry(self._db, operation="backtest props_batch_insert")
        return events, signals

    async def _process_prop_snapshots(
        self,
        run_id: str,
        hypothesis_id: str,
        prop_lines: list[dict],
        target_book: str,
        edge_threshold: float,
        devig_method: str,
        config: dict,
        h_sport: str,
        filters: dict = None,
    ) -> tuple[int, int]:
        """Process prop_snapshots data for player prop backtesting.

        Each prop_line is a dict with multi-book data for one player/market/line.
        We devig the non-target books to get fair probability, then compute
        edge vs target book.

        Returns (total_events, total_signals).
        """
        from tools.math_utils import american_to_implied, american_to_decimal
        from tools.devig import devig_american
        from tools.ev import ev_binary
        from tools.sizing import kelly_binary

        events = 0
        signals = 0
        _pending_rows = []

        # Relaxed book requirement for props — prop markets are thinner
        MIN_BOOKS_FOR_PROP_SIGNAL = 2

        for prop in prop_lines:
            player = prop["player"]
            market = prop["market"]
            line = prop["line"]
            event_id = prop["event_id"]
            game_date = prop["game_date"]
            books_data = prop["books"]

            # Side filter from hypothesis
            side_filter = None
            if filters and "side_filter" in filters:
                side_filter = filters["side_filter"].lower()

            # Group books by side
            over_books = [b for b in books_data if b["side"].lower() == "over"]
            under_books = [b for b in books_data if b["side"].lower() == "under"]

            # Need at least Over + Under from different books for devig
            if not over_books or not under_books:
                continue

            # Find target book entries
            target_over = [b for b in over_books if b["book"].lower() == target_book]
            target_under = [b for b in under_books if b["book"].lower() == target_book]

            # Non-target books for consensus
            non_target_over = [b for b in over_books if b["book"].lower() != target_book]
            non_target_under = [b for b in under_books if b["book"].lower() != target_book]
            non_target_count = len(set(b["book"] for b in non_target_over + non_target_under))

            # Skip if no target book data
            if not target_over and not target_under:
                continue

            # Devig non-target books for fair probability
            fair_overs = []
            for bo in non_target_over:
                # Find matching under from same book
                matching_under = [bu for bu in non_target_under if bu["book"] == bo["book"]]
                if matching_under:
                    try:
                        result = devig_american(bo["price_american"], matching_under[0]["price_american"])
                        fair_overs.append((result["fair_prob_1"], bo["book"]))
                    except Exception:
                        continue

            if not fair_overs:
                continue

            consensus_over = sum(f for f, _ in fair_overs) / len(fair_overs)
            consensus_under = 1.0 - consensus_over

            for side, consensus_fair, target_entries in [
                ("Over", consensus_over, target_over),
                ("Under", consensus_under, target_under),
            ]:
                if side_filter and side.lower() != side_filter:
                    continue
                if not target_entries:
                    continue

                target_price = target_entries[0]["price_american"]
                target_implied = american_to_implied(target_price)
                edge = consensus_fair - target_implied
                ev = ev_binary(consensus_fair, american_to_decimal(target_price))
                kelly = kelly_binary(consensus_fair, american_to_decimal(target_price))

                # Hard cap on edge magnitude
                MAX_EDGE_MAGNITUDE = 0.15
                if abs(edge) > MAX_EDGE_MAGNITUDE:
                    edge = MAX_EDGE_MAGNITUDE if edge > 0 else -MAX_EDGE_MAGNITUDE

                is_signal = (edge >= edge_threshold
                             and non_target_count >= MIN_BOOKS_FOR_PROP_SIGNAL)

                events += 1
                if is_signal:
                    signals += 1

                _pending_rows.append((
                    run_id, event_id, hypothesis_id, h_sport,
                    player, market, line, side, target_book,
                    target_price, round(target_implied, 6),
                    round(consensus_fair, 6),
                    json.dumps({
                        "edge_method": "consensus_devig",
                        "books_used": non_target_count,
                        "devig_method": devig_method,
                        "target_book": target_book,
                        "consensus_fair_prob": round(consensus_fair, 6),
                        "contributing_books": [bk for _, bk in fair_overs],
                        "data_source": "prop_snapshots",
                    }),
                    round(edge, 6), round(ev, 6), round(kelly, 6),
                    is_signal, game_date, game_date,
                ))

        # Batch INSERT
        if _pending_rows:
            import random as _rnd
            for _attempt in range(5):
                try:
                    await self._db.executemany(
                        "INSERT OR IGNORE INTO backtest_events "
                        "(run_id, event_id, hypothesis_id, sport, player, market, "
                        "line, side, book, book_odds_american, book_implied_prob, "
                        "model_fair_prob, model_factors, edge, ev_pct, kelly_fraction, "
                        "signal_generated, game_date, snapshot_time) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        _pending_rows,
                    )
                    break
                except Exception as _e:
                    if "locked" in str(_e).lower() and _attempt < 4:
                        _wait = min(0.5 * (2 ** _attempt), 8) + _rnd.uniform(0, 0.5)
                        logger.warning(f"DB locked on backtest prop_snapshots executemany (attempt {_attempt+1}/5), retrying in {_wait:.1f}s")
                        await asyncio.sleep(_wait)
                    else:
                        raise
            from tools.db_utils import commit_with_retry
            await commit_with_retry(self._db, operation="backtest prop_snapshots_batch_insert")
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
            # Safety LIMIT: never load the entire table (38K+ rows).
            # Callers should pass sport or run_id for targeted resolution.
            cursor = await self._db.execute(
                "SELECT id, event_id, sport, market, side, line, game_date, model_factors "
                "FROM backtest_events WHERE actual_result IS NULL "
                "ORDER BY game_date DESC LIMIT 500",
            )

        unresolved = await cursor.fetchall()
        if not unresolved:
            return {"resolved": 0, "unresolved": 0}

        # Build a lookup of game results indexed by (sport, date) -> list of games
        # Primary: game_results table. Fallback: game_contexts table (has scores too).
        # MEMORY FIX: Only load game_results for the date range of unresolved events
        # (±1 day for timezone offsets). Previously loaded ALL rows (14K+), causing
        # ~50 MB allocation per call that CPython's pymalloc never returns to OS.
        from collections import defaultdict
        games_by_date = defaultdict(list)
        seen = set()

        unresolved_dates = [row[6] for row in unresolved if row[6]]  # game_date col
        if unresolved_dates:
            min_date = min(unresolved_dates)
            max_date = max(unresolved_dates)
        else:
            min_date = max_date = "2020-01-01"

        result_cursor = await self._db.execute(
            "SELECT sport, game_date, home_team, away_team, home_score, away_score "
            "FROM game_results WHERE game_date >= date(?, '-3 day') AND game_date <= date(?, '+3 day')",
            (min_date, max_date),
        )
        result_rows = await result_cursor.fetchall()
        for r_sport, r_date, r_home, r_away, r_hscore, r_ascore in result_rows:
            key = (r_sport, r_date, r_home, r_away)
            seen.add(key)
            games_by_date[(r_sport, r_date)].append((r_home, r_away, r_hscore, r_ascore))

        # Track unique dates for sport-agnostic fallback lookups
        _dates_with_games: set = set()
        for r_sport, r_date, *_ in result_rows:
            _dates_with_games.add(r_date)

        # Fallback: game_contexts also stores scores from ESPN
        ctx_cursor = await self._db.execute(
            "SELECT sport, game_date, home_team, away_team, home_score, away_score "
            "FROM game_contexts WHERE home_score IS NOT NULL AND away_score IS NOT NULL "
            "AND game_date >= date(?, '-3 day') AND game_date <= date(?, '+3 day')",
            (min_date, max_date),
        )
        ctx_rows = await ctx_cursor.fetchall()
        ctx_added = 0
        for r_sport, r_date, r_home, r_away, r_hscore, r_ascore in ctx_rows:
            key = (r_sport, r_date, r_home, r_away)
            if key not in seen:
                seen.add(key)
                games_by_date[(r_sport, r_date)].append((r_home, r_away, r_hscore, r_ascore))
                _dates_with_games.add(r_date)
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

            # Exact-date match only.
            #
            # Pre-fix this was ±1 day to compensate for the game_date vs
            # UTC-sliced commence_time timezone mismatch (see
            # tools/game_dates.py and migration 007). With local_game_date
            # now canonical across tables, the fuzzy window would just
            # occasionally match bets to the wrong adjacent-day game.
            scores = None
            date_candidates = [game_date]

            for try_date in date_candidates:
                candidates = games_by_date.get((ev_sport, try_date), [])
                # REMOVED: sport-agnostic fallback. A Cardinals MLB bet was
                # matching against NBA/NHL games on the same date, producing
                # random win/loss attribution. Only match within the same sport.

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

            # Update fingerprint cache so recalculate_all_active_runs() in Phase 5
            # sees these runs as already-current and skips the expensive re-recalc.
            # Without this, every resolution batch triggers double-recalculation:
            # once here, once in Phase 5 when it detects stale fingerprints.
            if affected_runs:
                fp_ph = ",".join("?" for _ in affected_runs)
                fp_cursor = await self._db.execute(
                    f"SELECT run_id, COUNT(*), "
                    f"SUM(CASE WHEN signal_generated = 1 THEN 1 ELSE 0 END), "
                    f"SUM(CASE WHEN actual_result IS NOT NULL THEN 1 ELSE 0 END) "
                    f"FROM backtest_events WHERE run_id IN ({fp_ph}) GROUP BY run_id",
                    affected_runs,
                )
                for row in await fp_cursor.fetchall():
                    self._run_fingerprints[row[0]] = (row[1] or 0, row[2] or 0, row[3] or 0)

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
        """Recalculate ALL run stats from backtest_events.

        Updates signals_generated, total_events, win/loss/hit_rate, and edge
        metrics. This is critical because retroactive signal updates and game
        result resolution change backtest_events AFTER the run completes.

        Signal metrics (signals_count, wins, losses, pushes, hit_rate, avg_edge,
        p-value, Sharpe, Brier, IC) are deduplicated per unique event_id, keeping
        only the best-edge row per event. This matches the deduplication in
        _get_backtest_signals / evaluate_significance. total_events still counts
        all book-level rows.
        """
        # Recount total events and signals from backtest_events (source of truth)
        # total_events = all rows (book-level); signals_count = unique event_ids with signal
        # raw_signals = all signal rows before dedup (for debugging inflation)
        cursor = await self._db.execute(
            "SELECT COUNT(*), "
            "COUNT(DISTINCT CASE WHEN signal_generated = 1 THEN event_id END), "
            "SUM(CASE WHEN signal_generated = 1 THEN 1 ELSE 0 END) "
            "FROM backtest_events WHERE run_id = ?",
            (run_id,),
        )
        row = await cursor.fetchone()
        total_events = row[0] or 0
        signals_count = row[1] or 0
        raw_signals = row[2] or 0

        # Get win/loss from SIGNAL events only — deduplicated by event_id (best edge)
        cursor = await self._db.execute(
            "WITH unique_signals AS ("
            "  SELECT event_id, actual_result, "
            "    ROW_NUMBER() OVER (PARTITION BY event_id ORDER BY edge DESC) as rn "
            "  FROM backtest_events "
            "  WHERE run_id = ? AND signal_generated = 1 AND actual_result IS NOT NULL"
            ") "
            "SELECT actual_result, COUNT(*) FROM unique_signals "
            "WHERE rn = 1 GROUP BY actual_result",
            (run_id,),
        )
        results = {r[0]: r[1] for r in await cursor.fetchall()}

        wins = results.get("won", 0)
        losses = results.get("lost", 0)
        pushes = results.get("push", 0)
        total_decided = wins + losses

        if total_decided == 0 and signals_count == 0:
            return False  # Nothing to update

        # Count unresolved — unique signal events with NULL result
        cursor = await self._db.execute(
            "SELECT COUNT(DISTINCT event_id) FROM backtest_events "
            "WHERE run_id = ? AND signal_generated = 1 AND actual_result IS NULL",
            (run_id,),
        )
        unresolved = (await cursor.fetchone())[0]

        hit_rate = wins / total_decided if total_decided > 0 else None

        # Calculate avg_edge, avg_ev from signal-generated events — deduplicated
        cursor = await self._db.execute(
            "WITH unique_signals AS ("
            "  SELECT event_id, edge, ev_pct, clv_implied, "
            "    ROW_NUMBER() OVER (PARTITION BY event_id ORDER BY edge DESC) as rn "
            "  FROM backtest_events "
            "  WHERE run_id = ? AND signal_generated = 1 AND actual_result IS NOT NULL"
            ") "
            "SELECT AVG(edge), AVG(ev_pct), AVG(clv_implied) "
            "FROM unique_signals WHERE rn = 1",
            (run_id,),
        )
        row = await cursor.fetchone()
        avg_edge = row[0]
        avg_ev = row[1]
        avg_clv = row[2]

        # ── Recalculate statistical metrics (p-value, Brier, IC, Sharpe, ROI) ──
        # These were previously left stale, blocking promotion even when hit rates improved.
        p_binomial = 1.0
        p_ttest = 1.0
        z_score = 0.0
        sharpe = 0.0
        sortino = None
        brier = None
        ic = None
        roi_pct = 0.0

        if total_decided > 0:
            from scipy.stats import binomtest, ttest_1samp
            import numpy as np

            # Compute expected win rate from avg book implied probability
            # (NOT 0.5 coin-flip — a 5W-0L record on -300 favorites is NOT
            # as impressive as 5W-0L on coin-flips. The null hypothesis must
            # match the market's expected rate.)
            cursor = await self._db.execute(
                "WITH unique_signals AS ("
                "  SELECT event_id, book_implied_prob, "
                "    ROW_NUMBER() OVER (PARTITION BY event_id ORDER BY edge DESC) as rn "
                "  FROM backtest_events "
                "  WHERE run_id = ? AND signal_generated = 1 "
                "  AND actual_result IN ('won', 'lost') "
                "  AND book_implied_prob IS NOT NULL AND book_implied_prob > 0"
                ") "
                "SELECT AVG(book_implied_prob) FROM unique_signals WHERE rn = 1",
                (run_id,),
            )
            row_imp = await cursor.fetchone()
            expected_rate = row_imp[0] if row_imp and row_imp[0] else 0.5

            result = binomtest(wins, total_decided, expected_rate, alternative="greater")
            p_binomial = result.pvalue

            # Get per-signal returns for t-test, Sharpe, Sortino, ROI — deduplicated
            cursor = await self._db.execute(
                "WITH unique_signals AS ("
                "  SELECT event_id, book_odds_american, actual_result, model_fair_prob, edge, "
                "    ROW_NUMBER() OVER (PARTITION BY event_id ORDER BY edge DESC) as rn "
                "  FROM backtest_events "
                "  WHERE run_id = ? AND signal_generated = 1 AND actual_result IN ('won', 'lost')"
                ") "
                "SELECT book_odds_american, actual_result, model_fair_prob, edge "
                "FROM unique_signals WHERE rn = 1",
                (run_id,),
            )
            signal_events = await cursor.fetchall()

            returns = []
            brier_preds = []
            brier_outcomes = []
            predicted_edges = []
            realized_edges = []

            for odds_am, result_str, fair_prob, edge_val in signal_events:
                if result_str == "won" and odds_am:
                    try:
                        from tools.math_utils import american_to_decimal
                        dec = american_to_decimal(odds_am)
                        returns.append(dec - 1.0)
                        if edge_val is not None:
                            predicted_edges.append(edge_val)
                            realized_edges.append(dec - 1.0)
                    except Exception:
                        returns.append(1.0)
                elif result_str == "lost":
                    returns.append(-1.0)
                    if edge_val is not None:
                        predicted_edges.append(edge_val)
                        realized_edges.append(-1.0)

                if fair_prob is not None:
                    brier_preds.append(fair_prob)
                    brier_outcomes.append(1 if result_str == "won" else 0)

            if len(returns) >= 2:
                arr = np.array(returns)
                t_stat, p_val = ttest_1samp(arr, 0)
                p_ttest = p_val / 2 if t_stat > 0 else 1 - p_val / 2
                z_score = t_stat
                sharpe = float(arr.mean() / arr.std()) if arr.std() > 0 else 0.0
                neg = arr[arr < 0]
                if len(neg) > 0 and neg.std() > 0:
                    sortino = float(arr.mean() / neg.std())

            if returns:
                roi_pct = sum(returns) / len(returns) * 100

            # Brier score
            if len(brier_preds) >= 2:
                bp = np.array(brier_preds)
                bo = np.array(brier_outcomes)
                brier = float(np.mean((bp - bo) ** 2))

            # Information coefficient (Pearson correlation)
            if len(predicted_edges) >= 3:
                pe = np.array(predicted_edges)
                re = np.array(realized_edges)
                if pe.std() > 0 and re.std() > 0:
                    ic = float(np.corrcoef(pe, re)[0, 1])

        from tools.db_utils import execute_with_retry, commit_with_retry
        await execute_with_retry(
            self._db,
            "UPDATE backtest_runs SET "
            "total_events = ?, signals_generated = ?, "
            "actual_win = ?, actual_loss = ?, actual_push = ?, unresolved = ?, "
            "hit_rate = ?, avg_edge = ?, avg_ev = ?, avg_clv = ?, "
            "p_value_binomial = ?, p_value_ttest = ?, z_score = ?, "
            "sharpe_ratio = ?, sortino_ratio_val = ?, "
            "brier_score = ?, information_coefficient = ?, roi_pct = ? "
            "WHERE run_id = ?",
            (total_events, signals_count, wins, losses, pushes, unresolved,
             hit_rate, avg_edge, avg_ev, avg_clv,
             p_binomial, p_ttest, z_score,
             sharpe, sortino,
             brier, ic, roi_pct,
             run_id),
            operation="backtest recalculate_run_stats",
        )
        await commit_with_retry(self._db, operation="backtest recalculate_run_stats")
        logger.info(
            f"Run {run_id}: recalculated — {wins}W/{losses}L/{pushes}P "
            f"({unresolved} unresolved), signals={signals_count} unique/{raw_signals} raw, "
            f"hr={hit_rate:.3f}, p={p_binomial:.4f}, "
            f"brier={f'{brier:.3f}' if brier is not None else 'N/A'}, ic={f'{ic:.3f}' if ic is not None else 'N/A'}"
            if hit_rate else
            f"Run {run_id}: recalculated — {wins}W/{losses}L/{pushes}P "
            f"({unresolved} unresolved), signals={signals_count} unique/{raw_signals} raw"
        )
        return True

    async def recalculate_all_active_runs(self, hypothesis_ids: list[str] | None = None) -> int:
        """Recompute stats for runs belonging to active (backtesting) hypotheses.

        This fixes the stale backtest_runs problem: when retroactive signal updates
        or game result resolution change backtest_events AFTER the original run,
        backtest_runs stats become outdated. The promotion gate checks these stats,
        so stale data blocks promotion of winning hypotheses.

        Uses a lightweight fingerprint cache to skip runs whose underlying
        backtest_events haven't changed since the last recalculation.  The
        fingerprint is (total_events, signals_count, resolved_count) per run —
        a single cheap aggregate query that catches new events, retroactive
        signal_generated flips, AND game result resolution updates.  Only runs
        with a changed fingerprint get the expensive scipy/numpy recompute.
        This cuts the typical 10-15 min stall to seconds.

        Args:
            hypothesis_ids: If provided, only recalculate runs for these hypotheses.
                           If None, recalculates ALL active runs (legacy behavior).

        Returns number of runs updated.
        """
        if hypothesis_ids:
            placeholders = ",".join("?" for _ in hypothesis_ids)
            cursor = await self._db.execute(
                f"SELECT DISTINCT run_id FROM backtest_runs "
                f"WHERE hypothesis_id IN ({placeholders})",
                hypothesis_ids,
            )
        else:
            cursor = await self._db.execute(
                "SELECT DISTINCT br.run_id FROM backtest_runs br "
                "JOIN hypotheses h ON br.hypothesis_id = h.hypothesis_id "
                "WHERE h.status = 'backtesting'"
            )
        run_ids = [row[0] for row in await cursor.fetchall()]

        if not run_ids:
            return 0

        # ── Staleness check: compute lightweight fingerprints ──
        # One query for ALL candidate runs — far cheaper than per-run recalculate_run_stats
        # which does 4 queries + scipy/numpy each.
        fp_placeholders = ",".join("?" for _ in run_ids)
        cursor = await self._db.execute(
            f"SELECT run_id, "
            f"  COUNT(*), "
            f"  SUM(CASE WHEN signal_generated = 1 THEN 1 ELSE 0 END), "
            f"  SUM(CASE WHEN actual_result IS NOT NULL THEN 1 ELSE 0 END) "
            f"FROM backtest_events "
            f"WHERE run_id IN ({fp_placeholders}) "
            f"GROUP BY run_id",
            run_ids,
        )
        current_fps: dict[str, tuple[int, int, int]] = {}
        for row in await cursor.fetchall():
            current_fps[row[0]] = (row[1] or 0, row[2] or 0, row[3] or 0)

        # Determine which runs actually need recalculation
        stale_run_ids = []
        for run_id in run_ids:
            fp = current_fps.get(run_id, (0, 0, 0))
            cached_fp = self._run_fingerprints.get(run_id)
            if fp != cached_fp:
                stale_run_ids.append(run_id)

        if not stale_run_ids:
            logger.debug(
                f"Staleness check: all {len(run_ids)} runs unchanged, skipping recalculation"
            )
            return 0

        logger.info(
            f"Staleness check: {len(stale_run_ids)}/{len(run_ids)} runs have new data, recalculating"
        )

        updated = 0
        for run_id in stale_run_ids:
            try:
                if await self.recalculate_run_stats(run_id):
                    updated += 1
                # Update cache AFTER successful recalculation
                if run_id in current_fps:
                    self._run_fingerprints[run_id] = current_fps[run_id]
            except Exception as e:
                logger.warning(f"Failed to recalculate run {run_id}: {e}")

        # Prune fingerprint cache — only keep entries for currently active runs
        if len(self._run_fingerprints) > self._RUN_FP_MAX:
            active_set = set(run_ids)
            pruned = {k: v for k, v in self._run_fingerprints.items() if k in active_set}
            self._run_fingerprints = pruned

        if updated:
            logger.info(f"Recalculated stats for {updated}/{len(stale_run_ids)} stale backtest runs (skipped {len(run_ids) - len(stale_run_ids)} unchanged)")
        return updated

    async def generate_paper_trade_signal(
        self,
        hypothesis_id: str,
        live_odds: dict,
    ) -> list[dict]:
        """
        For paper trading: apply model to current live odds.
        Returns signals meeting threshold. Does NOT place bets.

        HARD GATE: this method may ONLY run for hypotheses whose status is
        exactly ``"paper_trading"``. Any other status — including ``"live"``
        (or any future status) — returns ``[]`` immediately, before any odds
        processing. Accepting ``"live"`` here is FORBIDDEN: that would arm
        untested sizing/caps/kill-switch logic all at once. Live order flow
        must go through a separately reviewed and tested path.
        """
        h = await self.hypothesis_manager.get_hypothesis(hypothesis_id)
        if not h or reject_non_paper(h["status"]):
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

        def _game_date_from_commence(game_obj: dict) -> str:
            """Thin wrapper over the canonical helper in tools.signals.schedule."""
            return game_date_from_commence(game_obj, sport=sport, today=today)

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
            # Pass today's live games so context is computed for upcoming
            # games not yet in game_results (the previous code only had
            # context for completed games → today's games always got {} →
            # fail-closed filter rejected them all → 0 paper trades).
            live_game_tuples = [
                (today, g.get("home_team", ""), g.get("away_team", ""))
                for g in games
                if g.get("home_team") and g.get("away_team")
            ]
            schedule_context = await self._build_schedule_context(
                sport, context_start, today,
                live_games=live_game_tuples,
            )
            if schedule_context:
                logger.info(
                    f"Paper trade {hypothesis_id}: context filter ENABLED — "
                    f"{len(schedule_context)} games have schedule context"
                )
            else:
                logger.warning(
                    f"Paper trade {hypothesis_id}: context filter ENABLED but "
                    f"schedule_context is EMPTY — falling through WITHOUT context filter"
                )
                use_context_filter = False  # fail-open: proceed without context gating

        all_paper_rows: list[tuple] = []
        # Map event_id → (home_team, away_team, game_date) for paper trade insertion
        _paper_game_info: dict[str, tuple[str, str, str]] = {}
        total_events = 0
        total_signals_found = 0
        games_processed = 0
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

            games_processed += 1
            # Derive actual game date from commence_time (not signal date)
            actual_game_date = _game_date_from_commence(game)
            g_home = game.get("home_team", "")
            g_away = game.get("away_team", "")
            g_eid = game.get("id", "")
            if g_eid:
                _paper_game_info[g_eid] = (g_home, g_away, actual_game_date)

            # Use same processing logic as backtest
            if h["market_type"].startswith("player_"):
                events, _ = await self._process_game_props(
                    run_id="paper",  # won't be stored via run
                    hypothesis_id=hypothesis_id,
                    game=game,
                    game_date=actual_game_date,
                    snapshot_time=now,
                    market_type=h["market_type"],
                    target_book=target_book,
                    edge_threshold=edge_threshold,
                    devig_method=devig_method,
                    min_books=min_books,
                    config=config,
                    filters=filters,
                )
                total_events += events
            else:
                events, sigs, _paper_rows = await self._process_game_lines(
                    run_id="paper",
                    hypothesis_id=hypothesis_id,
                    game=game,
                    game_date=actual_game_date,
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
                total_events += events
                total_signals_found += sigs
                all_paper_rows.extend(_paper_rows)

        # Edge distribution diagnostic — shows why 0-signal cycles happen
        # Tuple layout: (run_id[0], event_id[1], hyp_id[2], sport[3], player[4],
        #   market[5], line[6], side[7], book[8], odds_american[9], implied[10],
        #   fair_prob[11], model_factors_json[12], edge[13], ev_pct[14],
        #   kelly[15], signal_generated[16], game_date[17], snapshot_time[18])
        if all_paper_rows:
            edges = [row[13] for row in all_paper_rows]
            max_edge = max(edges) if edges else 0
            min_edge = min(edges) if edges else 0
            above_thresh = sum(1 for e in edges if e >= edge_threshold)
            import json as _json
            books_counts = []
            for row in all_paper_rows:
                try:
                    factors = _json.loads(row[12]) if row[12] else {}
                    books_counts.append(factors.get("books_used", 0))
                except Exception:
                    pass
            min_books_seen = min(books_counts) if books_counts else 0
            max_books_seen = max(books_counts) if books_counts else 0
        else:
            max_edge = min_edge = 0
            above_thresh = 0
            min_books_seen = max_books_seen = 0

        # Diagnose WHY above_thresh > 0 but signals = 0 (prevents false "broken" alarms)
        suppression_reasons = []
        if above_thresh > 0 and total_signals_found == 0 and all_paper_rows:
            for row in all_paper_rows:
                edge_val = row[13]
                if edge_val < edge_threshold:
                    continue
                fair_prob = row[11]
                try:
                    factors = _json.loads(row[12]) if row[12] else {}
                except Exception:
                    factors = {}
                n_books = factors.get("books_used", 0)
                if h["market_type"] == "h2h" and fair_prob > 0.80:
                    suppression_reasons.append(
                        f"heavy_fav(fair={fair_prob:.3f},edge={edge_val:.4f},book={row[8]})"
                    )
                elif n_books < 4:
                    suppression_reasons.append(
                        f"min_books(n={n_books},edge={edge_val:.4f},book={row[8]})"
                    )

        logger.info(
            f"Paper trade {hypothesis_id[:12]}: {games_processed}/{len(games)} games processed, "
            f"{total_events} events, {total_signals_found} signals, "
            f"{len(all_paper_rows)} pending rows, "
            f"market={h['market_type']}, filters={filters}, threshold={edge_threshold}, "
            f"edge_range=[{min_edge:.4f}, {max_edge:.4f}], above_thresh={above_thresh}, "
            f"books_range=[{min_books_seen}, {max_books_seen}]"
        )
        if suppression_reasons:
            logger.info(
                f"Paper trade {hypothesis_id[:12]}: {above_thresh} edge(s) above threshold "
                f"SUPPRESSED — {'; '.join(suppression_reasons)}"
            )

        # Batch-insert paper events so the SELECT below can find signals.
        # _process_game_lines returns pending rows (deferred write pattern)
        # but never inserts them — the caller must do it.
        if all_paper_rows:
            await self._db.executemany(
                "INSERT OR IGNORE INTO backtest_events "
                "(run_id, event_id, hypothesis_id, sport, player, market, "
                "line, side, book, book_odds_american, book_implied_prob, "
                "model_fair_prob, model_factors, edge, ev_pct, kelly_fraction, "
                "signal_generated, game_date, snapshot_time) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                all_paper_rows,
            )
            await self._db.commit()

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

        # ── Game-level dedup: keep only best-edge book per game ──
        # Multiple books show edge for the same game. Recording all of them
        # inflates paper_trade counts 5x. Keep only the highest-edge entry
        # per event_id so paper_trades reflects independent betting opportunities.
        best_by_game: dict[str, dict] = {}
        for row in rows:
            event = dict(zip(cols, row))
            eid = event.get("event_id", "")
            existing = best_by_game.get(eid)
            if existing is None or (event.get("edge") or 0) > (existing.get("edge") or 0):
                best_by_game[eid] = event
        deduped_events = list(best_by_game.values())
        multi_book_skipped = len(rows) - len(deduped_events)
        if multi_book_skipped:
            logger.info(
                f"Paper trade {hypothesis_id[:12]}: kept {len(deduped_events)} "
                f"best-edge trades, skipped {multi_book_skipped} multi-book duplicates"
            )

        dupes_skipped = 0
        for event in deduped_events:

            # Look up game info (home_team, away_team, actual game_date)
            eid = event.get("event_id", "")
            gi = _paper_game_info.get(eid, ("", "", event.get("game_date", today)))
            home_team, away_team, actual_gd = gi

            # ── Dedup: skip if we already recorded this game for this hypothesis ──
            dup_cur = await self._db.execute(
                "SELECT 1 FROM paper_trades "
                "WHERE hypothesis_id = ? AND game_date = ? AND home_team = ? AND away_team = ?",
                (hypothesis_id, actual_gd, home_team, away_team),
            )
            if await dup_cur.fetchone():
                dupes_skipped += 1
                continue

            trade_id = str(uuid.uuid4())[:12]

            # Move to paper_trades table. ``actual_gd`` is already the
            # venue-local date (see _game_date_from_commence above) — write
            # it to BOTH game_date (legacy) and local_game_date (canonical)
            # so new rows don't need a backfill.
            await self._db.execute(
                "INSERT OR IGNORE INTO paper_trades "
                "(trade_id, hypothesis_id, event_id, sport, player, market, "
                "line, side, book, signal_time, signal_odds_american, "
                "signal_implied_prob, model_fair_prob, edge, ev_pct, "
                "kelly_fraction, game_date, local_game_date, "
                "home_team, away_team) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    trade_id, hypothesis_id, eid,
                    event["sport"], event.get("player"), event["market"],
                    event.get("line"), event["side"], event["book"],
                    now, event["book_odds_american"],
                    event["book_implied_prob"], event["model_fair_prob"],
                    event["edge"], event["ev_pct"],
                    event.get("kelly_fraction"), actual_gd, actual_gd,
                    home_team, away_team,
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

        if dupes_skipped:
            logger.info(
                f"Paper trade {hypothesis_id[:12]}: skipped {dupes_skipped} "
                f"duplicate trades (already recorded)"
            )

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
