"""Tests for :mod:`tools.ml_drift` — verify a planted distribution shift
gets flagged.

We construct a ``TrainedModel`` stub with known training stats, then hand
``detect_feature_drift`` a monkey-patched recent-sample collector that emits
a distribution whose mean is many standard deviations away from the stored
training mean. The drift detector's KS test should flag >30% of features,
triggering ``is_stale = True``.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tools.ml_classifier import TrainedModel  # noqa: E402
import tools.ml_drift as drift_mod  # noqa: E402


class _DummyModelObj:
    feature_importances_ = np.zeros(5)


def _fake_trained_model() -> TrainedModel:
    names = ["f0", "f1", "f2", "f3", "f4"]
    stats = {
        n: {"mean": 0.0, "std": 1.0, "n": 500}
        for n in names
    }
    return TrainedModel(
        model=_DummyModelObj(),
        feature_names=names,
        sport="test_sport",
        market="totals",
        stat_type=None,
        threshold=100.0,
        trained_at="20260101T000000Z",
        n_train=500,
        n_test=100,
        metrics={},
        train_date_range=("2026-01-01", "2026-03-01"),
        feature_importances=[(n, 0.0) for n in names],
        train_feature_stats=stats,
    )


def test_drift_flags_mean_shift(tmp_path, monkeypatch):
    tm = _fake_trained_model()
    model_path = tmp_path / "fake_model.joblib"

    # Patch joblib.load so detect_feature_drift returns our stub without
    # ever touching disk.
    def _load(_p):
        return tm
    monkeypatch.setattr(drift_mod, "load_model", _load)

    # Patch the recent-sample collector to return a HEAVILY shifted sample:
    # mean = 5 * train_std on every feature.
    def _fake_collect(model, recent_days, conn, max_rows=500):
        rng = np.random.default_rng(0)
        X = rng.normal(loc=5.0, scale=1.0, size=(200, len(model.feature_names)))
        dates = [f"2026-04-{(i % 20) + 1:02d}" for i in range(200)]
        return X, dates

    monkeypatch.setattr(drift_mod, "_collect_recent_samples", _fake_collect)
    # Avoid the real SQLite handshake
    monkeypatch.setattr(drift_mod, "_open_ro", lambda *a, **k: _FakeConn())

    report = drift_mod.detect_feature_drift(model_path)
    assert report.n_shifted >= 4, f"expected near-all features flagged, got {report.n_shifted}/{report.n_features}"
    assert report.shift_fraction >= 0.8
    assert report.is_stale is True


def test_drift_nominal_distribution(tmp_path, monkeypatch):
    tm = _fake_trained_model()
    model_path = tmp_path / "fake_model.joblib"

    monkeypatch.setattr(drift_mod, "load_model", lambda _p: tm)

    def _nominal(model, recent_days, conn, max_rows=500):
        rng = np.random.default_rng(1)
        X = rng.normal(loc=0.0, scale=1.0, size=(200, len(model.feature_names)))
        dates = [f"2026-04-{(i % 20) + 1:02d}" for i in range(200)]
        return X, dates

    monkeypatch.setattr(drift_mod, "_collect_recent_samples", _nominal)
    monkeypatch.setattr(drift_mod, "_open_ro", lambda *a, **k: _FakeConn())

    report = drift_mod.detect_feature_drift(model_path)
    # With matching distributions, KS should rarely reject H0 → not stale.
    assert report.is_stale is False
    assert report.shift_fraction < 0.3


class _FakeConn:
    def close(self):
        pass

    def execute(self, *a, **k):
        class _Cur:
            def fetchall(self_inner):
                return []

            def fetchone(self_inner):
                return None

        return _Cur()
