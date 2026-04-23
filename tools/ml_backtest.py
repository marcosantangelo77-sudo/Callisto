"""ml_backtest — simulate trading off trained ML classifier signals.

Given a ``TrainedModel`` saved by :mod:`tools.ml_classifier`, replay history
and score each row as ``prob_over = model.predict_proba(X)[:, 1]``. Whenever
``prob_over > threshold`` (or ``1 - prob_over > threshold`` for unders),
emit a synthetic wager against historical closing prices and accumulate the
hit-rate / ROI / CLV / Sharpe profile — the exact shape the hand-crafted
hypothesis backtests produce.

The purpose is apples-to-apples comparison: does an XGBoost classifier,
trained on the feature store alone, beat (or at minimum match) the best
hand-seeded hypothesis? If yes — promote the ML baseline. If no — the
features are the constraint and we iterate on the feature store.

This module is STRICTLY read-only against the DB. No writes, no network.
"""
from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

import numpy as np

try:
    from tools.ml_classifier import TrainedModel, load_model
    from tools.ml_features import (
        build_game_total_features,
        build_player_prop_features,
    )
except ImportError:  # pragma: no cover
    from ml_classifier import TrainedModel, load_model  # type: ignore
    from ml_features import (  # type: ignore
        build_game_total_features,
        build_player_prop_features,
    )

logger = logging.getLogger("callisto.ml_backtest")


# ──────────────────────────────────────────────────────────────────────────

@dataclass
class MLBacktestReport:
    model_path: str
    market: str
    sport: str
    threshold: float
    n_signals: int
    n_resolved: int
    hits: int
    pushes: int
    misses: int
    hit_rate: Optional[float]
    roi_pct: Optional[float]            # flat-stake ROI (assumes -110 if book odds missing)
    clv_implied_mean: Optional[float]   # mean closing_implied delta (prob space)
    sharpe: Optional[float]
    per_day_pnl: list[tuple[str, float]]
    per_fold_summary: Optional[list[dict]] = None


def _open_ro(path: Optional[str] = None) -> sqlite3.Connection:
    p = path or os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _american_to_decimal(a: Optional[int]) -> float:
    if a is None:
        return 1.909  # -110 default
    a = int(a)
    if a >= 0:
        return 1.0 + a / 100.0
    return 1.0 + 100.0 / abs(a)


def _sharpe(pnl_per_day: Sequence[float]) -> Optional[float]:
    if len(pnl_per_day) < 2:
        return None
    arr = np.asarray(pnl_per_day, dtype=float)
    std = arr.std(ddof=1)
    if std == 0:
        return None
    # annualise by sqrt(252) — betting-day approximation
    return float(arr.mean() / std * math.sqrt(252))


# ──────────────────────────────────────────────────────────────────────────
# Totals backtest
# ──────────────────────────────────────────────────────────────────────────

def _iter_total_events(
    conn: sqlite3.Connection, sport: str
) -> Iterable[sqlite3.Row]:
    """Walk totals events that carry both a posted line and a realised total."""
    cur = conn.execute(
        """
        SELECT be.event_id,
               COALESCE(be.local_game_date, be.game_date) AS gd,
               be.line AS posted_line,
               be.side,
               be.book_odds_american,
               be.closing_odds,
               be.closing_implied,
               be.actual_result,
               gr.total_score
          FROM backtest_events be
          JOIN game_contexts gc ON gc.sport=be.sport AND gc.event_id=be.event_id
          JOIN game_results gr ON gr.sport=be.sport
               AND gr.home_team=gc.home_team AND gr.away_team=gc.away_team
               AND COALESCE(gr.local_game_date, gr.game_date)
                   = COALESCE(gc.local_game_date, gc.game_date)
         WHERE be.sport = ?
           AND be.market = 'totals'
           AND gr.total_score IS NOT NULL
           AND be.actual_result IN ('won','lost','push')
         GROUP BY be.event_id, be.side, be.line
         ORDER BY gd ASC
        """,
        (sport,),
    )
    while True:
        rows = cur.fetchmany(200)
        if not rows:
            return
        for r in rows:
            yield r


def _iter_prop_events(
    conn: sqlite3.Connection,
    sport: str,
    stat_type: str,
) -> Iterable[sqlite3.Row]:
    """Every resolved player_stat that we'd have been able to bet over/under on."""
    cur = conn.execute(
        """
        SELECT ps.player_name, ps.stat_type, ps.event_id,
               ps.game_date AS gd, ps.stat_value
          FROM player_stats ps
         WHERE ps.sport = ?
           AND ps.stat_type = ?
           AND ps.stat_value IS NOT NULL
         ORDER BY ps.game_date ASC
        """,
        (sport, stat_type),
    )
    while True:
        rows = cur.fetchmany(200)
        if not rows:
            return
        for r in rows:
            yield r


def ml_backtest(
    model_path: str | Path,
    *,
    threshold: float = 0.55,
    walk_forward: bool = True,
    max_events: Optional[int] = None,
) -> MLBacktestReport:
    """Replay history for a trained model and compute realised P/L.

    Parameters
    ----------
    model_path : path to a ``.joblib`` produced by :mod:`ml_classifier`.
    threshold : probability cutoff for emitting a wager.
    walk_forward : if True, refit the model on a growing window before each
        fold's predictions. Not yet implemented here — the flag is accepted
        for signature stability with the hand-crafted backtest. The model
        we ship today is a single final fit.
    max_events : optional cap, mostly for testing.
    """
    model = load_model(model_path)
    conn = _open_ro()
    try:
        if model.market == "totals":
            return _backtest_totals(model, str(model_path), conn, threshold, max_events)
        if model.market.startswith("player_prop_"):
            return _backtest_props(model, str(model_path), conn, threshold, max_events)
        raise ValueError(f"Unsupported market for backtest: {model.market!r}")
    finally:
        conn.close()


