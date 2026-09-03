"""
tools.hypothesis.promote — auto_promote, live review, and thin query delegates.

Data-access helpers live in ``tools.hypothesis.promote_queries``; this mixin
keeps the original method names as one-line delegates (hasattr pins).

``review_live_hypotheses`` and ``auto_promote`` stay defined here as thin
delegates; bodies live in ``promote_review`` and ``promote_auto``.
auto_promote remains diagnose-only (no evidence rewrite).

Split out of tools/hypothesis.py (facade re-exports everything).

``check_promotion_readiness`` lives on ``HypothesisSignificanceMixin``
(MRO-winner). A duplicate copy here was unreachable and is gone.

auto_promote is DIAGNOSE-ONLY with respect to edge_threshold /
signal_generated: it may log a threshold diagnosis and hold, but never writes
to edge_threshold or signal_generated (source-pinned; see
tests/test_hyp_split.py).
"""
from __future__ import annotations

import json
import logging
import math
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from tools.bankroll_sim import simulate_before_promote  # pre-LIVE sim gate
from tools.hypothesis.config import (
    PROMOTION_GATES,
    AUTO_REJECT_P,
    AUTO_REJECT_MIN_N,
    AUTO_REJECT_STRONG_P,
    AUTO_REJECT_STRONG_MIN_N,
    AUTO_REJECT_EXTREME_P,
    AUTO_REJECT_EXTREME_MIN_N,
    AUTO_REJECT_IC,
    AUTO_REJECT_IC_MIN_N,
    AUTO_REJECT_IC_STRONG,
    AUTO_REJECT_IC_STRONG_MIN_N,
    AUTO_REJECT_LOW_SIGNAL_RATE,
    AUTO_REJECT_LOW_SIGNAL_MIN_EVENTS,
    MIN_DAYS_PAPER,
    MIN_PAPER_TRADES,
    MIN_CLV_RATE,
    MIN_CANONICAL_CLV_SAMPLE,
    MAX_LIVE_OVERLAP_PCT,
    PORTFOLIO_OVERLAP_WINDOW_DAYS,
    SIM_GATE_ENABLED,
    MAX_PRE_PROMOTE_RUIN,
    PRE_PROMOTE_N_SIMS,
    PRE_PROMOTE_HORIZON,
    STAGE_ORDER,
    get_adaptive_p_value_threshold,
)

logger = logging.getLogger("callisto.hypothesis")


