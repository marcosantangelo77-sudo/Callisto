"""Tests for CLVTracker's paper-trade → clv_log pipeline.

Paper trades are Callisto's main bet-like data while the real executor is
disabled. Every resolved paper trade must land in ``clv_log`` — the permanent
signal-quality ledger — or the promotion gates that grade hypotheses from
clv_log have nothing to grade.

These tests lock down the invariant with a real in-memory sqlite connection
so the SQL itself is verified, not just Python control flow.
"""

from __future__ import annotations

import pytest
import aiosqlite

from tools.clv_tracker import CLVTracker


# ─── fixtures ────────────────────────────────────────────────────────


async def _setup_db() -> aiosqlite.Connection:
    """Build a minimal in-memory db with the tables CLVTracker touches."""
    db = await aiosqlite.connect(":memory:")
    await db.execute(
        """CREATE TABLE paper_trades (
            trade_id TEXT PRIMARY KEY,
            hypothesis_id TEXT,
            event_id TEXT,
            sport TEXT,
            player TEXT,
            market TEXT,
            line REAL,
            side TEXT,
            book TEXT,
            signal_time TEXT,
            signal_odds_american INTEGER,
            signal_implied_prob REAL,
            model_fair_prob REAL,
            edge REAL,
            ev_pct REAL,
            closing_odds INTEGER,
            closing_implied REAL,
            clv_implied REAL,
            actual_result TEXT,
            actual_stat REAL,
            hypothetical_pnl REAL,
            game_date TEXT
        )"""
    )
    await db.execute(
        """CREATE TABLE clv_log (
            bet_id TEXT PRIMARY KEY,
            event TEXT,
            outcome TEXT,
            point REAL,
            book TEXT,
            our_odds_decimal REAL,
            pinnacle_close_fair_prob REAL,
            pinnacle_close_fair_decimal REAL,
            clv_cents REAL,
            clv_prob_bp REAL,
            actual_result TEXT,
            actual_pnl REAL,
            close_reliable BOOLEAN,
            logged_at TEXT
        )"""
    )
    await db.commit()
    return db


async def _insert_trade(db, **kw) -> None:
    """Insert a paper_trades row with sensible defaults. Override via kw."""
    defaults = dict(
        trade_id="pt-001",
        hypothesis_id="h-001",
        event_id="E1",
        sport="baseball_mlb",
        player=None,
        market="h2h",
        line=None,
        side="Cleveland Guardians",
        book="draftkings",
        signal_time="2026-04-18T20:00:00+00:00",
        signal_odds_american=-110,
        signal_implied_prob=0.524,
        model_fair_prob=0.560,
        edge=0.036,
        ev_pct=7.5,
        closing_odds=-105,
        closing_implied=0.512,
        clv_implied=-0.012,
        actual_result="won",
        actual_stat=None,
        hypothetical_pnl=90.9,
        game_date="2026-04-18",
    )
    defaults.update(kw)
    cols = ", ".join(defaults.keys())
    qs = ", ".join("?" for _ in defaults)
    await db.execute(
        f"INSERT INTO paper_trades ({cols}) VALUES ({qs})",
        tuple(defaults.values()),
    )
    await db.commit()


def _make_tracker(db) -> CLVTracker:
    tracker = CLVTracker(db_path=":memory:")
    tracker._db = db
    return tracker


