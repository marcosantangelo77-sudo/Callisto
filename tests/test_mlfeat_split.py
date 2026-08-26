"""Tests for the tools.mlfeat split of ``tools.ml_features``.

The original ~1100-line ``tools/ml_features.py`` was extracted into the
``tools/mlfeat/`` package:

    base        — FeatureVector, read-only DB helpers, asof/numeric helpers,
                  static venue metadata
    fetchers    — read-only SQL fetchers
    player_prop — build_player_prop_features + names
    game_total  — build_game_total_features + names

These tests verify that:

1. the facade re-exports the full public surface and it is identical to
   what the submodules export;
2. feature extraction on a synthetic SQLite DB produces vectors whose
   names/values line up, with correct rolling statistics, no-lookahead
   behaviour, venue/park factors and target resolution;
3. the internal helpers behave (asof parsing, stdev/slope/mean NaN
   semantics, park-factor fuzzy matching, altitude/dome tables,
   season-week proxy);
4. connections are never written to (read-only guarantee).
"""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import tools.ml_features as facade  # noqa: E402
from tools.mlfeat import base as mlfeat_base  # noqa: E402
from tools.mlfeat import fetchers as mlfeat_fetchers  # noqa: E402
from tools.mlfeat import game_total as mlfeat_game_total  # noqa: E402
from tools.mlfeat import player_prop as mlfeat_player_prop  # noqa: E402


# ──────────────────────────────────────────────────────────────────────────
# Facade / package structure
# ──────────────────────────────────────────────────────────────────────────

PUBLIC_SURFACE = [
    "FeatureVector",
    "build_player_prop_features",
    "build_game_total_features",
    "feature_names_player_prop",
    "feature_names_game_total",
]


def test_facade_reexports_public_surface():
    for name in PUBLIC_SURFACE:
        assert hasattr(facade, name), f"facade missing {name}"
        assert name in facade.__all__


def test_facade_aliases_package_objects():
    assert facade.FeatureVector is mlfeat_base.FeatureVector
    assert (
        facade.build_player_prop_features
        is mlfeat_player_prop.build_player_prop_features
    )
    assert (
        facade.build_game_total_features is mlfeat_game_total.build_game_total_features
    )
    assert (
        facade.feature_names_player_prop
        is mlfeat_player_prop.feature_names_player_prop
    )
    assert (
        facade.feature_names_game_total is mlfeat_game_total.feature_names_game_total
    )


def test_feature_names_match_between_facade_and_modules():
    assert facade.feature_names_player_prop() == list(
        mlfeat_player_prop._PLAYER_FEATURE_NAMES
    )
    assert facade.feature_names_game_total() == list(
        mlfeat_game_total._GAME_FEATURE_NAMES
    )


def test_player_prop_names_unique_and_ordered():
    names = facade.feature_names_player_prop()
    assert len(names) == len(set(names))
    assert names[0] == "p_roll5_mean"
    assert names[-1] == "clv_dev_count"
    assert "park_factor" in names and "is_dome" in names
    assert sum(n.startswith("dow_") for n in names) == 7
    assert sum(n.startswith("month_") for n in names) == 12


def test_game_total_names_unique():
    names = facade.feature_names_game_total()
    assert len(names) == len(set(names))
    assert names[0] == "home_lineup_recent_ppg"
    assert names[-1] == "n_line_movements"


# ──────────────────────────────────────────────────────────────────────────
# base helpers
# ──────────────────────────────────────────────────────────────────────────

class TestAsofDate:
    def test_plain_date_passthrough(self):
        d = date(2026, 4, 1)
        assert mlfeat_base._asof_date(d) is d

    def test_datetime_takes_date_part(self):
        dt = datetime(2026, 4, 1, 23, 59, tzinfo=timezone.utc)
        assert mlfeat_base._asof_date(dt) == date(2026, 4, 1)

    def test_iso_date_string(self):
        assert mlfeat_base._asof_date("2026-04-01") == date(2026, 4, 1)

    def test_iso_datetime_string(self):
        assert mlfeat_base._asof_date("2026-04-01T02:30:00Z") == date(2026, 4, 1)
        assert mlfeat_base._asof_date("2026-04-01 02:30:00") == date(2026, 4, 1)

    def test_garbage_raises(self):
        with pytest.raises(ValueError):
            mlfeat_base._asof_date("not-a-date")
        with pytest.raises(ValueError):
            mlfeat_base._asof_date(12345)


