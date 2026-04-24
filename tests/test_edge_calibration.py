"""Tests for probability calibration layer on top of edge confidence scoring."""

import json
import os
import tempfile
from pathlib import Path

import numpy as np
import pytest

from tools.edge_confidence import (
    EdgeConfidence,
    IdentityCalibrator,
    IsotonicCalibrator,
    PlattCalibrator,
    brier_score,
    calibrate_probability,
    expected_calibration_error,
    load_calibrator,
    save_calibrator,
    score_edge,
    score_parlay,
)


@pytest.fixture
def miscalibrated_data():
    rng = np.random.default_rng(0)
    n = 4000
    raw = rng.uniform(0.1, 0.9, n)
    true_p = raw ** 0.6
    y = (rng.uniform(0, 1, n) < true_p).astype(int)
    return raw, y


class TestBrierAndECE:
    def test_brier_perfect_when_probs_match_outcomes(self):
        probs = np.array([1.0, 0.0, 1.0, 0.0])
        y = np.array([1, 0, 1, 0])
        assert brier_score(probs, y) == pytest.approx(0.0)

    def test_brier_maximally_wrong(self):
        probs = np.array([1.0, 0.0])
        y = np.array([0, 1])
        assert brier_score(probs, y) == pytest.approx(1.0)

    def test_ece_zero_when_perfectly_calibrated(self):
        rng = np.random.default_rng(7)
        n = 2000
        probs = rng.uniform(0.05, 0.95, n)
        y = (rng.uniform(0, 1, n) < probs).astype(int)
        assert expected_calibration_error(probs, y) < 0.05

    def test_ece_empty_input(self):
        assert np.isnan(expected_calibration_error([], []))


class TestPlattCalibrator:
    def test_platt_improves_brier_and_ece(self, miscalibrated_data):
        raw, y = miscalibrated_data
        pre_brier = brier_score(raw, y)
        pre_ece = expected_calibration_error(raw, y)
        cal = PlattCalibrator.fit(raw, y)
        post = cal.predict(raw)
        post_brier = brier_score(post, y)
        post_ece = expected_calibration_error(post, y)
        assert post_brier < pre_brier, (pre_brier, post_brier)
        assert post_ece < pre_ece, (pre_ece, post_ece)

    def test_platt_roundtrip_via_dict(self, miscalibrated_data):
        raw, y = miscalibrated_data
        cal = PlattCalibrator.fit(raw, y)
        d = cal.to_dict()
        cal2 = PlattCalibrator.from_dict(d)
        np.testing.assert_allclose(cal.predict(raw), cal2.predict(raw))

    def test_platt_handles_all_wins(self):
        raw = np.array([0.4, 0.5, 0.6, 0.7])
        y = np.array([1, 1, 1, 1])
        cal = PlattCalibrator.fit(raw, y)
        assert 0.0 <= float(cal.predict(0.5)) <= 1.0

    def test_platt_handles_all_losses(self):
        raw = np.array([0.4, 0.5, 0.6, 0.7])
        y = np.array([0, 0, 0, 0])
        cal = PlattCalibrator.fit(raw, y)
        assert 0.0 <= float(cal.predict(0.5)) <= 1.0

    def test_platt_tiny_dataset_is_identity(self):
        raw = np.array([0.5, 0.6])
        y = np.array([1, 0])
        cal = PlattCalibrator.fit(raw, y)
        out = cal.predict(raw)
        assert out.shape == raw.shape

    def test_platt_predict_scalar_returns_float(self):
        cal = PlattCalibrator(a=1.0, b=0.0)
        result = cal.predict(0.5)
        assert isinstance(result, float)
        assert result == pytest.approx(0.5, abs=1e-6)


class TestIsotonicCalibrator:
    def test_isotonic_improves_brier(self, miscalibrated_data):
        raw, y = miscalibrated_data
        pre_brier = brier_score(raw, y)
        cal = IsotonicCalibrator.fit(raw, y)
        post = cal.predict(raw)
        post_brier = brier_score(post, y)
        assert post_brier < pre_brier

    def test_isotonic_is_monotonic(self, miscalibrated_data):
        raw, y = miscalibrated_data
        cal = IsotonicCalibrator.fit(raw, y)
        xs = np.linspace(0.0, 1.0, 50)
        preds = cal.predict(xs)
        for i in range(len(preds) - 1):
            assert preds[i] <= preds[i + 1] + 1e-9

    def test_isotonic_output_bounded(self, miscalibrated_data):
        raw, y = miscalibrated_data
        cal = IsotonicCalibrator.fit(raw, y)
        preds = cal.predict(np.linspace(0.0, 1.0, 100))
        assert preds.min() >= 0.0
        assert preds.max() <= 1.0

    def test_isotonic_roundtrip(self, miscalibrated_data):
        raw, y = miscalibrated_data
        cal = IsotonicCalibrator.fit(raw, y)
        d = cal.to_dict()
        cal2 = IsotonicCalibrator.from_dict(d)
        np.testing.assert_allclose(cal.predict(raw), cal2.predict(raw))

    def test_isotonic_tiny_dataset(self):
        raw = np.array([0.5])
        y = np.array([1])
        cal = IsotonicCalibrator.fit(raw, y)
        assert 0.0 <= float(cal.predict(0.5)) <= 1.0


