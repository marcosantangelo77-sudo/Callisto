"""Engine delegators extracted from tools/backtest.py (slice 6).

The remaining thin wrappers on BacktestEngine — run entry point, snapshot
enrichment, per-game processing, resolution, run-stat recalculation, and the
paper-trade signal entry — are grouped here as mixin classes. BacktestEngine
inherits them, so method names, signatures, docstrings, and call sites are
unchanged.

HARD GATE: PaperSignalMixin.generate_paper_trade_signal keeps the
``reject_non_paper(h["status"])`` check BEFORE delegating to
tools.btest.paper_pipeline, and ``_PAPER_TRADE_SIGNAL_STATUSES`` stays
frozenset({"paper_trading"}) in tools/signals/paper.py. Never add "live".
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from tools.btest import (
    game_line_processing,
    prop_processing,
    run_orchestration,
    run_pipeline,
)
from tools.btest.snapshots import enrich_snapshot_with_multibook

logger = logging.getLogger("callisto.backtest")

if TYPE_CHECKING:  # pragma: no cover
    from tools.backtest import BacktestEngine

    _E = BacktestEngine
else:
    _E = object


class RunPipelineMixin(_E):
    """run_backtest + snapshot enrichment + signal population."""

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


class GameProcessingMixin(_E):
    """Per-game line/prop processing delegators."""

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


class ResolutionMixin(_E):
    """Resolution + run-stat recalculation + results retrieval."""

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

    async def get_run_results(self, run_id: str) -> dict:
        """Retrieve full backtest results for a run."""
        return await run_orchestration.get_run_results(self._db, run_id)
