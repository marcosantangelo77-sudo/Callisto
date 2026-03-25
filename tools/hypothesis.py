"""
Hypothesis lifecycle manager — define, test, promote, or reject betting theses.

Pipeline:  draft → backtesting → paper_trading → live → retired
           ↘ rejected (at any stage if data actively disproves)

Every promotion gate requires:
  - Minimum sample size met
  - Statistical significance (p < threshold)
  - Positive CLV rate

No scipy dependency — all statistical tests implemented in pure Python.
"""

import json
import logging
import math
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

import aiosqlite
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("callisto.hypothesis")

DB_PATH = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")

# Promotion gates: {transition: {min_n, max_p, min_clv_rate, extras}}
# Note: min_signals must be achievable given current data volume.
# Historical odds data is mostly single-book consensus → structural 1% signal rate.
# With 50 events per hypothesis, expect ~0.5 signals. Need gates that don't require
# thousands of events. Binomial test at n=5, p<0.10 is still statistically meaningful.
# Paper→live gate is the real quality filter (CLV, drawdown, 14-day duration).
PROMOTION_GATES = {
    "backtesting→paper_trading": {
        "min_signals": 5,          # lowered from 30 — 1% signal rate needs ~500 events for 5
        "max_p_value": 0.10,       # relax for backtest→paper; tighten at live gate
        "min_clv_rate": 0.0,       # CLV not available in historical backtests
        "min_sharpe": 0.0,         # don't gate on Sharpe for first promotion
    },
    "paper_trading→live": {
        "min_signals": 20,         # lowered from 50 — real filter is CLV + drawdown
        "max_p_value": 0.05,
        "min_clv_rate": 0.50,
        "max_drawdown": 0.30,
        "min_days": 14,
    },
}

# Auto-rejection: if p > 0.20 with sufficient N, the data disproves the thesis
AUTO_REJECT_P = 0.30
AUTO_REJECT_MIN_N = 30             # lowered from 50 — reject faster to clear queue

STAGE_ORDER = ["draft", "backtesting", "paper_trading", "live", "retired"]


# ──────────────────────────────────────────────────
# PURE PYTHON STATISTICS
# ──────────────────────────────────────────────────

def _erfc(x: float) -> float:
    """Complementary error function approximation (Abramowitz & Stegun 7.1.26)."""
    t = 1.0 / (1.0 + 0.3275911 * abs(x))
    poly = t * (0.254829592 + t * (-0.284496736 + t * (1.421413741 +
           t * (-1.453152027 + t * 1.061405429))))
    result = poly * math.exp(-x * x)
    return result if x >= 0 else 2.0 - result


def _norm_cdf(x: float) -> float:
    """Standard normal CDF."""
    return 0.5 * _erfc(-x / math.sqrt(2))


def _norm_sf(x: float) -> float:
    """Standard normal survival function: P(Z > x)."""
    return 1.0 - _norm_cdf(x)


def binomial_pvalue(wins: int, total: int, expected_rate: float) -> float:
    """
    One-sided binomial test using normal approximation with continuity correction.
    H0: true win rate = expected_rate
    H1: true win rate > expected_rate
    Valid for N > 30.
    """
    if total < 1 or expected_rate <= 0 or expected_rate >= 1:
        return 1.0
    mean = total * expected_rate
    std = math.sqrt(total * expected_rate * (1 - expected_rate))
    if std < 1e-9:
        return 1.0
    z = (wins - 0.5 - mean) / std
    return _norm_sf(z)


def ttest_one_sample(values: list[float]) -> tuple[float, float]:
    """
    One-sample t-test: is mean(values) significantly > 0?
    Returns (t_statistic, p_value).
    Uses normal approximation (valid for N > 30).
    """
    n = len(values)
    if n < 2:
        return 0.0, 1.0
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    if var < 1e-12:
        if abs(mean) < 1e-12:
            return 0.0, 1.0
        # Zero variance but nonzero mean: perfectly significant
        return float("inf") if mean > 0 else float("-inf"), 0.0 if mean > 0 else 1.0
    se = math.sqrt(var / n)
    t = mean / se
    p = _norm_sf(t)
    return t, p