class TestNumericHelpers:
    def test_safe_stdev_short(self):
        assert np.isnan(mlfeat_base._safe_stdev([1.0]))
        assert np.isnan(mlfeat_base._safe_stdev([]))

    def test_safe_stdev_values(self):
        s = mlfeat_base._safe_stdev([2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0])
        assert abs(s - 2.13809) < 1e-4

    def test_mean_empty_all_nan(self):
        assert np.isnan(mlfeat_base._mean([]))
        assert np.isnan(mlfeat_base._mean([float("nan")]))

    def test_mean_skips_nan_entries(self):
        assert mlfeat_base._mean([1.0, float("nan"), 3.0]) == 2.0

    def test_trend_slope_needs_two_points(self):
        assert np.isnan(mlfeat_base._trend_slope([5.0]))
        assert np.isnan(mlfeat_base._trend_slope([]))

    def test_trend_slope_positive(self):
        slope = mlfeat_base._trend_slope([1.0, 2.0, 3.0, 4.0])
        assert abs(slope - 1.0) < 1e-9

    def test_trend_slope_negative(self):
        slope = mlfeat_base._trend_slope([4.0, 3.0, 2.0, 1.0])
        assert abs(slope - (-1.0)) < 1e-9


class TestVenueTables:
    def test_park_factor_known(self):
        assert mlfeat_base._park_factor("Coors Field") == pytest.approx(1.35)
        assert mlfeat_base._park_factor("Oracle Park") == pytest.approx(0.83)

    def test_park_factor_unknown_is_neutral(self):
        assert mlfeat_base._park_factor(None) == 1.0
        assert mlfeat_base._park_factor("") == 1.0
        assert mlfeat_base._park_factor("Nowhere Park") == 1.0

    def test_park_factor_fuzzy_suffix(self):
        assert mlfeat_base._park_factor("Fenway Park (test)") == pytest.approx(1.07)

    def test_is_dome(self):
        assert mlfeat_base._is_dome("SoFi Stadium") == 1
        assert mlfeat_base._is_dome("Fenway Park") == 0
        assert mlfeat_base._is_dome(None) == 0

    def test_altitude_factor(self):
        assert mlfeat_base._altitude_factor(None) == 0.0
        # Coors Field at 5200 ft / 5280 ft reference.
        assert mlfeat_base._altitude_factor("Coors Field") == pytest.approx(
            5200 / 5280
        )
        assert mlfeat_base._altitude_factor("Fenway Park") == 0.0


def test_open_ro_rejects_writes(tmp_path):
    db = tmp_path / "ro.db"
    conn_w = sqlite3.connect(str(db))
    conn_w.execute("CREATE TABLE t (x INTEGER)")
    conn_w.execute("INSERT INTO t VALUES (1)")
    conn_w.commit()
    conn_w.close()

    ro = mlfeat_base._open_ro(str(db))
    try:
        assert ro.execute("SELECT x FROM t").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("INSERT INTO t VALUES (2)")
    finally:
        ro.close()


# ──────────────────────────────────────────────────────────────────────────
# Synthetic DB fixture
# ──────────────────────────────────────────────────────────────────────────

