"""
tools.hypothesis.promote — auto_promote, live review, and data-access helpers.

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
from tools.resolvers.base_rates import (
    base_rate_relative_floor,
    expected_base_rate_from_events,
)
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
    LIVE_REVIEW_WINDOW_DAYS,
    MAX_LIVE_OVERLAP_PCT,
    PORTFOLIO_OVERLAP_WINDOW_DAYS,
    SIM_GATE_ENABLED,
    MAX_PRE_PROMOTE_RUIN,
    PRE_PROMOTE_N_SIMS,
    PRE_PROMOTE_HORIZON,
    STAGE_ORDER,
    SIGNAL_COLLAPSE_MODE,
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
        """Review all LIVE hypotheses and demote underperformers to 'paused'.

        Pulls the trailing `window_days` of resolved bets from paper_trades
        (and clv_log as supplementary CLV evidence), computes rolling hit-rate,
        ROI, Sharpe, and max drawdown, and demotes when:

          * hit_rate < effective_floor      (sub-prior performance)
          * max_drawdown > max_drawdown     (excessive drawdown)
          * avg CLV < clv_negative_threshold (betting bad prices)

        Effective floor: when base_rate_relative=True (default), each
        hypothesis's hit-rate floor is derived from its own expected base
        rate (mean book implied probability of its trades) via
        tools.resolvers.base_rates.base_rate_relative_floor; the
        ``hit_rate_floor`` argument then acts only as the legacy ceiling.
        Low-base-rate claims are judged against their own prior, not the
        50%-domain constant.

        Returns a list of per-hypothesis outcome dicts.
        """
        from tools.math_utils import american_to_decimal
        from tools.market_microstructure import sortino_ratio as _sortino

        window = window_days if window_days is not None else LIVE_REVIEW_WINDOW_DAYS
        cutoff = (datetime.now(timezone.utc) - timedelta(days=window)).isoformat()

        live_rows = await self.list_hypotheses(status="live")
        results: list[dict] = []

        for h in live_rows:
            hid = h["hypothesis_id"]
            # ── Pull resolved bets within window from paper_trades ──
            trade_cur = await self._db.execute(
                "SELECT signal_odds_american, actual_result, clv_implied, "
                "signal_implied_prob, game_date "
                "FROM paper_trades "
                "WHERE hypothesis_id = ? "
                "  AND actual_result IN ('won','lost','push') "
                "  AND (created_at >= ? OR game_date >= ?) "
                "ORDER BY game_date",
                (hid, cutoff, cutoff[:10]),
            )
            rows = await trade_cur.fetchall()

            # Supplementary CLV from clv_log (the signal-quality ledger). We
            # don't require a hypothesis_id match here — clv_log is not always
            # tagged — so this is a best-effort supplement only.
            clv_values: list[float] = []
            for row in rows:
                clv = row[2]
                imp = row[3]
                if clv is not None and imp is not None:
                    # Positive = model priced above close (got the better price).
                    clv_values.append(float(clv) - float(imp))

            returns: list[float] = []
            wins = losses = pushes = 0
            for odds_american, actual_result, _clv, _imp, _gd in rows:
                if actual_result == "won":
                    try:
                        dec = american_to_decimal(int(odds_american))
                        returns.append(dec - 1.0)
                    except Exception:
                        returns.append(0.0)
                    wins += 1
                elif actual_result == "lost":
                    returns.append(-1.0)
                    losses += 1
                elif actual_result == "push":
                    returns.append(0.0)
                    pushes += 1

            n_resolved = wins + losses  # pushes don't count toward hit rate
            hit_rate = wins / n_resolved if n_resolved else 0.0
            roi = sum(returns) / len(returns) if returns else 0.0
            # Drawdown
            mdd = 0.0
            if returns:
                equity = 0.0
                peak = 0.0
                for r in returns:
                    equity += r
                    if equity > peak:
                        peak = equity
                    dd = (peak - equity) / (abs(peak) + 1.0)
                    if dd > mdd:
                        mdd = dd
            sortino = _sortino(returns) if returns else None
            avg_clv = sum(clv_values) / len(clv_values) if clv_values else None

            outcome: dict = {
                "hypothesis_id": hid,
                "name": h.get("name"),
                "window_days": window,
                "n_resolved": n_resolved,
                "wins": wins,
                "losses": losses,
                "pushes": pushes,
                "hit_rate": round(hit_rate, 4),
                "roi": round(roi, 4),
                "max_drawdown": round(mdd, 4),
                "sortino": round(sortino, 4) if sortino is not None else None,
                "avg_clv": round(avg_clv, 4) if avg_clv is not None else None,
                "demoted": False,
                "reasons": [],
            }

            # Don't demote with insufficient data — a 2-game losing streak
            # should not pause a freshly-promoted hypothesis.
            if n_resolved < min_resolved:
                outcome["decision"] = "hold_insufficient_sample"
                results.append(outcome)
                continue

            reasons = []
            # Base-rate-relative effective floor (B1): judge the claim
            # against its own prior. Unknown base rate → legacy floor.
            _eff_floor = hit_rate_floor
            if base_rate_relative:
                _base = expected_base_rate_from_events(
                    [{"book_implied_prob": imp} for (_o, _r, _c, imp, _g) in rows
                     if imp is not None]
                )
                _eff_floor = base_rate_relative_floor(
                    _base, legacy_floor=hit_rate_floor
                )
                outcome["effective_hit_rate_floor"] = round(_eff_floor, 4)
                outcome["expected_base_rate"] = (
                    round(_base, 4) if _base is not None else None
                )
            if hit_rate < _eff_floor:
                reasons.append(
                    f"hit_rate {hit_rate:.1%} < {_eff_floor:.0%} floor"
                    + (f" (base-rate-relative; prior={_base:.0%})" if base_rate_relative and _base is not None else "")
                )
            if mdd > max_drawdown:
                reasons.append(
                    f"drawdown {mdd:.1%} > {max_drawdown:.0%} threshold"
                )
            if avg_clv is not None and avg_clv < clv_negative_threshold:
                reasons.append(
                    f"avg CLV {avg_clv:.4f} < {clv_negative_threshold}"
                )

            if not reasons:
                outcome["decision"] = "hold_healthy"
                results.append(outcome)
                continue

            # Demote → 'paused'. CAS on 'live' so a concurrent retirement
            # doesn't race us.
            reason_str = "auto:live_underperform — " + "; ".join(reasons)
            cas = await self.update_status(
                hid, "paused", reason_str, expected_status="live",
            )
            outcome["reasons"] = reasons
            outcome["demoted"] = bool(cas.get("changed"))
            outcome["decision"] = (
                "demoted_to_paused" if cas.get("changed") else "cas_noop"
            )

            # Log the demotion into hypothesis_stats for visibility. Best-effort
            # — we don't block demotion on the logging call.
            try:
                from tools.db_utils import execute_with_retry, commit_with_retry
                await execute_with_retry(
                    self._db,
                    "INSERT INTO hypothesis_stats "
                    "(hypothesis_id, stage, computed_at, total_n, signals_n, "
                    " win, loss, push_, hit_rate, avg_clv, "
                    " positive_clv_rate, roi_pct, max_drawdown, sortino, is_significant) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        hid,
                        "live_review",
                        datetime.now(timezone.utc).isoformat(),
                        n_resolved + pushes,
                        n_resolved,
                        wins,
                        losses,
                        pushes,
                        hit_rate,
                        avg_clv,
                        None,
                        roi * 100.0,
                        mdd,
                        sortino,
                        False,
                    ),
                    operation="live_review insert stats",
                )
                await commit_with_retry(self._db, operation="live_review stats")
            except Exception as e:
                logger.warning(f"live_review stats insert failed for {hid}: {e}")

            # Attach a wiki article so demotions appear in the research trail.
            # Pre-2026-04-22: this used (article_id, title, body, domain,
            # created_at) — a schema that NEVER existed. The INSERT raised
            # every call, was swallowed by `except Exception: pass`, and not
            # a single demotion ever made it into the wiki. Now routed through
            # knowledge_wiki.write_lesson_article which uses the real schema
            # (topic PK, title, content, summary, related_topics, ...) and
            # increments _wiki_writes_failed on any real error so we notice.
            try:
                from tools.knowledge_wiki import get_wiki
                wiki = get_wiki()
                demotion_content = (
                    f"LIVE demotion post-mortem for {h.get('name', hid)} "
                    f"(hypothesis_id={hid}).\n\n"
                    f"Reason: {reason_str}\n\n"
                    f"Performance window (last {window} days):\n"
                    f"  - n_resolved: {n_resolved}\n"
                    f"  - hit_rate: {hit_rate:.1%}\n"
                    f"  - roi: {roi:.2%}\n"
                    f"  - max_drawdown: {mdd:.1%}\n"
                    f"  - avg_clv: {avg_clv}\n"
                    f"  - sortino: {sortino}\n\n"
                    f"Sport: {h.get('sport', 'unknown')}\n"
                    f"Market: {h.get('market_type', 'unknown')}\n"
                    f"Demoted at: {datetime.now(timezone.utc).isoformat()}\n\n"
                    f"This article is retrievable by the hypothesis generator "
                    f"so similar patterns in the same cohort aren't "
                    f"re-proposed without acknowledging this prior failure."
                )
                topic_slug = f"{hid}_live_demotion_lessons"
                write_result = await wiki.write_lesson_article(
                    self._db,
                    topic=topic_slug,
                    title=f"LIVE demotion: {h.get('name', hid)}",
                    content=demotion_content,
                    domain="SIGNAL",
                    related_topics=[
                        "demotion_lessons",
                        f"sport:{h.get('sport', 'unknown')}",
                        f"market:{h.get('market_type', 'unknown')}",
                        "live_review_failure",
                    ],
                    confidence=0.7,
                )
                outcome["wiki_article_topic"] = topic_slug
                outcome["wiki_write_action"] = write_result.get("action")
                if write_result.get("action") == "failed":
                    # Loud, not silent — the whole point of this fix.
                    logger.warning(
                        f"Demotion wiki write FAILED for {hid}: "
                        f"{write_result.get('error')}"
                    )
            except Exception as e:
                # Explicit log + counter bump — replaces the old bare `pass`.
                logger.warning(
                    f"Demotion wiki lesson write raised for {hid}: "
                    f"{type(e).__name__}: {e}"
                )
                try:
                    from tools import knowledge_wiki as _kw
                    _kw._wiki_writes_failed += 1
                except Exception:
                    pass

            results.append(outcome)

        return results

    # ── DATA ACCESSORS ──

    async def _get_backtest_signals(self, hypothesis_id: str) -> list[dict]:
        """Get backtest signal events, deduplicated by unique event.

        Pre-2026-04-22 this kept the best-edge row per event_id.  For player-
        prop hypotheses that produce many rows per game that is *selection
        bias* — a 10-row game reports the max edge, not the representative
        edge.  We switch to one of three modes (``CALLISTO_SIGNAL_COLLAPSE_MODE``):

        * ``random_row`` — pick one row per event_id with a deterministic seed
          keyed on event_id+hypothesis_id.  Backward-compatible shape,
          eliminates best-edge selection bias.  Default.
        * ``composite``  — aggregate rows within an event into a single
          composite signal (averaged edge/ev/fair_prob, summed kelly_fraction
          capped at 1.0).  Matches real-world correlated-prop behavior.
        * ``best_edge``  — legacy pre-audit behavior; kept only for hypotheses
          marked ``model_config['legacy']=True``.  Not recommended.
        """
        import random as _random

        cursor = await self._db.execute(
            "SELECT * FROM backtest_events "
            "WHERE hypothesis_id = ? AND signal_generated = 1 "
            "ORDER BY game_date, id",
            (hypothesis_id,),
        )
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        all_events = [dict(zip(cols, row)) for row in rows]

        # Group rows by event_id
        by_event: dict[str, list[dict]] = {}
        for ev in all_events:
            eid = ev["event_id"]
            by_event.setdefault(eid, []).append(ev)

        # Per-hypothesis mode: legacy hyps use best_edge; new hyps use the
        # configured collapse mode (default random_row).
        h = await self.get_hypothesis(hypothesis_id)
        cfg = (h or {}).get("model_config") or {}
        is_legacy = bool(cfg.get("legacy") is True) if isinstance(cfg, dict) else False
        mode = "best_edge" if is_legacy else SIGNAL_COLLAPSE_MODE

        collapsed: list[dict] = []
        for eid, group in by_event.items():
            if len(group) == 1:
                collapsed.append(group[0])
                continue

            if mode == "best_edge":
                pick = max(group, key=lambda e: (e.get("edge") or 0))
                collapsed.append(pick)
            elif mode == "composite":
                # Aggregate across rows: average edge/ev/fair/implied,
                # sum kelly (capped at 1.0 — stake fraction), keep first
                # row's metadata.  actual_result: "won" iff any row won;
                # otherwise "lost" if any row resolved.
                base = dict(group[0])
                n_g = len(group)
                def _avg(key):
                    vals = [r.get(key) for r in group if r.get(key) is not None]
                    return (sum(vals) / len(vals)) if vals else base.get(key)
                base["edge"] = _avg("edge")
                base["ev_pct"] = _avg("ev_pct")
                base["model_fair_prob"] = _avg("model_fair_prob")
                base["book_implied_prob"] = _avg("book_implied_prob")
                kelly_sum = sum((r.get("kelly_fraction") or 0) for r in group)
                base["kelly_fraction"] = min(1.0, kelly_sum)
                # Composite outcome: count a win only if majority of rows won.
                # This matches "correlated parlay" semantics within an event.
                wins = sum(1 for r in group if r.get("actual_result") == "won")
                losses = sum(1 for r in group if r.get("actual_result") == "lost")
                if wins == 0 and losses == 0:
                    base["actual_result"] = None
                elif wins > losses:
                    base["actual_result"] = "won"
                elif losses > wins:
                    base["actual_result"] = "lost"
                else:
                    base["actual_result"] = "push"
                base["_composite_n"] = n_g
                collapsed.append(base)
            else:  # random_row (default)
                rng = _random.Random(f"{hypothesis_id}|{eid}")
                pick = rng.choice(group)
                collapsed.append(pick)

        return sorted(collapsed, key=lambda e: e.get("game_date", ""))

    async def _get_backtest_resolved(self, hypothesis_id: str) -> list[dict]:
        """Get resolved backtest events, deduplicated by unique event.

        Fallback for evaluate_significance when 0 signal events exist —
        lets us determine if the thesis has any merit before auto-rejecting.
        Uses the same collapse mode as _get_backtest_signals
        (CALLISTO_SIGNAL_COLLAPSE_MODE) to avoid re-introducing best-edge
        selection bias on the fallback path.
        """
        import random as _random

        cursor = await self._db.execute(
            "SELECT * FROM backtest_events "
            "WHERE hypothesis_id = ? AND actual_result IS NOT NULL "
            "ORDER BY game_date, id",
            (hypothesis_id,),
        )
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        all_events = [dict(zip(cols, row)) for row in rows]

        by_event: dict[str, list[dict]] = {}
        for ev in all_events:
            by_event.setdefault(ev["event_id"], []).append(ev)

        h = await self.get_hypothesis(hypothesis_id)
        cfg = (h or {}).get("model_config") or {}
        is_legacy = bool(cfg.get("legacy") is True) if isinstance(cfg, dict) else False
        mode = "best_edge" if is_legacy else SIGNAL_COLLAPSE_MODE

        collapsed: list[dict] = []
        for eid, group in by_event.items():
            if len(group) == 1:
                collapsed.append(group[0])
                continue
            if mode == "best_edge":
                pick = max(group, key=lambda e: (e.get("edge") or 0))
            else:  # random_row / composite both select deterministically here
                rng = _random.Random(f"{hypothesis_id}|{eid}|resolved")
                pick = rng.choice(group)
            collapsed.append(pick)

        return sorted(collapsed, key=lambda e: e.get("game_date", ""))

    async def _diagnose_edge_threshold(self, hypothesis_id: str) -> dict:
        """Check if a hypothesis's edge_threshold is suppressing valid signals.

        Looks at the edge distribution of backtest events to determine if the
        threshold is set above the max observed edge (meaning signals can never fire).
        """
        h = await self.get_hypothesis(hypothesis_id)
        current_threshold = h.get("edge_threshold", 0.03) if h else 0.03

        cursor = await self._db.execute(
            "SELECT edge FROM backtest_events "
            "WHERE hypothesis_id = ? AND edge IS NOT NULL "
            "ORDER BY edge DESC LIMIT 100",
            (hypothesis_id,),
        )
        edges = [r[0] for r in await cursor.fetchall()]

        if not edges:
            return {"threshold_too_high": False, "current_threshold": current_threshold}

        max_edge = max(edges)
        avg_edge = sum(edges) / len(edges)
        above_threshold = sum(1 for e in edges if e >= current_threshold)

        result = {
            "current_threshold": current_threshold,
            "max_edge": max_edge,
            "avg_edge": avg_edge,
            "total_edges": len(edges),
            "above_threshold": above_threshold,
            "threshold_too_high": above_threshold == 0 and max_edge > 0,
        }

        if result["threshold_too_high"]:
            # Set new threshold to 60% of max observed edge (leaves room for real signals)
            result["recommended_threshold"] = round(max(max_edge * 0.6, 0.01), 4)

        return result

    async def _get_best_run_stats(self, hypothesis_id: str) -> Optional[dict]:
        """Get the best backtest run stats for a hypothesis (by hit_rate)."""
        cursor = await self._db.execute(
            "SELECT actual_win, actual_loss, hit_rate, avg_edge, avg_ev "
            "FROM backtest_runs "
            "WHERE hypothesis_id = ? AND hit_rate IS NOT NULL "
            "ORDER BY hit_rate DESC LIMIT 1",
            (hypothesis_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return {
            "wins": row[0],
            "losses": row[1],
            "hit_rate": row[2],
            "avg_edge": row[3],
            "avg_ev": row[4],
        }

    async def _days_of_odds_data(self, hypothesis_id: str) -> Optional[int]:
        """How many days of historical odds data exist for this hypothesis's sport."""
        cursor = await self._db.execute(
            "SELECT sport FROM hypotheses WHERE hypothesis_id = ?",
            (hypothesis_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        sport = row[0]
        cursor = await self._db.execute(
            "SELECT COUNT(DISTINCT snapshot_date) FROM historical_odds_cache "
            "WHERE sport = ?",
            (sport,),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def _avg_books_used(self, hypothesis_id: str) -> Optional[float]:
        """Average books_used across backtest events for this hypothesis.

        Returns None if no events have model_factors with books_used.
        A value < 2.0 means the devig was based on a single book — unreliable.
        """
        cursor = await self._db.execute(
            "SELECT model_factors FROM backtest_events "
            "WHERE hypothesis_id = ? AND model_factors IS NOT NULL "
            "LIMIT 50",
            (hypothesis_id,),
        )
        rows = await cursor.fetchall()
        if not rows:
            return None
        import json as _json
        books = []
        for (mf,) in rows:
            try:
                factors = _json.loads(mf)
                b = factors.get("books_used")
                if b is not None:
                    books.append(b)
            except (ValueError, TypeError):
                continue
        return sum(books) / len(books) if books else None

    async def _count_unresolved(self, hypothesis_id: str) -> int:
        """Count backtest events that haven't been resolved against game results."""
        cursor = await self._db.execute(
            "SELECT COUNT(*) FROM backtest_events "
            "WHERE hypothesis_id = ? AND actual_result IS NULL",
            (hypothesis_id,),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def _get_paper_trades(self, hypothesis_id: str) -> list[dict]:
        """Get paper trades for a hypothesis, deduplicated to best-edge per unique game.

        Each game can produce multiple paper trades (one per book showing edge).
        For evaluation, we keep only the highest-edge trade per unique game
        (game_date + home_team + away_team) to avoid inflating sample counts.
        """
        cursor = await self._db.execute(
            """
            SELECT * FROM paper_trades
            WHERE rowid IN (
                SELECT rowid FROM (
                    SELECT rowid,
                           ROW_NUMBER() OVER (
                               PARTITION BY hypothesis_id, game_date, home_team, away_team
                               ORDER BY edge DESC
                           ) as rn
                    FROM paper_trades
                    WHERE hypothesis_id = ?
                )
                WHERE rn = 1
            )
            ORDER BY game_date
            """,
            (hypothesis_id,),
        )
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        result = [dict(zip(cols, row)) for row in rows]

        # Map paper_trades column names to backtest_events names so that
        # evaluate_significance() (which expects book_odds_american etc.)
        # works transparently with paper trade data.
        for row in result:
            row["book_odds_american"] = row.get("signal_odds_american")
            row["book_implied_prob"] = row.get("signal_implied_prob")
            row["signal_generated"] = 1  # all paper trades are signals

        return result

    async def _get_paper_trades_all(self, hypothesis_id: str) -> list[dict]:
        """Get ALL paper trades including multi-book duplicates (for detailed reporting)."""
        cursor = await self._db.execute(
            "SELECT * FROM paper_trades WHERE hypothesis_id = ? ORDER BY game_date",
            (hypothesis_id,),
        )
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in rows]

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
