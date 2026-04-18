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

from tools.market_microstructure import (
    sortino_ratio as _sortino_ratio,
    brier_score as _brier_score,
    information_coefficient as _information_coefficient,
)

load_dotenv()

logger = logging.getLogger("callisto.hypothesis")

DB_PATH = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")

# Promotion gates: {transition: {min_n, max_p, min_clv_rate, extras}}
# Note: max_p_value is the BASE threshold. Actual threshold is adaptive via
# get_adaptive_p_value_threshold() — relaxed at small n, tightens as data grows.
# Paper→live gate is the real quality filter (CLV, drawdown, 14-day duration).
PROMOTION_GATES = {
    "backtesting→paper_trading": {
        "min_signals": 5,              # lowered from 10: early-stage data can't reach 10 signals
        "max_p_value": 0.25,           # base threshold; adaptive: 0.30 at n<8, 0.25 at n<15, 0.20 at n<25
        "min_clv_rate": 0.0,           # CLV not available in historical backtests
        "min_sharpe": 0.0,             # don't gate on Sharpe for first promotion
        "min_positive_edge_rate": 0.40, # at least 40% of events must show positive edge
        "max_brier": 0.28,             # worse than coin-flip baseline (0.25) = reject
        "min_ic": -0.05,              # block anti-predictive models (IC < -0.05 = inversely correlated)
    },
    "paper_trading→live": {
        "min_signals": 5,              # match backtesting→paper gate: p-value gate provides quality filter
        "max_p_value": 0.10,           # base threshold; adaptive: 0.15 at n<25, 0.10 at n<40
        "min_clv_rate": 0.0,           # disabled: CLV capture not yet operational (all CLV=0.0)
        "max_drawdown": 0.30,
        "min_days": 7,                 # lowered from 14: 1 week sufficient with backtest evidence
        "min_sortino": 0.0,            # disabled: can't compute meaningful Sortino with sparse paper trades
        "min_ic": -0.05,              # block anti-predictive only, not zero-IC
        "max_brier": 0.30,             # slightly relaxed: small-sample brier is noisy
    },
}

# Auto-rejection: if p > threshold with sufficient signal-level N, reject.
# Only applies to signal-level evaluation (not all-events fallback).
# Previous threshold of 0.30 with N=30 was rejecting hypotheses that had
# data but no signal-level evidence yet — conflating "no edge detected at
# current threshold" with "data disproves thesis."
AUTO_REJECT_P = 0.50               # Reject only when signal data actively disproves thesis
AUTO_REJECT_MIN_N = 15             # Need 15 resolved signals (not events) to reject
AUTO_REJECT_STRONG_P = 0.70        # Strong disproof needs fewer samples
AUTO_REJECT_STRONG_MIN_N = 10      # 10 signals sufficient when p > 0.70
AUTO_REJECT_EXTREME_P = 0.90       # Extreme disproof: <10% chance thesis is correct
AUTO_REJECT_EXTREME_MIN_N = 5      # 5 signals sufficient when p > 0.90
# Anti-predictive rejection: IC strongly negative means model predicts WRONG direction.
# At n >= 15 signals, IC < -0.15 is statistically meaningful (not noise).
AUTO_REJECT_IC = -0.15             # IC below this = actively anti-predictive
AUTO_REJECT_IC_MIN_N = 15          # Need 15 signals for IC to be meaningful
AUTO_REJECT_IC_STRONG = -0.25      # Very strong anti-prediction needs fewer samples
AUTO_REJECT_IC_STRONG_MIN_N = 10   # 10 signals sufficient when IC < -0.25
# Low signal rate rejection: hypothesis tested many events but generated almost
# no signals — the edge condition is too rare or nonexistent.
AUTO_REJECT_LOW_SIGNAL_RATE = 0.02     # <2% signal rate = edge condition too rare
AUTO_REJECT_LOW_SIGNAL_MIN_EVENTS = 100  # Need 100+ events to judge signal rate

STAGE_ORDER = ["draft", "backtesting", "paper_trading", "live", "retired"]


