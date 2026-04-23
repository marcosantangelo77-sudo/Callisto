"""ml_drift — covariate-shift detection for trained Callisto classifiers.

A classifier fit on February's feature distributions silently degrades when
the distribution shifts (roster churn, weather, regime changes). We run a
Kolmogorov-Smirnov test per feature between the training population and a
rolling recent window; if a material fraction of features shift significantly
we flag the model as ``stale`` in its sidecar metadata.

This module is read-only against the DB. It does NOT mutate training
metadata in place — it writes a sibling ``*.drift.json`` file next to the
model and returns a :class:`DriftReport` describing the findings.
"""
from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional, Sequence

import numpy as np
from scipy.stats import ks_2samp

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

logger = logging.getLogger("callisto.ml_drift")


@dataclass
class FeatureDrift:
    name: str
    train_mean: Optional[float]
    train_std: Optional[float]
    recent_mean: Optional[float]
    recent_std: Optional[float]
    ks_statistic: Optional[float]
    p_value: Optional[float]
    is_shifted: bool


@dataclass
class DriftReport:
    model_path: str
    recent_days: int
    n_recent: int
    n_features: int
    n_shifted: int
    shift_fraction: float
    is_stale: bool
    features: list[FeatureDrift]
    recent_date_range: tuple[Optional[str], Optional[str]]


def _open_ro(path: Optional[str] = None) -> sqlite3.Connection:
    p = path or os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _collect_recent_samples(
    model: TrainedModel,
    recent_days: int,
    conn: sqlite3.Connection,
    max_rows: int = 500,
) -> tuple[np.ndarray, list[str]]:
    cutoff = (datetime.now(timezone.utc).date() - timedelta(days=recent_days))
    cutoff_iso = cutoff.isoformat()
    feat_rows: list[np.ndarray] = []
    dates_seen: list[str] = []

    if model.market.startswith("player_prop_") and model.stat_type:
        cur = conn.execute(
            """
            SELECT player_name, event_id, game_date
              FROM player_stats
             WHERE sport=? AND stat_type=? AND game_date >= ?
             ORDER BY game_date DESC
             LIMIT ?
            """,
            (model.sport, model.stat_type, cutoff_iso, max_rows),
        )
        for r in cur.fetchall():
            try:
                fv = build_player_prop_features(
                    r[0], model.stat_type, r[1], r[2], sport=model.sport, conn=conn
                )
            except Exception:
                continue
            feat_rows.append(fv.values)
            dates_seen.append(str(r[2])[:10])
    elif model.market == "totals":
        cur = conn.execute(
            """
            SELECT DISTINCT event_id,
                   COALESCE(local_game_date, game_date) AS gd
              FROM backtest_events
             WHERE sport=? AND market='totals'
               AND COALESCE(local_game_date, game_date) >= ?
             ORDER BY gd DESC
             LIMIT ?
            """,
            (model.sport, cutoff_iso, max_rows),
        )
        for r in cur.fetchall():
            try:
                fv = build_game_total_features(r[0], r[1], sport=model.sport, conn=conn)
            except Exception:
                continue
            feat_rows.append(fv.values)
            dates_seen.append(str(r[1])[:10])
    else:
        return np.zeros((0, len(model.feature_names))), []

    if not feat_rows:
        return np.zeros((0, len(model.feature_names))), []
    return np.vstack(feat_rows), dates_seen


def _synthetic_train_sample(
    model: TrainedModel, n_per_feature: int = 200, seed: int = 42
) -> np.ndarray:
    """Generate a pseudo training sample from the stored per-feature stats.

    The training data itself isn't saved with the model (that would bloat
    joblib files); we only saved mean/std. KS on gaussian resamples is still
    a useful first-order drift check — under H0 the recent distribution has
    the same mean+std and a large KS p-value results.
    """
    rng = np.random.default_rng(seed)
    cols = []
    for name in model.feature_names:
        st = model.train_feature_stats.get(name, {})
        mean = st.get("mean")
        std = st.get("std")
        if mean is None or std is None or st.get("n", 0) == 0:
            cols.append(np.full(n_per_feature, np.nan))
            continue
        if std == 0:
            cols.append(np.full(n_per_feature, float(mean)))
            continue
        cols.append(rng.normal(loc=mean, scale=max(std, 1e-6), size=n_per_feature))
    return np.stack(cols, axis=1)


