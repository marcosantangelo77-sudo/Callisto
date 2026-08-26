"""
tools.hypothesis.config — thresholds, gates, and env-overridable knobs.

Split out of tools/hypothesis.py (facade re-exports everything).
"""
from __future__ import annotations

import logging
import math
import os

from dotenv import load_dotenv

load_dotenv()


logger = logging.getLogger("callisto.hypothesis")

DB_PATH = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")


def _env_float(name: str, default: float) -> float:
    """Parse env var as float, fall back to default on invalid input."""
    try:
        raw = os.getenv(name)
        return float(raw) if raw is not None else default
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    """Parse env var as int, fall back to default on invalid input."""
    try:
        raw = os.getenv(name)
        return int(raw) if raw is not None else default
    except (TypeError, ValueError):
        return default


# ── ENV-OVERRIDABLE GATE THRESHOLDS ──
# Marco tightens/loosens without code changes:
#   CALLISTO_MIN_DAYS_PAPER        — min days in paper_trading before live promotion
#   CALLISTO_MIN_PAPER_TRADES      — min resolved paper trades before live promotion
#   CALLISTO_MIN_CLV_RATE          — min positive-CLV rate for paper→live
#   CALLISTO_LIVE_REVIEW_WINDOW_DAYS — rolling window for LIVE demotion review
MIN_DAYS_PAPER = _env_int("CALLISTO_MIN_DAYS_PAPER", 7)
MIN_PAPER_TRADES = _env_int("CALLISTO_MIN_PAPER_TRADES", 10)
# Positive-CLV RATE floor (fraction of resolved forward-tests whose canonical
# devigged CLV closed positive, 0..1). Pre-B1 default 0.005 was a unit error:
# it read like a probability magnitude against a trade-rate, binding only when
# literally every trade closed negative. Raised to a meaningful rate floor
# (this TIGHTENS the gate — permitted; automated actors may never lower one).
MIN_CLV_RATE = _env_float("CALLISTO_MIN_CLV_RATE", 0.50)
# Minimum canonical (clv_log.clv_prob_bp) samples before the gate trusts the
# devigged statistic over the legacy raw-implied-delta fallback.
MIN_CANONICAL_CLV_SAMPLE = _env_int("CALLISTO_MIN_CANONICAL_CLV_SAMPLE", 3)
LIVE_REVIEW_WINDOW_DAYS = _env_int("CALLISTO_LIVE_REVIEW_WINDOW_DAYS", 14)

# FWER (Šidák) correction window in days. 'inf' counts every hypothesis ever
# tested. Every hypothesis that ever ran a backtest counts — not just
# currently-active ones — because each of those was a multiple-comparison
# opportunity for false alpha. With 4500+ lifetime hypotheses α can go below
# 1e-5; that is intentional (no floor).
FWER_LOOKBACK_DAYS_RAW = os.getenv("CALLISTO_FWER_LOOKBACK_DAYS", "365")
try:
    FWER_LOOKBACK_DAYS: float = (
        float("inf")
        if FWER_LOOKBACK_DAYS_RAW.strip().lower() in ("inf", "infinity", "all", "0")
        else float(FWER_LOOKBACK_DAYS_RAW)
    )
except (TypeError, ValueError):
    FWER_LOOKBACK_DAYS = 365.0

# Portfolio correlation: reject LIVE promotion if candidate's signals overlap
# >X% with an existing LIVE hypothesis's signals on the same events
# (correlated signals = non-independent bets).
MAX_LIVE_OVERLAP_PCT = _env_float("CALLISTO_MAX_LIVE_OVERLAP_PCT", 0.40)
PORTFOLIO_OVERLAP_WINDOW_DAYS = _env_int(
    "CALLISTO_PORTFOLIO_OVERLAP_WINDOW_DAYS", 30
)

# Pre-LIVE Monte Carlo simulation gate (feat/bankroll-montecarlo-sim 2026-04-22):
#   CALLISTO_SIM_GATE=1                  enables the simulate-before-promote gate
#   CALLISTO_MAX_PRE_PROMOTE_RUIN=0.02   max 15%-drawdown ruin prob over 30d
#   CALLISTO_PRE_PROMOTE_N_SIMS=500      n_sims for the gate
#   CALLISTO_PRE_PROMOTE_HORIZON=30      horizon days for the gate
SIM_GATE_ENABLED = os.getenv("CALLISTO_SIM_GATE", "1").strip() not in ("0", "false", "False")
MAX_PRE_PROMOTE_RUIN = _env_float("CALLISTO_MAX_PRE_PROMOTE_RUIN", 0.02)
PRE_PROMOTE_N_SIMS = _env_int("CALLISTO_PRE_PROMOTE_N_SIMS", 500)
PRE_PROMOTE_HORIZON = _env_int("CALLISTO_PRE_PROMOTE_HORIZON", 30)

# Signal-collapse mode for per-event dedup in `_get_backtest_signals`.
#   "random_row" — pick one row per event_id with a deterministic seed
#                  (removes best-edge selection bias; matches the
#                  "bet one, not the best" real-world scenario)
#   "composite"  — aggregate per-event: sum stakes, average edge/ev across
#                  all prop rows within the event, return a single
#                  composite-signal row per event_id.  Models how
#                  correlated prop bets behave in practice.
#   "best_edge"  — legacy (pre-audit) behavior; KEPT ONLY for backward-
#                  compat with hypotheses created before FWER fix.  Not
#                  recommended; selection-biases upward.
SIGNAL_COLLAPSE_MODE = os.getenv(
    "CALLISTO_SIGNAL_COLLAPSE_MODE", "random_row"
).strip().lower()
if SIGNAL_COLLAPSE_MODE not in ("random_row", "composite", "best_edge"):
    logger.warning(
        f"Invalid CALLISTO_SIGNAL_COLLAPSE_MODE={SIGNAL_COLLAPSE_MODE!r}; "
        f"falling back to 'random_row'"
    )
    SIGNAL_COLLAPSE_MODE = "random_row"

# Promotion gates: {transition: {min_n, max_p, min_clv_rate, extras}}
# Note: max_p_value is the BASE threshold. Actual threshold is adaptive via
# get_adaptive_p_value_threshold() — relaxed at small n, tightens as data grows.
# Paper→live gate is the real quality filter (CLV, drawdown, duration, paper-trade sample).
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
        "min_signals": 5,              # minimum distinct signals (overlaps with min_paper_trades)
        "min_paper_trades": MIN_PAPER_TRADES,  # HARD gate: resolved paper trades required for LIVE
        "max_p_value": 0.10,           # base threshold; adaptive: 0.15 at n<25, 0.10 at n<40
        "min_clv_rate": MIN_CLV_RATE,  # floor (env-overridable); was 0.0 pre-audit
        "max_drawdown": 0.30,
        "min_days": MIN_DAYS_PAPER,    # MUST spend this many days in paper_trading
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

