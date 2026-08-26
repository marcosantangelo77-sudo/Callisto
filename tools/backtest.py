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
from tools.btest import game_line_processing


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
    ) -> tuple[int, int, list]:
        """Process a single game — see tools.btest.game_line_processing.

        Thin async delegator; the market resolution + multi-book gating
        body lives in game_line_processing.process_game. Signature and
        return contract ((events, signals, pending_rows)) are unchanged.
        """
        return await game_line_processing.process_game(
            self, run_id, hypothesis_id, game, game_date, snapshot_time,
            market_type, target_book, edge_threshold, devig_method,
            min_books, config, h_sport=h_sport, thesis=thesis,
            filters=filters,
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
    ) -> tuple[int, int, list]:
        """Process spreads/totals/h2h lines for a game.

        Thin async delegator over
        tools.btest.game_line_processing.process_game_lines (cross-book
        edge detection). Signature unchanged.
        """
        return await game_line_processing.process_game_lines(
            self, run_id, hypothesis_id, game, game_date, snapshot_time,
            market_type, target_book, edge_threshold, devig_method,
            min_books, config, h_sport=h_sport, filters=filters,
        )

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