def detect_feature_drift(
    model_name: str | Path,
    *,
    recent_days: int = 7,
    p_value_threshold: float = 0.01,
    shift_fraction_threshold: float = 0.30,
    max_rows: int = 500,
) -> DriftReport:
    """Compute drift for every feature on a trained model.

    Parameters
    ----------
    model_name : path to a ``.joblib`` trained model.
    recent_days : days of recent history to compare against training stats.
    p_value_threshold : per-feature KS p-value below which we flag a shift.
    shift_fraction_threshold : fraction of shifted features above which we
        mark the whole model as ``stale``.
    """
    path = Path(model_name)
    model = load_model(path)
    conn = _open_ro()
    try:
        X_recent, dates = _collect_recent_samples(
            model, recent_days, conn, max_rows=max_rows
        )
        X_train = _synthetic_train_sample(model, n_per_feature=max(200, X_recent.shape[0] or 200))
    finally:
        conn.close()

    features: list[FeatureDrift] = []
    for i, name in enumerate(model.feature_names):
        st = model.train_feature_stats.get(name, {})
        train_mean = st.get("mean")
        train_std = st.get("std")
        if X_recent.shape[0] == 0:
            features.append(
                FeatureDrift(
                    name=name,
                    train_mean=train_mean,
                    train_std=train_std,
                    recent_mean=None,
                    recent_std=None,
                    ks_statistic=None,
                    p_value=None,
                    is_shifted=False,
                )
            )
            continue
        recent_col = X_recent[:, i]
        train_col = X_train[:, i]
        r_mask = np.isfinite(recent_col)
        t_mask = np.isfinite(train_col)
        if r_mask.sum() < 5 or t_mask.sum() < 5:
            features.append(
                FeatureDrift(
                    name=name,
                    train_mean=train_mean,
                    train_std=train_std,
                    recent_mean=float(recent_col[r_mask].mean()) if r_mask.any() else None,
                    recent_std=float(recent_col[r_mask].std(ddof=0)) if r_mask.any() else None,
                    ks_statistic=None,
                    p_value=None,
                    is_shifted=False,
                )
            )
            continue
        try:
            ks = ks_2samp(train_col[t_mask], recent_col[r_mask])
            stat = float(ks.statistic)
            pval = float(ks.pvalue)
        except Exception:
            stat, pval = None, None
        shifted = (pval is not None) and (pval < p_value_threshold)
        features.append(
            FeatureDrift(
                name=name,
                train_mean=train_mean,
                train_std=train_std,
                recent_mean=float(recent_col[r_mask].mean()),
                recent_std=float(recent_col[r_mask].std(ddof=0)),
                ks_statistic=stat,
                p_value=pval,
                is_shifted=shifted,
            )
        )

    shifted = sum(1 for f in features if f.is_shifted)
    frac = shifted / len(features) if features else 0.0
    is_stale = frac >= shift_fraction_threshold

    report = DriftReport(
        model_path=str(path),
        recent_days=recent_days,
        n_recent=int(X_recent.shape[0]),
        n_features=len(features),
        n_shifted=shifted,
        shift_fraction=float(frac),
        is_stale=is_stale,
        features=features,
        recent_date_range=(min(dates) if dates else None, max(dates) if dates else None),
    )

    # Persist the drift report next to the model. This is WRITE but only to
    # the model metadata directory, not the live DB — which is the
    # read-only constraint we care about.
    try:
        sidecar = path.with_suffix(".drift.json")
        with open(sidecar, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "model_path": report.model_path,
                    "recent_days": report.recent_days,
                    "n_recent": report.n_recent,
                    "n_features": report.n_features,
                    "n_shifted": report.n_shifted,
                    "shift_fraction": report.shift_fraction,
                    "is_stale": report.is_stale,
                    "recent_date_range": list(report.recent_date_range),
                    "features": [f.__dict__ for f in report.features],
                    "evaluated_at": datetime.now(timezone.utc).isoformat(),
                },
                f,
                indent=2,
            )
    except Exception as exc:
        logger.warning("drift sidecar write failed for %s: %s", path, exc)

    return report


__all__ = ["DriftReport", "FeatureDrift", "detect_feature_drift"]