def _backtest_totals(
    model: TrainedModel,
    model_path: str,
    conn: sqlite3.Connection,
    threshold: float,
    max_events: Optional[int],
) -> MLBacktestReport:
    hits = pushes = misses = 0
    n_signals = n_resolved = 0
    pnl_by_day: dict[str, float] = {}
    clv_sum = 0.0
    clv_n = 0
    count = 0

    for row in _iter_total_events(conn, model.sport):
        count += 1
        if max_events is not None and count > max_events:
            break
        try:
            fv = build_game_total_features(
                row["event_id"], row["gd"], sport=model.sport, conn=conn
            )
        except Exception as exc:
            logger.debug("skip %s: %s", row["event_id"], exc)
            continue
        if np.all(np.isnan(fv.values[:5])):
            continue
        p_over = float(model.model.predict_proba(fv.values.reshape(1, -1))[0, 1])
        side = row["side"]
        # Which side does the ML model think we should take?
        prob_for_side = p_over if side.lower() == "over" else (1.0 - p_over)
        if prob_for_side < threshold:
            continue
        n_signals += 1

        result = row["actual_result"]
        if result == "push":
            pushes += 1
            pnl = 0.0
        else:
            n_resolved += 1
            dec = _american_to_decimal(row["book_odds_american"])
            if result == "won":
                pnl = dec - 1.0
                hits += 1
            else:
                pnl = -1.0
                misses += 1

        gd = str(row["gd"])[:10]
        pnl_by_day[gd] = pnl_by_day.get(gd, 0.0) + pnl

        if row["closing_implied"] is not None:
            # positive CLV = we took a better price than close
            implied_us = (1.0 / _american_to_decimal(row["book_odds_american"]))
            clv_sum += float(implied_us) - float(row["closing_implied"])
            clv_n += 1

    total_bets = hits + misses + pushes
    hit_rate = (hits / (hits + misses)) if (hits + misses) else None
    roi = (
        sum(pnl_by_day.values()) / float(hits + misses)
    ) if (hits + misses) else None
    per_day_sorted = sorted(pnl_by_day.items(), key=lambda kv: kv[0])
    sharpe = _sharpe([pnl for _, pnl in per_day_sorted])
    clv_mean = (clv_sum / clv_n) if clv_n else None

    return MLBacktestReport(
        model_path=model_path,
        market=model.market,
        sport=model.sport,
        threshold=threshold,
        n_signals=n_signals,
        n_resolved=n_resolved,
        hits=hits,
        pushes=pushes,
        misses=misses,
        hit_rate=hit_rate,
        roi_pct=(roi * 100.0) if roi is not None else None,
        clv_implied_mean=clv_mean,
        sharpe=sharpe,
        per_day_pnl=per_day_sorted,
    )


def _backtest_props(
    model: TrainedModel,
    model_path: str,
    conn: sqlite3.Connection,
    threshold: float,
    max_events: Optional[int],
) -> MLBacktestReport:
    # For player props we don't have per-row posted lines in player_stats;
    # the classifier was trained against the MEDIAN target. We synthesise
    # bets at -110 against a line = model.threshold.
    stat_type = model.stat_type
    assert stat_type is not None, "prop model must carry stat_type"
    hits = misses = pushes = 0
    n_signals = n_resolved = 0
    pnl_by_day: dict[str, float] = {}
    count = 0

    line = float(model.threshold or 0.0)

    for row in _iter_prop_events(conn, model.sport, stat_type):
        count += 1
        if max_events is not None and count > max_events:
            break
        try:
            fv = build_player_prop_features(
                player=row["player_name"],
                stat_type=stat_type,
                event_id=row["event_id"],
                asof_ts=row["gd"],
                sport=model.sport,
                conn=conn,
            )
        except Exception:
            continue
        if np.all(np.isnan(fv.values[:9])):
            continue
        p_over = float(model.model.predict_proba(fv.values.reshape(1, -1))[0, 1])
        side = "over" if p_over >= 0.5 else "under"
        prob_for_side = max(p_over, 1.0 - p_over)
        if prob_for_side < threshold:
            continue
        n_signals += 1
        actual = float(row["stat_value"])
        if actual == line:
            pushes += 1
            pnl = 0.0
        elif (side == "over" and actual > line) or (side == "under" and actual < line):
            hits += 1
            pnl = _american_to_decimal(-110) - 1.0
            n_resolved += 1
        else:
            misses += 1
            pnl = -1.0
            n_resolved += 1
        gd = str(row["gd"])[:10]
        pnl_by_day[gd] = pnl_by_day.get(gd, 0.0) + pnl

    hit_rate = (hits / (hits + misses)) if (hits + misses) else None
    roi = (
        sum(pnl_by_day.values()) / float(hits + misses)
    ) if (hits + misses) else None
    per_day_sorted = sorted(pnl_by_day.items(), key=lambda kv: kv[0])
    sharpe = _sharpe([pnl for _, pnl in per_day_sorted])
    return MLBacktestReport(
        model_path=model_path,
        market=model.market,
        sport=model.sport,
        threshold=threshold,
        n_signals=n_signals,
        n_resolved=n_resolved,
        hits=hits,
        pushes=pushes,
        misses=misses,
        hit_rate=hit_rate,
        roi_pct=(roi * 100.0) if roi is not None else None,
        clv_implied_mean=None,
        sharpe=sharpe,
        per_day_pnl=per_day_sorted,
    )


__all__ = ["MLBacktestReport", "ml_backtest"]
