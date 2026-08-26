"""Tests for the tools/temporal split of temporal_analysis.

Verifies that:
1. The facade (tools.temporal_analysis) re-exports the full public API.
2. Each submodule behaves identically to the original monolith.
3. Temporal isolation guarantees hold (gap enforcement, Bonferroni, etc.).
"""

import json
import math
import sqlite3

import polars as pl
import pytest

import tools.temporal_analysis as facade
from tools.temporal.loading import load_game_results
from tools.temporal.patterns import (
    _binomial_pvalue,
    _bonferroni_finalize,
    cross_tabulate,
    find_ats_patterns,
    find_player_prop_patterns,
)
from tools.temporal.splits import create_temporal_split, rolling_window_splits
from tools.temporal.stats import _erfc, _norm_sf
from tools.temporal.validation import validate_temporal_isolation


# ──────────────────────────────────────────────────
# Facade re-export contract
# ──────────────────────────────────────────────────

PUBLIC_API = [
    "DB_PATH",
    "_connect",
    "load_backtest_events",
    "load_game_results",
    "load_odds_snapshots",
    "load_player_stats",
    "create_temporal_split",
    "rolling_window_splits",
    "_erfc",
    "_norm_sf",
    "_binomial_pvalue",
    "_bonferroni_finalize",
    "find_ats_patterns",
    "find_player_prop_patterns",
    "cross_tabulate",
    "generate_hypotheses_from_analysis",
    "validate_temporal_isolation",
    "get_data_summary",
]


@pytest.mark.parametrize("name", PUBLIC_API)
def test_facade_reexports_public_api(name):
    assert hasattr(facade, name), f"facade missing {name}"
    # Facade names must be the same objects as the submodule implementations.
    if name == "DB_PATH":
        return
    assert getattr(facade, name) is not None


def test_facade_matches_submodule_objects():
    assert facade.create_temporal_split is create_temporal_split
    assert facade.find_ats_patterns is find_ats_patterns
    assert facade.validate_temporal_isolation is validate_temporal_isolation
    assert facade._binomial_pvalue is _binomial_pvalue
    assert facade.load_game_results is load_game_results


def test_never_add_live_to_paper_trade_statuses():
    """Guard: this refactor must not touch paper-trade status semantics."""
    import tools.temporal_analysis as t

    statuses = getattr(t, "_PAPER_TRADE_SIGNAL_STATUSES", None)
    if statuses is not None:
        assert "live" not in statuses


# ──────────────────────────────────────────────────
# stats module
# ──────────────────────────────────────────────────

def test_erfc_matches_math_erfc():
    for x in [-3.0, -1.0, -0.5, 0.0, 0.5, 1.0, 3.0]:
        assert abs(_erfc(x) - math.erfc(x)) < 1e-5


def test_norm_sf_known_values():
    assert abs(_norm_sf(0.0) - 0.5) < 1e-9
    assert abs(_norm_sf(1.96) - 0.025) < 1e-3
    assert _norm_sf(10.0) < 1e-10


def test_binomial_pvalue_edges():
    assert _binomial_pvalue(0, 0) == 1.0
    assert _binomial_pvalue(5, 10, 0.0) == 1.0
    assert _binomial_pvalue(5, 10, 1.0) == 1.0


def test_binomial_pvalue_monotone_in_wins():
    # More wins in the same number of trials -> smaller right-tail p-value
    assert _binomial_pvalue(16, 20) < _binomial_pvalue(12, 20) < _binomial_pvalue(10, 20)
    assert _binomial_pvalue(16, 20) < 0.01
    assert _binomial_pvalue(10, 20) == pytest.approx(0.5885, abs=0.01)


# ──────────────────────────────────────────────────
# splits module
# ──────────────────────────────────────────────────

@pytest.fixture
def games_df():
    rows = []
    d = pl.date  # noqa: F841
    from datetime import date, timedelta

    start = date(2024, 1, 1)
    for i in range(120):
        day = (start + timedelta(days=i)).strftime("%Y-%m-%d")
        home, away = i % 2, (i + 1) % 2
        rows.append({
            "id": i,
            "sport": "basketball_nba",
            "game_date": day,
            "home_team": f"Team{home}",
            "away_team": f"Team{away}",
            "home_score": 100 + (i % 7),
            "away_score": 95 + (i % 5),
            "total_score": 195 + (i % 7) + (i % 5),
            "spread_result": float((i % 7) - (i % 5)),
            "winner": f"Team{home}" if i % 7 > i % 5 else f"Team{away}",
            "source": "test",
        })
    return pl.DataFrame(rows)


def test_create_temporal_split_gap(games_df):
    train, test, meta = create_temporal_split(games_df, "2024-03-01", min_gap_days=7)

    assert meta["train_end_date"] == "2024-03-01"
    assert meta["test_start_date"] == "2024-03-08"
    assert meta["gap_days"] == 7

    max_train = train.select(pl.col("game_date").max()).item()
    min_test = test.select(pl.col("game_date").min()).item()
    assert max_train <= "2024-03-01"
    assert min_test >= "2024-03-08"

    # Gap rows excluded: 2024-03-02..2024-03-07 => 6 days of games
    assert meta["gap_rows_excluded"] == games_df.filter(
        (pl.col("game_date") > "2024-03-01") & (pl.col("game_date") < "2024-03-08")
    ).height
    assert train.height + test.height + meta["gap_rows_excluded"] == games_df.height


