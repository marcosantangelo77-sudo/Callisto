"""Accuracy hardening tests for tools.clv_tracker.

These cover the fixes from feat/clv-accuracy-hardening:
  * Known-answer CLV computation (prob-bp, vig-adjusted).
  * Push bets: CLV must be None.
  * 0-stat player-prop bets (actual_stat missing): CLV must be None.
  * Side-and-line match: closing line for a different line must NOT stomp.
  * 30-minute close window: captures outside the window don't flag
    within_close_window, and refresh_clv_for_event prefers within-window rows.
  * Late-arriving closing lines refresh CLV on already-placed bets.

Everything runs against an in-memory aiosqlite DB so the SQL paths are
exercised end-to-end. No live-DB dependency.
"""

from __future__ import annotations

import asyncio
import math

import aiosqlite
import pytest

from tools import clv_tracker as clv_mod
from tools.clv_tracker import (
    CLVTracker,
    _compute_clv_prob_bp,
    _half_vig_devig,
    _american_to_decimal,
)
from tools.odds_api import calculate_implied_probability


# ──────────────────────────── fixtures ────────────────────────────


async def _fresh_tracker() -> CLVTracker:
    tracker = CLVTracker(db_path=":memory:")
    tracker._db = await aiosqlite.connect(":memory:")
    await tracker._db.execute("PRAGMA busy_timeout = 60000")
    for stmt in (
        """CREATE TABLE bets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            placed_at TEXT, sport TEXT, event_id TEXT, game_description TEXT,
            bet_type TEXT, team TEXT, market TEXT, bookmaker TEXT,
            placement_odds INTEGER, placement_point REAL,
            placement_implied_prob REAL,
            closing_odds INTEGER, closing_point REAL, closing_implied_prob REAL,
            closing_source TEXT, clv_odds INTEGER, clv_implied REAL,
            clv_bps_v2 REAL, clv_stale INTEGER DEFAULT 0,
            stake REAL, result TEXT DEFAULT 'pending', payout REAL,
            edge_at_placement REAL, kelly_at_placement REAL,
            notes TEXT, tags TEXT
        )""",
        """CREATE TABLE paper_trades (
            trade_id TEXT PRIMARY KEY, hypothesis_id TEXT, event_id TEXT,
            sport TEXT, player TEXT, market TEXT, line REAL, side TEXT,
            book TEXT, signal_time TEXT,
            signal_odds_american INTEGER, signal_implied_prob REAL,
            model_fair_prob REAL, edge REAL, ev_pct REAL,
            closing_odds INTEGER, closing_implied REAL, clv_implied REAL,
            actual_result TEXT, actual_stat REAL, hypothetical_pnl REAL,
            game_date TEXT
        )""",
        """CREATE TABLE closing_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL, sport TEXT,
            captured_at TEXT NOT NULL,
            commence_time TEXT,
            seconds_to_commence INTEGER,
            within_close_window INTEGER DEFAULT 0,
            source TEXT, market TEXT, team TEXT, line REAL,
            closing_odds INTEGER, closing_point REAL, closing_implied REAL
        )""",
        """CREATE TABLE clv_log (
            bet_id TEXT PRIMARY KEY, event TEXT, outcome TEXT,
            point REAL, book TEXT, our_odds_decimal REAL,
            pinnacle_close_fair_prob REAL, pinnacle_close_fair_decimal REAL,
            clv_cents REAL, clv_prob_bp REAL,
            actual_result TEXT, actual_pnl REAL,
            close_reliable INTEGER, logged_at TEXT,
            regime_phase_at_placement TEXT
        )""",
    ):
        await tracker._db.execute(stmt)
    await tracker._db.commit()
    return tracker


# ─────────────────── known-answer CLV computation ───────────────────


def test_known_answer_clv_positive_we_beat_the_close():
    """Placement +150, close moved to +130 — we beat the close.

    Implied probs: +150 -> 0.40, +130 -> ~0.4348.
    After half-vig devig (placement vig=0.05, close vig=0.025):
      placement_fair = 0.40 / (1 + 0.025)  = 0.3902
      close_fair     = 0.4348 / (1 + 0.0125) = 0.4294
    clv_bps = (0.4294 - 0.3902) * 10000 = ~392 bps (positive).
    """
    clv = _compute_clv_prob_bp(
        placement_implied=calculate_implied_probability(+150),
        closing_implied=calculate_implied_probability(+130),
        placement_vig=0.05,
        closing_vig=0.025,
    )
    assert clv is not None
    assert clv > 0, clv
    assert 200 <= clv <= 500, f"Expected ~392 bps, got {clv}"


