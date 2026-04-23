"""
Tests for tools/market_regime.py, tools/regime_api.py, tools/regime_replay.py.

All tests use synthetic in-memory SQLite fixtures; no live DB access.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import date, timedelta

import pytest

from tools.market_regime import (
    MarketRegime,
    _classify_phase,
    _is_noisy_phase,
    _phase_bounds_for,
    _SPORT_CALENDAR,
    current_regime_multiplier,
    detect_regime,
    regime_safe_for_trading,
)


# ──────────────────────────────────────────────────────────────────────
# Fixtures — a tiny on-disk DB that satisfies the subset of the Callisto
# schema detect_regime() touches. We write a temp file, then set
# CALLISTO_DB_PATH for the duration of the test.
# ──────────────────────────────────────────────────────────────────────

@pytest.fixture
def synthetic_db(tmp_path, monkeypatch):
    db_path = tmp_path / "callisto_test.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE game_contexts (
            id INTEGER PRIMARY KEY,
            sport TEXT, event_id TEXT, game_date DATE,
            home_team TEXT, away_team TEXT,
            home_score INTEGER, away_score INTEGER,
            context_json TEXT
        );
        CREATE TABLE backtest_events (
            id INTEGER PRIMARY KEY,
            run_id TEXT, event_id TEXT,
            hypothesis_id TEXT, sport TEXT, player TEXT,
            market TEXT, line REAL, side TEXT, book TEXT,
            book_odds_american INTEGER, book_implied_prob REAL,
            model_fair_prob REAL, model_factors TEXT,
            edge REAL, ev_pct REAL, kelly_fraction REAL,
            signal_generated BOOLEAN,
            actual_result TEXT, actual_stat REAL,
            closing_odds INTEGER, closing_implied REAL, clv_implied REAL,
            game_date DATE, snapshot_time DATETIME
        );
        CREATE TABLE paper_trades (
            trade_id TEXT PRIMARY KEY,
            hypothesis_id TEXT, event_id TEXT, sport TEXT, player TEXT,
            market TEXT, line REAL, side TEXT, book TEXT,
            signal_time DATETIME, signal_odds_american INTEGER,
            signal_implied_prob REAL, model_fair_prob REAL,
            edge REAL, ev_pct REAL, kelly_fraction REAL,
            recommended_stake REAL,
            closing_odds INTEGER, closing_implied REAL, clv_implied REAL,
            actual_result TEXT, actual_stat REAL, hypothetical_pnl REAL,
            game_date DATE, home_team TEXT, away_team TEXT
        );
        """
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("CALLISTO_DB_PATH", str(db_path))
    return db_path


def _insert_games(db_path, sport, dates_and_teams):
    """dates_and_teams: list of (iso_date, home, away)."""
    conn = sqlite3.connect(db_path)
    for i, (d, h, a) in enumerate(dates_and_teams):
        conn.execute(
            "INSERT INTO game_contexts(sport,event_id,game_date,home_team,away_team,context_json)"
            " VALUES (?,?,?,?,?,?)",
            (sport, f"evt_{i}", d, h, a, "{}"),
        )
    conn.commit()
    conn.close()


def _insert_backtest_event(
    db_path, *, hypothesis_id, sport, game_date, odds_american, result,
    signal_generated=True, clv_implied=None, book_implied_prob=0.5,
):
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        INSERT INTO backtest_events(
            run_id, event_id, hypothesis_id, sport, market, side, book,
            book_odds_american, book_implied_prob, model_fair_prob,
            edge, ev_pct, signal_generated, actual_result,
            clv_implied, game_date, snapshot_time
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "run1", "e1", hypothesis_id, sport, "ML", "home", "dk",
            odds_american, book_implied_prob, 0.55,
            0.02, 0.02, 1 if signal_generated else 0, result,
            clv_implied, game_date, f"{game_date}T12:00:00",
        ),
    )
    conn.commit()
    conn.close()


def _insert_paper_trade(
    db_path, *, trade_id, hypothesis_id, sport, game_date,
    odds_american, result, hypothetical_pnl,
):
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        INSERT INTO paper_trades(
            trade_id, hypothesis_id, event_id, sport, market, side, book,
            signal_time, signal_odds_american, signal_implied_prob,
            model_fair_prob, edge, ev_pct, actual_result,
            hypothetical_pnl, game_date
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            trade_id, hypothesis_id, "e1", sport, "ML", "home", "dk",
            f"{game_date}T10:00:00", odds_american, 0.5,
            0.55, 0.02, 0.02, result, hypothetical_pnl, game_date,
        ),
    )
    conn.commit()
    conn.close()


