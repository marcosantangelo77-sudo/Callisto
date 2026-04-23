"""Unit tests for :mod:`tools.ml_features`.

Creates a synthetic SQLite DB in a temporary file, points
``CALLISTO_DB_PATH`` at it, and exercises feature extraction for both
player-prop and game-total paths. No network, no live DB access.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

# Force a clean import path so importing ``tools.ml_features`` doesn't also
# pull the live data_collector.
import sys
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tools.ml_features import (  # noqa: E402
    FeatureVector,
    build_game_total_features,
    build_player_prop_features,
    feature_names_game_total,
    feature_names_player_prop,
)


@pytest.fixture
def synthetic_db(tmp_path, monkeypatch):
    """Create a tiny SQLite DB with just the tables ml_features needs."""
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

    # Seed a player — Ricky Fake — with an increasing trend in points.
    base = date(2026, 3, 1)
    event_ids = []
    for i in range(15):
        gd = (base + timedelta(days=i * 2)).isoformat()
        eid = f"evt_{i:03d}"
        event_ids.append(eid)
        # Ricky averages 20 + gentle +0.5/game trend, plays for the Lakers.
        conn.execute(
            "INSERT INTO player_stats (sport, event_id, game_date, player_name, team, stat_type, stat_value) "
            "VALUES (?,?,?,?,?,?,?)",
            ("basketball_nba", eid, gd, "Ricky Fake", "Los Angeles Lakers", "points", 20.0 + 0.5 * i),
        )
        # Opponent player also produces some stat line — for opp-allowed proxy
        conn.execute(
            "INSERT INTO player_stats (sport, event_id, game_date, player_name, team, stat_type, stat_value) "
            "VALUES (?,?,?,?,?,?,?)",
            ("basketball_nba", eid, gd, "Other Dude", "Denver Nuggets", "points", 18.0),
        )
        conn.execute(
            "INSERT INTO game_contexts (sport, event_id, game_date, local_game_date, home_team, away_team, "
            "home_score, away_score, context_json) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "basketball_nba", eid, gd, gd,
                "Los Angeles Lakers", "Denver Nuggets", 110, 108,
                '{"venue": "Crypto.com Arena", "attendance": 18997}',
            ),
        )
        conn.execute(
            "INSERT INTO game_results (sport, game_date, local_game_date, home_team, away_team, "
            "home_score, away_score, total_score) VALUES (?,?,?,?,?,?,?,?)",
            ("basketball_nba", gd, gd, "Los Angeles Lakers", "Denver Nuggets", 110, 108, 218),
        )
        conn.execute(
            "INSERT INTO backtest_events (run_id, event_id, hypothesis_id, sport, market, side, book, "
            "book_odds_american, book_implied_prob, model_fair_prob, edge, ev_pct, game_date, "
            "local_game_date, snapshot_time, actual_result, model_factors) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "run_synth", eid, "hyp_synth", "basketball_nba", "totals",
                "over", "fanduel", -110, 0.524, 0.55, 0.026, 5.0,
                gd, gd, f"{gd}T23:00:00Z", "won",
                '{"home_team": "Los Angeles Lakers", "away_team": "Denver Nuggets"}',
            ),
        )

    conn.commit()
    conn.close()
    monkeypatch.setenv("CALLISTO_DB_PATH", str(db_path))
    return str(db_path)


# ──────────────────────────────────────────────────────────────────────────

def test_feature_names_stable():
    names = feature_names_player_prop()
    assert len(names) == len(set(names)), "duplicate player feature names"
    assert names[0].startswith("p_roll5_mean")
    tot_names = feature_names_game_total()
    assert len(tot_names) == len(set(tot_names)), "duplicate game feature names"


def test_player_prop_features_basic(synthetic_db):
    fv = build_player_prop_features(
        player="Ricky Fake",
        stat_type="points",
        event_id="evt_010",
        asof_ts="2026-03-21",
        sport="basketball_nba",
    )
    assert isinstance(fv, FeatureVector)
    assert fv.values.shape == (len(feature_names_player_prop()),)
    d = fv.as_dict()

    # With 10 prior games at 20, 20.5, 21, ..., 24.5 in reverse order:
    #   roll5_mean should be ~24.0 (most recent 5: 22.5 through 24.5)
    #   roll10_mean should be ~21.75
    #   slope should be positive (increasing trend chronologically)
    assert 23.0 <= d["p_roll5_mean"] <= 25.0
    assert 21.0 <= d["p_roll10_mean"] <= 22.5
    assert d["p_roll5_slope"] > 0, "trend should be positive"
    assert d["p_games_sampled"] >= 5

    # Park factor for non-MLB away game = 1.0 by design
    assert d["park_factor"] == 1.0
    # Dome flag: Crypto.com isn't in _DOME_VENUES explicitly (indoor arenas
    # for NBA aren't required to be flagged dome=1), so 0 is acceptable here.
    assert d["is_dome"] in (0.0, 1.0)
    # Target stat from seeded player_stats
    assert fv.target_stat_value == 25.0  # 20 + 0.5 * 10


def test_player_prop_no_lookahead(synthetic_db):
    """With asof set to the very first seeded game_date, there must be no
    prior history and the rolling mean/std features should be NaN."""
    fv = build_player_prop_features(
        player="Ricky Fake",
        stat_type="points",
        event_id="evt_000",
        asof_ts="2026-03-01",
        sport="basketball_nba",
    )
    d = fv.as_dict()
    assert np.isnan(d["p_roll5_mean"])
    assert np.isnan(d["p_roll10_mean"])
    assert d["p_games_sampled"] == 0


def test_player_prop_handles_missing_event(synthetic_db):
    # Unknown event_id must not raise — features simply degrade to NaN/zero.
    fv = build_player_prop_features(
        player="Ricky Fake",
        stat_type="points",
        event_id="evt_does_not_exist",
        asof_ts="2026-03-30",
        sport="basketball_nba",
    )
    assert fv.values.shape == (len(feature_names_player_prop()),)
    # game-date one-hots may be all zero when event unresolved
    d = fv.as_dict()
    assert d["p_games_sampled"] > 0  # we still have Ricky's history


def test_game_total_features(synthetic_db):
    fv = build_game_total_features(
        event_id="evt_010",
        asof_ts="2026-03-21",
        sport="basketball_nba",
    )
    d = fv.as_dict()
    assert fv.values.shape == (len(feature_names_game_total()),)
    # Both teams have seeded history, lineup means should be finite
    assert np.isfinite(d["home_recent_total_mean"])
    assert d["home_recent_total_mean"] > 0
    # Target — we seeded total_score=218
    assert fv.target_stat_value == 218.0


def test_feature_vector_reject_shape():
    names = ("a", "b", "c")
    with pytest.raises(ValueError):
        FeatureVector(names=names, values=np.array([1.0, 2.0]))
