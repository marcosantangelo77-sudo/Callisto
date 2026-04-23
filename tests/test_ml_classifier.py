"""Tests for :mod:`tools.ml_classifier` with synthetic, LEARNABLE data.

Instead of reaching into ml_features (which talks to SQLite), we bypass it
by calling the private walk-forward + XGBoost helper directly on a matrix
we construct. Two properties we verify:

  1. When the label is a simple deterministic function of one feature, the
     classifier achieves CV AUC > 0.7 — i.e. XGBoost is wired up correctly.
  2. The time-series split never lets a fold train on rows dated after any
     row in its own test set (no temporal leakage).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tools.ml_classifier import (  # noqa: E402
    _fit_xgb,
    _walk_forward_eval,
    evaluate,
)


def _make_learnable(n=1200, seed=7):
    """Label = 1 iff feature_0 + small-noise > 0. Trivially learnable."""
    rng = np.random.default_rng(seed)
    X = rng.normal(size=(n, 8))
    noise = rng.normal(scale=0.25, size=n)
    y = (X[:, 0] + noise > 0).astype(int)
    # Distinct dates across rows so time-series split works.
    dates = np.array([f"2026-03-{(i % 27) + 1:02d}" for i in range(n)], dtype=object)
    return X, y.astype(float), dates


def test_xgb_learns_obvious_signal():
    X, y, _ = _make_learnable()
    # Use half/half split by index for this sanity check
    n = X.shape[0]
    tr, te = slice(0, int(n * 0.7)), slice(int(n * 0.7), n)
    model = _fit_xgb(X[tr], y[tr].astype(int))
    m = evaluate(model, X[te], y[te])
    assert m["auc"] is not None and m["auc"] > 0.7, m


def test_walk_forward_no_lookahead():
    X, y, dates = _make_learnable()
    # threshold 0.5 against {0,1} y is equivalent to the label itself
    model, metrics, n_train, n_test = _walk_forward_eval(
        X, y, dates, threshold=0.5, n_splits=4
    )
    # At least one fold should have been evaluated and each fold's
    # train_last_date must be <= its test_first_date.
    folds = metrics.get("folds", [])
    assert folds, "expected at least one fold"
    for f in folds:
        assert f["train_last_date"] <= f["test_first_date"], f
        assert f.get("auc") is None or f["auc"] > 0.6, f
    assert n_train == X.shape[0]


def test_evaluate_handles_single_class():
    # XGBoost refuses a single-class y; that's expected. We only want to
    # assert that our evaluate() helper degrades gracefully when given
    # a y array that's all one class but a model trained on balanced data.
    rng = np.random.default_rng(0)
    X_train = rng.normal(size=(100, 4))
    y_train = (X_train[:, 0] > 0).astype(int)
    model = _fit_xgb(X_train, y_train)
    # Eval set with single class — AUC should be None (undefined), not crash.
    X_eval = rng.normal(size=(30, 4))
    y_eval_single = np.ones(30, dtype=float)
    m = evaluate(model, X_eval, y_eval_single)
    assert m["auc"] is None
    assert m["base_rate"] == 1.0