# ─── tests ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_log_paper_trade_clv_writes_namespaced_row():
    db = await _setup_db()
    try:
        await _insert_trade(db, trade_id="pt-xyz")
        t = _make_tracker(db)
        cursor = await db.execute("SELECT * FROM paper_trades WHERE trade_id='pt-xyz'")
        cols = [d[0] for d in cursor.description]
        trade = dict(zip(cols, await cursor.fetchone()))

        wrote = await t.log_paper_trade_clv(trade)
        assert wrote is True

        row = await (await db.execute(
            "SELECT bet_id, event, outcome, actual_result, actual_pnl, "
            "close_reliable, clv_cents, our_odds_decimal "
            "FROM clv_log WHERE bet_id = 'pt:pt-xyz'"
        )).fetchone()
        assert row is not None, "paper-trade row should land in clv_log"
        bet_id, event, outcome, result, pnl, reliable, clv_cents, our_dec = row
        assert bet_id == "pt:pt-xyz"        # namespaced so it can't collide with int bet ids
        assert event == "E1"
        assert outcome == "Cleveland Guardians"
        assert result == "won"
        assert pnl == pytest.approx(90.9)
        assert reliable == 1
        # clv_cents now canonical prob-bp (odds-freshness audit fix).
        # signal -110 (~0.524 implied, devigged ~0.512) vs close -105
        # (~0.512 implied, devigged ~0.506): close fair - signal fair
        # is slightly negative (we gave up a few prob bp on devig). The
        # legacy American-point -5 semantic is retired because it was
        # incompatible with the prob-bp path in _log_clv — see the
        # CLV unit-mix audit fix.
        assert clv_cents is not None
        assert -150 < clv_cents < 150, (
            f"CLV out of expected magnitude band: {clv_cents}"
        )
        # -110 → decimal ~1.909
        assert our_dec == pytest.approx(1.0 + 100 / 110, rel=1e-4)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_log_paper_trade_clv_skips_unresolved_trades():
    db = await _setup_db()
    try:
        await _insert_trade(db, trade_id="pt-open", actual_result=None)
        t = _make_tracker(db)
        cursor = await db.execute("SELECT * FROM paper_trades WHERE trade_id='pt-open'")
        cols = [d[0] for d in cursor.description]
        trade = dict(zip(cols, await cursor.fetchone()))

        wrote = await t.log_paper_trade_clv(trade)
        assert wrote is False
        n = (await (await db.execute("SELECT COUNT(*) FROM clv_log")).fetchone())[0]
        assert n == 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_log_paper_trade_clv_requires_signal_implied_prob():
    db = await _setup_db()
    try:
        await _insert_trade(db, trade_id="pt-no-imp", signal_implied_prob=None)
        t = _make_tracker(db)
        cursor = await db.execute("SELECT * FROM paper_trades WHERE trade_id='pt-no-imp'")
        cols = [d[0] for d in cursor.description]
        trade = dict(zip(cols, await cursor.fetchone()))

        assert await t.log_paper_trade_clv(trade) is False
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_log_paper_trade_clv_marks_unmatched_close_as_unreliable():
    """A trade with no closing data at all should still log, but flag unreliable."""
    db = await _setup_db()
    try:
        await _insert_trade(
            db, trade_id="pt-no-close",
            closing_odds=None, closing_implied=None, clv_implied=None,
        )
        t = _make_tracker(db)
        cursor = await db.execute("SELECT * FROM paper_trades WHERE trade_id='pt-no-close'")
        cols = [d[0] for d in cursor.description]
        trade = dict(zip(cols, await cursor.fetchone()))

        assert await t.log_paper_trade_clv(trade) is True
        row = await (await db.execute(
            "SELECT close_reliable, clv_cents FROM clv_log WHERE bet_id='pt:pt-no-close'"
        )).fetchone()
        reliable, clv = row
        assert reliable == 0
        assert clv is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_sync_paper_trades_backfills_all_unlogged():
    """With N resolved-but-unlogged trades, one sync call should log all N."""
    db = await _setup_db()
    try:
        for i in range(5):
            await _insert_trade(
                db, trade_id=f"pt-{i:03d}",
                signal_odds_american=-110 + i,
                actual_result="won" if i % 2 == 0 else "lost",
            )
        t = _make_tracker(db)
        written = await t.sync_paper_trades_to_clv_log()
        assert written == 5
        n = (await (await db.execute("SELECT COUNT(*) FROM clv_log")).fetchone())[0]
        assert n == 5
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_sync_paper_trades_is_idempotent():
    """Running sync twice must not duplicate rows or error."""
    db = await _setup_db()
    try:
        await _insert_trade(db, trade_id="pt-dup")
        t = _make_tracker(db)
        w1 = await t.sync_paper_trades_to_clv_log()
        w2 = await t.sync_paper_trades_to_clv_log()
        assert w1 == 1
        assert w2 == 0  # nothing new to write — the anti-join excludes the existing row
        n = (await (await db.execute("SELECT COUNT(*) FROM clv_log")).fetchone())[0]
        assert n == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_sync_paper_trades_skips_unresolved_ones():
    """Unresolved trades (no actual_result) must not land in clv_log."""
    db = await _setup_db()
    try:
        await _insert_trade(db, trade_id="pt-done", actual_result="won")
        await _insert_trade(db, trade_id="pt-open", actual_result=None)
        t = _make_tracker(db)
        written = await t.sync_paper_trades_to_clv_log()
        assert written == 1
        rows = await (await db.execute("SELECT bet_id FROM clv_log ORDER BY bet_id")).fetchall()
        assert [r[0] for r in rows] == ["pt:pt-done"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_backfill_clv_log_handles_both_bets_and_paper_trades():
    """One call must sweep resolved bets AND paper trades into clv_log."""
    db = await _setup_db()
    # backfill_clv_log reads from `bets` too — add that table.
    await db.execute(
        """CREATE TABLE bets (
            id INTEGER PRIMARY KEY,
            placed_at TEXT, sport TEXT, event_id TEXT, game_description TEXT,
            bet_type TEXT, team TEXT, market TEXT, bookmaker TEXT,
            placement_odds INTEGER, placement_point REAL, placement_implied_prob REAL,
            closing_odds INTEGER, closing_point REAL, closing_implied_prob REAL,
            closing_source TEXT, clv_odds INTEGER, clv_implied REAL,
            stake REAL, result TEXT, payout REAL,
            edge_at_placement REAL, kelly_at_placement REAL,
            notes TEXT, tags TEXT
        )"""
    )
    await db.execute(
        "INSERT INTO bets (id, placed_at, sport, event_id, team, market, bookmaker, "
        "placement_odds, placement_implied_prob, closing_odds, closing_implied_prob, "
        "closing_source, stake, result, payout) "
        "VALUES (1, '2026-04-18T20:00+00:00', 'baseball_mlb', 'E1', 'Cleveland Guardians', "
        "'h2h', 'draftkings', -110, 0.524, -105, 0.512, 'pinnacle', 100, 'won', 195.24)"
    )
    await _insert_trade(db, trade_id="pt-only")
    await db.commit()
    try:
        t = _make_tracker(db)
        total = await t.backfill_clv_log()
        assert total == 2
        rows = await (await db.execute("SELECT bet_id FROM clv_log ORDER BY bet_id")).fetchall()
        ids = {r[0] for r in rows}
        assert ids == {"1", "pt:pt-only"}
    finally:
        await db.close()