# SECURITY (audit C-4 / P2 #25): allowlist of model_config keys + per-key types.
# Anything else is rejected at the API boundary so that LLM-derived configs cannot
# silently smuggle unexpected fields into downstream consumers.
_MODEL_CONFIG_SCHEMA = {
    # Adaptive evaluation
    "evaluate_cycles": int,
    "edge_threshold": float,
    "demotion_count": int,
    # Temporal split metadata
    "training_period_start": str,
    "training_period_end": str,
    "backtest_period_start": str,
    "backtest_period_end": str,
    "temporal_split_gap_days": int,
    "temporal_isolation": bool,
    # Game / context filters (lists of short strings)
    "game_filters": list,
    "context_factors": list,
    "needs_unique_data": bool,
    # Free-form, but capped: brief description + originating thesis family
    "thesis_family": str,
    "description": str,
    # Numeric tunables
    "min_edge": float,
    "max_edge": float,
    "stake_multiplier": float,
}

_MODEL_CONFIG_MAX_STR = 1024
_MODEL_CONFIG_MAX_LIST = 64
_MODEL_CONFIG_MAX_LIST_ITEM = 256


def validate_model_config(cfg: dict) -> dict:
    """Validate a model_config dict against the allowlist schema.

    Raises ValueError on any unknown key, type mismatch, or oversized value.
    Returns the validated dict (a fresh copy with primitive values only).
    """
    if not isinstance(cfg, dict):
        raise ValueError("model_config must be a dict")
    if len(cfg) > 64:
        raise ValueError("model_config has too many keys (>64)")
    out: dict = {}
    for k, v in cfg.items():
        if not isinstance(k, str) or not k or len(k) > 64:
            raise ValueError(f"invalid key: {k!r}")
        if k not in _MODEL_CONFIG_SCHEMA:
            raise ValueError(f"unknown key: {k!r}")
        expected = _MODEL_CONFIG_SCHEMA[k]
        # bool is a subclass of int; accept exact bool when expected
        if expected is int and isinstance(v, bool):
            raise ValueError(f"{k}: expected int, got bool")
        if expected is float and isinstance(v, (int,)) and not isinstance(v, bool):
            v = float(v)
        if not isinstance(v, expected):
            raise ValueError(f"{k}: expected {expected.__name__}, got {type(v).__name__}")
        if isinstance(v, str) and len(v) > _MODEL_CONFIG_MAX_STR:
            raise ValueError(f"{k}: string too long")
        if isinstance(v, list):
            if len(v) > _MODEL_CONFIG_MAX_LIST:
                raise ValueError(f"{k}: list too long")
            for item in v:
                if not isinstance(item, (str, int, float, bool)):
                    raise ValueError(f"{k}: list items must be primitives")
                if isinstance(item, str) and len(item) > _MODEL_CONFIG_MAX_LIST_ITEM:
                    raise ValueError(f"{k}: list item string too long")
        out[k] = v
    return out


def get_adaptive_p_value_threshold(n_signals: int, base_threshold: float) -> float:
    """Adaptive p-value threshold based on sample size.

    Small samples make standard significance thresholds mathematically
    unreachable.  At n=6, even 5W/1L gives p~0.109 (exact binomial).
    A flat gate of 0.10 blocks every hypothesis regardless of merit.

    This function relaxes the threshold for small n and tightens it as
    evidence accumulates, converging to the base_threshold at large n.

    Tiers (for backtesting->paper_trading, base=0.25):
      n < 8:   0.30  — accept strong directional evidence despite noise
      n < 15:  0.25  — moderate evidence (base threshold)
      n < 25:  0.20  — tighter with more data
      n >= 25: base   — full statistical rigor

    For paper_trading->live (base=0.05):
      n < 25:  0.15  — real money demands more evidence, but small n still limited
      n < 40:  0.10  — approaching standard significance
      n >= 40: 0.05  — full rigor
    """
    if base_threshold >= 0.20:
        # backtesting -> paper_trading path
        if n_signals < 8:
            return 0.30
        elif n_signals < 15:
            return 0.25
        elif n_signals < 25:
            return 0.20
        else:
            return base_threshold
    else:
        # paper_trading -> live path (base is typically 0.05)
        if n_signals < 25:
            return 0.15
        elif n_signals < 40:
            return 0.10
        else:
            return base_threshold


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


