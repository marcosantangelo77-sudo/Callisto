"""Tests for the tools.golf split of golf_masters."""

import sqlite3

from tools import golf_masters
from tools.golf import (
    backtest,
    db as golf_db,
    field,
    historical,
    predictions,
)


def test_facade_reexports_all_public_api():
    expected = [
        "DB_PATH",
        "MASTERS_SCHEMA",
        "ensure_masters_schema",
        "fetch_masters_historical",
        "_normalize_player_name",
        "_parse_position",
        "_get_embedded_masters_data",
        "_fetch_espn_masters_year",
        "_fetch_masters_year_fallback",
        "fetch_current_season_stats",
        "fetch_masters_field",
        "_spearman_rank_correlation",
        "_compute_masters_fit_score_for_player",
        "leave_one_out_backtest",
        "rolling_window_backtest",
        "generate_2026_predictions",
        "compute_masters_fit_score",
    ]
    for name in expected:
        assert hasattr(golf_masters, name), f"facade missing {name}"
        assert name in golf_masters.__all__


def test_submodules_are_distinct_and_wired():
    # facade symbols point at the split implementations, not local copies
    assert golf_masters.fetch_masters_historical is historical.fetch_masters_historical
    assert golf_masters.ensure_masters_schema is golf_db.ensure_masters_schema
    assert golf_masters.leave_one_out_backtest is backtest.leave_one_out_backtest
    assert golf_masters.rolling_window_backtest is backtest.rolling_window_backtest
    assert golf_masters.generate_2026_predictions is predictions.generate_2026_predictions
    assert golf_masters.compute_masters_fit_score is predictions.compute_masters_fit_score
    assert golf_masters.fetch_current_season_stats is field.fetch_current_season_stats
    assert golf_masters.fetch_masters_field is field.fetch_masters_field


def test_normalize_player_name():
    assert golf_masters._normalize_player_name("Tiger Woods (a)") == "Tiger Woods"
    assert golf_masters._normalize_player_name("  Rory   McIlroy ") == "Rory McIlroy"


def test_parse_position():
    assert golf_masters._parse_position("1") == ("1", 1, True)
    assert golf_masters._parse_position("T10") == ("T10", 10, True)
    assert golf_masters._parse_position("CUT") == ("CUT", 999, False)
    assert golf_masters._parse_position("WD") == ("WD", 998, False)
    assert golf_masters._parse_position("DQ") == ("DQ", 997, False)
    assert golf_masters._parse_position("") == ("", 999, False)


def test_embedded_masters_data_covers_expected_years():
    data = golf_masters._get_embedded_masters_data()
    assert set(range(2010, 2026)) <= set(data)
    for year, entries in data.items():
        assert len(entries) >= 5, year
        winner = [e for e in entries if e["position"] == "1"]
        assert len(winner) == 1, year


def test_spearman_rank_correlation():
    preds = [("a", 90), ("b", 80), ("c", 70), ("d", 60)]
    actuals = [("a", 1), ("b", 2), ("c", 3), ("d", 4)]
    assert golf_masters._spearman_rank_correlation(preds, actuals) == 1.0
    reversed_actuals = [("a", 4), ("b", 3), ("c", 2), ("d", 1)]
    assert golf_masters._spearman_rank_correlation(preds, reversed_actuals) == -1.0
    # too few common players -> 0.0
    assert golf_masters._spearman_rank_correlation(preds[:1], actuals[:1]) == 0.0


def _seed_db(path):
    conn = sqlite3.connect(path)
    golf_db.ensure_masters_schema(str(path))
    data = golf_masters._get_embedded_masters_data()
    from tools.golf.historical import _fetch_masters_year_fallback

    for year in sorted(data):  # seed all embedded years
        _fetch_masters_year_fallback(year, conn)
    conn.commit()
    conn.close()


def test_leave_one_out_backtest(tmp_path):
    db_path = tmp_path / "golf.db"
    _seed_db(db_path)
    result = golf_masters.leave_one_out_backtest(
        "h_test_loo",
        {"type": "general", "name": "Test LOO"},
        years=range(2010, 2016),
        db_path=str(db_path),
    )
    assert "error" not in result
    assert result["n_folds"] >= 3
    for key in (
        "avg_top10_accuracy",
        "avg_top10_recall",
        "avg_rank_correlation",
    ):
        assert key in result
    assert -1.0 <= result["avg_rank_correlation"] <= 1.0


def test_rolling_window_backtest(tmp_path):
    db_path = tmp_path / "golf.db"
    _seed_db(db_path)
    result = golf_masters.rolling_window_backtest(
        "h_test_roll",
        {"type": "general", "name": "Test Rolling"},
        train_window=3,
        years=range(2010, 2016),
        db_path=str(db_path),
    )
    assert "error" not in result
    assert result["n_folds"] >= 1
    assert -1.0 <= result["avg_rank_correlation"] <= 1.0


def test_fit_score_bounds(tmp_path):
    db_path = tmp_path / "golf.db"
    _seed_db(db_path)
    conn = sqlite3.connect(str(db_path))
    all_historical = {}
    for year in range(2010, 2016):
        rows = conn.execute(
            "SELECT * FROM masters_historical WHERE year = ?", (year,)
        ).fetchall()
        cols = [
            d[0]
            for d in conn.execute("SELECT * FROM masters_historical LIMIT 0").description
        ]
        all_historical[year] = [dict(zip(cols, row)) for row in rows]
    conn.close()

    score = golf_masters._compute_masters_fit_score_for_player(
        "Tiger Woods", list(all_historical), all_historical, {"type": "general"}
    )
    assert 0.0 <= score <= 100.0
    unknown = golf_masters._compute_masters_fit_score_for_player(
        "Nobody Here", list(all_historical), all_historical, {"type": "general"}
    )
    assert 0.0 <= unknown <= 100.0


def test_generate_predictions_and_composite(tmp_path):
    db_path = tmp_path / "golf.db"
    _seed_db(db_path)
    result = golf_masters.generate_2026_predictions(
        "h_test_pred",
        {"type": "general", "name": "Test Pred"},
        db_path=str(db_path),
    )
    assert result["field_size"] > 0
    preds = result["predictions"]
    scores = [p["masters_fit_score"] for p in preds]
    assert scores == sorted(scores, reverse=True)
    top = preds[0]
    for key in ("predicted_rank", "win_prob", "top10_prob", "cut_prob"):
        assert key in top

    # composite scorer reads the app-wide hypotheses table; seed a minimal one
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS hypotheses "
        "(hypothesis_id TEXT PRIMARY KEY, name TEXT, sport TEXT, status TEXT)"
    )
    conn.execute(
        "INSERT OR REPLACE INTO hypotheses VALUES "
        "('h_test_pred', 'Test Pred', 'golf_pga_masters', 'active')"
    )
    conn.commit()
    conn.close()

    composite = golf_masters.compute_masters_fit_score(
        preds[0]["player"], year=2026, db_path=str(db_path)
    )
    assert composite.get("player") == preds[0]["player"]
    assert composite.get("composite_score") is not None


def test_fetch_masters_field_creates_field(tmp_path):
    db_path = tmp_path / "field.db"
    result = golf_masters.fetch_masters_field(year=2026, db_path=str(db_path))
    assert result["status"] == "created"
    assert result["players"] >= 40
    cached = golf_masters.fetch_masters_field(year=2026, db_path=str(db_path))
    assert cached == {"status": "cached", "players": result["players"]}