# ──────────────────────────────────────────────────────────────────────
# Season-phase classification (pure, no DB needed)
# ──────────────────────────────────────────────────────────────────────

class TestSeasonPhaseClassification:
    def test_mlb_regular_season_april(self):
        phase, _, _ = _classify_phase("baseball_mlb", date(2026, 4, 15))
        assert phase == "regular"

    def test_mlb_playoffs_mid_october(self):
        phase, _, _ = _classify_phase("baseball_mlb", date(2026, 10, 20))
        assert phase == "playoffs"

    def test_mlb_offseason_deep_winter(self):
        phase, _, _ = _classify_phase("baseball_mlb", date(2026, 1, 15))
        assert phase == "offseason"

    def test_nfl_offseason_february_post_super_bowl(self):
        # Feb 10 = still championship window (Super Bowl is early-mid Feb).
        # Feb 20 should be squarely offseason.
        phase, _, _ = _classify_phase("americanfootball_nfl", date(2026, 2, 20))
        assert phase == "offseason"

    def test_nfl_regular_season_october(self):
        phase, _, _ = _classify_phase("americanfootball_nfl", date(2026, 10, 15))
        assert phase == "regular"

    def test_nfl_regular_season_wraps_into_january(self):
        # First weekend of January — Week 18-ish.
        phase, _, _ = _classify_phase("americanfootball_nfl", date(2026, 1, 4))
        assert phase == "regular"

    def test_nba_playoffs_may(self):
        phase, _, _ = _classify_phase("basketball_nba", date(2026, 5, 10))
        assert phase == "playoffs"

    def test_nba_offseason_august(self):
        phase, _, _ = _classify_phase("basketball_nba", date(2026, 8, 15))
        assert phase == "offseason"

    def test_alias_shorthand_resolves(self):
        # detect_regime should accept 'mlb' and produce the same phase
        # as 'baseball_mlb'.
        r1 = detect_regime("mlb", date(2026, 4, 15))
        r2 = detect_regime("baseball_mlb", date(2026, 4, 15))
        assert r1.season_phase == r2.season_phase == "regular"
        assert r1.sport == "baseball_mlb"

    def test_unknown_sport_is_offseason(self):
        r = detect_regime("curling", date(2026, 4, 15))
        assert r.season_phase == "offseason"


# ──────────────────────────────────────────────────────────────────────
# detect_regime — DB-driven fields
# ──────────────────────────────────────────────────────────────────────

class TestDetectRegimeDB:
    def test_num_games_today_reads_game_contexts(self, synthetic_db):
        today = date(2026, 4, 22)
        _insert_games(
            synthetic_db,
            "baseball_mlb",
            [
                (today.isoformat(), "BOS", "NYY"),
                (today.isoformat(), "LAD", "SF"),
                (today.isoformat(), "HOU", "SEA"),
                # yesterday — should NOT count for num_games_today
                ((today - timedelta(days=1)).isoformat(), "CHC", "STL"),
            ],
        )
        r = detect_regime("baseball_mlb", today)
        assert r.num_games_today == 3
        assert r.num_games_last_7d == 4

    def test_num_games_zero_when_no_rows(self, synthetic_db):
        r = detect_regime("baseball_mlb", date(2026, 4, 22))
        assert r.num_games_today == 0
        assert r.num_games_last_7d == 0

    def test_prior_roi_clv_computed_from_last_year_same_phase(
        self, synthetic_db
    ):
        # Today is 2026-04-22 (MLB regular). Prior window is 2025's MLB
        # regular — insert a couple of backtest_events dated in that
        # window and confirm we get non-None priors.
        for gd, res, odds in [
            ("2025-05-01", "win", +150),
            ("2025-05-02", "loss", -110),
            ("2025-05-10", "win", -105),
        ]:
            _insert_backtest_event(
                synthetic_db,
                hypothesis_id="h1",
                sport="baseball_mlb",
                game_date=gd,
                odds_american=odds,
                result=res,
                clv_implied=0.52,
                book_implied_prob=0.50,
            )
        r = detect_regime("baseball_mlb", date(2026, 4, 22))
        assert r.historical_roi_prior is not None
        assert r.historical_clv_prior is not None
        assert r.sample_sizes.get("prior_backtest_events") == 3

    def test_prior_volatility_from_paper_trades(self, synthetic_db):
        for i, (gd, pnl) in enumerate(
            [
                ("2025-05-01", 1.5),
                ("2025-05-02", -1.0),
                ("2025-05-03", 0.5),
                ("2025-05-04", -0.8),
                ("2025-05-05", 2.0),
            ]
        ):
            _insert_paper_trade(
                synthetic_db,
                trade_id=f"pt{i}",
                hypothesis_id="h1",
                sport="baseball_mlb",
                game_date=gd,
                odds_american=+120,
                result="win" if pnl > 0 else "loss",
                hypothetical_pnl=pnl,
            )
        r = detect_regime("baseball_mlb", date(2026, 4, 22))
        assert r.volatility_estimate is not None
        assert r.volatility_estimate > 0


