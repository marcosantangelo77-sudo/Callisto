"""
Backtest engine — replay historical odds through a model and evaluate predictions.

This is the core of the scientific method applied to betting theses:
  1. Load hypothesis config (model params, factors, thresholds)
  2. Fetch historical odds for date range (cached after first fetch)
  3. For each event: run model, compare to book, record prediction
  4. Resolve outcomes against actual results
  5. Compute aggregate statistics and significance

Slice-4 diet: the full run pipeline (tools.btest.run_pipeline), player-prop
processing (tools.btest.prop_processing), and the paper-trade signal body
(tools.btest.paper_pipeline) now live in the tools.btest package.
BacktestEngine re-binds everything as thin delegators so call sites,
method names, and signatures are unchanged.
"""

import asyncio
import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

import aiosqlite
from dotenv import load_dotenv

from tools.historical_odds import HistoricalOddsFetcher
from tools.hypothesis import HypothesisManager
from tools.math_utils import american_to_decimal, american_to_implied
from tools.devig import devig_market, power_devig, multiplicative_devig  # noqa: F401 (re-exported facade)
from tools.ev import ev_binary, evaluate_edge  # noqa: F401 (re-exported facade)
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

# Slice-2 extracted helpers (tools/btest package)
from tools.btest.events_io import (
    dedup_best_edge_by_event,
    insert_pending_rows,
    new_trade_id,
    signal_confidence as _signal_confidence_impl,
)
from tools.btest.market_processing import (
    SHARP_BOOKS as _SHARP_BOOKS,
    build_event_row as _build_event_row,
    clean_outliers as _clean_outliers,
    collect_book_snapshot_quality as _collect_book_snapshot_quality,
    devig_pair as _devig_pair,
    effective_game_market as _effective_game_market,
    evaluate_side as _evaluate_side,
    group_sides as _group_sides,
    index_lines_by_key as _index_lines_by_key,
    index_props as _index_props,
)
from tools.btest.paper_diagnostics import (
    edge_distribution as _edge_distribution,
    suppression_reasons as _suppression_reasons,
)
from tools.btest.resolution import (
    build_results_index as _build_results_index,
    extract_home_away_teams as _extract_home_away_teams,
    find_scores_for_event as _find_scores_for_event,
    scores_from_odds_api_game as _scores_from_odds_api_game,
)
from tools.btest.run_stats import (
    compute_signal_metrics as _compute_signal_metrics,
    fingerprint_stale as _fingerprint_stale,
    prune_fingerprints as _prune_fingerprints,
)
from tools.btest.run_orchestration import (
    get_affected_run_ids as _get_affected_run_ids_impl,
    populate_signals_from_backtest as _populate_signals_impl,
)
from tools.btest import run_orchestration
from tools.btest.snapshots import enrich_snapshot_with_multibook
from tools.btest import paper_pipeline, prop_processing, run_pipeline