def test_create_temporal_split_empty():
    df = pl.DataFrame(schema={"game_date": pl.Utf8})
    train, test, meta = create_temporal_split(df, "2024-01-01")
    assert train.height == 0 and test.height == 0


def test_rolling_window_splits_no_overlap_leakage(games_df):
    folds = rolling_window_splits(
        games_df, window_size_days=30, step_days=15, test_size_days=15, gap_days=7
    )
    assert len(folds) > 1
    for train, test, meta in folds:
        assert train.height > 0 and test.height > 0
        assert train.select(pl.col("game_date").max()).item() <= meta["train_end_date"]
        assert test.select(pl.col("game_date").min()).item() >= meta["test_start_date"]
        assert (
            train.select(pl.col("game_date").max()).item()
            < test.select(pl.col("game_date").min()).item()
        )
        assert meta["fold_index"] >= 0
    # fold indices are sequential
    assert [m["fold_index"] for _, _, m in folds] == list(range(len(folds)))


def test_rolling_window_splits_empty():
    df = pl.DataFrame(schema={"game_date": pl.Utf8})
    assert rolling_window_splits(df) == []


# ──────────────────────────────────────────────────
# patterns module
# ──────────────────────────────────────────────────

def _ats_schema_df():
    return pl.DataFrame(schema={
        "pattern_type": pl.Utf8, "pattern_key": pl.Utf8,
        "sample_size": pl.Int64, "wins": pl.Int64,
        "hit_rate": pl.Float64, "edge_pct": pl.Float64,
        "p_value": pl.Float64, "p_value_adj": pl.Float64,
        "n_tests": pl.Int64, "pattern_hash": pl.Utf8,
    })


def test_find_ats_patterns_finds_strong_bias():
    # TeamA at home wins 90% of 40 games -> strong pattern
    rows = []
    for i in range(40):
        rows.append({
            "game_date": f"2024-01-{(i % 28) + 1:02d}",
            "sport": "basketball_nba",
            "home_team": "TeamA",
            "away_team": "TeamB",
            "home_score": 110,
            "away_score": 90,
            "spread_result": 20.0,
        })
    df = pl.DataFrame(rows)
    result = find_ats_patterns(df, min_sample=20, min_edge=3.0)
    team_rows = result.filter(pl.col("pattern_type") == "team_home_win")
    assert team_rows.height >= 1
    top = team_rows.row(0, named=True)
    assert json.loads(top["pattern_key"])["home_team"] == "TeamA"
    assert top["hit_rate"] > 0.85
    assert top["p_value"] < 0.001


def test_find_ats_patterns_bonferroni_applied(games_df):
    result = find_ats_patterns(games_df, min_sample=10, min_edge=2.0)
    if result.height > 0:
        k = result.select(pl.col("n_tests")).row(0)[0]
        assert k == result.height
        raw = result.select(pl.col("p_value")).to_series().to_list()
        adj = result.select(pl.col("p_value_adj")).to_series().to_list()
        expected = [min(1.0, round(p * k, 6)) for p in raw]
        assert adj == pytest.approx(expected, abs=1e-6)


def test_bonferroni_finalize_single_and_empty():
    one = [{"p_value": 0.02}]
    _bonferroni_finalize(one)
    assert one[0]["p_value_adj"] == 0.02
    assert one[0]["n_tests"] == 1

    many = [{"p_value": 0.01}, {"p_value": 0.5}, {"p_value": None}]
    _bonferroni_finalize(many)
    assert many[0]["p_value_adj"] == 0.03
    assert many[1]["p_value_adj"] == 1.0
    assert many[2]["p_value_adj"] == 1.0  # None treated as 1.0
    assert all(p["n_tests"] == 3 for p in many)


def test_find_ats_patterns_empty_and_missing_columns():
    empty = find_ats_patterns(pl.DataFrame(), min_sample=5)
    assert empty.height == 0

    no_scores = pl.DataFrame({"game_date": ["2024-01-01"], "sport": ["x"]})
    assert find_ats_patterns(no_scores).height == 0


def test_pattern_hash_deterministic():
    from tools.temporal.patterns import _pattern_hash

    a = _pattern_hash({"type": "x", "key": {"a": 1}})
    b = _pattern_hash({"key": {"a": 1}, "type": "x"})
    assert a == b and len(a) == 16