# ──────────────────────────────────────────────────────────────────────
# Confidence behaviour
# ──────────────────────────────────────────────────────────────────────

class TestConfidence:
    def test_confidence_low_in_first_two_weeks_of_phase(self, synthetic_db):
        # NBA regular season begins ~Oct 21 (per calendar). Day 3 of the
        # phase should produce confidence < 0.5 regardless of priors.
        phase_start = date(2026, 10, 21)
        day_3 = phase_start + timedelta(days=3)
        r = detect_regime("basketball_nba", day_3)
        assert r.season_phase == "regular"
        assert r.days_into_phase <= 14
        assert r.confidence < 0.5

    def test_confidence_higher_mid_phase(self, synthetic_db):
        # 2 months into NBA regular season, with game volume AND prior
        # history. Both dimensions feed the confidence score; we assert
        # it rises strictly above the early-phase floor.
        day_60 = date(2026, 12, 21)
        # 21 distinct games across the last 7 days (~3 per night)
        games = []
        for k in range(7):
            for j in range(3):
                games.append(
                    ((day_60 - timedelta(days=k)).isoformat(),
                     f"H{k}_{j}", f"A{k}_{j}")
                )
        _insert_games(synthetic_db, "basketball_nba", games)
        # Prior-year same-phase backtest events for ROI/CLV credit
        for k in range(50):
            _insert_backtest_event(
                synthetic_db,
                hypothesis_id=f"h_{k}",
                sport="basketball_nba",
                game_date=(date(2025, 12, 21) - timedelta(days=k % 30)).isoformat(),
                odds_american=-110,
                result="win" if k % 2 == 0 else "loss",
            )

        # Early-phase (day 3) reference point — must be low confidence.
        early = detect_regime("basketball_nba", date(2026, 10, 24))
        mid = detect_regime("basketball_nba", day_60)
        assert mid.season_phase == "regular"
        assert mid.days_into_phase > 14
        # Mid-phase should beat early-phase confidence meaningfully.
        assert mid.confidence > early.confidence
        assert mid.confidence >= 0.45

    def test_offseason_confidence_is_low(self, synthetic_db):
        r = detect_regime("baseball_mlb", date(2026, 1, 10))
        assert r.confidence <= 0.3


# ──────────────────────────────────────────────────────────────────────
# Safety / multiplier helpers
# ──────────────────────────────────────────────────────────────────────

class TestRegimeSafety:
    def test_offseason_is_unsafe(self, synthetic_db):
        assert regime_safe_for_trading("baseball_mlb", date(2026, 1, 15)) is False

    def test_preseason_is_unsafe(self, synthetic_db):
        assert regime_safe_for_trading("baseball_mlb", date(2026, 3, 5)) is False

    def test_mlb_last_week_of_regular_is_noisy(self, synthetic_db):
        # MLB regular ends Sep 30 in the calendar; Sep 28 is within 7d.
        assert regime_safe_for_trading("baseball_mlb", date(2026, 9, 28)) is False

    def test_nba_last_10_days_of_regular_is_noisy(self, synthetic_db):
        # NBA regular ends Apr 15; Apr 10 is within 10d (tank window).
        assert regime_safe_for_trading("basketball_nba", date(2026, 4, 10)) is False

    def test_mid_regular_season_is_safe(self, synthetic_db):
        assert regime_safe_for_trading("baseball_mlb", date(2026, 6, 15)) is True

    def test_noisy_flag_set_on_regime(self, synthetic_db):
        r = detect_regime("baseball_mlb", date(2026, 9, 29))
        assert r.noisy_window is True


