"""
tools.hypothesis.promote — auto_promote, live review, and thin query delegates.

Data-access helpers live in ``tools.hypothesis.promote_queries``; this mixin
keeps the original method names as one-line delegates (hasattr pins).

``review_live_hypotheses`` stays defined here as a thin delegate; the body
lives in ``tools.hypothesis.promote_review``. auto_promote stays here
(diagnose-only; no evidence rewrite).

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
        h = await self.get_hypothesis(hypothesis_id)
        if not h:
            return {"action": "error", "reason": "Hypothesis not found"}

        status = h["status"]
        _use_backtest_evidence = False  # set True when paper_trading has 0 trades but backtest data suffices

        # ── Hard data-existence gates ──
        if status == "backtesting":
            events = await self._get_backtest_signals(hypothesis_id)
            if not events:
                # No signal events — try ALL resolved events before entering rejection path.
                # evaluate_significance now falls back to resolved events, so run it
                # to populate stats even without signal-level data.
                resolved_events = await self._get_backtest_resolved(hypothesis_id)
                if resolved_events:
                    # Run significance on resolved events — this populates stats
                    sig_report = await self.evaluate_significance(hypothesis_id, "backtest")
                    if sig_report.get("sample_size", 0) > 0:
                        logger.info(
                            f"Hypothesis {hypothesis_id}: 0 signals but {sig_report['sample_size']} "
                            f"resolved events evaluated (hit_rate={sig_report.get('results', {}).get('hit_rate', 'N/A')})"
                        )

                # Check if there are ANY backtest events at all.
                # This distinguishes "never backtested" from "backtested but no edge found."
                total_events_row = await (await self._db.execute(
                    "SELECT COUNT(DISTINCT event_id) FROM backtest_events WHERE hypothesis_id = ?",
                    (hypothesis_id,),
                )).fetchone()
                total_events = total_events_row[0] if total_events_row else 0

                model_config = h.get("model_config", {})
                if isinstance(model_config, str):
                    import json as _json
                    try:
                        model_config = _json.loads(model_config)
                    except (json.JSONDecodeError, TypeError):
                        model_config = {}
                eval_cycles = model_config.get("evaluate_cycles", 0) + 1
                model_config["evaluate_cycles"] = eval_cycles
                from tools.db_utils import execute_with_retry, commit_with_retry
                await execute_with_retry(
                    self._db,
                    "UPDATE hypotheses SET model_config = ? WHERE hypothesis_id = ?",
                    (json.dumps(model_config), hypothesis_id),
                    operation="hypothesis evaluate_hypothesis eval_cycles",
                )
                await commit_with_retry(self._db, operation="hypothesis evaluate_hypothesis eval_cycles")

                # Before rejecting, check if the edge threshold is too high.
                # If events exist but 0 signals, the threshold may be suppressing
                # valid edges. Check edge distribution first.
                if total_events > 0 and eval_cycles >= 2:
                    edge_diag = await self._diagnose_edge_threshold(hypothesis_id)
                    if edge_diag.get("threshold_too_high"):
                        # Diagnose-only policy: never rewrite evidence to fit
                        # the gate. Log and hold; an operator decides whether
                        # to lower the threshold manually.
                        logger.info(
                            f"Hypothesis {hypothesis_id}: holding — edge_threshold "
                            f"too high. current_threshold="
                            f"{edge_diag.get('current_threshold'):.3f}, "
                            f"recommended_threshold="
                            f"{edge_diag.get('recommended_threshold'):.3f}, "
                            f"max_edge={edge_diag.get('max_edge'):.3f}, "
                            f"events={total_events}, signals=0 "
                            f"(evaluate_cycles={eval_cycles})"
                        )
                        return {
                            "action": "held",
                            "reason": "threshold_too_high",
                            "diagnosis": edge_diag,
                        }

                # Use 6 cycles before rejecting — gives time for threshold
                # adjustment (at cycle 2) plus 4 more cycles to accumulate signals.
                if eval_cycles >= 6:
                    if total_events > 0:
                        # Check data quality before rejecting — 1-book devig
                        # produces garbage edges, don't blame the hypothesis.
                        avg_books = await self._avg_books_used(hypothesis_id)
                        if avg_books is not None and avg_books < 2.0:
                            # Don't hold forever — after 15 eval cycles with thin data,
                            # reject rather than consuming budget indefinitely
                            if eval_cycles >= 15:
                                return {
                                    "action": "rejected",
                                    "reason": (
                                        f"Rejecting after {eval_cycles} cycles: avg "
                                        f"books_used={avg_books:.1f} never improved past 2.0. "
                                        f"Data quality insufficient for this hypothesis."
                                    ),
                                }
                            return {
                                "action": "held",
                                "reason": (
                                    f"0 signals in {total_events} events but avg "
                                    f"books_used={avg_books:.1f} — devig data too thin "
                                    f"to produce reliable edges. Holding for better data "
                                    f"(cycle {eval_cycles}/15 before auto-reject)."
                                ),
                            }

                        # Check run-level stats before rejecting — only hold if
                        # run stats show genuine promise (hit_rate > 55% with
                        # enough resolved events). Previously this held ANY
                        # hypothesis with a backtest_run entry, blocking rejection.
                        run_stats = await self._get_best_run_stats(hypothesis_id)
                        if (run_stats
                                and run_stats.get("hit_rate") is not None
                                and run_stats["hit_rate"] > 0.55
                                and (run_stats.get("wins", 0) or 0) + (run_stats.get("losses", 0) or 0) >= 10):
                            return {
                                "action": "held",
                                "reason": (
                                    f"0 signals at threshold but run-level data shows promise: "
                                    f"{run_stats['wins']}W/{run_stats['losses']}L "
                                    f"(hit_rate={run_stats['hit_rate']:.1%}). "
                                    f"Consider threshold adjustment."
                                ),
                            }

                        # Check for unresolved events — don't reject if results
                        # haven't been collected yet
                        unresolved = await self._count_unresolved(hypothesis_id)
                        if unresolved > 0:
                            return {
                                "action": "held",
                                "reason": (
                                    f"{unresolved}/{total_events} events still "
                                    f"unresolved (awaiting game results). "
                                    f"Cannot reject without resolution data."
                                ),
                            }

                        cas = await self.update_status(
                            hypothesis_id, "rejected",
                            "auto:no_edge_after_backtest",
                            expected_status=status,
                        )
                        if not cas.get("changed"):
                            return {"action": "held", "reason": "Concurrent transition won; rejection skipped."}
                        return {
                            "action": "rejected",
                            "reason": (
                                f"Backtested {total_events} events over {eval_cycles} cycles "
                                f"with {avg_books:.1f} avg books "
                                f"but 0 generated signals (no exploitable edge found)."
                            ),
                        }
                    else:
                        # No events found — but this might be because the
                        # data window is too narrow, not because the hypothesis
                        # is bad. Check how much data we actually have.
                        days_of_data = await self._days_of_odds_data(hypothesis_id)
                        if days_of_data is not None and days_of_data < 30:
                            # Too little data to declare untestable — hold for later
                            return {
                                "action": "held",
                                "reason": (
                                    f"0 events after {eval_cycles} cycles, but only "
                                    f"{days_of_data} days of odds data available. "
                                    f"Holding until data window grows."
                                ),
                            }

                        cas = await self.update_status(
                            hypothesis_id, "rejected",
                            "auto:no_backtest_data_after_5_cycles",
                            expected_status=status,
                        )
                        if not cas.get("changed"):
                            return {"action": "held", "reason": "Concurrent transition won; rejection skipped."}
                        return {
                            "action": "rejected",
                            "reason": (
                                f"No backtest events after {eval_cycles} evaluation "
                                f"cycles with {days_of_data or '?'} days of data — "
                                f"untestable."
                            ),
                        }

                if total_events > 0:
                    return {
                        "action": "held",
                        "reason": (
                            f"{total_events} events tested but 0 signals "
                            f"(cycle {eval_cycles}/10 before auto-reject)."
                        ),
                    }
                return {
                    "action": "held",
                    "reason": f"No backtest events yet (cycle {eval_cycles}/10 before auto-reject).",
                }

            # Events exist — now check minimum quality bar
            # Must match PROMOTION_GATES["backtesting→paper_trading"]["min_signals"]
            n = len(events)
            min_for_promotion = PROMOTION_GATES["backtesting→paper_trading"]["min_signals"]

            # Track evaluate_cycles for ALL hypotheses (not just 0-signal ones)
            model_config = h.get("model_config", {})
            if isinstance(model_config, str):
                import json as _json
                try:
                    model_config = _json.loads(model_config)
                except (json.JSONDecodeError, TypeError):
                    model_config = {}
            eval_cycles = model_config.get("evaluate_cycles", 0) + 1
            model_config["evaluate_cycles"] = eval_cycles
            from tools.db_utils import execute_with_retry, commit_with_retry
            await execute_with_retry(
                self._db,
                "UPDATE hypotheses SET model_config = ? WHERE hypothesis_id = ?",
                (json.dumps(model_config), hypothesis_id),
                operation="hypothesis evaluate_hypothesis eval_cycles_2",
            )
            await commit_with_retry(self._db, operation="hypothesis evaluate_hypothesis eval_cycles_2")

            # Always recompute stats when we have signal events — fixes
            # staleness after threshold adjustment creates signals retroactively
            # (evaluate_significance only ran in the 0-signal fallback path before).
            await self.evaluate_significance(hypothesis_id, "backtest")

            if n < min_for_promotion:
                return {
                    "action": "held",
                    "reason": f"Only {n}/{min_for_promotion} signal events — need more before promotion.",
                }

        elif status == "paper_trading":
            trades = await self._get_paper_trades(hypothesis_id)
            resolved_trades = [t for t in (trades or []) if t.get("actual_result")]

            if resolved_trades:
                if len(resolved_trades) >= 2:
                    try:
                        await self.evaluate_significance(hypothesis_id, "paper_trade")
                    except Exception as e:
                        logger.warning(f"Paper trade stats cache failed for {hypothesis_id}: {e}")

                returns = []
                for t in resolved_trades:
                    if t["actual_result"] == "won":
                        from tools.math_utils import american_to_decimal
                        dec = american_to_decimal(t["signal_odds_american"])
                        returns.append(dec - 1)
                    elif t["actual_result"] == "lost":
                        returns.append(-1.0)
                    elif t["actual_result"] == "push":
                        returns.append(0.0)
                if returns:
                    roi = sum(returns) / len(returns)
                    if roi <= 0:
                        # Auto-reject paper traders with clearly losing records.
                        # Without this, losing hypotheses stay in paper_trading
                        # indefinitely since the only exit is promotion (ROI>0)
                        # or the bt_p>0.30/bt_n>30 backtest gate below.
                        n_resolved = len(resolved_trades)
                        n_losses = sum(1 for r in returns if r < 0)
                        loss_rate = n_losses / n_resolved if n_resolved else 0
                        if n_resolved >= 7 and loss_rate >= 0.60:
                            cas = await self.update_status(hypothesis_id, "rejected", "auto", expected_status=status)
                            if not cas.get("changed"):
                                return {"action": "held", "reason": "Concurrent transition won; rejection skipped."}
                            return {
                                "action": "rejected",
                                "reason": (
                                    f"Paper trade ROI={roi:.2%}, {n_losses}/{n_resolved} losses "
                                    f"({loss_rate:.0%}) — auto-demoting losing paper trader."
                                ),
                            }
                        return {
                            "action": "held",
                            "reason": f"Paper trade ROI is {roi:.2%} — need positive ROI for live promotion.",
                        }
            else:
                # AUDIT FIX 2026-04-21 (hypothesis.py:1613 escape hatch removed):
                # Previously, a paper_trading hypothesis with zero resolved paper
                # trades would fall back to backtest evidence and promote to LIVE
                # on historical data alone — the #1 money-loss risk identified
                # by the lifecycle audit. LIVE promotion now HARD-requires
                # `min_paper_trades` resolved paper trades. No fallback.
                min_req = PROMOTION_GATES["paper_trading→live"].get(
                    "min_paper_trades", MIN_PAPER_TRADES
                )
                return {
                    "action": "held",
                    "reason": (
                        f"paper_trade_sample_insufficient — 0 resolved paper "
                        f"trades (need {min_req}). LIVE promotion requires "
                        f"forward-tested evidence; backtest-only escape hatch "
                        f"was removed in the 2026-04-21 audit."
                    ),
                }

        # ── Backtest-based rejection gate for paper_trading hypotheses ──
        # Paper trade n is usually tiny, so the standard auto-reject (p>0.15, n>=30)
        # only fires on backtest data. Check backtest stats directly: if the thesis
        # is noise after 30+ backtest signals, don't wait for paper trade confirmation.
        if status == "paper_trading" and not _use_backtest_evidence:
            bt_report = await self.evaluate_significance(hypothesis_id, "backtest")
            bt_p = bt_report.get("significance", {}).get("p_value_binomial", 1.0)
            bt_n = bt_report.get("sample_size", 0)
            bt_used_all = bt_report.get("used_all_events", False)
            if not bt_used_all and bt_p > 0.30 and bt_n > 30:
                # SECURITY (audit C-7): CAS on original status so a concurrent
                # promoter can't double-transition the row.
                cas = await self.update_status(hypothesis_id, "rejected", "auto", expected_status=status)
                if not cas.get("changed"):
                    return {"action": "held", "reason": "Concurrent transition won; this rejection is a no-op."}
                return {
                    "action": "rejected",
                    "reason": (
                        f"Backtest p={bt_p:.4f} > 0.30 with {bt_n} signals — "
                        f"noise after sufficient data exposure."
                    ),
                }

        # ── Standard readiness check (statistical significance, gates, etc.) ──
        # AUDIT FIX 2026-04-21: stage_override="backtest" removed from paper→live
        # path. The only remaining use would be for paper-stage hypotheses with
        # 0 paper trades, which now return "held" above rather than reach here.
        # `_use_backtest_evidence` is retained as a guard for future callers but
        # is never set True in the current flow.
        _stage_override = "backtest" if _use_backtest_evidence else None
        readiness = await self.check_promotion_readiness(hypothesis_id, stage_override=_stage_override)

        if readiness.get("should_reject"):
            cas = await self.update_status(hypothesis_id, "rejected", "auto", expected_status=status)
            if not cas.get("changed"):
                return {"action": "held", "reason": "Concurrent transition won; rejection skipped."}
            return {"action": "rejected", "reason": "Data actively disproves thesis."}

        if readiness.get("ready"):
            next_stage = readiness["next_stage"]
            cas = await self.update_status(hypothesis_id, next_stage, "auto", expected_status=status)
            if not cas.get("changed"):
                # Another worker already advanced this row; treat as success but report it.
                return {"action": "held", "reason": f"Concurrent promotion already moved row past {status!r}."}
            return {"action": "promoted", "new_status": next_stage}

        return {"action": "held", "checks": readiness.get("checks", [])}

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