def test_known_answer_clv_negative_we_chased_wrong_side():
    """Placement -130, close -110 — we took the worse number."""
    clv = _compute_clv_prob_bp(
        placement_implied=calculate_implied_probability(-130),
        closing_implied=calculate_implied_probability(-110),
        placement_vig=0.05,
        closing_vig=0.025,
    )
    assert clv is not None
    assert clv < 0, clv


def test_known_answer_clv_returns_none_when_legs_missing():
    """Either leg None → None. Not 0, not NaN."""
    assert _compute_clv_prob_bp(None, 0.5) is None
    assert _compute_clv_prob_bp(0.5, None) is None
    assert _compute_clv_prob_bp(None, None) is None


def test_american_to_decimal_roundtrip():
    assert _american_to_decimal(+100) == pytest.approx(2.0)
    assert _american_to_decimal(-110) == pytest.approx(1.0 + 100 / 110)
    assert _american_to_decimal(None) is None
    assert _american_to_decimal(0) is None


# ─────────────────── push handling ───────────────────


@pytest.mark.asyncio
async def test_log_clv_nulls_clv_on_push():
    """Pushed bets must log with clv_prob_bp = NULL.

    Rationale: a void bet returns the stake; the closing line is
    irrelevant to the outcome. Including pushes in CLV aggregates biases
    the distribution toward wherever pushes happen to cluster.
    """
    t = await _fresh_tracker()
    try:
        bet = {
            "id": 7, "event_id": "e1", "team": "A",
            "placement_point": 2.5, "bookmaker": "DraftKings",
            "closing_source": "Pinnacle",
            "placement_odds": -110, "closing_odds": -115,
            "placement_implied_prob": calculate_implied_probability(-110),
            "closing_implied_prob": calculate_implied_probability(-115),
            "sport": "basketball_nba",
        }
        await t._log_clv(bet, result="push", payout=None, change=0)
        await t._db.commit()
        row = await (await t._db.execute(
            "SELECT clv_prob_bp, actual_result FROM clv_log WHERE bet_id = '7'"
        )).fetchone()
        assert row is not None
        clv_prob_bp, actual_result = row
        assert actual_result == "push"
        assert clv_prob_bp is None, f"push must null CLV, got {clv_prob_bp}"
    finally:
        await t._db.close()


# ─────────────────── 0-stat player-prop handling ───────────────────


@pytest.mark.asyncio
async def test_log_paper_trade_clv_nulls_on_zero_stat_prop():
    """A player prop with no actual_stat must not carry a CLV number."""
    t = await _fresh_tracker()
    try:
        await t._db.execute(
            "INSERT INTO paper_trades (trade_id, hypothesis_id, event_id, sport, "
            "player, market, line, side, book, signal_time, signal_odds_american, "
            "signal_implied_prob, model_fair_prob, edge, ev_pct, closing_odds, "
            "closing_implied, clv_implied, actual_result, actual_stat, "
            "hypothetical_pnl, game_date) VALUES "
            "('t1', 'h1', 'e1', 'basketball_nba', 'Jayson Tatum', "
            "'player_points', 27.5, 'Over', 'draftkings', "
            "'2026-04-20T23:00+00:00', -110, 0.524, 0.56, 0.036, 7.5, "
            "-105, 0.512, -0.012, 'lost', NULL, -100, '2026-04-20')"
        )
        await t._db.commit()
        cursor = await t._db.execute("SELECT * FROM paper_trades WHERE trade_id='t1'")
        cols = [d[0] for d in cursor.description]
        trade = dict(zip(cols, await cursor.fetchone()))
        wrote = await t.log_paper_trade_clv(trade)
        assert wrote is True
        row = await (await t._db.execute(
            "SELECT clv_prob_bp, clv_cents FROM clv_log WHERE bet_id = 'pt:t1'"
        )).fetchone()
        assert row is not None
        clv_prob_bp, clv_cents = row
        assert clv_prob_bp is None, (
            "player prop with NULL actual_stat must have NULL CLV, "
            f"got {clv_prob_bp}"
        )
        assert clv_cents is None
    finally:
        await t._db.close()