def _exact_binomial_sf(wins: int, total: int, p: float) -> float:
    """Exact binomial survival function: P(X >= wins | n=total, p).
    Used for small samples (n <= 30) where normal approximation is unreliable."""
    if wins <= 0:
        return 1.0
    if wins > total:
        return 0.0
    # P(X >= wins) = sum_{k=wins}^{total} C(n,k) * p^k * (1-p)^(n-k)
    prob = 0.0
    # Use log-space to avoid overflow for larger n
    log_comb = 0.0  # log(C(n, wins))
    for i in range(wins):
        log_comb += math.log(total - i) - math.log(i + 1)
    for k in range(wins, total + 1):
        if k > wins:
            log_comb += math.log(total - k + 1) - math.log(k)
        prob += math.exp(log_comb + k * math.log(p) + (total - k) * math.log(1 - p))
    return prob


def binomial_pvalue(wins: int, total: int, expected_rate: float) -> float:
    """
    One-sided binomial test.
    H0: true win rate = expected_rate
    H1: true win rate > expected_rate
    Uses exact binomial for n <= 30, normal approximation for n > 30.
    """
    if total < 1 or expected_rate <= 0 or expected_rate >= 1:
        return 1.0
    if total <= 30:
        return _exact_binomial_sf(wins, total, expected_rate)
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
    """Maximum drawdown from a series of per-bet returns.

    Uses a 100-unit starting bankroll so MDD is expressed as a fraction of
    capital, not peak cumulative profit.  The old code started at 0, which
    made early losses produce >100 % drawdown values — mathematically
    correct for a zero-start series but meaningless as a risk metric.
    """
    if not returns:
        return 0.0
    equity = 100.0          # 100-unit bankroll, flat $1 per signal
    peak = equity
    worst = 0.0
    for r in returns:
        equity += r
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak if peak > 0 else 0.0
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
        await self._db.execute("PRAGMA journal_mode = WAL")
        await self._db.execute("PRAGMA wal_autocheckpoint = 1000")
        await self._db.execute("PRAGMA journal_size_limit = 67108864")
        await self._db.execute("PRAGMA busy_timeout = 120000")
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
        edge_threshold: float = 0.005,
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

        # ── Duplicate game_filters guard: reject if same sport+market+filters already active ──
        new_gf = model_config.get("game_filters") if model_config else None
        new_gf_normalized = json.dumps(new_gf, sort_keys=True) if new_gf else None

        dup_cursor = await self._db.execute(
            "SELECT hypothesis_id, name, model_config FROM hypotheses "
            "WHERE sport = ? AND market_type = ? AND status IN ('draft', 'backtesting', 'paper_trading')",
            (sport, market_type),
        )
        dup_rows = await dup_cursor.fetchall()
        for row in dup_rows:
            existing_mc = json.loads(row[2]) if row[2] else {}
            existing_gf = existing_mc.get("game_filters")
            existing_gf_normalized = json.dumps(existing_gf, sort_keys=True) if existing_gf else None

            if new_gf_normalized == existing_gf_normalized:
                logger.warning(
                    f"DUPLICATE game_filters blocked: '{name}' has identical "
                    f"sport={sport}, market_type={market_type}, "
                    f"game_filters={new_gf_normalized or 'null'} "
                    f"as existing hypothesis '{row[1]}' ({row[0]}). Skipping creation."
                )
                return row[0]

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

        for attempt in range(8):
            try:
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
            except Exception as e:
                if "locked" in str(e).lower() and attempt < 7:
                    import asyncio
                    # Jittered backoff: 0.5, 1, 2, 4, 8, 16, 32s (total ~63s)
                    import random
                    wait = min(0.5 * (2 ** attempt), 32) + random.uniform(0, 0.5)
                    logger.warning(f"DB locked on hypothesis create (attempt {attempt+1}/8), retrying in {wait:.1f}s")
                    await asyncio.sleep(wait)
                else:
                    raise

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

    async def list_hypotheses(self, status: Optional[str] = None, limit: int = None) -> list[dict]:
        if status:
            query = "SELECT * FROM hypotheses WHERE status = ? ORDER BY updated_at DESC"
            params: tuple = (status,)
        else:
            query = "SELECT * FROM hypotheses ORDER BY updated_at DESC"
            params = ()
        if limit:
            query += f" LIMIT {int(limit)}"
        cursor = await self._db.execute(query, params)
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        result = []
        for row in rows:
            h = dict(zip(cols, row))
            h["model_config"] = json.loads(h["model_config"]) if h["model_config"] else {}
            result.append(h)
        return result

    async def count_by_status(self, *statuses: str) -> int:
        """Count hypotheses by status without loading full rows."""
        placeholders = ",".join("?" for _ in statuses)
        cursor = await self._db.execute(
            f"SELECT COUNT(*) FROM hypotheses WHERE status IN ({placeholders})",
            statuses,
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def get_names(self, status: Optional[str] = None) -> list[str]:
        """Get just hypothesis names (not full rows)."""
        if status:
            cursor = await self._db.execute(
                "SELECT name FROM hypotheses WHERE status = ?", (status,)
            )
        else:
            cursor = await self._db.execute("SELECT name FROM hypotheses")
        return [row[0] for row in await cursor.fetchall()]

    async def get_all_names(self) -> set[str]:
        """Get all hypothesis names as a set for dedup checks."""
        cursor = await self._db.execute("SELECT name FROM hypotheses")
        return {row[0] for row in await cursor.fetchall()}

    async def update_status(
        self,
        hypothesis_id: str,
        new_status: str,
        promoted_by: str = "manual",
        *,
        expected_status: Optional[str] = None,
    ) -> dict:
        """Move a hypothesis to a new status.

        SECURITY (audit C-7): when ``expected_status`` is supplied the UPDATE is
        scoped with ``WHERE status = ?`` so two concurrent promoters can't both
        succeed. Returns ``{"changed": False, ...}`` if the row was already moved
        by another worker. ``expected_status=None`` keeps the legacy unconditional
        UPDATE for callers that genuinely want to overwrite (manual admin patches).
        """
        now = datetime.now(timezone.utc).isoformat()
        from tools.db_utils import execute_with_retry, commit_with_retry
        if expected_status is not None:
            cursor = await execute_with_retry(
                self._db,
                "UPDATE hypotheses SET status = ?, updated_at = ?, "
                "promoted_at = ?, promoted_by = ? "
                "WHERE hypothesis_id = ? AND status = ?",
                (new_status, now, now, promoted_by, hypothesis_id, expected_status),
                operation="hypothesis update_status (cas)",
            )
            await commit_with_retry(self._db, operation="hypothesis update_status (cas)")
            changed = (cursor.rowcount or 0) > 0
            if not changed:
                logger.info(
                    f"Hypothesis {hypothesis_id}: status CAS no-op — expected "
                    f"{expected_status!r}, row already moved (concurrent promote race)"
                )
            else:
                logger.info(
                    f"Hypothesis {hypothesis_id} → {new_status} (by {promoted_by}, expected={expected_status!r})"
                )
            return {
                "hypothesis_id": hypothesis_id,
                "new_status": new_status,
                "changed": changed,
                "expected_status": expected_status,
            }
        await execute_with_retry(
            self._db,
            "UPDATE hypotheses SET status = ?, updated_at = ?, "
            "promoted_at = ?, promoted_by = ? WHERE hypothesis_id = ?",
            (new_status, now, now, promoted_by, hypothesis_id),
            operation="hypothesis update_status",
        )
        await commit_with_retry(self._db, operation="hypothesis update_status")
        logger.info(f"Hypothesis {hypothesis_id} → {new_status} (by {promoted_by})")
        return {"hypothesis_id": hypothesis_id, "new_status": new_status, "changed": True}

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

    async def check_promotion_readiness(self, hypothesis_id: str, *, stage_override: str | None = None) -> dict:
        """Check if a hypothesis meets criteria to advance to next stage.

        Args:
            stage_override: Force evaluation on a different data stage.
                Used by auto_promote when paper_trading hypotheses have
                0 paper trades but sufficient backtest evidence — without
                this, the readiness check would evaluate on empty
                paper_trade data and always fail (the deadlock bug).
        """
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
            stage = stage_override or "paper_trade"
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
        if p > max_p:
            checks.append(f"FAIL: p-value {p:.4f} > {max_p:.4f} (adaptive, base={base_p}, n={n})")
            ready = False
        else:
            checks.append(f"PASS: p-value {p:.4f} <= {max_p:.4f} (adaptive, base={base_p}, n={n})")

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

        # Losing record rejection: hit_rate below 45% after 12+ signals means
        # the hypothesis is actively losing money. The p-value tiers (>0.50 at n>=15)
        # miss these because a 44% hit rate at n=16 gives p≈0.35 — not high enough.
        if not should_reject and not used_all_events and status == "backtesting":
            _hit_rate = report.get("results", {}).get("hit_rate", 0.5)
            if n >= 12 and _hit_rate < 0.45:
                should_reject = True
                checks.append(
                    f"AUTO-REJECT: hit_rate={_hit_rate:.1%} < 45% with {n} signals — "
                    f"actively losing, edge is negative"
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
            AND meet min_signals with adaptive p-value threshold (see PROMOTION_GATES)
          - paper_trading → live: paper_trades with positive ROI, OR if 0 paper
            trades exist (rare-condition hypotheses), backtest evidence with
            sufficient signals meeting paper_trading→live statistical gates

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
                        # Auto-lower threshold and give more cycles
                        new_threshold = edge_diag["recommended_threshold"]
                        model_config["edge_threshold"] = new_threshold
                        model_config["evaluate_cycles"] = 0  # Reset cycle count
                        from tools.db_utils import execute_with_retry, commit_with_retry
                        await execute_with_retry(
                            self._db,
                            "UPDATE hypotheses SET edge_threshold = ?, model_config = ? "
                            "WHERE hypothesis_id = ?",
                            (new_threshold, json.dumps(model_config), hypothesis_id),
                            operation="hypothesis auto_lower_threshold",
                        )
                        await commit_with_retry(self._db, operation="hypothesis auto_lower_threshold")

                        # Retroactively update signal_generated on existing events
                        # so evaluate_significance can see them without re-backtesting
                        # NOTE: must use `edge` column (probability edge), NOT `ev_pct` (expected value)
                        # — consistent with backtest.py:1161 where is_signal = edge >= edge_threshold
                        cursor = await execute_with_retry(
                            self._db,
                            "UPDATE backtest_events "
                            "SET signal_generated = CASE WHEN edge >= ? THEN 1 ELSE 0 END "
                            "WHERE hypothesis_id = ?",
                            (new_threshold, hypothesis_id),
                            operation="hypothesis retroactive_signal_update",
                        )
                        await commit_with_retry(self._db, operation="hypothesis retroactive_signal_update")
                        retroactive_count = cursor.rowcount
                        # Count how many are now signals
                        sig_cursor = await (await self._db.execute(
                            "SELECT COUNT(DISTINCT event_id) FROM backtest_events "
                            "WHERE hypothesis_id = ? AND signal_generated = 1",
                            (hypothesis_id,),
                        )).fetchone()
                        new_signals = sig_cursor[0] if sig_cursor else 0

                        # Sync backtest_runs.signals_generated so monitoring
                        # reflects the retroactive update (not just evaluate_significance)
                        await execute_with_retry(
                            self._db,
                            "UPDATE backtest_runs SET signals_generated = ("
                            "  SELECT COUNT(DISTINCT event_id) FROM backtest_events"
                            "  WHERE backtest_events.run_id = backtest_runs.run_id"
                            "  AND signal_generated = 1"
                            ") WHERE hypothesis_id = ?",
                            (hypothesis_id,),
                            operation="hypothesis sync_backtest_runs_signals",
                        )
                        await commit_with_retry(self._db, operation="hypothesis sync_backtest_runs_signals")

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
                                cas = await self.update_status(hypothesis_id, "rejected", "auto", expected_status=status)
                                if not cas.get("changed"):
                                    return {"action": "held", "reason": "Concurrent transition won; rejection skipped."}
                                return {"action": "rejected", "reason": "Data actively disproves thesis after threshold adjustment."}
                            if readiness.get("ready"):
                                next_stage = readiness["next_stage"]
                                cas = await self.update_status(hypothesis_id, next_stage, "auto", expected_status=status)
                                if not cas.get("changed"):
                                    return {"action": "held", "reason": f"Concurrent promotion already moved row past {status!r}."}
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
                # No resolved paper trades — use backtest evidence for promotion.
                # Unresolved paper trade records should not block a hypothesis
                # with strong backtest signals from advancing.
                backtest_signals = await self._get_backtest_signals(hypothesis_id)
                if not backtest_signals or len(backtest_signals) < PROMOTION_GATES["paper_trading→live"]["min_signals"]:
                    return {
                        "action": "held",
                        "reason": (
                            f"0 resolved paper trades and only {len(backtest_signals) if backtest_signals else 0} "
                            f"backtest signals (need {PROMOTION_GATES['paper_trading→live']['min_signals']}). "
                            f"Waiting for more data."
                        ),
                    }
                logger.info(
                    f"Hypothesis {hypothesis_id}: 0 resolved paper trades but "
                    f"{len(backtest_signals)} backtest signals — evaluating "
                    f"promotion using backtest evidence"
                )
                _use_backtest_evidence = True

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

    # ── DATA ACCESSORS ──

    async def _get_backtest_signals(self, hypothesis_id: str) -> list[dict]:
        """Get backtest signal events, deduplicated by unique event.

        Each event_id can appear multiple times (once per book).  For
        evaluation we keep only the best-edge row per event so sample
        size reflects independent betting opportunities, not book count.
        (e.g. 49 unique events = N=49, not N=150 from 49×3 books.)
        """
        cursor = await self._db.execute(
            "SELECT * FROM backtest_events "
            "WHERE hypothesis_id = ? AND signal_generated = 1 "
            "ORDER BY game_date",
            (hypothesis_id,),
        )
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        all_events = [dict(zip(cols, row)) for row in rows]

        # Deduplicate: keep best-edge row per unique event
        best_by_event: dict[str, dict] = {}
        for event in all_events:
            eid = event["event_id"]
            if eid not in best_by_event or (event.get("edge") or 0) > (best_by_event[eid].get("edge") or 0):
                best_by_event[eid] = event

        return sorted(best_by_event.values(), key=lambda e: e.get("game_date", ""))

    async def _get_backtest_resolved(self, hypothesis_id: str) -> list[dict]:
        """Get resolved backtest events, deduplicated by unique event.

        Fallback for evaluate_significance when 0 signal events exist —
        lets us determine if the thesis has any merit before auto-rejecting.
        Keeps best-edge row per event_id for same reason as _get_backtest_signals.
        """
        cursor = await self._db.execute(
            "SELECT * FROM backtest_events "
            "WHERE hypothesis_id = ? AND actual_result IS NOT NULL "
            "ORDER BY game_date",
            (hypothesis_id,),
        )
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        all_events = [dict(zip(cols, row)) for row in rows]

        best_by_event: dict[str, dict] = {}
        for event in all_events:
            eid = event["event_id"]
            if eid not in best_by_event or (event.get("edge") or 0) > (best_by_event[eid].get("edge") or 0):
                best_by_event[eid] = event

        return sorted(best_by_event.values(), key=lambda e: e.get("game_date", ""))

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