class TestMultiplier:
    def test_multiplier_is_float(self, synthetic_db):
        m = current_regime_multiplier("baseball_mlb", date(2026, 4, 22))
        assert isinstance(m, float)
        assert 0.5 <= m <= 1.5

    def test_offseason_multiplier_is_half(self, synthetic_db):
        m = current_regime_multiplier("baseball_mlb", date(2026, 1, 15))
        assert m == 0.5

    def test_noisy_window_multiplier_is_half(self, synthetic_db):
        m = current_regime_multiplier("baseball_mlb", date(2026, 9, 28))
        assert m == 0.5


# ──────────────────────────────────────────────────────────────────────
# regime_api.build_payload
# ──────────────────────────────────────────────────────────────────────

class TestRegimeAPI:
    def test_build_payload_includes_expected_keys(self, synthetic_db):
        from tools.regime_api import build_payload
        payload = build_payload("baseball_mlb", "2026-04-22")
        for k in (
            "sport", "as_of", "season_phase", "num_games_today",
            "num_games_last_7d", "confidence", "multiplier",
            "safe_for_trading",
        ):
            assert k in payload
        assert payload["sport"] == "baseball_mlb"
        assert payload["season_phase"] == "regular"

    def test_build_payload_rejects_bad_date(self):
        from tools.regime_api import build_payload
        with pytest.raises(ValueError):
            build_payload("baseball_mlb", "not-a-date")


# ──────────────────────────────────────────────────────────────────────
# regime_replay
# ──────────────────────────────────────────────────────────────────────

class TestRegimeReplay:
    def test_replay_buckets_by_phase(self, synthetic_db):
        from tools.regime_replay import replay_hypothesis
        # Insert wins during regular season and losses during playoffs
        for gd in ("2025-05-01", "2025-05-10", "2025-06-01"):
            _insert_backtest_event(
                synthetic_db,
                hypothesis_id="hx",
                sport="baseball_mlb",
                game_date=gd,
                odds_american=+100,
                result="win",
            )
        for gd in ("2025-10-05", "2025-10-10"):
            _insert_backtest_event(
                synthetic_db,
                hypothesis_id="hx",
                sport="baseball_mlb",
                game_date=gd,
                odds_american=-110,
                result="loss",
            )
        stats = replay_hypothesis("hx")
        assert "baseball_mlb:regular" in stats
        assert "baseball_mlb:playoffs" in stats
        assert stats["baseball_mlb:regular"]["n"] == 3
        assert stats["baseball_mlb:playoffs"]["n"] == 2
        # regular season all wins at +100 → ROI = +1.0, hit_rate = 1.0
        assert stats["baseball_mlb:regular"]["hit_rate"] == pytest.approx(1.0)
        assert stats["baseball_mlb:regular"]["roi"] == pytest.approx(1.0)
        # playoffs all losses → ROI = -1.0, hit_rate = 0.0
        assert stats["baseball_mlb:playoffs"]["hit_rate"] == pytest.approx(0.0)

    def test_replay_unknown_hypothesis_returns_empty(self, synthetic_db):
        from tools.regime_replay import replay_hypothesis
        assert replay_hypothesis("does-not-exist") == {}


# ──────────────────────────────────────────────────────────────────────
# Low-level calendar invariants
# ──────────────────────────────────────────────────────────────────────

class TestCalendarInvariants:
    def test_every_sport_has_at_least_one_window(self):
        assert set(_SPORT_CALENDAR.keys()) >= {
            "baseball_mlb", "basketball_nba",
            "icehockey_nhl", "americanfootball_nfl",
        }
        for sport, wins in _SPORT_CALENDAR.items():
            assert wins, f"{sport} has no phase windows"

    def test_phase_bounds_length_is_positive(self):
        for sport, wins in _SPORT_CALENDAR.items():
            for w in wins:
                start, end = _phase_bounds_for(w, date(2026, 6, 1))
                assert end >= start, (sport, w)
