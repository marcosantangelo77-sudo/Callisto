"""Tests for :mod:`tools.ml_promotion_gate` — the wiring layer that runs
``tools.ml_backtest`` as an additional paper_trading → live gate and
falls back to the hand-crafted gate when no ML model exists or the
model's drift sidecar flags it stale.

We never load a real ``.joblib`` here — the point is to verify the
decision tree, not the XGBoost math (that is covered by
``test_ml_classifier``). ``tools.ml_backtest.ml_backtest`` is
monkey-patched to return a canned :class:`MLBacktestReport`.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import tools.ml_promotion_gate as gate_mod  # noqa: E402
from tools.ml_backtest import MLBacktestReport  # noqa: E402


def _fake_report(**overrides) -> MLBacktestReport:
    base = dict(
        model_path="fake.joblib",
        market="totals",
        sport="baseball_mlb",
        threshold=0.55,
        n_signals=200,
        n_resolved=180,
        hits=110,
        pushes=5,
        misses=65,
        hit_rate=0.60,
        roi_pct=4.5,
        clv_implied_mean=0.002,
        sharpe=1.2,
        per_day_pnl=[("2026-04-01", 3.5), ("2026-04-02", -1.0)],
    )
    base.update(overrides)
    return MLBacktestReport(**base)


@pytest.fixture
def models_dir(tmp_path, monkeypatch):
    monkeypatch.setenv(gate_mod.MODELS_DIR_ENV, str(tmp_path))
    return tmp_path


def _plant_model(models_dir: Path, sport: str, market: str, drift: dict | None):
    mp = models_dir / f"{sport}_{market}_20260401T000000Z.joblib"
    mp.write_bytes(b"stub")
    if drift is not None:
        mp.with_suffix(".drift.json").write_text(json.dumps(drift))
    return mp


def test_no_model_returns_not_applicable(models_dir):
    r = gate_mod.evaluate_ml_gate(
        hypothesis_id="h1",
        sport="baseball_mlb",
        market_type="totals",
        model_config={},
    )
    assert r["applicable"] is False
    assert r["ready"] is False
    assert r["stale_model"] is None
    assert any("no trained model" in x for x in r["reasons"])


def test_unsupported_market_type(models_dir):
    r = gate_mod.evaluate_ml_gate(
        hypothesis_id="h1",
        sport="baseball_mlb",
        market_type="spreads",
        model_config=None,
    )
    assert r["applicable"] is False
    assert any("no ML coverage" in x for x in r["reasons"])


def test_stale_model_falls_through(models_dir, monkeypatch):
    _plant_model(
        models_dir, "baseball_mlb", "totals",
        drift={"is_stale": True, "shift_fraction": 0.55, "n_recent": 120,
               "recent_date_range": ["2026-04-01", "2026-04-22"]},
    )
    # ml_backtest should NEVER be called on stale-flagged models.
    def _explode(*_a, **_kw):
        raise AssertionError("ml_backtest must not run on stale models")
    monkeypatch.setattr(gate_mod, "ml_backtest", _explode)
    r = gate_mod.evaluate_ml_gate(
        hypothesis_id="h1",
        sport="baseball_mlb",
        market_type="totals",
        model_config={},
    )
    assert r["applicable"] is False
    assert r["stale_model"] is not None
    assert r["ready"] is False


def test_passing_thresholds_marks_ready(models_dir, monkeypatch):
    _plant_model(models_dir, "baseball_mlb", "totals", drift=None)
    monkeypatch.setattr(
        gate_mod, "ml_backtest",
        lambda path, threshold=0.55: _fake_report(),
    )
    r = gate_mod.evaluate_ml_gate(
        hypothesis_id="h1",
        sport="baseball_mlb",
        market_type="totals",
        model_config={},
    )
    assert r["applicable"] is True
    assert r["ready"] is True
    assert any("PASS: ML hit_rate" in x for x in r["reasons"])
    assert any("PASS: ML CLV" in x for x in r["reasons"])
    assert any("PASS: ML Sharpe" in x for x in r["reasons"])


def test_low_hit_rate_fails(models_dir, monkeypatch):
    _plant_model(models_dir, "baseball_mlb", "totals", drift=None)
    monkeypatch.setattr(
        gate_mod, "ml_backtest",
        lambda path, threshold=0.55: _fake_report(hit_rate=0.48),
    )
    r = gate_mod.evaluate_ml_gate(
        hypothesis_id="h1", sport="baseball_mlb",
        market_type="totals", model_config={},
    )
    assert r["applicable"] is True
    assert r["ready"] is False
    assert any("FAIL: ML hit_rate" in x for x in r["reasons"])


def test_negative_clv_fails_when_signals_sufficient(models_dir, monkeypatch):
    _plant_model(models_dir, "baseball_mlb", "totals", drift=None)
    monkeypatch.setattr(
        gate_mod, "ml_backtest",
        lambda path, threshold=0.55: _fake_report(
            n_signals=150, clv_implied_mean=-0.001,
        ),
    )
    r = gate_mod.evaluate_ml_gate(
        hypothesis_id="h1", sport="baseball_mlb",
        market_type="totals", model_config={},
    )
    assert r["applicable"] is True
    assert r["ready"] is False
    assert any("FAIL: ML CLV" in x for x in r["reasons"])


def test_negative_clv_waived_at_low_signal_count(models_dir, monkeypatch):
    _plant_model(models_dir, "baseball_mlb", "totals", drift=None)
    monkeypatch.setattr(
        gate_mod, "ml_backtest",
        lambda path, threshold=0.55: _fake_report(
            n_signals=50, clv_implied_mean=-0.001,  # negative but under min_signals
        ),
    )
    r = gate_mod.evaluate_ml_gate(
        hypothesis_id="h1", sport="baseball_mlb",
        market_type="totals", model_config={},
    )
    assert r["applicable"] is True
    assert r["ready"] is True
    assert any("SKIP: ML CLV" in x for x in r["reasons"])


def test_negative_sharpe_fails(models_dir, monkeypatch):
    _plant_model(models_dir, "baseball_mlb", "totals", drift=None)
    monkeypatch.setattr(
        gate_mod, "ml_backtest",
        lambda path, threshold=0.55: _fake_report(sharpe=-0.5),
    )
    r = gate_mod.evaluate_ml_gate(
        hypothesis_id="h1", sport="baseball_mlb",
        market_type="totals", model_config={},
    )
    assert r["applicable"] is True
    assert r["ready"] is False
    assert any("FAIL: ML Sharpe" in x for x in r["reasons"])


def test_player_prop_mapping_requires_stat_type(models_dir):
    r = gate_mod.evaluate_ml_gate(
        hypothesis_id="h1", sport="basketball_nba",
        market_type="player_props", model_config={},
    )
    assert r["applicable"] is False
    assert any("no ML coverage" in x for x in r["reasons"])


def test_player_prop_with_stat_type_finds_model(models_dir, monkeypatch):
    _plant_model(models_dir, "basketball_nba", "player_prop_points", drift=None)
    monkeypatch.setattr(
        gate_mod, "ml_backtest",
        lambda path, threshold=0.55: _fake_report(
            market="player_prop_points", sport="basketball_nba",
        ),
    )
    r = gate_mod.evaluate_ml_gate(
        hypothesis_id="h1", sport="basketball_nba",
        market_type="player_props",
        model_config={"stat_type": "points"},
    )
    assert r["applicable"] is True
    assert r["ready"] is True
    assert "basketball_nba_player_prop_points_" in r["model_path"]


def test_list_stale_models(models_dir):
    _plant_model(
        models_dir, "baseball_mlb", "totals",
        drift={"is_stale": True, "shift_fraction": 0.5, "n_recent": 50,
               "recent_date_range": ["2026-04-01", "2026-04-22"],
               "evaluated_at": "2026-04-22T12:00Z",
               "model_path": "ignored"},
    )
    _plant_model(
        models_dir, "basketball_nba", "totals",
        drift={"is_stale": False, "shift_fraction": 0.1, "n_recent": 100},
    )
    stale = gate_mod.list_stale_models()
    assert len(stale) == 1
    assert stale[0]["shift_fraction"] == 0.5
    assert stale[0]["n_recent"] == 50


def test_ml_backtest_exception_is_swallowed(models_dir, monkeypatch):
    _plant_model(models_dir, "baseball_mlb", "totals", drift=None)
    def _raise(*_a, **_kw):
        raise RuntimeError("xgboost exploded")
    monkeypatch.setattr(gate_mod, "ml_backtest", _raise)
    r = gate_mod.evaluate_ml_gate(
        hypothesis_id="h1", sport="baseball_mlb",
        market_type="totals", model_config={},
    )
    assert r["applicable"] is False
    assert "xgboost exploded" in (r["error"] or "")