@pytest.mark.asyncio
async def test_log_paper_trade_clv_keeps_clv_when_stat_resolved():
    """Once actual_stat is populated, the same prop bet gets a real CLV."""
    t = await _fresh_tracker()
    try:
        await t._db.execute(
            "INSERT INTO paper_trades (trade_id, hypothesis_id, event_id, sport, "
            "player, market, line, side, book, signal_time, signal_odds_american, "
            "signal_implied_prob, model_fair_prob, edge, ev_pct, closing_odds, "
            "closing_implied, clv_implied, actual_result, actual_stat, "
            "hypothetical_pnl, game_date) VALUES "
            "('t2', 'h1', 'e1', 'basketball_nba', 'Jayson Tatum', "
            "'player_points', 27.5, 'Over', 'draftkings', "
            "'2026-04-20T23:00+00:00', -110, 0.524, 0.56, 0.036, 7.5, "
            "-130, 0.565, 0.041, 'won', 31, 90.9, '2026-04-20')"
        )
        await t._db.commit()
        cursor = await t._db.execute("SELECT * FROM paper_trades WHERE trade_id='t2'")
        cols = [d[0] for d in cursor.description]
        trade = dict(zip(cols, await cursor.fetchone()))
        assert await t.log_paper_trade_clv(trade) is True
        clv = (await (await t._db.execute(
            "SELECT clv_prob_bp FROM clv_log WHERE bet_id = 'pt:t2'"
        )).fetchone())[0]
        assert clv is not None
        assert clv > 0, f"bet was -110 vs close -130 → positive CLV; got {clv}"
    finally:
        await t._db.close()


# ─────────────────── side / line match ───────────────────


@pytest.mark.asyncio
async def test_record_closing_line_matches_on_side_and_line():
    """Two bets on the same event+market+team but DIFFERENT lines must each
    receive the correct closing line. Pre-fix behavior: closing_lines keyed
    only by (event, market, team) — both bets got the same number."""
    t = await _fresh_tracker()
    try:
        # Two bets: same team, different run lines.
        await t._db.execute(
            "INSERT INTO bets (placed_at, sport, event_id, game_description, "
            "bet_type, team, market, bookmaker, placement_odds, placement_point, "
            "placement_implied_prob, stake, result) VALUES "
            "('2026-04-20T20:00+00:00', 'baseball_mlb', 'evtX', 'A @ B', 'single', "
            "'Team A', 'run_line', 'draftkings', -110, -1.5, 0.524, 100, 'pending')"
        )
        await t._db.execute(
            "INSERT INTO bets (placed_at, sport, event_id, game_description, "
            "bet_type, team, market, bookmaker, placement_odds, placement_point, "
            "placement_implied_prob, stake, result) VALUES "
            "('2026-04-20T20:00+00:00', 'baseball_mlb', 'evtX', 'A @ B', 'single', "
            "'Team A', 'run_line', 'draftkings', +140, +1.5, 0.417, 100, 'pending')"
        )
        await t._db.commit()

        # Record closing line for -1.5 only.
        await t.record_closing_line(
            event_id="evtX", market="run_line", team="Team A",
            closing_odds=-130, closing_point=-1.5,
            source="pinnacle", sport="baseball_mlb",
            line=-1.5,
        )
        rows = await (await t._db.execute(
            "SELECT placement_point, closing_odds FROM bets "
            "WHERE event_id='evtX' ORDER BY placement_point"
        )).fetchall()
        by_point = {pt: c for pt, c in rows}
        assert by_point[-1.5] == -130, "matching-line bet must receive the close"
        assert by_point[1.5] is None, (
            "non-matching-line bet must NOT receive that close. "
            f"Got {by_point[1.5]} (pre-fix behavior)"
        )

        # Now record the other line's close.
        await t.record_closing_line(
            event_id="evtX", market="run_line", team="Team A",
            closing_odds=+160, closing_point=+1.5,
            source="pinnacle", sport="baseball_mlb",
            line=+1.5,
        )
        rows = await (await t._db.execute(
            "SELECT placement_point, closing_odds FROM bets "
            "WHERE event_id='evtX' ORDER BY placement_point"
        )).fetchall()
        by_point = {pt: c for pt, c in rows}
        assert by_point[-1.5] == -130
        assert by_point[1.5] == 160, "second line must get its own close"
    finally:
        await t._db.close()


# ─────────────────── 30-min close window ───────────────────


@pytest.mark.asyncio
async def test_closing_line_outside_window_does_not_flag_within_window():
    """commence_time 4 hours out → within_close_window = 0."""
    t = await _fresh_tracker()
    try:
        from datetime import datetime, timedelta, timezone
        commence = datetime.now(timezone.utc) + timedelta(hours=4)
        await t.record_closing_line(
            event_id="e_far", market="h2h", team="Team A",
            closing_odds=-110, source="pinnacle", sport="baseball_mlb",
            commence_time=commence.isoformat(),
        )
        row = await (await t._db.execute(
            "SELECT within_close_window, seconds_to_commence "
            "FROM closing_lines WHERE event_id='e_far'"
        )).fetchone()
        within, secs = row
        assert within == 0, f"4hr-out snapshot must not be in-window, got {within}"
        assert secs is not None and secs > 10_000
    finally:
        await t._db.close()


