"""ml_classifier — XGBoost baselines for Callisto.

Two classifier heads:

    * ``train_prop_classifier(sport, stat_type, line, ...)`` — predicts
      ``P(actual_stat >= line)`` for a player-prop. The "line" argument is
      the threshold we want to classify against; the classifier emits the
      probability-over given the feature vector.
    * ``train_total_classifier(sport, ...)`` — predicts P(over) for a game
      total, trained against realised total scores with the MEDIAN historical
      total serving as the threshold (so the label is well-balanced even
      when we don't have per-event posted totals that pre-date the outcome).

The training loop uses a time-series walk-forward split (``TimeSeriesSplit``)
keyed on ``local_game_date`` where available — never mixes future rows into
training folds. Models and their metadata are persisted via joblib under
``models/{sport}_{market}_{trained_at}.joblib``.

Two utilities surface calibration quality:

    * ``evaluate(model, X, y)`` returns a dict with AUC, Brier, log-loss and
      a 10-bin reliability diagram.
    * ``predict(model, feature_vector)`` returns ``(prob_over, confidence)``
      where ``confidence`` = ``1 - 2*|prob - 0.5|`` flipped into ``|prob-0.5|``
      space (0 = coin flip, 1 = fully confident).

The module is deliberately light on dependencies: XGBoost + scikit-learn +
joblib. Pandas is NOT required — all data handling runs on numpy arrays so
the training footprint stays proportional to ``n_samples * n_features * 8``.
"""
from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

import joblib
import numpy as np
from sklearn.metrics import (
    brier_score_loss,
    log_loss,
    roc_auc_score,
)
from sklearn.model_selection import TimeSeriesSplit
from xgboost import XGBClassifier

try:
    from tools.ml_features import (
        FeatureVector,
        build_game_total_features,
        build_player_prop_features,
        feature_names_game_total,
        feature_names_player_prop,
    )
except ImportError:  # pragma: no cover
    from ml_features import (  # type: ignore
        FeatureVector,
        build_game_total_features,
        build_player_prop_features,
        feature_names_game_total,
        feature_names_player_prop,
    )

logger = logging.getLogger("callisto.ml_classifier")


# ──────────────────────────────────────────────────────────────────────────
# Data classes
# ──────────────────────────────────────────────────────────────────────────

@dataclass
class TrainedModel:
    """Serialisable wrapper around a fitted XGBClassifier."""

    model: Any  # XGBClassifier
    feature_names: list[str]
    sport: str
    market: str
    stat_type: Optional[str]
    threshold: Optional[float]         # line used to binarise continuous targets
    trained_at: str                     # ISO-8601 UTC
    n_train: int
    n_test: int
    metrics: dict                       # AUC / Brier / log-loss / reliability
    train_date_range: tuple[str, str]   # (min, max) local_game_date
    feature_importances: list[tuple[str, float]]
    train_feature_stats: dict           # per-feature mean/std for drift checks
    source: str = "callisto_ml_baseline_v1"


def _resolve_db_path() -> str:
    return os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")