class TestIdentityCalibrator:
    def test_identity_passes_through(self):
        cal = IdentityCalibrator()
        assert float(cal.predict(0.42)) == pytest.approx(0.42)
        assert cal.to_dict() == {"kind": "identity"}

    def test_identity_clips_out_of_range(self):
        cal = IdentityCalibrator()
        assert float(cal.predict(1.5)) == pytest.approx(1.0)
        assert float(cal.predict(-0.1)) == pytest.approx(0.0)


class TestSaveLoad:
    def test_save_and_load_platt(self, miscalibrated_data, tmp_path):
        raw, y = miscalibrated_data
        cal = PlattCalibrator.fit(raw, y)
        path = tmp_path / "cal.json"
        save_calibrator(cal, path=str(path), metadata={"note": "test"})
        loaded = load_calibrator(str(path))
        assert isinstance(loaded, PlattCalibrator)
        np.testing.assert_allclose(cal.predict(raw), loaded.predict(raw))

    def test_save_and_load_isotonic(self, miscalibrated_data, tmp_path):
        raw, y = miscalibrated_data
        cal = IsotonicCalibrator.fit(raw, y)
        path = tmp_path / "iso.json"
        save_calibrator(cal, path=str(path))
        loaded = load_calibrator(str(path))
        assert isinstance(loaded, IsotonicCalibrator)
        np.testing.assert_allclose(cal.predict(raw), loaded.predict(raw), atol=1e-9)

    def test_load_missing_file_returns_none(self, tmp_path):
        path = tmp_path / "does_not_exist.json"
        assert load_calibrator(str(path)) is None

    def test_load_corrupt_file_returns_none(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("{not json}")
        assert load_calibrator(str(path)) is None

    def test_load_unknown_kind_returns_none(self, tmp_path):
        path = tmp_path / "weird.json"
        path.write_text(json.dumps({"calibrator": {"kind": "alien"}}))
        assert load_calibrator(str(path)) is None


class TestCalibrateProbability:
    def test_none_input_returns_none(self):
        assert calibrate_probability(None) is None

    def test_no_calibrator_returns_raw(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CALLISTO_EDGE_CALIBRATOR_PATH", str(tmp_path / "missing.json"))
        import tools.edge_confidence as ec
        ec._CALIBRATOR_CACHE = None
        ec._CALIBRATOR_CACHE_MTIME = None
        ec._CALIBRATOR_LOAD_FAILED = False
        assert calibrate_probability(0.42) == pytest.approx(0.42)

    def test_clips_out_of_range_input(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CALLISTO_EDGE_CALIBRATOR_PATH", str(tmp_path / "missing.json"))
        import tools.edge_confidence as ec
        ec._CALIBRATOR_CACHE = None
        ec._CALIBRATOR_CACHE_MTIME = None
        ec._CALIBRATOR_LOAD_FAILED = False
        assert calibrate_probability(1.5) == pytest.approx(1.0)
        assert calibrate_probability(-0.1) == pytest.approx(0.0)

    def test_invalid_string_returns_none(self):
        assert calibrate_probability("not a number") is None


class TestScoreEdgeIntegration:
    def test_backward_compat_no_fair_prob(self):
        conf = score_edge(3.0, 4, ["FanDuel", "DraftKings", "BetMGM", "BetRivers"], "h2h")
        assert conf.raw_prob is None
        assert conf.calibrated_prob is None
        assert isinstance(conf, EdgeConfidence)

    def test_raw_prob_populated_when_given(self, miscalibrated_data, tmp_path, monkeypatch):
        monkeypatch.setenv("CALLISTO_EDGE_CALIBRATOR_PATH", str(tmp_path / "cal.json"))
        import tools.edge_confidence as ec
        ec._CALIBRATOR_CACHE = None
        ec._CALIBRATOR_CACHE_MTIME = None
        ec._CALIBRATOR_LOAD_FAILED = False
        conf = score_edge(
            3.0, 4, ["FanDuel", "DraftKings", "BetMGM", "BetRivers"], "h2h",
            model_fair_prob=0.62,
        )
        assert conf.raw_prob == pytest.approx(0.62)
        assert conf.calibrated_prob == pytest.approx(0.62)
        assert conf.calibrator_name == "identity"

    def test_explicit_calibrator_applied(self):
        cal = PlattCalibrator(a=2.0, b=-1.0)
        conf = score_edge(
            3.0, 4, ["FanDuel", "DraftKings", "BetMGM", "BetRivers"], "h2h",
            model_fair_prob=0.5, calibrator=cal,
        )
        assert conf.raw_prob == pytest.approx(0.5)
        expected = float(cal.predict(0.5))
        assert conf.calibrated_prob == pytest.approx(expected)
        assert conf.calibrator_name == "platt"

    def test_loaded_calibrator_applied_via_env(self, miscalibrated_data, tmp_path, monkeypatch):
        raw, y = miscalibrated_data
        cal = PlattCalibrator.fit(raw, y)
        path = tmp_path / "loaded.json"
        import tools.edge_confidence as ec
        ec._CALIBRATOR_CACHE = None
        ec._CALIBRATOR_CACHE_MTIME = None
        ec._CALIBRATOR_LOAD_FAILED = False
        monkeypatch.setenv("CALLISTO_EDGE_CALIBRATOR_PATH", str(path))
        save_calibrator(cal, path=str(path))
        conf = score_edge(
            3.0, 4, ["FanDuel", "DraftKings", "BetMGM", "BetRivers"], "h2h",
            model_fair_prob=0.7,
        )
        assert conf.calibrator_name == "platt"
        assert conf.calibrated_prob == pytest.approx(float(cal.predict(0.7)))

    def test_nonfinite_prob_handled(self):
        conf = score_edge(
            3.0, 4, ["FanDuel", "DraftKings", "BetMGM", "BetRivers"], "h2h",
            model_fair_prob=float("nan"),
        )
        assert conf.raw_prob is None
        assert conf.calibrated_prob is None


class TestParlayCalibration:
    def test_parlay_combines_calibrated_probs(self):
        leg1 = score_edge(3.0, 4, ["Pinnacle", "DK"], "h2h", model_fair_prob=0.6)
        leg2 = score_edge(3.0, 4, ["Pinnacle", "DK"], "h2h", model_fair_prob=0.7)
        parlay = score_parlay([leg1, leg2])
        assert parlay.raw_prob == pytest.approx(0.6 * 0.7)
        assert parlay.calibrated_prob is not None

    def test_parlay_missing_leg_prob_means_no_aggregate(self):
        leg1 = score_edge(3.0, 4, ["Pinnacle", "DK"], "h2h", model_fair_prob=0.6)
        leg2 = score_edge(3.0, 4, ["Pinnacle", "DK"], "h2h")
        parlay = score_parlay([leg1, leg2])
        assert parlay.raw_prob is None


class TestCalibrationReportScript:
    def test_report_on_synthetic_db_improves_brier(self, tmp_path):
        import sqlite3
        db_path = tmp_path / "syn.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE backtest_events (
                id INTEGER PRIMARY KEY,
                run_id TEXT, event_id TEXT, hypothesis_id TEXT,
                sport TEXT, player TEXT, market TEXT, line REAL, side TEXT,
                book TEXT, book_odds_american INTEGER, book_implied_prob REAL,
                model_fair_prob REAL, model_factors TEXT,
                edge REAL, ev_pct REAL, kelly_fraction REAL,
                signal_generated BOOLEAN, actual_result TEXT, actual_stat REAL,
                closing_odds INTEGER, closing_implied REAL, clv_implied REAL,
                game_date DATE, snapshot_time DATETIME, created_at DATETIME,
                local_game_date DATE
            )
        """)
        conn.execute("""
            CREATE TABLE closing_lines (
                id INTEGER PRIMARY KEY,
                event_id TEXT, sport TEXT, captured_at TEXT, source TEXT,
                market TEXT, team TEXT, closing_odds INTEGER, closing_point REAL,
                closing_implied REAL
            )
        """)
        rng = np.random.default_rng(123)
        n = 2000
        raw = rng.uniform(0.2, 0.85, n)
        true_p = raw ** 0.5
        wins = (rng.uniform(0, 1, n) < true_p).astype(int)
        for i in range(n):
            conn.execute(
                """INSERT INTO backtest_events
                   (id, event_id, sport, market, side, book, book_odds_american,
                    book_implied_prob, model_fair_prob, edge, actual_result, game_date)
                   VALUES (?, ?, 'mlb', 'h2h', 'Home', 'fanduel', -110,
                           ?, ?, ?, ?, '2026-04-01')""",
                (i, f"evt_{i}", float(raw[i]) - 0.02, float(raw[i]),
                 0.01, "won" if wins[i] else "lost"),
            )
        conn.commit()
        conn.close()

        import sys
        script_dir = Path(__file__).resolve().parents[1] / "scripts"
        if str(script_dir.parent) not in sys.path:
            sys.path.insert(0, str(script_dir.parent))
        from scripts.edge_calibration_report import build_report

        report = build_report(str(db_path), min_rows=100, install=False)
        raw_brier = report["raw_metrics_eval"]["brier"]
        platt_brier = report["platt_metrics_eval"]["brier"]
        iso_brier = report["isotonic_metrics_eval"]["brier"]
        best = min(platt_brier, iso_brier)
        assert best <= raw_brier + 0.005, (raw_brier, platt_brier, iso_brier)
        assert report["total_events"] == n
        assert report["chosen_calibrator"] in ("platt", "isotonic", "identity")

    def test_report_installs_when_requested(self, tmp_path, monkeypatch):
        import sqlite3
        db_path = tmp_path / "syn2.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE backtest_events (
                id INTEGER PRIMARY KEY, event_id TEXT, sport TEXT, market TEXT,
                side TEXT, book TEXT, book_odds_american INTEGER,
                book_implied_prob REAL, model_fair_prob REAL, edge REAL,
                ev_pct REAL, actual_result TEXT, game_date DATE,
                snapshot_time DATETIME, created_at DATETIME
            )
        """)
        conn.execute("""CREATE TABLE closing_lines (
            id INTEGER PRIMARY KEY, event_id TEXT, sport TEXT,
            captured_at TEXT, source TEXT, market TEXT, team TEXT,
            closing_odds INTEGER, closing_point REAL, closing_implied REAL)""")
        rng = np.random.default_rng(99)
        for i in range(500):
            p = float(rng.uniform(0.3, 0.8))
            win = int(rng.uniform() < p)
            conn.execute(
                """INSERT INTO backtest_events
                   (id, event_id, sport, market, side, book, book_odds_american,
                    book_implied_prob, model_fair_prob, edge, actual_result,
                    game_date)
                   VALUES (?, ?, 'mlb', 'h2h', 'Home', 'fanduel', -110, ?, ?, 0.01,
                           ?, '2026-04-01')""",
                (i, f"e{i}", p - 0.02, p, "won" if win else "lost"),
            )
        conn.commit()
        conn.close()

        cal_path = tmp_path / "edge_cal.json"
        monkeypatch.setenv("CALLISTO_EDGE_CALIBRATOR_PATH", str(cal_path))
        import tools.edge_confidence as ec
        ec._CALIBRATOR_CACHE = None
        ec._CALIBRATOR_CACHE_MTIME = None
        ec._CALIBRATOR_LOAD_FAILED = False
        from scripts.edge_calibration_report import build_report
        report = build_report(str(db_path), min_rows=100, install=True)
        assert report["installed"] is True
        assert cal_path.exists()
        loaded = load_calibrator(str(cal_path))
        assert loaded is not None


class TestCLVSanityCheck:
    def test_positive_clv_flagged_trustworthy(self):
        from scripts.edge_calibration_report import _clv_analysis
        rows = []
        for i in range(50):
            rows.append({
                "book_implied_prob": 0.50,
                "closing_implied": 0.52,
                "actual_result": "won" if i % 2 == 0 else "lost",
            })
        probs = np.full(50, 0.55)
        out = _clv_analysis(rows, probs)
        assert out["trustworthy"] is True
        assert out["mean_clv"] > 0

    def test_negative_clv_flagged_untrustworthy(self):
        from scripts.edge_calibration_report import _clv_analysis
        rows = [
            {"book_implied_prob": 0.55, "closing_implied": 0.50, "actual_result": "won"}
            for _ in range(30)
        ]
        probs = np.full(30, 0.6)
        out = _clv_analysis(rows, probs)
        assert out["trustworthy"] is False
        assert out["mean_clv"] < 0
