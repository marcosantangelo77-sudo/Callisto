"""
tools.hypothesis.significance — statistical evaluation and promotion-readiness.

Split out of tools/hypothesis.py (facade re-exports everything).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

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
    FWER_LOOKBACK_DAYS,
    SIGNAL_COLLAPSE_MODE,
    MAX_LIVE_OVERLAP_PCT,
    MAX_PRE_PROMOTE_RUIN,
    MIN_CANONICAL_CLV_SAMPLE,
    PORTFOLIO_OVERLAP_WINDOW_DAYS,
    PRE_PROMOTE_HORIZON,
    PRE_PROMOTE_N_SIMS,
    SIM_GATE_ENABLED,
    STAGE_ORDER,
    get_adaptive_p_value_threshold,
)
from tools.hypothesis.stats import (
    binomial_pvalue,
    calibration_bins,
    max_drawdown,
    sharpe_ratio,
    ttest_one_sample,
    z_score,
)
from tools.resolvers.base_rates import (
    base_rate_relative_floor,
    expected_base_rate_from_events,
)
from tools.market_microstructure import (
    sortino_ratio as _sortino_ratio,
    brier_score as _brier_score,
    information_coefficient as _information_coefficient,
)

logger = logging.getLogger("callisto.hypothesis")


class HypothesisSignificanceMixin:
    async def evaluate_significance(
        self, hypothesis_id: str, stage: str = "backtest",
    ) -> dict:
        """
        Run all statistical tests on a hypothesis at a given stage.
        Returns comprehensive significance report.
        """
        used_all_events = False
        if stage == "backtest":
            events = await self._get_backtest_signals(hypothesis_id)
            if not events:
                # Fall back to ALL resolved events — lets us evaluate hypotheses
                # even when edge_threshold suppressed all signals
                events = await self._get_backtest_resolved(hypothesis_id)
                used_all_events = bool(events)
        elif stage == "paper_trade":
            events = await self._get_paper_trades(hypothesis_id)
            if not events:
                # Fall back to backtest signals — context-dependent hypotheses
                # may go days/weeks without a matching live game, so paper_trades
                # stays empty. Using backtest signals lets the promotion gate
                # evaluate the hypothesis on its proven historical performance.
                events = await self._get_backtest_signals(hypothesis_id)
                if events:
                    used_all_events = True  # flag that we used backtest data
                    logger.info(
                        f"Hypothesis {hypothesis_id}: 0 paper trades, falling back "
                        f"to {len(events)} backtest signals for promotion evaluation"
                    )
        else:
            return {"error": f"Unknown stage: {stage}"}

        if not events:
            return {
                "hypothesis_id": hypothesis_id,
                "stage": stage,
                "sample_size": 0,
                "is_significant": False,
                "recommendation": "No data yet.",
            }

        # Extract core metrics
        wins = sum(1 for e in events if e["actual_result"] == "won")
        losses = sum(1 for e in events if e["actual_result"] == "lost")
        pushes = sum(1 for e in events if e["actual_result"] == "push")
        resolved = wins + losses + pushes
        unresolved = len(events) - resolved

        if resolved < 2:
            return {
                "hypothesis_id": hypothesis_id,
                "stage": stage,
                "sample_size": resolved,
                "is_significant": False,
                "recommendation": f"Need more resolved events (have {resolved}).",
            }

        decided = wins + losses
        hit_rate = wins / decided if decided > 0 else 0

        # Expected hit rate = average of book implied probabilities for signals
        expected_rates = [e["book_implied_prob"] for e in events if e.get("book_implied_prob")]
        expected_rate = sum(expected_rates) / len(expected_rates) if expected_rates else 0.50

        # Per-bet returns for t-test and Sharpe
        returns = []
        for e in events:
            if e["actual_result"] == "won":
                from tools.math_utils import american_to_decimal
                dec = american_to_decimal(e["book_odds_american"])
                returns.append(dec - 1)  # profit on $1 bet
            elif e["actual_result"] == "lost":
                returns.append(-1.0)
            elif e["actual_result"] == "push":
                returns.append(0.0)

        # CLV metrics
        clv_values = [e.get("clv_implied", 0) for e in events if e.get("clv_implied") is not None]
        avg_clv = sum(clv_values) / len(clv_values) if clv_values else 0
        positive_clv_rate = (
            sum(1 for v in clv_values if v > 0) / len(clv_values) if clv_values else 0
        )

        # Edge and EV
        edges = [e["edge"] for e in events if e.get("edge") is not None]
        evs = [e["ev_pct"] for e in events if e.get("ev_pct") is not None]
        avg_edge = sum(edges) / len(edges) if edges else 0
        avg_ev = sum(evs) / len(evs) if evs else 0
        positive_edge_rate = (
            sum(1 for e in edges if e > 0) / len(edges) if edges else 0
        )

        # Statistical tests
        p_binomial = binomial_pvalue(wins, decided, expected_rate)
        t_stat, p_ttest = ttest_one_sample(returns)
        z = z_score(wins, decided, expected_rate)
        sr = sharpe_ratio(returns)
        mdd = max_drawdown(returns)

        # ── Microstructure metrics (sortino, brier, IC) ──
        # Sortino: downside-only risk — better than Sharpe for betting
        # because we care about loss variance, not upside variance.
        sortino = _sortino_ratio(returns)

        # Brier score: calibration quality of predicted probabilities
        brier_preds = []
        brier_outcomes = []
        for e in events:
            if e["actual_result"] in ("won", "lost") and e.get("model_fair_prob") is not None:
                brier_preds.append(e["model_fair_prob"])
                brier_outcomes.append(1 if e["actual_result"] == "won" else 0)
        brier = _brier_score(brier_preds, brier_outcomes)

        # Information coefficient: correlation between predicted and realized edges
        predicted_edges = []
        realized_edges = []
        for e in events:
            if e.get("edge") is not None and e["actual_result"] in ("won", "lost"):
                predicted_edges.append(e["edge"])
                # Realized edge: 1 means the prediction was correct at the predicted
                # edge magnitude; -1 means it was wrong. Scale by edge for correlation.
                if e["actual_result"] == "won":
                    from tools.math_utils import american_to_decimal
                    dec = american_to_decimal(e["book_odds_american"])
                    realized_edges.append(dec - 1.0)  # actual return
                else:
                    realized_edges.append(-1.0)
        ic = _information_coefficient(predicted_edges, realized_edges)

        # ROI
        total_staked = len(returns)  # $1 per bet
        total_returned = sum(r + 1 for r in returns if r > -1) + sum(0 for r in returns if r <= -1)
        roi = (sum(returns) / total_staked * 100) if total_staked > 0 else 0

        # Significance determination
        h = await self.get_hypothesis(hypothesis_id)
        sig_level = h["significance_level"] if h else 0.05
        is_significant = (
            (p_binomial < sig_level or p_ttest < sig_level)
            and decided >= (h["min_sample_size"] if h else 50)
        )

        # Calibration
        preds = []
        for e in events:
            if e["actual_result"] in ("won", "lost"):
                preds.append((e["model_fair_prob"], e["actual_result"] == "won"))
        cal_bins = calibration_bins(preds)

        # Recommendation
        if is_significant and avg_clv > 0:
            rec = "PROMOTE — statistically significant edge with positive CLV."
        elif decided < 100:
            rec = "WAIT — insufficient sample size for conclusion."
        elif p_binomial > AUTO_REJECT_P and decided > AUTO_REJECT_MIN_N:
            rec = "REJECT — data actively disproves this thesis."
        elif p_binomial < sig_level:
            rec = "PROMISING — significant p-value, but check CLV and drawdown."
        else:
            rec = "INCONCLUSIVE — continue collecting data."

        report = {
            "hypothesis_id": hypothesis_id,
            "stage": stage,
            "sample_size": resolved,
            "unresolved": unresolved,
            "used_all_events": used_all_events,
            "results": {
                "wins": wins,
                "losses": losses,
                "pushes": pushes,
                "hit_rate": round(hit_rate, 4),
                "expected_rate": round(expected_rate, 4),
            },
            "significance": {
                "p_value_binomial": round(p_binomial, 6),
                "p_value_ttest": round(p_ttest, 6),
                "z_score": round(z, 4),
                "is_significant": is_significant,
                "significance_level": sig_level,
            },
            "edge_metrics": {
                "avg_edge": round(avg_edge, 4),
                "avg_ev": round(avg_ev, 4),
                "roi_pct": round(roi, 2),
                "positive_edge_rate": round(positive_edge_rate, 4),
            },
            "clv": {
                "avg_clv": round(avg_clv, 4),
                "positive_clv_rate": round(positive_clv_rate, 4),
                "clv_sample_size": len(clv_values),
            },
            "risk": {
                "sharpe_ratio": round(sr, 4),
                "sortino_ratio": round(sortino, 4) if sortino is not None else None,
                "max_drawdown": round(mdd, 4),
            },
            "calibration": cal_bins,
            "calibration_score": {
                "brier_score": round(brier, 6) if brier is not None else None,
                "information_coefficient": round(ic, 4) if ic is not None else None,
            },
            "recommendation": rec,
            "total_events": 0,   # placeholder, updated below with true event count
            "total_signals": 0,  # placeholder, updated below with true signal count
        }

        # Store in hypothesis_stats (upsert: one row per hypothesis+stage)
        now = datetime.now(timezone.utc).isoformat()
        from tools.db_utils import execute_with_retry, commit_with_retry

        # Query true total_n and signals_n from ALL backtest_events for this
        # hypothesis — not just the signal-only subset used for evaluation.
        # Previously total_n was set to `resolved` (wins+losses+pushes from
        # signal events), making it identical to signals_n.
        if stage == "backtest":
            count_cursor = await self._db.execute(
                "SELECT COUNT(DISTINCT event_id), "
                "COUNT(DISTINCT CASE WHEN signal_generated = 1 THEN event_id END) "
                "FROM backtest_events WHERE hypothesis_id = ?",
                (hypothesis_id,),
            )
            count_row = await count_cursor.fetchone()
            stats_total_n = count_row[0] or 0
            stats_signals_n = count_row[1] or 0
        else:
            # For paper_trade stage, total_n = resolved signals evaluated above
            stats_total_n = resolved
            stats_signals_n = sum(1 for e in events if e.get("signal_generated"))

        report["total_events"] = stats_total_n
        report["total_signals"] = stats_signals_n

        await execute_with_retry(
            self._db,
            "DELETE FROM hypothesis_stats WHERE hypothesis_id = ? AND stage = ?",
            (hypothesis_id, stage),
            operation="hypothesis evaluate_significance delete",
        )
        await execute_with_retry(
            self._db,
            "INSERT INTO hypothesis_stats "
            "(hypothesis_id, stage, computed_at, total_n, signals_n, win, loss, push_, "
            "hit_rate, avg_edge, avg_ev, avg_clv, positive_clv_rate, roi_pct, "
            "sharpe, max_drawdown, p_value, is_significant, "
            "sortino, brier_score, information_coefficient) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (hypothesis_id, stage, now, stats_total_n, stats_signals_n,
             wins, losses, pushes,
             hit_rate, avg_edge, avg_ev, avg_clv, positive_clv_rate, roi,
             sr, mdd, p_binomial, is_significant,
             sortino, brier, ic),
            operation="hypothesis evaluate_significance insert",
        )
        await commit_with_retry(self._db, operation="hypothesis evaluate_significance")

        return report

    async def check_promotion_readiness(
        self,
        hypothesis_id: str,
        *,
        stage_override: str | None = None,
        status_override: str | None = None,
    ) -> dict:
        """Check if a hypothesis meets criteria to advance to next stage.

        Args:
            stage_override: Force evaluation on a different data stage.
                Used by auto_promote when paper_trading hypotheses have
                0 paper trades but sufficient backtest evidence — without
                this, the readiness check would evaluate on empty
                paper_trade data and always fail (the deadlock bug).
            status_override: Evaluate the gate as if the hypothesis were
                in ``status_override`` instead of its real status.  Added
                2026-04-22 for the LIVE-cascade migration script: LIVE
                rows were grandfathered past the new paper→live gates,
                and we need to re-run the exact paper→live gate against
                the current state without flipping the row first.  Does
                not mutate the DB; only affects this evaluation.
        """
        h = await self.get_hypothesis(hypothesis_id)
        if not h:
            return {"error": "Hypothesis not found"}

        status = status_override or h["status"]
        # Only allow status_override to downgrade LIVE → paper_trading for
        # the cascade use case; don't let callers fake a draft.
        if status_override and status_override not in ("backtesting", "paper_trading"):
            return {"ready": False, "reason": f"invalid status_override={status_override!r}"}
        if status in ("live", "retired", "rejected"):
            return {"ready": False, "reason": f"Cannot promote from {status}"}

        if status == "draft":
            return {"ready": True, "next_stage": "backtesting", "reason": "Draft → backtesting requires no data."}

        # Determine transition and evaluate
        if status == "backtesting":
            transition = "backtesting→paper_trading"
            stage = "backtest"
        elif status == "paper_trading":
            transition = "paper_trading→live"
            stage = stage_override or "paper_trade"
        else:
            return {"ready": False, "reason": f"Unknown status: {status}"}

        gate = PROMOTION_GATES[transition]
        report = await self.evaluate_significance(hypothesis_id, stage)

        checks = []
        ready = True

        # ── HARD GATE: min_days in current stage ──
        # Enforces time-in-stage before any promotion advances. Previously the
        # gate key existed in PROMOTION_GATES but was never read, letting rapid
        # promotions reach LIVE in minutes. Audit finding hypothesis.py:57.
        if "min_days" in gate:
            promoted_at = h.get("promoted_at") or h.get("created_at")
            days_in_stage = None
            if promoted_at:
                try:
                    ts = promoted_at
                    if isinstance(ts, str):
                        # Accept both ISO-with-tz and naive ISO strings
                        _norm = ts.replace("Z", "+00:00")
                        try:
                            promoted_dt = datetime.fromisoformat(_norm)
                        except ValueError:
                            # SQLite CURRENT_TIMESTAMP is "YYYY-MM-DD HH:MM:SS"
                            promoted_dt = datetime.strptime(
                                ts.split(".")[0], "%Y-%m-%d %H:%M:%S"
                            )
                    else:
                        promoted_dt = ts
                    if promoted_dt.tzinfo is None:
                        promoted_dt = promoted_dt.replace(tzinfo=timezone.utc)
                    days_in_stage = (
                        datetime.now(timezone.utc) - promoted_dt
                    ).total_seconds() / 86400.0
                except Exception as e:
                    logger.warning(
                        f"Hypothesis {hypothesis_id}: could not parse promoted_at "
                        f"({promoted_at!r}): {e}"
                    )
            if days_in_stage is None:
                checks.append(
                    f"FAIL: insufficient_time_in_stage — promoted_at is null, "
                    f"cannot verify {gate['min_days']}-day minimum"
                )
                ready = False
            elif days_in_stage < gate["min_days"]:
                checks.append(
                    f"FAIL: insufficient_time_in_stage — {days_in_stage:.1f}d < "
                    f"{gate['min_days']}d required"
                )
                ready = False
            else:
                checks.append(
                    f"PASS: time-in-stage {days_in_stage:.1f}d >= {gate['min_days']}d"
                )

        # ── HARD GATE: min_paper_trades for paper_trading→live ──
        # A hypothesis cannot promote to LIVE on backtest evidence alone. The
        # stage_override="backtest" escape hatch was removed in auto_promote —
        # this gate ensures readiness also rejects direct LIVE promotion when
        # paper_trade sample is insufficient. Audit finding hypothesis.py:1613.
        if "min_paper_trades" in gate:
            required_trades = gate["min_paper_trades"]
            try:
                trade_cur = await self._db.execute(
                    "SELECT COUNT(*) FROM paper_trades "
                    "WHERE hypothesis_id = ? AND actual_result IN ('won','lost','push')",
                    (hypothesis_id,),
                )
                resolved_paper_trades = int((await trade_cur.fetchone())[0] or 0)
            except Exception as e:
                logger.warning(f"paper_trade count failed for {hypothesis_id}: {e}")
                resolved_paper_trades = 0
            if resolved_paper_trades < required_trades:
                checks.append(
                    f"FAIL: paper_trade_sample_insufficient — "
                    f"{resolved_paper_trades}/{required_trades} resolved paper trades"
                )
                ready = False
            else:
                checks.append(
                    f"PASS: {resolved_paper_trades}/{required_trades} resolved paper trades"
                )

        # Sample size
        n = report.get("sample_size", 0)
        required_n = gate["min_signals"]
        if n < required_n:
            checks.append(f"FAIL: {n}/{required_n} signals")
            ready = False
        else:
            checks.append(f"PASS: {n}/{required_n} signals")

        # P-value — use adaptive threshold based on sample size.
        # Small n makes standard thresholds mathematically unreachable;
        # adaptive gate relaxes for small samples, tightens as n grows.
        p = report.get("significance", {}).get("p_value_binomial", 1.0)
        base_p = gate["max_p_value"]
        max_p = get_adaptive_p_value_threshold(n, base_p)
        if max_p != base_p:
            logger.info(
                f"Hypothesis {hypothesis_id}: adaptive p-value threshold "
                f"{max_p:.2f} (base={base_p:.2f}, n={n})"
            )
        # SECURITY (audit H-5 + 2026-04-22 FWER fix): apply a Šidák family-wise
        # correction against the **lifetime** pool of hypotheses that were ever
        # backtested in the lookback window.  Every ever-tested hypothesis
        # represents a multiple-comparison opportunity for a false positive,
        # not just currently-active ones.  With ~4600 lifetime hypotheses and
        # α_family = 0.05 the per-hypothesis α is ~1.1e-5; this is correct and
        # the floor at 0.001 (pre-audit) was masking the true FWER.
        #
        # Denominator: COUNT(DISTINCT hypothesis_id) FROM backtest_runs within
        # the lookback window (CALLISTO_FWER_LOOKBACK_DAYS; 'inf' supported).
        # Legacy hypotheses with model_config['legacy']=True are grandfathered
        # and still use the active-only denominator to avoid mass demotions.
        lifetime_n = 0
        try:
            if math.isinf(FWER_LOOKBACK_DAYS):
                lifetime_cur = await self._db.execute(
                    "SELECT COUNT(DISTINCT hypothesis_id) FROM backtest_runs "
                    "WHERE completed_at IS NOT NULL"
                )
            else:
                lookback_iso = (
                    datetime.now(timezone.utc)
                    - timedelta(days=FWER_LOOKBACK_DAYS)
                ).isoformat()
                lifetime_cur = await self._db.execute(
                    "SELECT COUNT(DISTINCT hypothesis_id) FROM backtest_runs "
                    "WHERE completed_at IS NOT NULL AND completed_at > ?",
                    (lookback_iso,),
                )
            lifetime_n = int((await lifetime_cur.fetchone())[0] or 0)
        except Exception as e:
            logger.warning(f"FWER lifetime count failed: {e}; falling back to active-only")
            lifetime_n = 0

        try:
            active_cur = await self._db.execute(
                "SELECT COUNT(*) FROM hypotheses WHERE status IN ('backtesting','paper_trading')"
            )
            active_n = int((await active_cur.fetchone())[0] or 1)
        except Exception:
            active_n = 1

        # Legacy grandfather: hypotheses flagged legacy=True stay on the
        # old active-only denominator.  New hypotheses (default) get the
        # full lifetime denominator — true FWER control.
        is_legacy = bool((h.get("model_config") or {}).get("legacy") is True) if isinstance(h.get("model_config"), dict) else False
        fwer_n = active_n if is_legacy else max(lifetime_n, active_n, 1)

        if fwer_n > 1:
            # Šidák: α_per_test = 1 - (1 - α_family)^(1/n_tests)
            sidak = 1.0 - (1.0 - max_p) ** (1.0 / fwer_n)
            # NO FLOOR — α below 1e-5 is the correct behavior at lifetime N
            # in the thousands; floor was masking the true family-wise rate.
            corrected_p = min(max_p, sidak)
            denom_tag = "active-only (legacy)" if is_legacy else f"lifetime (lookback_days={FWER_LOOKBACK_DAYS})"
            checks.append(
                f"INFO: Šidák FWER correction over n={fwer_n} [{denom_tag}] → "
                f"p threshold {corrected_p:.2e} (was {max_p:.4f})"
            )
            max_p = corrected_p
        if p > max_p:
            checks.append(f"FAIL: p-value {p:.4f} > {max_p:.2e} (adaptive+FWER, base={base_p}, n={n}, fwer_n={fwer_n})")
            ready = False
        else:
            checks.append(f"PASS: p-value {p:.4f} <= {max_p:.2e} (adaptive+FWER, base={base_p}, n={n}, fwer_n={fwer_n})")

        # ── CLV gate (B1 rebuild) — reads the CANONICAL devigged statistic ──
        # Canonical source: clv_log.clv_prob_bp, basis points of devigged
        # probability between placement and close (positive = better price),
        # unit-consistent by construction (instance2 VERIFIED finding).
        # Gate statistic: fraction of resolved forward-tests whose canonical
        # CLV closed positive, compared against min_clv_rate (a RATE floor,
        # 0..1). Falls back to the legacy raw-implied-delta rate only when
        # fewer than MIN_CANONICAL_CLV_SAMPLE canonical rows exist.
        # NULL/insufficient data is reported honestly as insufficient, not
        # rendered as a 0% failure.
        min_clv = gate["min_clv_rate"]
        clv_rate: Optional[float] = None
        if min_clv <= 0:
            checks.append(
                f"INFO: CLV gate disabled for this transition (min_clv_rate={min_clv})"
            )
        else:
            canon_mean_bp: Optional[float] = None
            canon_n = 0
            try:
                from tools.resolvers.betting import BettingOutcomeResolver

                _resolver = BettingOutcomeResolver(self._db)
                canon_mean_bp, canon_n = await _resolver.mean_clv_prob_bp(hypothesis_id)
            except Exception as e:
                logger.debug(f"canonical CLV lookup failed for {hypothesis_id}: {e}")

            if canon_n >= MIN_CANONICAL_CLV_SAMPLE:
                # Rate over the same canonical sample.
                try:
                    pos_cur = await self._db.execute(
                        "SELECT COUNT(*) FROM paper_trades pt "
                        "JOIN clv_log cl ON cl.bet_id = 'pt:' || pt.trade_id "
                        "WHERE pt.hypothesis_id = ? "
                        "  AND pt.actual_result IN ('won','lost','push') "
                        "  AND cl.clv_prob_bp IS NOT NULL AND cl.clv_prob_bp > 0",
                        (hypothesis_id,),
                    )
                    canon_pos = int((await pos_cur.fetchone())[0] or 0)
                    clv_rate: Optional[float] = canon_pos / canon_n
                    clv_src = (
                        f"canonical clv_log.clv_prob_bp "
                        f"(n={canon_n}, mean={canon_mean_bp:.1f}bp)"
                    )
                except Exception:
                    clv_rate = None
                    clv_src = "canonical lookup error"
            else:
                # Legacy fallback: raw implied-delta fraction from the report
                # (same 0..1 trade-rate scale as the threshold).
                clv_rate = report.get("clv", {}).get("positive_clv_rate")
                clv_n = report.get("clv", {}).get("clv_sample_size", 0)
                clv_src = f"legacy clv_implied delta (n={clv_n}; canonical n={canon_n} < {MIN_CANONICAL_CLV_SAMPLE})"

            if clv_rate is None:
                checks.append(
                    f"FAIL: CLV insufficient data — no usable CLV sample ({clv_src}); "
                    f"need >= {MIN_CANONICAL_CLV_SAMPLE} canonical samples"
                )
                ready = False
            elif clv_rate < min_clv:
                checks.append(
                    f"FAIL: CLV positive-rate {clv_rate:.2%} < {min_clv:.2%} [{clv_src}]"
                )
                ready = False
            else:
                checks.append(
                    f"PASS: CLV positive-rate {clv_rate:.2%} >= {min_clv:.2%} [{clv_src}]"
                )

        # Sharpe (backtest only)
        if "min_sharpe" in gate:
            sr = report.get("risk", {}).get("sharpe_ratio", 0)
            if sr < gate["min_sharpe"]:
                checks.append(f"FAIL: Sharpe {sr:.2f} < {gate['min_sharpe']}")
                ready = False
            else:
                checks.append(f"PASS: Sharpe {sr:.2f}")

        # Max drawdown (paper trade only)
        # At n < 10, a single loss creates MDD = 1.0 (100%) regardless of
        # strategy quality. Waive the gate at small n — the p-value gate is
        # the real quality filter for small samples.
        if "max_drawdown" in gate:
            mdd = report.get("risk", {}).get("max_drawdown", 1.0)
            if n < 10:
                checks.append(f"SKIP: Drawdown {mdd:.1%} (n={n} < 10, waived — single loss = 100% MDD at small n)")
            elif mdd > gate["max_drawdown"]:
                checks.append(f"FAIL: Drawdown {mdd:.1%} > {gate['max_drawdown']:.0%}")
                ready = False
            else:
                checks.append(f"PASS: Drawdown {mdd:.1%}")

        # Sortino ratio (paper trade → live gate)
        # Sortino is superior to Sharpe for betting: penalizes only downside
        # variance, so profitable high-variance strategies aren't punished.
        if "min_sortino" in gate:
            sortino_val = report.get("risk", {}).get("sortino_ratio")
            min_sortino = gate["min_sortino"]
            if sortino_val is None:
                checks.append(f"WARN: Sortino unavailable (insufficient data)")
            elif sortino_val < min_sortino:
                checks.append(f"FAIL: Sortino {sortino_val:.2f} < {min_sortino}")
                ready = False
            else:
                checks.append(f"PASS: Sortino {sortino_val:.2f} >= {min_sortino}")

        # Positive edge rate (backtest gate)
        if "min_positive_edge_rate" in gate:
            pos_rate = report.get("edge_metrics", {}).get("positive_edge_rate", 0)
            min_per = gate["min_positive_edge_rate"]
            if pos_rate < min_per:
                checks.append(f"FAIL: Positive edge rate {pos_rate:.1%} < {min_per:.0%}")
                ready = False
            else:
                checks.append(f"PASS: Positive edge rate {pos_rate:.1%} >= {min_per:.0%}")

        # ── HARD GATE: lookahead-free snapshot quality ──
        # Every backtest event row records snapshot_quality in its
        # model_factors JSON ('pre_commence' | 'closing_fallback' |
        # 'closing_mode'). At least 80% of the SIGNAL sample must be
        # 'pre_commence' — otherwise the hypothesis earned its p-value on
        # closing-line lookahead data and can't be trusted to promote.
        # Silent-skip when the metadata isn't present (e.g. legacy rows
        # from before the 2026-04-22 fix) — the re-eval harness quantifies
        # historic damage separately.
        if transition == "backtesting→paper_trading":
            try:
                quality_cur = await self._db.execute(
                    "SELECT "
                    "  COALESCE(json_extract(model_factors,'$.snapshot_quality'),'unknown') AS q, "
                    "  COUNT(*) "
                    "FROM backtest_events "
                    "WHERE hypothesis_id = ? AND signal_generated = 1 "
                    "GROUP BY q",
                    (hypothesis_id,),
                )
                rows = await quality_cur.fetchall()
                counts = {r[0]: int(r[1]) for r in rows}
                total = sum(counts.values())
                pre = counts.get("pre_commence", 0)
                legacy_unknown = counts.get("unknown", 0)
                # If the sample is majority legacy-unknown, don't block —
                # those are rows from before the fix. Quantify separately
                # via scripts/reeval_backtests_no_lookahead.py.
                if total > 0 and (total - legacy_unknown) > 0:
                    instrumented = total - legacy_unknown
                    pre_rate = pre / instrumented
                    if pre_rate < 0.80:
                        fb = counts.get("closing_fallback", 0)
                        cm = counts.get("closing_mode", 0)
                        checks.append(
                            f"FAIL: snapshot_quality sample only {pre_rate:.1%} "
                            f"pre_commence (need >=80%; pre={pre}, "
                            f"closing_fallback={fb}, closing_mode={cm}, "
                            f"instrumented={instrumented}/{total})"
                        )
                        ready = False
                    else:
                        checks.append(
                            f"PASS: snapshot_quality {pre_rate:.1%} pre_commence "
                            f"(instrumented={instrumented}/{total})"
                        )
                elif total > 0:
                    checks.append(
                        f"INFO: snapshot_quality unknown on all {total} signals "
                        f"(legacy data — gate skipped)"
                    )
            except Exception as e:
                logger.debug(f"snapshot_quality gate error for {hypothesis_id}: {e}")

        # Overall edge distribution check (SIGNAL events only, deduplicated by event_id)
        # CRITICAL FIX: Previous version averaged ALL events including non-signals.
        # Non-signal events having negative edge is EXPECTED — the hypothesis correctly
        # didn't fire on those. Same bug was fixed for rejections in commit 10e61db.
        # A hypothesis with 10 winning signals should not be blocked because 6
        # non-signal events drag the average negative.
        if transition == "backtesting→paper_trading":
            try:
                edge_cursor = await self._db.execute(
                    "SELECT event_id, MAX(edge) FROM backtest_events "
                    "WHERE hypothesis_id = ? AND edge IS NOT NULL "
                    "AND signal_generated = 1 "
                    "GROUP BY event_id",
                    (hypothesis_id,),
                )
                dedup_edges = [row[1] for row in await edge_cursor.fetchall()]
                if dedup_edges:
                    overall_avg_edge = sum(dedup_edges) / len(dedup_edges)
                    if overall_avg_edge < 0:
                        checks.append(
                            f"FAIL: signal edge distribution is negative "
                            f"(avg_edge={overall_avg_edge:.4f} across {len(dedup_edges)} signal events)"
                        )
                        ready = False
                    else:
                        checks.append(
                            f"PASS: signal edge distribution positive "
                            f"(avg_edge={overall_avg_edge:.4f} across {len(dedup_edges)} signal events)"
                        )
                else:
                    checks.append("FAIL: no signal events with edge data")
                    ready = False
            except Exception as e:
                logger.warning(f"Could not check overall edge distribution: {e}")

        # Brier score (calibration quality)
        # At n < 5, Brier is statistically meaningless — variance dominates.
        # For underdog strategies, Brier baseline is ~0.33 not 0.25 because
        # (implied_prob - outcome)^2 is structurally high when betting +150 dogs.
        # At n < 20, Brier SE is large enough that a 0.01 difference is noise.
        # Waive when p-value and hit_rate prove real alpha despite Brier noise.
        if "max_brier" in gate:
            brier = report.get("calibration_score", {}).get("brier_score")
            max_brier = gate["max_brier"]
            market_type = h.get("market_type", "")
            if n < 5 and transition == "backtesting→paper_trading":
                # Waive: Brier needs minimum samples for any statistical meaning
                if brier is not None:
                    checks.append(f"SKIP: Brier score {brier:.4f} (n={n} < 5, waived for paper promotion)")
            else:
                if market_type == "h2h" and n < 30:
                    max_brier = 0.30
                hit_rate_val = report.get("results", {}).get("hit_rate", 0)
                if brier is not None and brier > max_brier:
                    # Waive Brier gate when p-value and hit_rate prove real alpha.
                    # At n<20, Brier SE is ~0.10+, so 0.28 vs 0.29 is pure noise.
                    if (p <= 0.15 and hit_rate_val > 0.55) or (hit_rate_val > 0.70 and n >= 10):
                        checks.append(
                            f"WAIVED: Brier {brier:.4f} > {max_brier} but p={p:.4f} and "
                            f"hit_rate={hit_rate_val:.1%} demonstrate real alpha — "
                            f"Brier noise at n={n} with binary outcomes"
                        )
                    else:
                        checks.append(f"FAIL: Brier score {brier:.4f} > {max_brier} (worse than coin-flip)")
                        ready = False
                elif brier is not None:
                    checks.append(f"PASS: Brier score {brier:.4f} <= {max_brier}")

        # Information coefficient
        # IC measures correlation between predicted edge and realized return.
        # At n < 5 with binary outcomes, IC has no statistical power — two
        # unlucky high-edge losses can drive IC to -0.8 on noise alone.
        # Waive only at very small n; quality gates must apply early.
        if "min_ic" in gate:
            ic = report.get("calibration_score", {}).get("information_coefficient")
            min_ic = gate["min_ic"]
            market_type = h.get("market_type", "")
            if n < 5 and transition == "backtesting→paper_trading":
                # Waive: IC needs minimum samples for any statistical meaning
                if ic is not None:
                    checks.append(f"SKIP: IC {ic:.4f} (n={n} < 5, waived for paper promotion)")
            else:
                if market_type == "h2h":
                    min_ic = -0.10
                # Waive IC gate when p-value and hit_rate demonstrate real alpha.
                # IC measures edge-magnitude calibration, not predictive quality.
                # At n<30 with binary outcomes, IC is dominated by noise — one
                # unlucky high-edge loss can drive IC to -0.8 on a profitable hypothesis.
                hit_rate = report.get("results", {}).get("hit_rate", 0)
                if ic is not None and ic < min_ic:
                    if (p <= 0.15 and hit_rate > 0.55) or (hit_rate > 0.70 and n >= 10):
                        checks.append(
                            f"WAIVED: IC {ic:.4f} < {min_ic} but p={p:.4f} and "
                            f"hit_rate={hit_rate:.1%} demonstrate real alpha — "
                            f"IC noise at n={n} with binary outcomes"
                        )
                    else:
                        checks.append(f"FAIL: IC {ic:.4f} < {min_ic} (model is anti-predictive)")
                        ready = False
                elif ic is not None:
                    checks.append(f"PASS: IC {ic:.4f} >= {min_ic}")

        # Auto-rejection check — only reject based on signal-level data.
        # If we fell back to all-events (used_all_events=True), the p-value
        # reflects "random bet outcomes" not "edge thesis is wrong."
        #
        # Two rejection paths:
        # 1. Original: p > 0.50 with 15+ signals = actively disproven
        # 2. New: p > 0.15 with 30+ signals = no edge after large sample
        #    (prevents zombie hypotheses with 50/50 W/L from consuming budget)
        used_all_events = report.get("used_all_events", False)
        should_reject = (
            not used_all_events
            and (
                (p > AUTO_REJECT_P and n >= AUTO_REJECT_MIN_N)
                or (p > AUTO_REJECT_STRONG_P and n >= AUTO_REJECT_STRONG_MIN_N)
                or (p > AUTO_REJECT_EXTREME_P and n >= AUTO_REJECT_EXTREME_MIN_N)
                or (p > 0.15 and n >= 30)
            )
        )

        # Losing record rejection: hit_rate below the claim's BASE-RATE-
        # RELATIVE floor after 12+ signals means the hypothesis is actively
        # underperforming its own market prior. The old absolute 45% floor
        # was correct only at ~50% base rates and mass-rejected true
        # positives in low-base-rate domains. Derived floor = base_rate ×
        # (1 + lift), clamped; unknown base rate → legacy 0.45.
        if not should_reject and not used_all_events and status == "backtesting":
            _hit_rate = report.get("results", {}).get("hit_rate", 0.5)
            # Base rate from the report's own expected_rate (mean market
            # implied prob of the evaluated sample).
            _base = report.get("results", {}).get("expected_rate")
            if not isinstance(_base, (int, float)) or not 0 < _base <= 1:
                _base = None
            _floor = base_rate_relative_floor(_base, legacy_floor=0.45)
            if n >= 12 and _hit_rate < _floor:
                should_reject = True
                checks.append(
                    f"AUTO-REJECT: hit_rate={_hit_rate:.1%} < "
                    f"{_floor:.1%} (base-rate-relative floor; expected base rate "
                    f"{_base if _base is not None else 'unknown'}) with {n} signals — "
                    f"actively losing against its own prior"
                )

        # Low signal rate rejection: hypothesis tested 100+ distinct events but
        # generated signals on <2%. The edge condition is too rare or nonexistent.
        # All existing tiers gate on signal count n, so hypotheses with 500 events
        # and 2 signals (0.4%) slip through every tier indefinitely.
        if not should_reject and status == "backtesting":
            total_events = report.get("total_events", 0)
            if total_events >= AUTO_REJECT_LOW_SIGNAL_MIN_EVENTS:
                signal_rate = n / total_events if total_events > 0 else 0
                if signal_rate < AUTO_REJECT_LOW_SIGNAL_RATE:
                    should_reject = True
                    checks.append(
                        f"AUTO-REJECT: signal rate {n}/{total_events} = "
                        f"{signal_rate:.1%} < {AUTO_REJECT_LOW_SIGNAL_RATE:.0%} — "
                        f"edge condition too rare to be actionable"
                    )

        # Anti-predictive IC rejection: model predicts the WRONG direction.
        # These hypotheses can never promote (IC gate blocks them) but without
        # explicit rejection they sit in backtesting forever as zombies.
        if not should_reject and not used_all_events:
            _ic = report.get("calibration_score", {}).get("information_coefficient")
            if _ic is not None:
                # Waive IC rejection when p-value and hit_rate demonstrate real alpha.
                # IC measures edge-magnitude calibration, not predictive quality.
                # At small n with binary outcomes, IC is dominated by noise.
                _hit_rate = report.get("results", {}).get("hit_rate", 0)
                _ic_waived = (p <= 0.15 and _hit_rate > 0.55) or (_hit_rate > 0.70 and n >= 10)
                if _ic_waived:
                    if _ic < AUTO_REJECT_IC:
                        checks.append(
                            f"IC-WAIVER: IC {_ic:.4f} < {AUTO_REJECT_IC} but p={p:.4f} "
                            f"hit_rate={_hit_rate:.1%} — keeping despite poor IC"
                        )
                    elif _ic < -0.05:
                        checks.append(
                            f"IC-WAIVER: IC {_ic:.4f} below promotion gate but p={p:.4f} "
                            f"hit_rate={_hit_rate:.1%} — waived, not a zombie"
                        )
                elif (_ic < AUTO_REJECT_IC and n >= AUTO_REJECT_IC_MIN_N):
                    should_reject = True
                    checks.append(
                        f"AUTO-REJECT: IC {_ic:.4f} < {AUTO_REJECT_IC} with "
                        f"{n} signals (anti-predictive)"
                    )
                elif (_ic < AUTO_REJECT_IC_STRONG and n >= AUTO_REJECT_IC_STRONG_MIN_N):
                    should_reject = True
                    checks.append(
                        f"AUTO-REJECT: IC {_ic:.4f} < {AUTO_REJECT_IC_STRONG} with "
                        f"{n} signals (strongly anti-predictive)"
                    )
                else:
                    # Zombie detection: IC below promotion gate with enough signals
                    # to be evaluated means this hypothesis can NEVER promote.
                    # The promotion gate requires min_ic=-0.05; if IC is below that
                    # with min_signals worth of data, it's permanently stuck.
                    # Note: _ic_waived is False here (else branch), so no waiver needed.
                    promo_gate = PROMOTION_GATES.get("backtesting→paper_trading", {})
                    gate_ic = promo_gate.get("min_ic", -0.05)
                    gate_n = promo_gate.get("min_signals", 5)
                    if _ic < gate_ic and n >= gate_n:
                        should_reject = True
                        checks.append(
                            f"AUTO-REJECT: IC {_ic:.4f} < promotion gate {gate_ic} with "
                            f"{n} signals — can never promote (zombie)"
                        )

        # Brier zombie detection: brier above promotion gate with enough signals
        # means hypothesis is poorly calibrated and can never promote.
        # WAIVER: if p-value and hit_rate demonstrate real alpha, Brier noise
        # at small n should not trigger auto-rejection. Brier measures calibration
        # quality of fair_prob, not predictive power — a hypothesis can win bets
        # while having slightly miscalibrated probabilities.
        if not should_reject and not used_all_events and status == "backtesting":
            _brier = report.get("calibration_score", {}).get("brier_score")
            if _brier is not None:
                promo_gate = PROMOTION_GATES.get("backtesting→paper_trading", {})
                gate_brier = promo_gate.get("max_brier", 0.28)
                gate_n = promo_gate.get("min_signals", 5)
                _hit_rate = report.get("results", {}).get("hit_rate", 0)
                _brier_waived = (p <= 0.15 and _hit_rate > 0.55) or (_hit_rate > 0.70 and n >= 10)
                if _brier > gate_brier and n >= gate_n:
                    if _brier_waived:
                        checks.append(
                            f"BRIER-WAIVER: Brier {_brier:.4f} > {gate_brier} but p={p:.4f} "
                            f"hit_rate={_hit_rate:.1%} — keeping despite poor calibration"
                        )
                    else:
                        should_reject = True
                        checks.append(
                            f"AUTO-REJECT: Brier {_brier:.4f} > promotion gate {gate_brier} with "
                            f"{n} signals — can never promote (zombie)"
                        )

        # ── PORTFOLIO CORRELATION GATE (paper_trading → live only) ──
        # Reject promotion when the candidate's last-30d signals overlap >X%
        # with an existing LIVE hypothesis's signals on the same event_ids.
        # Correlated bets behave as ONE bet for risk — 21 LIVE signals on the
        # same Dodgers–Giants game are not 21 independent edges.
        # Grandfathered for legacy hypotheses.
        portfolio_overlap = None
        if ready and transition == "paper_trading→live" and not is_legacy:
            try:
                portfolio_overlap = await self._compute_portfolio_overlap(hypothesis_id)
                worst_pair = None
                worst_pct = 0.0
                for other_id, pct in portfolio_overlap.items():
                    if pct > worst_pct:
                        worst_pct = pct
                        worst_pair = other_id
                if worst_pct > MAX_LIVE_OVERLAP_PCT:
                    checks.append(
                        f"FAIL: portfolio_correlation_too_high — {worst_pct:.1%} of "
                        f"last-{PORTFOLIO_OVERLAP_WINDOW_DAYS}d signals overlap with "
                        f"LIVE hyp {worst_pair} (cap: {MAX_LIVE_OVERLAP_PCT:.0%})"
                    )
                    ready = False
                elif portfolio_overlap:
                    max_pair = f" (max={worst_pct:.1%} vs {worst_pair})" if worst_pair else ""
                    checks.append(
                        f"PASS: portfolio_correlation within cap ({MAX_LIVE_OVERLAP_PCT:.0%}){max_pair}"
                    )
            except Exception as e:
                logger.warning(f"portfolio_overlap check failed: {e}")

        # ── PRE-LIVE MONTE CARLO SIMULATION GATE ──
        # feat/bankroll-montecarlo-sim (2026-04-22): before promoting to LIVE,
        # simulate the candidate + all current LIVE hyps across 500 bootstrapped
        # 30-day paths. If 15%-drawdown ruin probability exceeds
        # CALLISTO_MAX_PRE_PROMOTE_RUIN (default 0.02), block promotion.
        sim_result = None
        if (
            ready
            and transition == "paper_trading→live"
            and SIM_GATE_ENABLED
            and not is_legacy
        ):
            try:
                sim_result = simulate_before_promote(
                    hypothesis_id,
                    n_sims=PRE_PROMOTE_N_SIMS,
                    horizon_days=PRE_PROMOTE_HORIZON,
                )
                ruin = sim_result.get("ruin_prob_30d", 0.0)
                if ruin > MAX_PRE_PROMOTE_RUIN:
                    checks.append(
                        f"FAIL: simulation_ruin_risk — ruin_prob_30d={ruin:.3%} > "
                        f"cap {MAX_PRE_PROMOTE_RUIN:.1%}. Expected monthly ROI "
                        f"{sim_result.get('expected_monthly_roi', 0):.2%}, "
                        f"median drawdown {sim_result.get('expected_drawdown', 0):.1%} "
                        f"across {sim_result.get('hyp_count', '?')} hyps."
                    )
                    ready = False
                else:
                    checks.append(
                        f"PASS: simulation_ruin_risk — ruin_prob_30d={ruin:.3%} "
                        f"(cap {MAX_PRE_PROMOTE_RUIN:.1%}), monthly ROI "
                        f"{sim_result.get('expected_monthly_roi', 0):.2%}"
                    )
            except Exception as e:
                # Simulation must not be a silent-failure vector — surface the
                # error so operators see "simulation failed" rather than the
                # gate being skipped invisibly.
                logger.warning(f"simulate_before_promote failed for {hypothesis_id}: {e}")
                checks.append(f"WARN: simulation_gate_error — {e}")

        # ── REGIME-DIVERSITY GATE (paper_trading → live only) ──
        # feat/regime-aware-sizing (2026-04-22): a hypothesis whose resolved
        # paper trades all fell inside ONE regime has not proven it
        # generalizes. e.g. mlb_sandwich_spot_managerial_conservation_under
        # showed +0.57 ROI on 57 REGULAR trades but only 2 PRESEASON — the
        # regular-season fit might be overfitting to that phase.
        # Require ≥2 distinct regimes (sport|season_phase buckets) in the
        # paper-trade sample. Gated by CALLISTO_REGIME_DIVERSITY_GATE.
        # Placed LAST so ``ready`` from upstream checks (p-value, portfolio
        # overlap, simulation) is not masked when operators review the
        # failure categories — the regime gate sets ready=False but doesn't
        # hide any other reason line.
        if (
            transition == "paper_trading→live"
            and not is_legacy
            and os.getenv("CALLISTO_REGIME_DIVERSITY_GATE", "1") == "1"
        ):
            try:
                cur = await self._db.execute(
                    "SELECT DISTINCT sport, game_date FROM paper_trades "
                    "WHERE hypothesis_id = ? "
                    "AND actual_result IN ('won','lost','push','win','loss')",
                    (hypothesis_id,),
                )
                rows = await cur.fetchall()
                regimes_seen: set[str] = set()
                if rows:
                    try:
                        # _classify_phase is pure calendar math — no DB access,
                        # no external calls. Safe to call inside the existing
                        # async tx on self._db.
                        from tools.market_regime import (
                            _classify_phase as _mr_classify,
                            _canonical_sport as _mr_canon,
                        )
                        from datetime import date as _date
                        for sp, gd in rows:
                            if not sp or not gd:
                                continue
                            try:
                                d = _date.fromisoformat(str(gd)[:10])
                            except Exception:
                                continue
                            try:
                                sp_norm = _mr_canon(sp)
                                phase, _win, _bounds = _mr_classify(sp_norm, d)
                                regimes_seen.add(f"{sp_norm}|{phase}")
                            except Exception:
                                continue
                    except Exception as _e:
                        logger.debug(f"regime-diversity import failed: {_e}")
                if rows and len(regimes_seen) < 2:
                    only = next(iter(regimes_seen), "unknown")
                    checks.append(
                        f"FAIL: single_regime_sample — all {len(rows)} resolved "
                        f"paper trades fall in one regime ({only}); need >=2 "
                        f"distinct regimes to promote"
                    )
                    ready = False
                elif rows:
                    checks.append(
                        f"PASS: regime_diversity — {len(regimes_seen)} distinct "
                        f"regimes across {len(rows)} resolved paper trades "
                        f"({sorted(regimes_seen)})"
                    )
            except Exception as e:
                logger.debug(f"regime-diversity gate error for {hypothesis_id}: {e}")

        next_stage = STAGE_ORDER[STAGE_ORDER.index(status) + 1] if ready else None

        return {
            "hypothesis_id": hypothesis_id,
            "current_status": status,
            "ready": ready,
            "next_stage": next_stage,
            "should_reject": should_reject,
            "checks": checks,
            "portfolio_overlap": portfolio_overlap,
            "simulation": sim_result,
            "report_summary": {
                "n": n,
                "p_value": round(p, 6),
                "hit_rate": report.get("results", {}).get("hit_rate", 0),
                "roi_pct": report.get("edge_metrics", {}).get("roi_pct", 0),
                "clv_rate": clv_rate,
            },
        }