def _open_ro(db_path: Optional[str] = None) -> sqlite3.Connection:
    path = db_path or _resolve_db_path()
    conn = sqlite3.connect(
        f"file:{path}?mode=ro", uri=True, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    return conn


# ──────────────────────────────────────────────────────────────────────────
# Dataset builders — stream from SQLite, one (event, asof) tuple per row.
# ──────────────────────────────────────────────────────────────────────────

def _iter_player_prop_samples(
    conn: sqlite3.Connection,
    sport: str,
    stat_type: str,
    batch_size: int = 500,
) -> Iterable[tuple[str, str, str, str, float]]:
    """Stream resolved player-stat rows that we can turn into training samples.

    Yields ``(player, stat_type, event_id, asof_date, actual_stat)`` tuples.
    The ``asof_date`` is ``game_date`` itself — features will strictly exclude
    that date from their rolling windows, so there is no leakage.
    """
    cur = conn.execute(
        """
        SELECT ps.player_name, ps.stat_type, ps.event_id,
               ps.game_date, ps.stat_value
          FROM player_stats ps
         WHERE ps.sport = ?
           AND ps.stat_type = ?
           AND ps.event_id IS NOT NULL
           AND ps.stat_value IS NOT NULL
         ORDER BY ps.game_date ASC
        """,
        (sport, stat_type),
    )
    while True:
        rows = cur.fetchmany(batch_size)
        if not rows:
            return
        for r in rows:
            yield (r[0], r[1], r[2], r[3], float(r[4]))


def _iter_game_total_samples(
    conn: sqlite3.Connection,
    sport: str,
    batch_size: int = 500,
) -> Iterable[tuple[str, str, float]]:
    """Yield ``(event_id, asof_date, total_score)`` for games with realised totals.

    We prefer ``backtest_events`` (it carries ``event_id`` and ``local_game_date``)
    but fall back to ``game_contexts`` joined to ``game_results``.
    """
    # Path 1: backtest_events already indexed by event_id + local_game_date.
    cur = conn.execute(
        """
        SELECT DISTINCT be.event_id,
               COALESCE(be.local_game_date, be.game_date) AS gd,
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
         ORDER BY gd ASC
        """,
        (sport,),
    )
    while True:
        rows = cur.fetchmany(batch_size)
        if not rows:
            return
        for r in rows:
            if r[1] is None:
                continue
            yield (r[0], r[1], float(r[2]))


# ──────────────────────────────────────────────────────────────────────────
# Matrix builders
# ──────────────────────────────────────────────────────────────────────────

def _build_player_prop_matrix(
    conn: sqlite3.Connection,
    sport: str,
    stat_type: str,
    max_samples: Optional[int] = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[str]]:
    """Materialise X, y (continuous stat), asof_date array and feature names."""
    names = feature_names_player_prop()
    X_rows: list[np.ndarray] = []
    y: list[float] = []
    dates: list[str] = []
    seen = 0
    for player, st, ev, gd, val in _iter_player_prop_samples(conn, sport, stat_type):
        seen += 1
        if max_samples is not None and seen > max_samples:
            break
        try:
            fv = build_player_prop_features(
                player=player,
                stat_type=st,
                event_id=ev,
                asof_ts=gd,
                sport=sport,
                conn=conn,
            )
        except Exception as exc:
            logger.debug("feature build failed for %s/%s/%s: %s", player, st, ev, exc)
            continue
        # Require at least SOME rolling history — drop cold-start rows with all-NaN
        if np.all(np.isnan(fv.values[: 3 * 3])):  # first 9 = mean/std/slope across windows
            continue
        X_rows.append(fv.values)
        y.append(val)
        dates.append(str(gd))
    if not X_rows:
        return (
            np.zeros((0, len(names))),
            np.zeros((0,)),
            np.array([], dtype=object),
            names,
        )
    X = np.vstack(X_rows)
    return X, np.asarray(y, dtype=float), np.asarray(dates, dtype=object), names


def _build_game_total_matrix(
    conn: sqlite3.Connection,
    sport: str,
    max_samples: Optional[int] = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[str]]:
    names = feature_names_game_total()
    X_rows: list[np.ndarray] = []
    y: list[float] = []
    dates: list[str] = []
    seen = 0
    for ev, gd, tot in _iter_game_total_samples(conn, sport):
        seen += 1
        if max_samples is not None and seen > max_samples:
            break
        try:
            fv = build_game_total_features(ev, gd, sport=sport, conn=conn)
        except Exception as exc:
            logger.debug("game-total feature build failed for %s: %s", ev, exc)
            continue
        if np.all(np.isnan(fv.values[: 5])):
            continue
        X_rows.append(fv.values)
        y.append(tot)
        dates.append(str(gd))
    if not X_rows:
        return (
            np.zeros((0, len(names))),
            np.zeros((0,)),
            np.array([], dtype=object),
            names,
        )
    X = np.vstack(X_rows)
    return X, np.asarray(y, dtype=float), np.asarray(dates, dtype=object), names


# ──────────────────────────────────────────────────────────────────────────
# Evaluation helpers
# ──────────────────────────────────────────────────────────────────────────

def _reliability_diagram(
    y_true: np.ndarray, y_prob: np.ndarray, bins: int = 10
) -> list[dict]:
    """Return per-bin (mean_predicted, frac_positive, count)."""
    edges = np.linspace(0.0, 1.0, bins + 1)
    out = []
    for i in range(bins):
        lo, hi = edges[i], edges[i + 1]
        mask = (y_prob >= lo) & (y_prob < hi if i < bins - 1 else y_prob <= hi)
        if not mask.any():
            out.append({"bin_lo": float(lo), "bin_hi": float(hi), "n": 0})
            continue
        out.append(
            {
                "bin_lo": float(lo),
                "bin_hi": float(hi),
                "n": int(mask.sum()),
                "mean_pred": float(y_prob[mask].mean()),
                "frac_pos": float(y_true[mask].mean()),
            }
        )
    return out


def evaluate(
    model: XGBClassifier, X: np.ndarray, y: np.ndarray
) -> dict:
    if X.shape[0] == 0:
        return {"n": 0}
    prob = model.predict_proba(X)[:, 1]
    y_int = y.astype(int)
    out: dict = {"n": int(X.shape[0])}
    try:
        out["auc"] = float(roc_auc_score(y_int, prob)) if len(set(y_int)) > 1 else None
    except Exception:
        out["auc"] = None
    try:
        out["brier"] = float(brier_score_loss(y_int, prob))
    except Exception:
        out["brier"] = None
    try:
        # log_loss wants clipping for hard 0/1 predictions
        prob_clip = np.clip(prob, 1e-6, 1 - 1e-6)
        out["log_loss"] = float(log_loss(y_int, prob_clip, labels=[0, 1]))
    except Exception:
        out["log_loss"] = None
    out["reliability"] = _reliability_diagram(y_int, prob)
    out["base_rate"] = float(y_int.mean()) if len(y_int) else None
    return out


# ──────────────────────────────────────────────────────────────────────────
# Training entry points
# ──────────────────────────────────────────────────────────────────────────

_MODELS_DIR = Path("models")


def _ensure_models_dir() -> Path:
    _MODELS_DIR.mkdir(parents=True, exist_ok=True)
    return _MODELS_DIR


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _fit_xgb(
    X_train: np.ndarray,
    y_train: np.ndarray,
    *,
    n_estimators: int = 200,
    max_depth: int = 4,
    learning_rate: float = 0.05,
    seed: int = 42,
) -> XGBClassifier:
    clf = XGBClassifier(
        n_estimators=n_estimators,
        max_depth=max_depth,
        learning_rate=learning_rate,
        subsample=0.9,
        colsample_bytree=0.9,
        reg_lambda=1.0,
        random_state=seed,
        tree_method="hist",
        n_jobs=2,
        eval_metric="logloss",
    )
    clf.fit(X_train, y_train)
    return clf


def _feature_stats(X: np.ndarray, names: Sequence[str]) -> dict:
    """Per-feature mean/std, skipping NaN. Used later for drift detection."""
    stats: dict = {}
    for i, n in enumerate(names):
        col = X[:, i]
        mask = np.isfinite(col)
        if not mask.any():
            stats[n] = {"mean": None, "std": None, "n": 0}
            continue
        stats[n] = {
            "mean": float(col[mask].mean()),
            "std": float(col[mask].std(ddof=0)),
            "n": int(mask.sum()),
        }
    return stats


def _walk_forward_eval(
    X: np.ndarray,
    y: np.ndarray,
    dates: np.ndarray,
    *,
    threshold: float,
    n_splits: int = 4,
) -> tuple[XGBClassifier, dict, int, int]:
    """Fit on a walk-forward schedule. Returns (final_model, metrics, n_train, n_test).

    We sort by date, then use scikit-learn's TimeSeriesSplit on the sorted
    indices. The returned ``model`` is refit on ALL data at the end — the
    evaluation dict carries per-fold metrics.
    """
    order = np.argsort(dates)
    X_sorted = X[order]
    y_sorted = (y[order] >= threshold).astype(int)
    dates_sorted = dates[order]

    n = X_sorted.shape[0]
    # Binary label must have both classes for AUC — if one-sided, fall back
    if len(set(y_sorted)) < 2:
        final = _fit_xgb(X_sorted, y_sorted)
        m = evaluate(final, X_sorted, y_sorted)
        m["folds"] = []
        m["note"] = "single-class label; walk-forward skipped"
        return final, m, n, 0

    splits_usable = min(n_splits, max(2, n // 100))
    tss = TimeSeriesSplit(n_splits=splits_usable)
    fold_results: list[dict] = []
    last_test_n = 0
    for fi, (tr_idx, te_idx) in enumerate(tss.split(X_sorted)):
        y_tr = y_sorted[tr_idx]
        if len(set(y_tr)) < 2:
            continue
        clf = _fit_xgb(X_sorted[tr_idx], y_tr)
        fold_m = evaluate(clf, X_sorted[te_idx], y_sorted[te_idx])
        fold_m["fold"] = fi
        fold_m["train_n"] = int(len(tr_idx))
        fold_m["test_n"] = int(len(te_idx))
        fold_m["train_last_date"] = str(dates_sorted[tr_idx[-1]])
        fold_m["test_first_date"] = str(dates_sorted[te_idx[0]])
        fold_results.append(fold_m)
        last_test_n = int(len(te_idx))

    # Final model fit on everything — used for production inference
    final = _fit_xgb(X_sorted, y_sorted)
    # Aggregate metrics: average AUC / Brier / log_loss across folds
    agg: dict = {"folds": fold_results}
    for key in ("auc", "brier", "log_loss"):
        vals = [f.get(key) for f in fold_results if f.get(key) is not None]
        agg[f"cv_{key}_mean"] = float(np.mean(vals)) if vals else None
    # Also report overall-fit metrics on all data (train error, for sanity only)
    overall = evaluate(final, X_sorted, y_sorted)
    agg["train_fit"] = overall
    return final, agg, n, last_test_n


def train_prop_classifier(
    sport: str,
    stat_type: str,
    *,
    line: Optional[float] = None,
    min_samples: int = 500,
    max_samples: Optional[int] = None,
    conn: Optional[sqlite3.Connection] = None,
    save: bool = True,
) -> Optional[TrainedModel]:
    """Train the XGBoost baseline for a single (sport, stat_type).

    ``line`` chooses the classification threshold; if omitted, we use the
    median observed value, which keeps labels balanced without requiring a
    posted line for every sample.
    """
    close_after = conn is None
    if conn is None:
        conn = _open_ro()
    try:
        X, y, dates, names = _build_player_prop_matrix(
            conn, sport, stat_type, max_samples=max_samples
        )
        if X.shape[0] < min_samples:
            logger.info(
                "prop classifier skipped: %s/%s has %d samples (< %d)",
                sport, stat_type, X.shape[0], min_samples,
            )
            return None
        if line is not None:
            threshold = float(line)
        else:
            # Median is the natural default but collapses to a single class
            # when >50% of observations sit at the same integer (common for
            # low-volume stats like blocks/steals where 0 dominates). Walk
            # up percentiles until we find one that yields a usable split.
            threshold = float(np.median(y))
            y_bin = (y >= threshold).astype(int)
            if len(set(y_bin)) < 2 and len(y) >= 20:
                for pct in (60, 70, 75, 80, 85, 90):
                    alt = float(np.percentile(y, pct))
                    if alt == threshold:
                        continue
                    y_alt = (y >= alt).astype(int)
                    if len(set(y_alt)) == 2 and y_alt.mean() not in (0.0, 1.0):
                        threshold = alt
                        break
        model, metrics, n_train, n_test = _walk_forward_eval(
            X, y, dates, threshold=threshold
        )
        imp = sorted(
            zip(names, getattr(model, "feature_importances_", np.zeros(len(names))).tolist()),
            key=lambda kv: kv[1],
            reverse=True,
        )
        tm = TrainedModel(
            model=model,
            feature_names=list(names),
            sport=sport,
            market=f"player_prop_{stat_type}",
            stat_type=stat_type,
            threshold=threshold,
            trained_at=_timestamp(),
            n_train=n_train,
            n_test=n_test,
            metrics=metrics,
            train_date_range=(str(dates.min()) if len(dates) else "",
                              str(dates.max()) if len(dates) else ""),
            feature_importances=imp[:20],
            train_feature_stats=_feature_stats(X, names),
        )
        if save:
            _persist(tm)
        return tm
    finally:
        if close_after:
            conn.close()


def train_total_classifier(
    sport: str,
    *,
    threshold: Optional[float] = None,
    min_samples: int = 2000,
    max_samples: Optional[int] = None,
    conn: Optional[sqlite3.Connection] = None,
    save: bool = True,
) -> Optional[TrainedModel]:
    close_after = conn is None
    if conn is None:
        conn = _open_ro()
    try:
        X, y, dates, names = _build_game_total_matrix(
            conn, sport, max_samples=max_samples
        )
        if X.shape[0] < min_samples:
            logger.info(
                "total classifier skipped: %s has %d samples (< %d)",
                sport, X.shape[0], min_samples,
            )
            return None
        thr = float(threshold) if threshold is not None else float(np.median(y))
        model, metrics, n_train, n_test = _walk_forward_eval(
            X, y, dates, threshold=thr
        )
        imp = sorted(
            zip(names, getattr(model, "feature_importances_", np.zeros(len(names))).tolist()),
            key=lambda kv: kv[1],
            reverse=True,
        )
        tm = TrainedModel(
            model=model,
            feature_names=list(names),
            sport=sport,
            market="totals",
            stat_type=None,
            threshold=thr,
            trained_at=_timestamp(),
            n_train=n_train,
            n_test=n_test,
            metrics=metrics,
            train_date_range=(str(dates.min()) if len(dates) else "",
                              str(dates.max()) if len(dates) else ""),
            feature_importances=imp[:20],
            train_feature_stats=_feature_stats(X, names),
        )
        if save:
            _persist(tm)
        return tm
    finally:
        if close_after:
            conn.close()


# ──────────────────────────────────────────────────────────────────────────
# Persist / load / predict
# ──────────────────────────────────────────────────────────────────────────

def _persist(tm: TrainedModel) -> Path:
    models_dir = _ensure_models_dir()
    fname = f"{tm.sport}_{tm.market}_{tm.trained_at}.joblib"
    path = models_dir / fname
    joblib.dump(tm, path)
    # Sidecar JSON for quick human inspection (without loading the pickle)
    sidecar = path.with_suffix(".json")
    with open(sidecar, "w", encoding="utf-8") as f:
        json.dump(
            {
                "sport": tm.sport,
                "market": tm.market,
                "stat_type": tm.stat_type,
                "threshold": tm.threshold,
                "trained_at": tm.trained_at,
                "n_train": tm.n_train,
                "n_test": tm.n_test,
                "metrics": tm.metrics,
                "train_date_range": list(tm.train_date_range),
                "feature_importances": tm.feature_importances,
                "feature_names": tm.feature_names,
                "source": tm.source,
            },
            f,
            indent=2,
            default=str,
        )
    logger.info("persisted model -> %s", path)
    return path


def load_model(path: str | Path) -> TrainedModel:
    return joblib.load(Path(path))


def predict(model: TrainedModel, fv: FeatureVector) -> tuple[float, float]:
    """Single-row inference.

    Returns ``(prob_over, confidence)`` where ``confidence`` is the magnitude
    of the deviation from 0.5 — 0 means coin-flip, ~0.5 means nearly certain.
    """
    if list(fv.names) != list(model.feature_names):
        # Reorder by name if feature sets intersect; otherwise refuse.
        name_to_idx = {n: i for i, n in enumerate(fv.names)}
        missing = [n for n in model.feature_names if n not in name_to_idx]
        if missing:
            raise ValueError(
                f"FeatureVector missing features required by model: {missing[:5]}..."
            )
        values = np.array(
            [fv.values[name_to_idx[n]] for n in model.feature_names],
            dtype=float,
        )
    else:
        values = fv.values
    X = values.reshape(1, -1)
    prob = float(model.model.predict_proba(X)[0, 1])
    confidence = abs(prob - 0.5) * 2.0
    return prob, confidence


__all__ = [
    "TrainedModel",
    "evaluate",
    "load_model",
    "predict",
    "train_prop_classifier",
    "train_total_classifier",
]
