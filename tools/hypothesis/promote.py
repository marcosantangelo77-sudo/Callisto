"""
tools.hypothesis.promote — auto_promote, live review, and data-access helpers.

Split out of tools/hypothesis.py (facade re-exports everything).

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
    async def check_promotion_readiness(
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