class HypothesisPromotionMixin:

    async def _compute_portfolio_overlap(
        self,
        hypothesis_id: str,
        window_days: int | None = None,
    ) -> dict[str, float]:
        """Compute % of candidate's signals that fall on events where an
        existing LIVE hypothesis also fired.

        Returns: {live_hypothesis_id: overlap_pct, …}
        where overlap_pct = |candidate_events ∩ live_events| / |candidate_events|.
        """
        window_days = window_days or PORTFOLIO_OVERLAP_WINDOW_DAYS
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=window_days)
        ).strftime("%Y-%m-%d")

        # Candidate's distinct signal event_ids in window
        cand_cur = await self._db.execute(
            "SELECT DISTINCT event_id FROM backtest_events "
            "WHERE hypothesis_id = ? AND signal_generated = 1 "
            "AND game_date >= ?",
            (hypothesis_id, cutoff),
        )
        cand_events = {r[0] for r in await cand_cur.fetchall()}
        if not cand_events:
            return {}

        # Live hyps (excluding candidate)
        live_cur = await self._db.execute(
            "SELECT hypothesis_id FROM hypotheses "
            "WHERE status = 'live' AND hypothesis_id != ?",
            (hypothesis_id,),
        )
        live_ids = [r[0] for r in await live_cur.fetchall()]

        overlap_map: dict[str, float] = {}
        for live_id in live_ids:
            live_ev_cur = await self._db.execute(
                "SELECT DISTINCT event_id FROM backtest_events "
                "WHERE hypothesis_id = ? AND signal_generated = 1 "
                "AND game_date >= ?",
                (live_id, cutoff),
            )
            live_events = {r[0] for r in await live_ev_cur.fetchall()}
            if not live_events:
                continue
            shared = cand_events & live_events
            if shared:
                overlap_map[live_id] = len(shared) / len(cand_events)

        return overlap_map

    async def auto_promote(self, hypothesis_id: str) -> dict:
        """If criteria met, advance to next stage. Returns result.

        Hard gates (cannot be bypassed by statistical tests):
          - backtesting → paper_trading: backtest_events MUST exist for this hypothesis
            AND meet min_signals with adaptive p-value threshold (see PROMOTION_GATES)
          - paper_trading → live: ALL of the following are required (audit 2026-04-21):
              * ≥ min_paper_trades resolved paper trades
              * ≥ min_days since promoted_at
              * CLV positive-rate ≥ min_clv_rate
              * Backtest-only evidence is NO LONGER a valid path to LIVE.

        Auto-rejection:
          - If a hypothesis has been in 'backtesting' through 10+ evaluate cycles
            with 0 backtest_events, it is auto-rejected as untestable.
          - If 0 signals after 10 cycles but events exist, check if threshold is
            the issue before rejecting (edge distribution diagnostic).
        """
        from tools.hypothesis.promote_auto import auto_promote as _auto_promote
        return await _auto_promote(self, hypothesis_id)


    # ──────────────────────────────────────────────────────────────────
    # LIVE-STAGE REVIEW + DEMOTION (audit 2026-04-21)
    # ──────────────────────────────────────────────────────────────────

    async def review_live_hypotheses(
        self,
        *,
        window_days: Optional[int] = None,
        hit_rate_floor: float = 0.45,
        max_drawdown: float = 0.40,
        min_resolved: int = 10,
        clv_negative_threshold: float = 0.0,
        base_rate_relative: bool = True,
    ) -> list[dict]:
        """Review all LIVE hypotheses and demote underperformers to 'paused'."""
        from tools.hypothesis.promote_review import review_live_hypotheses as _review_live_hypotheses
        return await _review_live_hypotheses(
            self,
            window_days=window_days,
            hit_rate_floor=hit_rate_floor,
            max_drawdown=max_drawdown,
            min_resolved=min_resolved,
            clv_negative_threshold=clv_negative_threshold,
            base_rate_relative=base_rate_relative,
        )

    # ── DATA ACCESSORS (thin delegates; bodies in promote_queries) ──

    async def _get_backtest_signals(self, hypothesis_id: str) -> list[dict]:
        from tools.hypothesis.promote_queries import _get_backtest_signals as _impl
        return await _impl(self, hypothesis_id)

    async def _get_backtest_resolved(self, hypothesis_id: str) -> list[dict]:
        from tools.hypothesis.promote_queries import _get_backtest_resolved as _impl
        return await _impl(self, hypothesis_id)

    async def _diagnose_edge_threshold(self, hypothesis_id: str) -> dict:
        from tools.hypothesis.promote_queries import _diagnose_edge_threshold as _impl
        return await _impl(self, hypothesis_id)

    async def _get_best_run_stats(self, hypothesis_id: str) -> Optional[dict]:
        from tools.hypothesis.promote_queries import _get_best_run_stats as _impl
        return await _impl(self, hypothesis_id)

    async def _days_of_odds_data(self, hypothesis_id: str) -> Optional[int]:
        from tools.hypothesis.promote_queries import _days_of_odds_data as _impl
        return await _impl(self, hypothesis_id)

    async def _avg_books_used(self, hypothesis_id: str) -> Optional[float]:
        from tools.hypothesis.promote_queries import _avg_books_used as _impl
        return await _impl(self, hypothesis_id)

    async def _count_unresolved(self, hypothesis_id: str) -> int:
        from tools.hypothesis.promote_queries import _count_unresolved as _impl
        return await _impl(self, hypothesis_id)

    async def _get_paper_trades(self, hypothesis_id: str) -> list[dict]:
        from tools.hypothesis.promote_queries import _get_paper_trades as _impl
        return await _impl(self, hypothesis_id)

    async def _get_paper_trades_all(self, hypothesis_id: str) -> list[dict]:
        from tools.hypothesis.promote_queries import _get_paper_trades_all as _impl
        return await _impl(self, hypothesis_id)


    async def get_hypothesis_report(self, hypothesis_id: str) -> dict:
        """Full report across all stages."""
        h = await self.get_hypothesis(hypothesis_id)
        if not h:
            return {"error": "Hypothesis not found"}

        report = {"hypothesis": h, "stages": {}}

        # Backtest stats
        bt_cursor = await self._db.execute(
            "SELECT * FROM backtest_runs WHERE hypothesis_id = ? ORDER BY completed_at DESC LIMIT 5",
            (hypothesis_id,),
        )
        bt_rows = await bt_cursor.fetchall()
        if bt_rows:
            bt_cols = [d[0] for d in bt_cursor.description]
            report["stages"]["backtest"] = {
                "runs": [dict(zip(bt_cols, r)) for r in bt_rows],
            }

        # Latest significance per stage
        for stage in ["backtest", "paper_trade"]:
            stats_cursor = await self._db.execute(
                "SELECT * FROM hypothesis_stats "
                "WHERE hypothesis_id = ? AND stage = ? ORDER BY computed_at DESC LIMIT 1",
                (hypothesis_id, stage),
            )
            stats_row = await stats_cursor.fetchone()
            if stats_row:
                stats_cols = [d[0] for d in stats_cursor.description]
                report["stages"][f"{stage}_latest_stats"] = dict(zip(stats_cols, stats_row))

        # Readiness check
        report["promotion_readiness"] = await self.check_promotion_readiness(hypothesis_id)

        # Temporal metadata
        temporal = self.get_temporal_metadata(h)
        if temporal:
            report["temporal_metadata"] = temporal

        return report

    @staticmethod
    def get_temporal_metadata(hypothesis: dict) -> Optional[dict]:
        """Extract temporal split metadata from a hypothesis's model_config.

        Returns None if no temporal metadata exists (legacy hypothesis).
        """
        config = hypothesis.get("model_config", {})
        training_end = config.get("training_period_end")
        if not training_end:
            return None
        return {
            "training_period_start": config.get("training_period_start"),
            "training_period_end": training_end,
            "temporal_split_gap_days": config.get("temporal_split_gap_days", 7),
            "training_sample_size": config.get("training_sample_size"),
            "training_hit_rate": config.get("training_hit_rate"),
            "training_p_value": config.get("training_p_value"),
            "has_temporal_isolation": True,
        }


# ──────────────────────────────────────────────────────────────────
# Sharpening hook: terminal status → wiki article.
# Fire-and-forget. Never blocks or raises up into update_status.
# Opt-in by env (default OFF to avoid disrupting existing behavior).
# ──────────────────────────────────────────────────────────────────