def _signal_confidence(edge: float) -> str:
    """Categorize edge into confidence tiers — see tools.btest.events_io.

    Real cross-book edges cap at ~2.5%. Thresholds reflect actual edge
    distribution: top-decile edges are ~2%+, median is ~1%.
    """
    return _signal_confidence_impl(edge)


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
        return await enrich_snapshot_with_multibook(
            self._db, sport, date_str, snapshot, target_book
        )

    async def run_backtest(
        self,
        hypothesis_id: str,
        start_date: str,
        end_date: str,
        credit_budget: int = 50,
    ) -> dict:
        """
        Full backtest pipeline for a hypothesis — see tools.btest.run_pipeline.

        The gate logic (spring-training skip, side_filter requirement,
        context-coverage untestable check, temporal isolation, duplicate
        fingerprint detection) and the deferred batch write all live in
        the extracted module; this method is the public entry point.
        """
        return await run_pipeline.run_backtest(
            self, hypothesis_id, start_date, end_date,
            credit_budget=credit_budget,
        )

    async def _populate_signals_from_backtest(
        self, run_id: str, hypothesis_id: str
    ) -> int:
        """Copy backtest events with signal_generated=1 into the signals table.

        Returns the number of signals inserted.
        """
        return await run_orchestration.populate_signals_from_backtest(
            self._db, run_id, hypothesis_id
        )

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
        effective_market = _effective_game_market(market_type, available_markets)
        if effective_market is None:
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
        book_snapshot_quality = _collect_book_snapshot_quality(bookmakers)
        lines_by_key = _index_lines_by_key(bookmakers, market_type)

        # For each unique line, find the opposite side and devig
        # Group by (market, point); spreads pair by abs(point) so
        # -7.5/+7.5 group correctly
        sides_by_line, signed_points = _group_sides(lines_by_key)

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
                    fa, fb = _devig_pair(price_a, price_b, devig_method)
                    all_fair_a[bk] = fa
                    all_fair_b[bk] = fb
                except (ValueError, ZeroDivisionError) as e:
                    logger.warning(
                        f"Devig failed for book={bk}, market={mkt_key}, "
                        f"prices=({price_a}, {price_b}): {e}"
                    )
                    continue

            # Need at least min_books devigged books for a reliable consensus.
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
            SHARP_BOOKS = _SHARP_BOOKS

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
                clean_a = _clean_outliers(others_a, consensus_a)
                clean_b = _clean_outliers(others_b, consensus_b)

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
                    verdict = _evaluate_side(
                        fair_val, target_price, edge_threshold,
                        non_target_count, mkt_key,
                    )
                    if verdict["skip"]:
                        continue

                    events += 1
                    if verdict["is_signal"]:
                        signals += 1

                    team = side_name
                    event_id = game.get("id") or f"{game_date}|{home}|{away}"
                    event_sport = game.get("sport_key") or h_sport

                    _pending_rows.append(_build_event_row(
                        run_id=run_id,
                        event_id=event_id,
                        hypothesis_id=hypothesis_id,
                        sport=event_sport,
                        player=None,
                        market=mkt_key,
                        line=side_signed_point,
                        side=side_name,
                        book=eval_target,
                        target_price=target_price,
                        target_implied=verdict["target_implied"],
                        fair_val=fair_val,
                        factors={
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
                        },
                        edge=verdict["edge"],
                        ev=verdict["ev"],
                        kelly=verdict["kelly"],
                        is_signal=verdict["is_signal"],
                        game_date=game_date,
                        snapshot_time=snapshot_time,
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
        Process player props for a game — see tools.btest.prop_processing.
        Thin async delegator over prop_processing.process_game_props.
        """
        return await prop_processing.process_game_props(
            self._db, run_id, hypothesis_id, game, game_date, snapshot_time,
            market_type, target_book, edge_threshold, devig_method,
            min_books, config, filters=filters,
        )

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
        filters: Optional[dict] = None,
    ) -> tuple[int, int]:
        """Process prop_snapshots data — see tools.btest.prop_processing."""
        return await prop_processing.process_prop_snapshots(
            self._db, run_id, hypothesis_id, prop_lines, target_book,
            edge_threshold, devig_method, config, h_sport, filters=filters,
        )

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
        return await run_orchestration.resolve_with_scores(
            self._db, run_id, sport
        )
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
        return await run_orchestration.resolve_from_game_results(
            self._db, run_id, sport,
            fingerprints_cache=self._run_fingerprints,
            recalc_fn=self.recalculate_run_stats,
        )
    async def _get_affected_run_ids(self, run_id: Optional[str] = None) -> list[str]:
        """Get run IDs that have resolved events but stale run-level stats."""
        return await run_orchestration.get_affected_run_ids(self._db, run_id)
    async def recalculate_run_stats(self, run_id: str) -> bool:
        """Recalculate ALL run stats from backtest_events.

        Updates signals_generated, total_events, win/loss/hit_rate, and edge
        metrics after retroactive signal updates or result resolution.
        See tools.btest.run_orchestration.
        """
        return await run_orchestration.recalculate_run_stats(self._db, run_id)
    async def recalculate_all_active_runs(self, hypothesis_ids: list[str] | None = None) -> int:
        """Recompute stats for runs belonging to active (backtesting) hypotheses.

        Uses a lightweight fingerprint cache to skip runs whose underlying
        backtest_events haven't changed since the last recalculation —
        cuts the typical 10-15 min stall to seconds. See
        tools.btest.run_orchestration.
        """
        return await run_orchestration.recalculate_all_active_runs(
            self._db,
            self._run_fingerprints,
            self._RUN_FP_MAX,
            self.recalculate_run_stats,
            hypothesis_ids=hypothesis_ids,
        )
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

        The signal-processing body lives in tools.btest.paper_pipeline;
        the status gate stays HERE so it always precedes any extraction.
        """
        h = await self.hypothesis_manager.get_hypothesis(hypothesis_id)
        if not h or reject_non_paper(h["status"]):
            return []
        return await paper_pipeline.generate_paper_trade_signal(
            self, hypothesis_id, live_odds
        )

    async def get_run_results(self, run_id: str) -> dict:
        """Retrieve full backtest results for a run."""
        return await run_orchestration.get_run_results(self._db, run_id)