def test_find_player_prop_patterns():
    rows = []
    for i in range(20):
        rows.append({
            "sport": "basketball_nba",
            "player_name": "VolatilityVictim",
            "stat_type": "points",
            "stat_value": 30.0 + (10 if i % 2 else -10),
        })
        rows.append({
            "sport": "basketball_nba",
            "player_name": "SteadyEddie",
            "stat_type": "points",
            "stat_value": 20.0,
        })
    df = pl.DataFrame(rows)
    result = find_player_prop_patterns(df, min_appearances=10)
    assert result.height == 2
    steady = result.filter(pl.col("player_name") == "SteadyEddie").row(0, named=True)
    volatile = result.filter(pl.col("player_name") == "VolatilityVictim").row(0, named=True)
    assert volatile["cv"] > steady["cv"]
    assert abs(volatile["over_rate_vs_mean"] - 0.5) < 1e-9
    assert len(volatile["pattern_hash"]) == 16


def test_cross_tabulate(games_df):
    df = games_df.with_columns((pl.col("spread_result") > 0).cast(pl.Int64).alias("home_won"))
    result = cross_tabulate(df, ["sport"], target="home_won", min_sample=10)
    assert result.height >= 1
    row = result.row(0, named=True)
    assert 0.0 <= row["hit_rate"] <= 1.0
    assert 0.0 <= row["p_value"] <= 1.0


# ──────────────────────────────────────────────────
# validation module
# ──────────────────────────────────────────────────

CFG = {
    "training_period_end": "2024-03-01",
    "temporal_split_gap_days": 7,
}


def test_validate_overlaps_training():
    res = validate_temporal_isolation(CFG, "2024-02-28", "2024-04-01")
    assert res["valid"] is False
    assert res["adjusted_start"] == "2024-03-08"


def test_validate_within_gap():
    res = validate_temporal_isolation(CFG, "2024-03-05", "2024-04-01")
    assert res["valid"] is False
    assert res["adjusted_start"] == "2024-03-08"


def test_validate_safe_range():
    res = validate_temporal_isolation(CFG, "2024-03-10", "2024-04-01")
    assert res["valid"] is True
    assert res["gap_days_actual"] == 9


def test_validate_legacy_hypothesis_has_metadata_false():
    res = validate_temporal_isolation({}, "2024-01-01", "2024-02-01")
    assert res["valid"] is True
    assert res["has_temporal_metadata"] is False


# ──────────────────────────────────────────────────
# loading + hypotheses modules (real sqlite)
# ──────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "callisto.db")
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE game_results (
            id INTEGER PRIMARY KEY, sport TEXT, game_date TEXT,
            home_team TEXT, away_team TEXT, home_score INTEGER,
            away_score INTEGER, total_score INTEGER,
            spread_result REAL, winner TEXT, source TEXT);
        CREATE TABLE player_stats (
            id INTEGER PRIMARY KEY, sport TEXT, event_id TEXT,
            game_date TEXT, player_name TEXT, team TEXT,
            stat_type TEXT, stat_value REAL, minutes_played REAL,
            source TEXT, created_at TEXT);
        CREATE TABLE backtest_events (id INTEGER PRIMARY KEY, run_id TEXT);
        CREATE TABLE hypotheses (id INTEGER PRIMARY KEY, status TEXT);
    """)
    from datetime import date, timedelta

    start = date(2024, 1, 1)
    for i in range(150):
        day = (start + timedelta(days=i)).strftime("%Y-%m-%d")
        hs, as_ = (112, 100) if i % 3 else (98, 105)
        conn.execute(
            "INSERT INTO game_results VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (i, "basketball_nba", day, f"H{i%3}", f"A{i%3}", hs, as_,
             hs + as_, float(hs - as_), "H" if hs > as_ else "A", "test"),
        )
        conn.execute(
            "INSERT INTO player_stats VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (i, "basketball_nba", f"ev{i}", day, "P1", "H", "points",
             25.0 + (i % 5), 32.0, "test", day),
        )
    conn.commit()
    conn.close()
    return path


def test_load_game_results(db):
    df = load_game_results(db)
    assert df.height == 150
    assert set(["game_date", "home_score"]).issubset(df.columns)
    filtered = load_game_results(db, sport="hockey_nhl")
    assert filtered.height == 0
    ranged = load_game_results(db, date_range=("2024-01-01", "2024-01-31"))
    assert ranged.height > 0
    assert ranged.select(pl.col("game_date").max()).item() <= "2024-01-31"


def test_generate_hypotheses_from_analysis_end_to_end(db):
    hyps = facade.generate_hypotheses_from_analysis(
        db_path=db,
        sport="basketball_nba",
        cutoff_date="2024-04-01",
        min_sample=20,
        gap_days=7,
    )
    # Should produce at least the ATS hypotheses; each carries temporal metadata
    assert isinstance(hyps, list)
    for h in hyps:
        mc = h["model_config"]
        assert "training_period_start" in mc
        assert "training_period_end" in mc
        assert mc["temporal_split_gap_days"] == 7
        # Temporal isolation rule: training end strictly before any test data
        assert mc["training_period_end"] <= "2024-04-01"


def test_get_data_summary(db):
    summary = facade.get_data_summary(db)
    assert summary["game_results_total"] == 150
    assert summary["player_stats_total"] == 150
    assert "basketball_nba" in summary["game_results"]


def test_get_training_window_default():
    win = facade.get_training_window()
    assert "training_period_start" in win and "training_cutoff" in win
    assert win["training_period_start"] < win["training_cutoff"]
