"""Hypothesis statistical evaluation extracted from significance mixin.

``HypothesisSignificanceMixin.evaluate_significance`` stays defined on the
mixin as a thin delegate so ``hasattr`` pins keep passing. The evaluation
body lives here so ``tools/hypothesis/significance.py`` can keep shrinking
without changing behaviour.

``check_promotion_readiness`` stays on ``HypothesisSignificanceMixin`` —
do not copy it onto ``HypothesisPromotionMixin``. Live promotion readiness
is significance-mixin only.

Do not import tools.autonomous. Do not arm live betting. Do not add live
to paper-signal.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from tools.hypothesis.config import (
    AUTO_REJECT_P,
    AUTO_REJECT_MIN_N,
)
from tools.hypothesis.stats import (
    binomial_pvalue,
    calibration_bins,
    max_drawdown,
    sharpe_ratio,
    ttest_one_sample,
    z_score,
)
from tools.market_microstructure import (
    sortino_ratio as _sortino_ratio,
    brier_score as _brier_score,
    information_coefficient as _information_coefficient,
)

logger = logging.getLogger("callisto.hypothesis")


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
