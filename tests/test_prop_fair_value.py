"""Tests for the prop fair-value model.

Uses a temp SQLite DB with synthetic player_stats rows — no network,
no production DB dependency. Exercises:

1. MLB pitcher K projection reflects rolling K/IP × expected IP.
2. MLB batter hits projection reflects rolling AVG × expected PAs.
3. NHL skater SOG projection reflects SOG/TOI × expected TOI.
4. Confidence band classification (LOW / MEDIUM / HIGH) based on n_games.
5. Empty data returns UNKNOWN rather than raising.
6. project_prop router returns None for unknown (sport, prop).
"""

from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.prop_fair_value import (
    project_mlb_batter_hits,
    project_mlb_pitcher_strikeouts,
    project_nhl_skater_shots_on_goal,
    project_prop,
)


def _seed_db(tmp_path, rows):
    db = str(tmp_path / "fair.db")
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE player_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sport TEXT NOT NULL,
            event_id TEXT,
            game_date DATE NOT NULL,
            player_name TEXT NOT NULL,
            team TEXT NOT NULL,
            stat_type TEXT NOT NULL,
            stat_value REAL NOT NULL
        )
    """)
    conn.executemany(
        "INSERT INTO player_stats (sport, event_id, game_date, player_name, team, stat_type, stat_value) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()
    return db


def test_mlb_pitcher_k_projection_reflects_rolling_rate(tmp_path):
    # 12 starts at 9 K / 6 IP = 1.5 K/IP. Expected 6 IP start -> 9 K.
    rows = []
    for i in range(12):
        rows.append(("mlb", f"g{i}", f"2026-04-{i+1:02d}", "Ace Mock", "ATL", "strikeouts", 9.0))
        rows.append(("mlb", f"g{i}", f"2026-04-{i+1:02d}", "Ace Mock", "ATL", "innings_pitched", 6.0))
    db = _seed_db(tmp_path, rows)

    result = project_mlb_pitcher_strikeouts(
        db_path=db,
        player="Ace Mock",
        expected_innings=6.0,
        opponent_k_rate=1.0,
        park_factor=1.0,
    )
    # Deterministic rate → expected 9 K with near-zero sigma
    assert abs(result.fair_value - 9.0) < 0.01
    assert result.confidence in ("MEDIUM", "HIGH")
    assert result.n_games == 12


def test_mlb_pitcher_k_projection_applies_opponent_adjustment(tmp_path):
    rows = []
    for i in range(15):
        rows.append(("mlb", f"g{i}", f"2026-04-{i+1:02d}", "Ace Mock", "ATL", "strikeouts", 8.0))
        rows.append(("mlb", f"g{i}", f"2026-04-{i+1:02d}", "Ace Mock", "ATL", "innings_pitched", 6.0))
    db = _seed_db(tmp_path, rows)

    baseline = project_mlb_pitcher_strikeouts(db, "Ace Mock", expected_innings=6.0, opponent_k_rate=1.0)
    high_k_opp = project_mlb_pitcher_strikeouts(db, "Ace Mock", expected_innings=6.0, opponent_k_rate=1.2)
    assert high_k_opp.fair_value > baseline.fair_value


def test_mlb_batter_hits_reflects_avg(tmp_path):
    # 15 games at 1 hit / 4 AB = .250 AVG, 4 PA → 1.0 expected hits
    rows = []
    for i in range(15):
        rows.append(("mlb", f"g{i}", f"2026-04-{i+1:02d}", "Batter Mock", "NYY", "hits", 1.0))
        rows.append(("mlb", f"g{i}", f"2026-04-{i+1:02d}", "Batter Mock", "NYY", "at_bats", 4.0))
    db = _seed_db(tmp_path, rows)

    result = project_mlb_batter_hits(db, "Batter Mock", expected_pas=4.0)
    assert 0.9 <= result.fair_value <= 1.1
    assert result.confidence in ("MEDIUM", "HIGH")


def test_nhl_skater_sog_projection(tmp_path):
    # 10 games at 4 SOG in 18 min = 4/18 = 0.222/min. 20 min → 4.44
    rows = []
    for i in range(10):
        rows.append(("nhl", f"g{i}", f"2026-04-{i+1:02d}", "Sniper Mock", "EDM", "shots_on_goal", 4.0))
        rows.append(("nhl", f"g{i}", f"2026-04-{i+1:02d}", "Sniper Mock", "EDM", "toi", 18.0))
    db = _seed_db(tmp_path, rows)

    result = project_nhl_skater_shots_on_goal(db, "Sniper Mock", expected_toi_min=20.0)
    assert 4.2 <= result.fair_value <= 4.7
    assert result.confidence in ("LOW", "MEDIUM", "HIGH")
    assert result.n_games == 10


def test_low_confidence_band_when_few_games(tmp_path):
    rows = []
    for i in range(3):
        rows.append(("mlb", f"g{i}", f"2026-04-{i+1:02d}", "Rookie Mock", "SEA", "strikeouts", 5.0))
        rows.append(("mlb", f"g{i}", f"2026-04-{i+1:02d}", "Rookie Mock", "SEA", "innings_pitched", 5.0))
    db = _seed_db(tmp_path, rows)

    result = project_mlb_pitcher_strikeouts(db, "Rookie Mock", expected_innings=6.0)
    assert result.confidence == "LOW"
    assert result.n_games == 3


def test_unknown_player_returns_unknown(tmp_path):
    db = _seed_db(tmp_path, [])
    result = project_mlb_pitcher_strikeouts(db, "Nobody", expected_innings=6.0)
    assert result.confidence == "UNKNOWN"
    assert result.n_games == 0
    assert result.fair_value == 0.0


def test_project_prop_router_returns_none_for_unknown():
    db = "/nonexistent.db"
    result = project_prop(db, "cricket", "player_runs", "Doesnt Matter")
    assert result is None