def z_score(observed: int, total: int, expected_rate: float) -> float:
    """Z-score for observed vs expected proportion."""
    if total < 1 or expected_rate <= 0 or expected_rate >= 1:
        return 0.0
    std = math.sqrt(expected_rate * (1 - expected_rate) / total)
    if std < 1e-9:
        return 0.0
    return (observed / total - expected_rate) / std


def sharpe_ratio(returns: list[float]) -> float:
    """Sharpe ratio (not annualized — daily or per-bet)."""
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    std = math.sqrt(var) if var > 0 else 0
    return mean / std if std > 1e-9 else 0.0


def max_drawdown(returns: list[float]) -> float:
    """Maximum drawdown from a series of per-bet returns."""
    if not returns:
        return 0.0
    cumulative = 0.0
    peak = 0.0
    worst = 0.0
    for r in returns:
        cumulative += r
        if cumulative > peak:
            peak = cumulative
        dd = (peak - cumulative) / max(peak, 1.0) if peak > 0 else 0.0
        if dd > worst:
            worst = dd
    return worst


def calibration_bins(
    predictions: list[tuple[float, bool]], n_bins: int = 10,
) -> list[dict]:
    """
    Bin predictions by predicted probability, compare to observed hit rate.
    Returns list of {bin_start, bin_end, count, predicted_avg, observed_rate}.
    """
    if not predictions:
        return []
    sorted_preds = sorted(predictions, key=lambda x: x[0])
    bin_size = max(len(sorted_preds) // n_bins, 1)
    bins = []
    for i in range(0, len(sorted_preds), bin_size):
        chunk = sorted_preds[i:i + bin_size]
        probs = [p for p, _ in chunk]
        outcomes = [o for _, o in chunk]
        bins.append({
            "bin_start": round(min(probs), 4),
            "bin_end": round(max(probs), 4),
            "count": len(chunk),
            "predicted_avg": round(sum(probs) / len(probs), 4),
            "observed_rate": round(sum(outcomes) / len(outcomes), 4) if outcomes else 0,
        })
    return bins


# ──────────────────────────────────────────────────
# HYPOTHESIS MANAGER
# ──────────────────────────────────────────────────

class HypothesisManager:
    """Manages hypothesis lifecycle: draft → backtest → paper_trade → live → retired."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def initialize(self) -> None:
        self._db = await aiosqlite.connect(self.db_path)
        await self._db.execute("PRAGMA busy_timeout = 10000")
        logger.info("Hypothesis manager initialized")

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    # ── CRUD ──

    async def create_hypothesis(
        self,
        name: str,
        thesis: str,
        sport: str,
        market_type: str,
        model_config: dict,
        edge_threshold: float = 0.01,
        min_sample_size: int = 50,
        significance_level: float = 0.05,
        notes: str = "",
    ) -> str:
        """Create a new hypothesis. Returns hypothesis_id.

        If a hypothesis with the same name already exists, returns the existing
        hypothesis_id instead of creating a duplicate.

        Temporal metadata in model_config (set by temporal_analysis.py):
            - training_period_start: First date used for pattern discovery
            - training_period_end: Last date used for pattern discovery
            - temporal_split_gap_days: Buffer days between train/test (default 7)

        These fields are used by backtest.py to enforce temporal isolation.
        """
        # ── Deduplication guard: skip if name already exists ──
        cursor = await self._db.execute(
            "SELECT hypothesis_id FROM hypotheses WHERE name = ? LIMIT 1",
            (name,),
        )
        existing = await cursor.fetchone()
        if existing:
            logger.debug(
                f"Hypothesis '{name}' already exists as {existing[0]} — skipping duplicate"
            )
            return existing[0]

        hid = str(uuid.uuid4())[:12]
        now = datetime.now(timezone.utc).isoformat()

        # Log whether temporal metadata is present
        has_temporal = bool(model_config.get("training_period_end"))
        if not has_temporal:
            logger.warning(
                f"Hypothesis '{name}' created WITHOUT temporal metadata in model_config. "
                "Backtest engine will not enforce temporal isolation. "
                "Use temporal_analysis.generate_hypotheses_from_analysis() to auto-populate."
            )
        else:
            logger.info(
                f"Hypothesis '{name}' has temporal metadata: "
                f"training {model_config.get('training_period_start')} "
                f"to {model_config.get('training_period_end')}, "
                f"gap {model_config.get('temporal_split_gap_days', 7)}d"
            )

        await self._db.execute(
            "INSERT INTO hypotheses "
            "(hypothesis_id, name, thesis, sport, market_type, model_config, "
            "edge_threshold, status, min_sample_size, significance_level, "
            "created_at, updated_at, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?)",
            (hid, name, thesis, sport, market_type, json.dumps(model_config),
             edge_threshold, min_sample_size, significance_level, now, now, notes),
        )
        await self._db.commit()
        logger.info(f"Hypothesis created: {hid} — {name}")
        return hid

    async def get_hypothesis(self, hypothesis_id: str) -> Optional[dict]:
        cursor = await self._db.execute(
            "SELECT * FROM hypotheses WHERE hypothesis_id = ?",
            (hypothesis_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cursor.description]
        h = dict(zip(cols, row))
        h["model_config"] = json.loads(h["model_config"]) if h["model_config"] else {}
        return h

    async def list_hypotheses(self, status: Optional[str] = None) -> list[dict]:
        if status:
            cursor = await self._db.execute(
                "SELECT * FROM hypotheses WHERE status = ? ORDER BY updated_at DESC",
                (status,),
            )
        else:
            cursor = await self._db.execute(
                "SELECT * FROM hypotheses ORDER BY updated_at DESC"
            )
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        result = []
        for row in rows:
            h = dict(zip(cols, row))
            h["model_config"] = json.loads(h["model_config"]) if h["model_config"] else {}
            result.append(h)
        return result

    async def update_status(
        self, hypothesis_id: str, new_status: str, promoted_by: str = "manual",
    ) -> dict:
        """Move a hypothesis to a new status."""
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            "UPDATE hypotheses SET status = ?, updated_at = ?, "
            "promoted_at = ?, promoted_by = ? WHERE hypothesis_id = ?",
            (new_status, now, now, promoted_by, hypothesis_id),
        )
        await self._db.commit()
        logger.info(f"Hypothesis {hypothesis_id} → {new_status} (by {promoted_by})")
        return {"hypothesis_id": hypothesis_id, "new_status": new_status}

    # ── STATISTICAL EVALUATION ──

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

        # Statistical tests
        p_binomial = binomial_pvalue(wins, decided, expected_rate)
        t_stat, p_ttest = ttest_one_sample(returns)
        z = z_score(wins, decided, expected_rate)
        sr = sharpe_ratio(returns)
        mdd = max_drawdown(returns)

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
            },
            "clv": {
                "avg_clv": round(avg_clv, 4),
                "positive_clv_rate": round(positive_clv_rate, 4),
                "clv_sample_size": len(clv_values),
            },
            "risk": {
                "sharpe_ratio": round(sr, 4),
                "max_drawdown": round(mdd, 4),
            },
            "calibration": cal_bins,
            "recommendation": rec,
        }

        # Store in hypothesis_stats
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            "INSERT INTO hypothesis_stats "
            "(hypothesis_id, stage, computed_at, total_n, signals_n, win, loss, push_, "
            "hit_rate, avg_edge, avg_ev, avg_clv, positive_clv_rate, roi_pct, "
            "sharpe, max_drawdown, p_value, is_significant) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (hypothesis_id, stage, now, resolved, resolved, wins, losses, pushes,
             hit_rate, avg_edge, avg_ev, avg_clv, positive_clv_rate, roi,
             sr, mdd, p_binomial, is_significant),
        )
        await self._db.commit()

        return report

    async def check_promotion_readiness(self, hypothesis_id: str) -> dict:
        """Check if a hypothesis meets criteria to advance to next stage."""
        h = await self.get_hypothesis(hypothesis_id)
        if not h:
            return {"error": "Hypothesis not found"}

        status = h["status"]
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
            stage = "paper_trade"
        else:
            return {"ready": False, "reason": f"Unknown status: {status}"}

        gate = PROMOTION_GATES[transition]
        report = await self.evaluate_significance(hypothesis_id, stage)

        checks = []
        ready = True

        # Sample size
        n = report.get("sample_size", 0)
        required_n = gate["min_signals"]
        if n < required_n:
            checks.append(f"FAIL: {n}/{required_n} signals")
            ready = False
        else:
            checks.append(f"PASS: {n}/{required_n} signals")

        # P-value
        p = report.get("significance", {}).get("p_value_binomial", 1.0)
        max_p = gate["max_p_value"]
        if p > max_p:
            checks.append(f"FAIL: p-value {p:.4f} > {max_p}")
            ready = False
        else:
            checks.append(f"PASS: p-value {p:.4f} < {max_p}")

        # CLV rate
        clv_rate = report.get("clv", {}).get("positive_clv_rate", 0)
        min_clv = gate["min_clv_rate"]
        if clv_rate < min_clv:
            checks.append(f"FAIL: CLV rate {clv_rate:.1%} < {min_clv:.0%}")
            ready = False
        else:
            checks.append(f"PASS: CLV rate {clv_rate:.1%} >= {min_clv:.0%}")

        # Sharpe (backtest only)
        if "min_sharpe" in gate:
            sr = report.get("risk", {}).get("sharpe_ratio", 0)
            if sr < gate["min_sharpe"]:
                checks.append(f"FAIL: Sharpe {sr:.2f} < {gate['min_sharpe']}")
                ready = False
            else:
                checks.append(f"PASS: Sharpe {sr:.2f}")

        # Max drawdown (paper trade only)
        if "max_drawdown" in gate:
            mdd = report.get("risk", {}).get("max_drawdown", 1.0)
            if mdd > gate["max_drawdown"]:
                checks.append(f"FAIL: Drawdown {mdd:.1%} > {gate['max_drawdown']:.0%}")
                ready = False
            else:
                checks.append(f"PASS: Drawdown {mdd:.1%}")

        # Auto-rejection check
        should_reject = (
            p > AUTO_REJECT_P and n > AUTO_REJECT_MIN_N
        )

        next_stage = STAGE_ORDER[STAGE_ORDER.index(status) + 1] if ready else None

        return {
            "hypothesis_id": hypothesis_id,
            "current_status": status,
            "ready": ready,
            "next_stage": next_stage,
            "should_reject": should_reject,
            "checks": checks,
            "report_summary": {
                "n": n,
                "p_value": round(p, 6),
                "hit_rate": report.get("results", {}).get("hit_rate", 0),
                "roi_pct": report.get("edge_metrics", {}).get("roi_pct", 0),
                "clv_rate": clv_rate,
            },
        }

    async def auto_promote(self, hypothesis_id: str) -> dict:
        """If criteria met, advance to next stage. Returns result.

        Hard gates (cannot be bypassed by statistical tests):
          - backtesting → paper_trading: backtest_events MUST exist for this hypothesis
            AND meet minimum sample size (>20) with p-value < 0.05
          - paper_trading → live: paper_trades MUST exist AND show positive ROI

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
                    "SELECT COUNT(*) FROM backtest_events WHERE hypothesis_id = ?",
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
                await self._db.execute(
                    "UPDATE hypotheses SET model_config = ? WHERE hypothesis_id = ?",
                    (json.dumps(model_config), hypothesis_id),
                )
                await self._db.commit()

                # Before rejecting, check if the edge threshold is too high.
                # If events exist but 0 signals, the threshold may be suppressing
                # valid edges. Check edge distribution first.
                if total_events > 0 and eval_cycles >= 5:
                    edge_diag = await self._diagnose_edge_threshold(hypothesis_id)
                    if edge_diag.get("threshold_too_high"):
                        # Auto-lower threshold and give more cycles
                        new_threshold = edge_diag["recommended_threshold"]
                        model_config["edge_threshold"] = new_threshold
                        model_config["evaluate_cycles"] = 0  # Reset cycle count
                        await self._db.execute(
                            "UPDATE hypotheses SET edge_threshold = ?, model_config = ? "
                            "WHERE hypothesis_id = ?",
                            (new_threshold, json.dumps(model_config), hypothesis_id),
                        )
                        await self._db.commit()

                        # Retroactively update signal_generated on existing events
                        # so evaluate_significance can see them without re-backtesting
                        # NOTE: must use `edge` column (probability edge), NOT `ev_pct` (expected value)
                        # — consistent with backtest.py:1161 where is_signal = edge >= edge_threshold
                        cursor = await self._db.execute(
                            "UPDATE backtest_events "
                            "SET signal_generated = CASE WHEN edge >= ? THEN 1 ELSE 0 END "
                            "WHERE hypothesis_id = ?",
                            (new_threshold, hypothesis_id),
                        )
                        await self._db.commit()
                        retroactive_count = cursor.rowcount
                        # Count how many are now signals
                        sig_cursor = await (await self._db.execute(
                            "SELECT COUNT(*) FROM backtest_events "
                            "WHERE hypothesis_id = ? AND signal_generated = 1",
                            (hypothesis_id,),
                        )).fetchone()
                        new_signals = sig_cursor[0] if sig_cursor else 0

                        logger.info(
                            f"Hypothesis {hypothesis_id}: lowered edge_threshold "
                            f"from {edge_diag['current_threshold']:.3f} to {new_threshold:.3f} "
                            f"(max observed edge: {edge_diag['max_edge']:.3f}). "
                            f"Retroactively updated {retroactive_count} events → {new_signals} signals"
                        )
                        # If retroactive update created enough signals, immediately
                        # check promotion readiness instead of returning early.
                        if new_signals >= PROMOTION_GATES["backtesting→paper_trading"]["min_signals"]:
                            logger.info(
                                f"Hypothesis {hypothesis_id}: {new_signals} signals after "
                                f"threshold adjustment — checking promotion readiness now"
                            )
                            readiness = await self.check_promotion_readiness(hypothesis_id)
                            if readiness.get("should_reject"):
                                await self.update_status(hypothesis_id, "rejected", "auto")
                                return {"action": "rejected", "reason": "Data actively disproves thesis after threshold adjustment."}
                            if readiness.get("ready"):
                                next_stage = readiness["next_stage"]
                                await self.update_status(hypothesis_id, next_stage, "auto")
                                return {"action": "promoted", "new_status": next_stage}
                            return {
                                "action": "threshold_adjusted",
                                "reason": (
                                    f"Threshold lowered to {new_threshold:.1%}, "
                                    f"{new_signals} signals now exist, but promotion "
                                    f"check not yet passing."
                                ),
                                "checks": readiness.get("checks", []),
                            }
                        else:
                            return {
                                "action": "threshold_adjusted",
                                "reason": (
                                    f"0 signals in {total_events} events because edge_threshold "
                                    f"({edge_diag['current_threshold']:.1%}) exceeds max observed "
                                    f"edge ({edge_diag['max_edge']:.1%}). Lowered to "
                                    f"{new_threshold:.1%} and reset eval cycles. "
                                    f"Retroactively updated {new_signals} events to signals."
                                ),
                            }

                # Use 10 cycles (not 5) before rejecting — gives more time for
                # data collection, especially for sports with few games.
                if eval_cycles >= 10:
                    if total_events > 0:
                        # Check data quality before rejecting — 1-book devig
                        # produces garbage edges, don't blame the hypothesis.
                        avg_books = await self._avg_books_used(hypothesis_id)
                        if avg_books is not None and avg_books < 2.0:
                            return {
                                "action": "held",
                                "reason": (
                                    f"0 signals in {total_events} events but avg "
                                    f"books_used={avg_books:.1f} — devig data too thin "
                                    f"to produce reliable edges. Holding for better data."
                                ),
                            }

                        # Check run-level stats before rejecting — the run may
                        # have real resolved data even if 0 signals at threshold.
                        run_stats = await self._get_best_run_stats(hypothesis_id)
                        if run_stats and run_stats.get("hit_rate") is not None:
                            return {
                                "action": "held",
                                "reason": (
                                    f"0 signals at threshold but run-level data exists: "
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

                        await self.update_status(
                            hypothesis_id, "rejected",
                            "auto:no_edge_after_backtest",
                        )
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

                        await self.update_status(
                            hypothesis_id, "rejected",
                            "auto:no_backtest_data_after_5_cycles",
                        )
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
            if n < min_for_promotion:
                return {
                    "action": "held",
                    "reason": f"Only {n}/{min_for_promotion} signal events — need more before promotion.",
                }

        elif status == "paper_trading":
            trades = await self._get_paper_trades(hypothesis_id)
            if not trades:
                return {
                    "action": "held",
                    "reason": "No paper trades exist — cannot promote to live.",
                }
            # Check positive ROI
            returns = []
            for t in trades:
                if t.get("actual_result") == "won":
                    from tools.math_utils import american_to_decimal
                    dec = american_to_decimal(t["book_odds_american"])
                    returns.append(dec - 1)
                elif t.get("actual_result") == "lost":
                    returns.append(-1.0)
                elif t.get("actual_result") == "push":
                    returns.append(0.0)
            if returns:
                roi = sum(returns) / len(returns)
                if roi <= 0:
                    return {
                        "action": "held",
                        "reason": f"Paper trade ROI is {roi:.2%} — need positive ROI for live promotion.",
                    }

        # ── Standard readiness check (statistical significance, gates, etc.) ──
        readiness = await self.check_promotion_readiness(hypothesis_id)

        if readiness.get("should_reject"):
            await self.update_status(hypothesis_id, "rejected", "auto")
            return {"action": "rejected", "reason": "Data actively disproves thesis."}

        if readiness.get("ready"):
            next_stage = readiness["next_stage"]
            await self.update_status(hypothesis_id, next_stage, "auto")
            return {"action": "promoted", "new_status": next_stage}

        return {"action": "held", "checks": readiness.get("checks", [])}

    # ── DATA ACCESSORS ──

    async def _get_backtest_signals(self, hypothesis_id: str) -> list[dict]:
        """Get all backtest events that generated signals."""
        cursor = await self._db.execute(
            "SELECT * FROM backtest_events "
            "WHERE hypothesis_id = ? AND signal_generated = 1 "
            "ORDER BY game_date",
            (hypothesis_id,),
        )
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in rows]

    async def _get_backtest_resolved(self, hypothesis_id: str) -> list[dict]:
        """Get all resolved backtest events (regardless of signal_generated).

        Fallback for evaluate_significance when 0 signal events exist —
        lets us determine if the thesis has any merit before auto-rejecting.
        """
        cursor = await self._db.execute(
            "SELECT * FROM backtest_events "
            "WHERE hypothesis_id = ? AND actual_result IS NOT NULL "
            "ORDER BY game_date",
            (hypothesis_id,),
        )
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in rows]

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
        """Get all paper trades for a hypothesis."""
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