@pytest.fixture
def synthetic_db(tmp_path, monkeypatch):
    """Create a tiny SQLite DB with just the tables mlfeat needs."""
    db_path = tmp_path / "synthetic.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE player_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sport TEXT, event_id TEXT, game_date DATE,
            player_name TEXT, team TEXT, stat_type TEXT,
            stat_value REAL, minutes_played REAL, source TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE game_contexts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sport TEXT, event_id TEXT, game_date DATE, local_game_date DATE,
            home_team TEXT, away_team TEXT, home_score INTEGER, away_score INTEGER,
            context_json TEXT, embedded BOOLEAN, created_at DATETIME
        );
        CREATE TABLE game_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sport TEXT, game_date TEXT, local_game_date DATE,
            home_team TEXT, away_team TEXT,
            home_score INTEGER, away_score INTEGER, total_score INTEGER,
            spread_result REAL, winner TEXT, source TEXT, regime TEXT
        );
        CREATE TABLE backtest_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT, event_id TEXT, hypothesis_id TEXT,
            sport TEXT, player TEXT, market TEXT, line REAL, side TEXT,
            book TEXT, book_odds_american INTEGER, book_implied_prob REAL,
            model_fair_prob REAL, model_factors TEXT, edge REAL, ev_pct REAL,
            kelly_fraction REAL, signal_generated BOOLEAN,
            actual_result TEXT, actual_stat REAL,
            closing_odds INTEGER, closing_implied REAL, clv_implied REAL,
            game_date DATE, local_game_date DATE,
            snapshot_time DATETIME, created_at DATETIME
        );
        CREATE TABLE line_movements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sport TEXT, detected_at TEXT, team TEXT, market TEXT,
            bookmaker TEXT, old_price INTEGER, new_price INTEGER,
            price_movement INTEGER, old_point REAL, new_point REAL,
            point_movement REAL, direction TEXT, ev_analysis TEXT,
            archived BOOLEAN
        );
        CREATE TABLE statcast_pitches (
            game_pk INTEGER, at_bat_number INTEGER, pitch_number INTEGER,
            game_date DATE, home_team TEXT, away_team TEXT, inning INTEGER,
            inning_topbot TEXT, pitcher_id INTEGER, pitcher_name TEXT,
            pitcher_throws TEXT, batter_id INTEGER, batter_name TEXT,
            batter_stands TEXT, pitch_type TEXT, pitch_name TEXT,
            release_speed REAL, release_spin_rate REAL, release_extension REAL,
            release_pos_x REAL, release_pos_y REAL, release_pos_z REAL,
            spin_axis REAL, pfx_x REAL, pfx_z REAL, plate_x REAL
        );
        """
    )

    base = date(2026, 3, 1)
    final_event = None
    for i in range(15):
        gd = (base + timedelta(days=i * 2)).isoformat()
        eid = f"evt_{i:03d}"
        if i == 14:
            final_event = eid
        conn.execute(
            "INSERT INTO player_stats (sport, event_id, game_date, player_name,"
            " team, stat_type, stat_value) VALUES (?,?,?,?,?,?,?)",
            ("basketball_nba", eid, gd, "Ricky Fake", "Los Angeles Lakers",
             "points", 20.0 + 0.5 * i),
        )
        conn.execute(
            "INSERT INTO player_stats (sport, event_id, game_date, player_name,"
            " team, stat_type, stat_value) VALUES (?,?,?,?,?,?,?)",
            ("basketball_nba", eid, gd, "Other Dude", "Denver Nuggets",
             "points", 18.0),
        )
        conn.execute(
            "INSERT INTO game_contexts (sport, event_id, game_date,"
            " local_game_date, home_team, away_team, home_score, away_score,"
            " context_json) VALUES (?,?,?,?,?,?,?,?,?)",
            ("basketball_nba", eid, gd, gd,
             "Los Angeles Lakers", "Denver Nuggets", 110, 108,
             '{"venue": "Crypto.com Arena", "attendance": 18997}'),
        )
        conn.execute(
            "INSERT INTO game_results (sport, game_date, local_game_date,"
            " home_team, away_team, home_score, away_score, total_score)"
            " VALUES (?,?,?,?,?,?,?,?)",
            ("basketball_nba", gd, gd, "Los Angeles Lakers", "Denver Nuggets",
             110, 108, 218),
        )
    # CLV history for Ricky: beat the line by +2 on average.
    for i in range(10):
        gd = (base + timedelta(days=i * 2)).isoformat()
        conn.execute(
            "INSERT INTO backtest_events (run_id, event_id, sport, player,"
            " market, line, actual_result, actual_stat, game_date,"
            " local_game_date) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("run_test", f"evt_{i:03d}", "basketball_nba", "Ricky Fake",
             "points", 20.0, "won", 22.0, gd, gd),
        )
    # Line movements on the totals market.
    for j in range(5):
        conn.execute(
            "INSERT INTO line_movements (sport, detected_at, team, market,"
            " point_movement) VALUES (?,?,?,?,?)",
            ("basketball_nba", "2026-03-25T18:00:00Z", "Los Angeles Lakers",
             "totals", -0.5 * (j + 1)),
        )
    conn.commit()
    yield str(db_path), final_event
    conn.close()


@pytest.fixture
def db_env(synthetic_db, monkeypatch):
    db_path, final_event = synthetic_db
    monkeypatch.setenv("CALLISTO_DB_PATH", db_path)
    return {"db": db_path, "event": final_event}


# ──────────────────────────────────────────────────────────────────────────
# Player-prop feature extraction
# ──────────────────────────────────────────────────────────────────────────

def test_build_player_prop_vector_shape_and_names(db_env):
    fv = facade.build_player_prop_features(
        "Ricky Fake", "points", db_env["event"], "2026-04-05"
    )
    assert isinstance(fv, facade.FeatureVector)
    assert fv.names == tuple(facade.feature_names_player_prop())
    assert fv.values.shape == (len(fv.names),)
    assert fv.values.dtype == np.float64
    d = fv.as_dict()
    assert set(d.keys()) == set(fv.names)


def test_build_player_prop_rolling_stats(db_env):
    fv = facade.build_player_prop_features(
        "Ricky Fake", "points", db_env["event"], "2026-04-05"
    )
    # History strictly before 2026-04-05: all 15 seeded games (Mar 1..Mar 29).
    # Most-recent-first; last five values are for i = 10..14.
    expected_roll5_mean = float(np.mean([20.0 + 0.5 * i for i in range(10, 15)]))
    assert fv.as_dict()["p_roll5_mean"] == pytest.approx(expected_roll5_mean)
    assert fv.as_dict()["p_games_sampled"] == 15.0


def test_build_player_prop_no_lookahead(db_env):
    """Features must not see stats on/after the asof cutoff."""
    early = facade.build_player_prop_features(
        "Ricky Fake", "points", db_env["event"], "2026-03-11"
    )
    late = facade.build_player_prop_features(
        "Ricky Fake", "points", db_env["event"], "2026-04-05"
    )
    assert early.as_dict()["p_games_sampled"] < late.as_dict()["p_games_sampled"]
    # Only 5 prior games (Mar 1..Mar 9), so partial-window means use what
    # exists: roll20 over a 5-game history averages all five values.
    early_d = early.as_dict()
    assert early_d["p_games_sampled"] == 5.0
    assert early_d["p_roll20_mean"] == pytest.approx(
        float(np.mean([20.0 + 0.5 * i for i in range(5)]))
    )


def test_build_player_prop_target_resolved_when_present(db_env):
    resolved = facade.build_player_prop_features(
        "Ricky Fake", "points", db_env["event"], "2026-04-05"
    )
    assert resolved.target_stat_value is not None
    assert resolved.target_stat_value == pytest.approx(27.0)  # 20 + 0.5*14

    unresolved = facade.build_player_prop_features(
        "Never Played Guy", "points", db_env["event"], "2026-04-05"
    )
    assert unresolved.target_stat_value is None


def test_build_player_prop_meta(db_env):
    fv = facade.build_player_prop_features(
        "Ricky Fake", "points", db_env["event"], "2026-04-05",
    )
    meta = fv.meta
    assert meta["player"] == "Ricky Fake"
    assert meta["stat_type"] == "points"
    assert meta["sport"] == "basketball_nba"
    assert meta["home_team"] == "Los Angeles Lakers"
    assert meta["away_team"] == "Denver Nuggets"
    assert meta["player_team"] == "Los Angeles Lakers"
    assert meta["opp_team"] == "Denver Nuggets"


def test_build_player_prop_clv_deviation(db_env):
    fv = facade.build_player_prop_features(
        "Ricky Fake", "points", db_env["event"], "2026-04-05"
    )
    d = fv.as_dict()
    # Ten resolved events all beating a 20.0 line by exactly +2.
    assert d["clv_dev_last10"] == pytest.approx(2.0)
    assert d["clv_dev_count"] == 10.0


def test_build_player_prop_unknown_event_is_nan_heavy(db_env):
    fv = facade.build_player_prop_features(
        "Ricky Fake", "points", "evt_missing", "2026-04-05"
    )
    d = fv.as_dict()
    # Event context unresolved -> timing/venue features are NaN / zeroed...
    assert d["days_rest"] == pytest.approx(float((date(2026, 4, 5) - date(2026, 3, 29)).days))
    assert np.isnan(d["local_hour"])
    # ...and there is no opponent context, so opp features stay NaN.
    assert np.isnan(d["opp_last10_allowed_mean"])
    assert d["opp_last10_allowed_count"] == 0.0
    # Rolling stats still exist because they come from player_stats history.
    assert not np.isnan(d["p_roll5_mean"])
    # All dow/month one-hots zero when no context resolves.
    assert all(d[f"dow_{i}"] == 0.0 for i in range(7))
    assert all(d[f"month_{i}"] == 0.0 for i in range(1, 13))


def test_build_player_prop_explicit_conn_not_closed(db_env):
    # Use a row-factory connection like real callers do.
    conn = sqlite3.connect(db_env["db"])
    conn.row_factory = sqlite3.Row
    try:
        fv = facade.build_player_prop_features(
            "Ricky Fake", "points", db_env["event"], "2026-04-05", conn=conn
        )
        assert isinstance(fv, facade.FeatureVector)
        # Connection remains usable afterwards.
        assert conn.execute("SELECT COUNT(*) FROM player_stats").fetchone()[0] > 0
    finally:
        conn.close()


def test_build_player_prop_opponent_allowed_proxy(db_env):
    fv = facade.build_player_prop_features(
        "Ricky Fake", "points", db_env["event"], "2026-04-05"
    )
    d = fv.as_dict()
    # Nuggets' last-10 dates are games i=5..14; excluding Denver players,
    # the contributions are Ricky's values 22.5..27.0 -> mean 24.75.
    expected = float(np.mean([20.0 + 0.5 * i for i in range(5, 15)]))
    assert d["opp_last10_allowed_mean"] == pytest.approx(expected)
    assert d["opp_last10_allowed_count"] == 10.0


# ──────────────────────────────────────────────────────────────────────────
# Game-total feature extraction
# ──────────────────────────────────────────────────────────────────────────

def test_build_game_total_vector_shape_and_names(db_env):
    fv = facade.build_game_total_features(db_env["event"], "2026-04-05")
    assert isinstance(fv, facade.FeatureVector)
    assert fv.names == tuple(facade.feature_names_game_total())
    assert fv.values.shape == (len(fv.names),)
    assert fv.values.dtype == np.float64


def test_build_game_total_recent_totals(db_env):
    fv = facade.build_game_total_features(db_env["event"], "2026-04-05")
    d = fv.as_dict()
    # Every seeded game totalled 218 points.
    assert d["home_recent_total_mean"] == pytest.approx(218.0)
    assert d["away_recent_total_mean"] == pytest.approx(218.0)
    assert d["home_recent_total_std"] == pytest.approx(0.0)
    # Lineup proxy: Lakers' opponents' contributions average above Denver's
    # flat 18.0 on the shared dates (Ricky's trend), so delta is positive.
    assert d["lineup_delta"] == pytest.approx(
        float(np.mean([20.0 + 0.5 * i for i in range(5, 15)]))
        - float(np.mean([18.0 for _ in range(10)]))
    )


def test_build_game_total_no_lookahead(db_env):
    conn = sqlite3.connect(db_env["db"])
    conn.row_factory = sqlite3.Row
    try:
        early = facade.build_game_total_features(
            db_env["event"], "2026-03-11").as_dict()
        # Strict cutoff: fewer totals visible at the earlier asof date.
        from tools.mlfeat.fetchers import _team_recent_totals
        early_totals = _team_recent_totals(
            conn, "basketball_nba", "Los Angeles Lakers", date(2026, 3, 11)
        )
        late_totals = _team_recent_totals(
            conn, "basketball_nba", "Los Angeles Lakers", date(2026, 4, 5)
        )
    finally:
        conn.close()
    assert len(early_totals) < len(late_totals)
    # All seeded games totalled 218 regardless of cutoff.
    assert early["home_recent_total_mean"] == pytest.approx(218.0)


def test_build_game_total_target_resolved(db_env):
    fv = facade.build_game_total_features(db_env["event"], "2026-04-05")
    assert fv.target_stat_value == pytest.approx(218.0)


def test_build_game_total_line_movements(db_env):
    fv = facade.build_game_total_features(db_env["event"], "2026-04-26")
    d = fv.as_dict()
    # Five movements of -0.5 each -> net -2.5, abs 2.5, n 5.
    assert d["n_line_movements"] == 5.0
    assert d["total_line_movement"] == pytest.approx(
        sum(-0.5 * (j + 1) for j in range(5))
    )
    assert d["total_line_movement_abs"] == pytest.approx(7.5)


def test_season_week_proxy():
    # NBA season starts Oct 20; Jan 15 is ~12 weeks later.
    sw = mlfeat_game_total._season_week(date(2026, 1, 15), "basketball_nba")
    expected = max(0.0, ((date(2026, 1, 15) - date(2025, 10, 20)).days) // 7)
    assert sw == expected
    assert mlfeat_game_total._season_week(date(2026, 7, 1), "baseball_mlb") > 0
    # Before season start clamps to 0.
    assert mlfeat_game_total._season_week(date(2026, 8, 1), "basketball_nba") >= 0.0


# ──────────────────────────────────────────────────────────────────────────
# Fetchers
# ──────────────────────────────────────────────────────────────────────────

def test_fetch_player_history_strict_before_asof(db_env):
    conn = sqlite3.connect(db_env["db"])
    conn.row_factory = sqlite3.Row
    try:
        rows = mlfeat_fetchers._fetch_player_history(
            conn, "basketball_nba", "Ricky Fake", "points", date(2026, 3, 11)
        )
        dates = [r["game_date"] for r in rows]
        assert all(d < "2026-03-11" for d in dates)
        # Ordered most-recent-first.
        assert dates == sorted(dates, reverse=True)
    finally:
        conn.close()


def test_fetch_opp_allowed_without_team(db_env):
    conn = sqlite3.connect(db_env["db"])
    try:
        mean, n = mlfeat_fetchers._fetch_opp_allowed(
            conn, "basketball_nba", "points", None, date(2026, 4, 5)
        )
        assert np.isnan(mean)
        assert n == 0
    finally:
        conn.close()


def test_fetch_event_context_backfills_from_backtest_events(db_env):
    conn = sqlite3.connect(db_env["db"])
    try:
        ctx = mlfeat_fetchers._fetch_event_context(
            conn, "basketball_nba", "evt_missing_ctx"
        )
        assert ctx["venue"] is None  # nothing to backfill from in this fixture
    finally:
        conn.close()


# ──────────────────────────────────────────────────────────────────────────
# Read-only guarantees
# ──────────────────────────────────────────────────────────────────────────

def test_extraction_does_not_mutate_db(db_env):
    conn = sqlite3.connect(db_env["db"])
    try:
        before = {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in (
                "player_stats", "game_contexts", "game_results",
                "backtest_events", "line_movements",
            )
        }
    finally:
        conn.close()

    facade.build_player_prop_features(
        "Ricky Fake", "points", db_env["event"], "2026-04-05"
    )
    facade.build_game_total_features(db_env["event"], "2026-04-05")

    conn = sqlite3.connect(db_env["db"])
    try:
        after = {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in before
        }
    finally:
        conn.close()
    assert before == after
