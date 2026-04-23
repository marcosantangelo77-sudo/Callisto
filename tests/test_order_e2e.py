"""End-to-end: submit -> approve -> submitted -> fill -> settle -> bets/bankroll
sync + reconciler happy path."""

from __future__ import annotations

import pytest
import pytest_asyncio

from tools.order_manager import (
    OrderManager, reconcile_filled_orders,
    SETTLED_WIN, SETTLED_LOSS, FILLED,
)


async def _noop(msg: str) -> str:
    return "msg"


async def _setup_bets_and_game_results(db):
    """Seed the mini schemas the e2e test depends on."""
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS bets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            placed_at TEXT,
            sport TEXT,
            event_id TEXT,
            game_description TEXT,
            bet_type TEXT,
            team TEXT,
            market TEXT,
            bookmaker TEXT,
            placement_odds INTEGER,
            placement_point REAL,
            placement_implied_prob REAL,
            stake REAL,
            result TEXT,
            payout REAL,
            edge_at_placement REAL,
            kelly_at_placement REAL,
            notes TEXT,
            tags TEXT
        )
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS game_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sport TEXT,
            game_date TEXT,
            home_team TEXT,
            away_team TEXT,
            home_score INTEGER,
            away_score INTEGER,
            total_score INTEGER,
            spread_result REAL,
            winner TEXT,
            source TEXT
        )
        """
    )
    await db.commit()


@pytest_asyncio.fixture
async def mgr(tmp_path):
    m = OrderManager(db_path=str(tmp_path / "e2e.db"), telegram_sender=_noop)
    await m.initialize()
    await _setup_bets_and_game_results(m._db)
    try:
        yield m
    finally:
        await m.close()


@pytest.mark.asyncio
async def test_full_lifecycle_with_bets_sync(mgr):
    sig = {
        "signal_id": "sig_e2e_1",
        "sport": "baseball_mlb",
        "event_id": "LAD",
        "market": "h2h",
        "side": "LAD",
        "price_american": -140,
        "game_description": "LAD @ SF",
    }
    oid = await mgr.submit_order(
        hypothesis_id="mlb_home_favs",
        signal=sig,
        stake_units=1.0,
        stake_dollars=100.0,
        edge=0.04,
        fair_prob=0.62,
    )
    await mgr.approve(oid)
    await mgr.mark_submitted(oid)
    await mgr.mark_filled(oid, actual_price=-138)

    # bets should have one row now, linked by bet_id.
    o = await mgr.get_order(oid)
    assert o.bet_id is not None
    cur = await mgr._db.execute(
        "SELECT result, stake, placement_odds FROM bets WHERE id = ?", (o.bet_id,)
    )
    row = await cur.fetchone()
    assert row["result"] == "pending"
    assert row["stake"] == pytest.approx(100.0)
    assert row["placement_odds"] == -138

    # Seed game_results with a matching winner ("LAD").
    await mgr._db.execute(
        "INSERT INTO game_results (sport, game_date, home_team, away_team, winner) "
        "VALUES (?, ?, ?, ?, ?)",
        ("baseball_mlb", "2026-04-22", "LAD", "SF", "LAD"),
    )
    await mgr._db.commit()

    stats = await reconcile_filled_orders(mgr)
    assert stats["settled"] == 1

    o = await mgr.get_order(oid)
    assert o.state == SETTLED_WIN
    assert o.pnl_dollars is not None
    assert o.pnl_dollars > 0  # -138 winner at $100 stake

    # bets row updated with result + payout.
    cur = await mgr._db.execute(
        "SELECT result, payout FROM bets WHERE id = ?", (o.bet_id,)
    )
    row = await cur.fetchone()
    assert row["result"] == "won"
    assert row["payout"] is not None
    assert row["payout"] > 100.0


@pytest.mark.asyncio
async def test_reconciler_settles_loss(mgr):
    sig = {
        "signal_id": "sig_e2e_2",
        "sport": "baseball_mlb",
        "event_id": "NYY",
        "market": "h2h",
        "side": "NYY",
        "price_american": 120,
    }
    oid = await mgr.submit_order(
        hypothesis_id="mlb_dogs", signal=sig,
        stake_units=0.5, stake_dollars=50.0,
        edge=0.02, fair_prob=0.47,
    )
    await mgr.approve(oid)
    await mgr.mark_submitted(oid)
    await mgr.mark_filled(oid, actual_price=120)

    await mgr._db.execute(
        "INSERT INTO game_results (sport, game_date, home_team, away_team, winner) "
        "VALUES (?, ?, ?, ?, ?)",
        ("baseball_mlb", "2026-04-22", "BOS", "NYY", "BOS"),
    )
    await mgr._db.commit()

    await reconcile_filled_orders(mgr)
    o = await mgr.get_order(oid)
    assert o.state == SETTLED_LOSS
    assert o.pnl_dollars == pytest.approx(-50.0)


@pytest.mark.asyncio
async def test_reconciler_skips_unresolved(mgr):
    sig = {
        "signal_id": "sig_e2e_3",
        "sport": "baseball_mlb",
        "event_id": "BOS",
        "market": "h2h",
        "side": "BOS",
        "price_american": -110,
    }
    oid = await mgr.submit_order(
        hypothesis_id="mlb_x", signal=sig,
        stake_units=1.0, stake_dollars=100.0,
    )
    await mgr.approve(oid)
    await mgr.mark_submitted(oid)
    await mgr.mark_filled(oid, actual_price=-110)
    stats = await reconcile_filled_orders(mgr)
    assert stats["settled"] == 0
    assert stats["skipped"] == 1
    o = await mgr.get_order(oid)
    assert o.state == FILLED  # unchanged