@pytest.mark.asyncio
async def test_closing_line_within_window_is_flagged():
    """commence_time 10 minutes out → within_close_window = 1."""
    t = await _fresh_tracker()
    try:
        from datetime import datetime, timedelta, timezone
        commence = datetime.now(timezone.utc) + timedelta(minutes=10)
        await t.record_closing_line(
            event_id="e_close", market="h2h", team="Team A",
            closing_odds=-110, source="pinnacle", sport="baseball_mlb",
            commence_time=commence.isoformat(),
        )
        row = await (await t._db.execute(
            "SELECT within_close_window, seconds_to_commence "
            "FROM closing_lines WHERE event_id='e_close'"
        )).fetchone()
        within, secs = row
        assert within == 1
        assert 0 < secs < 1800
    finally:
        await t._db.close()


@pytest.mark.asyncio
async def test_refresh_clv_prefers_within_window_rows():
    """Two snapshots for the same event — one pregame, one in-window. The
    in-window snapshot must be what drives the CLV refresh.
    """
    t = await _fresh_tracker()
    try:
        from datetime import datetime, timedelta, timezone
        # Insert the bet first so refresh has a target.
        await t._db.execute(
            "INSERT INTO bets (placed_at, sport, event_id, game_description, "
            "bet_type, team, market, bookmaker, placement_odds, placement_point, "
            "placement_implied_prob, stake, result) VALUES "
            "('2026-04-20T15:00+00:00', 'baseball_mlb', 'eR', 'A @ B', 'single', "
            "'Team A', 'h2h', 'draftkings', -110, NULL, 0.524, 100, 'pending')"
        )
        await t._db.commit()

        # Pregame snapshot: 4 hours before tip.
        commence = datetime.now(timezone.utc) + timedelta(minutes=10)
        pregame_commence = datetime.now(timezone.utc) + timedelta(hours=4)

        await t.record_closing_line(
            event_id="eR", market="h2h", team="Team A",
            closing_odds=+100,  # pregame
            source="pinnacle", sport="baseball_mlb",
            commence_time=pregame_commence.isoformat(),
        )
        await t.record_closing_line(
            event_id="eR", market="h2h", team="Team A",
            closing_odds=-130,  # in-window: the true close
            source="pinnacle", sport="baseball_mlb",
            commence_time=commence.isoformat(),
        )
        # After record, bets.closing_odds equals whichever came last (-130).
        # Refresh must still prefer the within-window row even if order differs.
        refreshed = await t.refresh_clv_for_event("eR")
        assert refreshed >= 1
        row = await (await t._db.execute(
            "SELECT closing_odds, clv_bps_v2 FROM bets WHERE event_id='eR'"
        )).fetchone()
        closing_odds, clv_bps_v2 = row
        assert closing_odds == -130, (
            f"refresh must pick the within-window snapshot, got {closing_odds}"
        )
        assert clv_bps_v2 is not None
    finally:
        await t._db.close()


# ─────────────────── late-arriving close refresh ───────────────────


@pytest.mark.asyncio
async def test_late_closing_line_refreshes_already_placed_bet():
    """Bet placed before any closing snapshot → closing_odds NULL. When a
    closing_lines row arrives later, record_closing_line must back-fill.
    """
    t = await _fresh_tracker()
    try:
        await t._db.execute(
            "INSERT INTO bets (placed_at, sport, event_id, game_description, "
            "bet_type, team, market, bookmaker, placement_odds, placement_point, "
            "placement_implied_prob, stake, result) VALUES "
            "('2026-04-20T15:00+00:00', 'baseball_mlb', 'eLate', 'A @ B', 'single', "
            "'Team A', 'h2h', 'draftkings', -110, NULL, 0.524, 100, 'pending')"
        )
        await t._db.commit()
        pre = await (await t._db.execute(
            "SELECT closing_odds FROM bets WHERE event_id='eLate'"
        )).fetchone()
        assert pre[0] is None  # no close yet

        # Late close
        await t.record_closing_line(
            event_id="eLate", market="h2h", team="Team A",
            closing_odds=-120, source="pinnacle", sport="baseball_mlb",
        )
        post = await (await t._db.execute(
            "SELECT closing_odds, clv_bps_v2 FROM bets WHERE event_id='eLate'"
        )).fetchone()
        assert post[0] == -120
        assert post[1] is not None
    finally:
        await t._db.close()


# ─────────────────── helpers sanity ───────────────────


def test_half_vig_devig_edge_cases():
    assert _half_vig_devig(None, 0.05) is None
    assert _half_vig_devig(0, 0.05) == 0
    v = _half_vig_devig(0.5, 0.05)
    assert 0 < v < 0.5


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
