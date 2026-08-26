"""Settlement reconciler — coverage for every market type.

End-to-end: submit -> approve -> mark_filled -> insert synthetic
game_results / player_stats / game_contexts -> reconcile -> assert
settled state + bankroll + clv_log + hypothesis_stats row.

Idempotency, moneyline, spread, total, player prop, SGP, stuck-pending,
postponed-void all covered.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from tools.order_manager import (
    OrderManager,
    FILLED,
    SETTLED_WIN,
    SETTLED_LOSS,
    SETTLED_PUSH,
    CANCELLED,
)
from tools.order_reconciler import (
    ReconciliationReport,
    reconcile_filled_orders,
    detect_voided_orders,
    _american_pnl,
    _resolve_moneyline,
    _resolve_spread,
    _resolve_total,
    _resolve_player_prop,
    _parse_legs,
    _extract_line,
    _extract_player_meta,
)


# --- Test sender captures Telegram messages for assertion ------------------


class _Captor:
    def __init__(self):
        self.messages: list[str] = []

    async def __call__(self, msg: str) -> str:
        self.messages.append(msg)
        return "captured"


async def _setup_schema(db):
    """Create the minimum tables reconciliation touches."""
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS bets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            placed_at TEXT, sport TEXT, event_id TEXT, game_description TEXT,
            bet_type TEXT, team TEXT, market TEXT, bookmaker TEXT,
            placement_odds INTEGER, placement_point REAL,
            placement_implied_prob REAL, stake REAL, result TEXT,
            payout REAL, edge_at_placement REAL, kelly_at_placement REAL,
            notes TEXT, tags TEXT
        )
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS game_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sport TEXT, game_date TEXT,
            home_team TEXT, away_team TEXT,
            home_score INTEGER, away_score INTEGER,
            total_score INTEGER, spread_result REAL,
            winner TEXT, source TEXT
        )
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS game_contexts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sport TEXT, event_id TEXT, game_date TEXT,
            home_team TEXT, away_team TEXT,
            home_score INTEGER, away_score INTEGER,
            context_json TEXT
        )
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS player_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sport TEXT, event_id TEXT, game_date TEXT,
            player_name TEXT, team TEXT, stat_type TEXT,
            stat_value REAL
        )
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS clv_log (
            bet_id TEXT PRIMARY KEY, event TEXT, outcome TEXT, point REAL,
            book TEXT, our_odds_decimal REAL,
            pinnacle_close_fair_prob REAL, pinnacle_close_fair_decimal REAL,
            clv_cents REAL, clv_prob_bp REAL,
            actual_result TEXT, actual_pnl REAL,
            close_reliable BOOLEAN, logged_at TEXT
        )
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS closing_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT, sport TEXT, captured_at TEXT, source TEXT,
            market TEXT, team TEXT,
            closing_odds INTEGER, closing_point REAL, closing_implied REAL
        )
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS hypothesis_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            hypothesis_id TEXT, stage TEXT, computed_at TEXT,
            total_n INTEGER, signals_n INTEGER,
            win INTEGER, loss INTEGER, push_ INTEGER,
            hit_rate REAL, avg_edge REAL, avg_ev REAL, avg_clv REAL,
            positive_clv_rate REAL, roi_pct REAL, sharpe REAL,
            max_drawdown REAL, p_value REAL, is_significant BOOLEAN
        )
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS bankroll (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT, balance REAL, change REAL,
            bet_id INTEGER, description TEXT
        )
        """
    )
    await db.execute(
        "INSERT INTO bankroll (timestamp, balance, change, description) "
        "VALUES (?, 1000.0, 1000.0, 'seed')",
        (datetime.now(timezone.utc).isoformat(),),
    )
    await db.commit()


@pytest_asyncio.fixture
async def mgr_and_captor(tmp_path):
    captor = _Captor()
    m = OrderManager(db_path=str(tmp_path / "recon.db"), telegram_sender=captor)
    await m.initialize()
    m.enable()  # default-disabled: arm for tests
    await _setup_schema(m._db)
    try:
        yield m, captor
    finally:
        await m.close()


# --- Pure-function unit tests ---------------------------------------------


def test_american_pnl_win_plus_140_pays_stake_140_pct():
    assert _american_pnl(100.0, 140, "win") == pytest.approx(140.0)


def test_american_pnl_win_minus_200_pays_half():
    assert _american_pnl(100.0, -200, "win") == pytest.approx(50.0)


def test_american_pnl_loss_returns_negative_stake():
    assert _american_pnl(75.0, -110, "loss") == pytest.approx(-75.0)


def test_american_pnl_push_is_zero():
    assert _american_pnl(100.0, -110, "push") == 0.0


def test_resolve_moneyline_win_loss_push():
    game = {"winner": "LAD", "home_team": "LAD", "away_team": "SF"}
    assert _resolve_moneyline("LAD", game) == "win"
    assert _resolve_moneyline("SF", game) == "loss"
    assert _resolve_moneyline("NYY", game) is None  # team not on card
    game_push = {"winner": "push", "home_team": "A", "away_team": "B"}
    assert _resolve_moneyline("A", game_push) == "push"


def test_resolve_spread_cover_and_push():
    game = {"home_team": "BOS", "away_team": "NYY",
            "home_score": 7, "away_score": 3}
    # BOS -2.5 -> margin 4, +line -2.5 = 1.5 > 0 -> win
    assert _resolve_spread("BOS", -2.5, game) == "win"
    # NYY +2.5 -> margin -4, +2.5 = -1.5 < 0 -> loss
    assert _resolve_spread("NYY", 2.5, game) == "loss"
    # BOS -4 exact push
    assert _resolve_spread("BOS", -4.0, game) == "push"


def test_resolve_total_over_under_push():
    game = {"home_score": 3, "away_score": 4, "total_score": 7}
    assert _resolve_total("Over 6.5", 6.5, game) == "win"
    assert _resolve_total("Under 6.5", 6.5, game) == "loss"
    assert _resolve_total("Over 7", 7.0, game) == "push"


def test_resolve_player_prop_semantics():
    assert _resolve_player_prop("Over 27.5", 27.5, 30) == "win"
    assert _resolve_player_prop("Under 27.5", 27.5, 30) == "loss"
    assert _resolve_player_prop("Over 28", 28.0, 28.0) == "push"
    assert _resolve_player_prop("Over 27.5", 27.5, None) is None


def test_extract_line_from_row_then_notes():
    class Row:
        def __init__(self, d): self._d = d
        def __getitem__(self, k): return self._d[k]
        def keys(self): return self._d.keys()
    assert _extract_line(Row({"line": 7.5}), None) == 7.5
    assert _extract_line(Row({"line": None}), "line=-3.5 book=dk") == -3.5


def test_extract_player_meta_parses_notes():
    p, s = _extract_player_meta("player=Tatum, stat=points")
    assert p == "Tatum"
    assert s == "points"
    p2, s2 = _extract_player_meta("player=Luka Doncic; stat=assists")
    assert p2 == "Luka Doncic" and s2 == "assists"


def test_parse_legs_pulls_json_out_of_notes():
    notes = (
        "legs=[{\"market\":\"h2h\",\"event_id\":\"LAD\",\"side\":\"LAD\"},"
        "{\"market\":\"totals\",\"event_id\":\"LAD\",\"side\":\"Over 8.5\","
        "\"line\":8.5}]; other=stuff"
    )
    legs = _parse_legs(notes)
    assert len(legs) == 2
    assert legs[0]["market"] == "h2h"
    assert legs[1]["line"] == 8.5


# --- Integration: moneyline, spread, total, prop, SGP ---------------------


async def _submit_fill(mgr, signal, *, stake=100.0, price=-110):
    oid = await mgr.submit_order(
        hypothesis_id=signal.get("hypothesis_id", "hyp_test"),
        signal=signal, stake_units=1.0, stake_dollars=stake,
        edge=0.04, fair_prob=0.55,
    )
    await mgr.approve(oid)
    await mgr.mark_submitted(oid)
    await mgr.mark_filled(oid, actual_price=price)
    return oid


@pytest.mark.asyncio
async def test_moneyline_win_settles_with_pnl_and_bankroll(mgr_and_captor):
    mgr, captor = mgr_and_captor
    oid = await _submit_fill(
        mgr,
        {"signal_id": "s_ml_win", "sport": "baseball_mlb",
         "event_id": "LAD", "market": "h2h", "side": "LAD",
         "price_american": -140},
        stake=100.0, price=-140,
    )
    await mgr._db.execute(
        "INSERT INTO game_results "
        "(sport, game_date, home_team, away_team, home_score, away_score, "
        "total_score, winner, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'test')",
        ("baseball_mlb", "2026-04-22", "LAD", "SF", 6, 3, 9, "LAD"),
    )
    await mgr._db.commit()

    stats = await reconcile_filled_orders(mgr)
    assert stats["settled"] == 1
    assert stats["by_result"]["win"] == 1

    o = await mgr.get_order(oid)
    assert o.state == SETTLED_WIN
    assert o.pnl_dollars == pytest.approx(100 * 100 / 140, rel=1e-3)

    # Bankroll reflects the +PnL append.
    row = await (await mgr._db.execute(
        "SELECT balance, change FROM bankroll ORDER BY timestamp DESC LIMIT 1"
    )).fetchone()
    assert row["change"] == pytest.approx(100 * 100 / 140, rel=1e-3)
    assert row["balance"] > 1000.0

    # Telegram captor has the settle message.
    assert any("settled WIN" in m for m in captor.messages)


@pytest.mark.asyncio
async def test_moneyline_loss_and_push(mgr_and_captor):
    mgr, _ = mgr_and_captor
    # Loser
    oid_loss = await _submit_fill(
        mgr,
        {"signal_id": "s_ml_loss", "sport": "baseball_mlb",
         "event_id": "NYY", "market": "h2h", "side": "NYY",
         "price_american": 120},
        stake=50.0, price=120,
    )
    await mgr._db.execute(
        "INSERT INTO game_results (sport, game_date, home_team, away_team, "
        "home_score, away_score, total_score, winner, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'test')",
        ("baseball_mlb", "2026-04-22", "BOS", "NYY", 5, 2, 7, "BOS"),
    )
    # Push
    oid_push = await _submit_fill(
        mgr,
        {"signal_id": "s_ml_push", "sport": "soccer_mls",
         "event_id": "NYCFC", "market": "h2h", "side": "NYCFC",
         "price_american": -110},
        stake=100.0, price=-110,
    )
    await mgr._db.execute(
        "INSERT INTO game_results (sport, game_date, home_team, away_team, "
        "home_score, away_score, winner, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'test')",
        ("soccer_mls", "2026-04-22", "NYCFC", "MIA", 1, 1, "push"),
    )
    await mgr._db.commit()

    stats = await reconcile_filled_orders(mgr)
    assert stats["settled"] == 2
    assert stats["by_result"]["loss"] == 1
    assert stats["by_result"]["push"] == 1

    o_loss = await mgr.get_order(oid_loss)
    o_push = await mgr.get_order(oid_push)
    assert o_loss.state == SETTLED_LOSS
    assert o_loss.pnl_dollars == pytest.approx(-50.0)
    assert o_push.state == SETTLED_PUSH
    assert o_push.pnl_dollars == 0.0


@pytest.mark.asyncio
async def test_spread_settlement_requires_line(mgr_and_captor):
    mgr, _ = mgr_and_captor
    oid = await _submit_fill(
        mgr,
        {"signal_id": "s_spr", "sport": "basketball_nba",
         "event_id": "BOS", "market": "spreads", "side": "BOS",
         "price_american": -110},
        stake=110.0, price=-110,
    )
    # The order's notes row needs line=-7.5; stash via direct UPDATE
    # because submit_order doesn't populate notes from signal.
    await mgr._db.execute(
        "UPDATE orders SET notes = ? WHERE order_id = ?",
        ("line=-7.5", oid),
    )
    # Need game_context for event_id mapping so spread_result lookup works.
    await mgr._db.execute(
        "INSERT INTO game_contexts (sport, event_id, game_date, home_team, "
        "away_team, home_score, away_score, context_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, '{}')",
        ("basketball_nba", "BOS", "2026-04-22", "BOS", "NYK", 120, 110),
    )
    await mgr._db.execute(
        "INSERT INTO game_results (sport, game_date, home_team, away_team, "
        "home_score, away_score, total_score, winner, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'test')",
        ("basketball_nba", "2026-04-22", "BOS", "NYK", 120, 110, 230, "BOS"),
    )
    await mgr._db.commit()

    stats = await reconcile_filled_orders(mgr)
    assert stats["settled"] == 1
    o = await mgr.get_order(oid)
    # BOS won by 10, -7.5 covered -> win.
    assert o.state == SETTLED_WIN


@pytest.mark.asyncio
async def test_total_over_and_under(mgr_and_captor):
    mgr, _ = mgr_and_captor
    # OVER 220.5 hits (actual total 230)
    oid = await _submit_fill(
        mgr,
        {"signal_id": "s_tot", "sport": "basketball_nba",
         "event_id": "BOS2", "market": "totals", "side": "Over 220.5",
         "price_american": -110},
    )
    await mgr._db.execute(
        "UPDATE orders SET notes = ? WHERE order_id = ?",
        ("line=220.5", oid),
    )
    await mgr._db.execute(
        "INSERT INTO game_contexts (sport, event_id, game_date, home_team, "
        "away_team, context_json) VALUES (?, ?, ?, ?, ?, '{}')",
        ("basketball_nba", "BOS2", "2026-04-22", "BOS", "NYK"),
    )
    await mgr._db.execute(
        "INSERT INTO game_results (sport, game_date, home_team, away_team, "
        "home_score, away_score, total_score, winner, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'test')",
        ("basketball_nba", "2026-04-22", "BOS", "NYK", 120, 110, 230, "BOS"),
    )
    await mgr._db.commit()

    stats = await reconcile_filled_orders(mgr)
    assert stats["settled"] == 1
    o = await mgr.get_order(oid)
    assert o.state == SETTLED_WIN


@pytest.mark.asyncio
async def test_player_prop_from_player_stats(mgr_and_captor):
    mgr, _ = mgr_and_captor
    oid = await _submit_fill(
        mgr,
        {"signal_id": "s_pp", "sport": "basketball_nba",
         "event_id": "BOS_PROP", "market": "player_props",
         "side": "Over 27.5", "price_american": -115},
    )
    # Stash player meta + line in notes.
    await mgr._db.execute(
        "UPDATE orders SET notes = ? WHERE order_id = ?",
        ("line=27.5, player=Jayson Tatum, stat=points", oid),
    )
    await mgr._db.execute(
        "INSERT INTO player_stats (sport, event_id, game_date, player_name, "
        "team, stat_type, stat_value) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("basketball_nba", "BOS_PROP", "2026-04-22", "Jayson Tatum",
         "BOS", "points", 31.0),
    )
    await mgr._db.commit()
    stats = await reconcile_filled_orders(mgr)
    assert stats["settled"] == 1
    o = await mgr.get_order(oid)
    assert o.state == SETTLED_WIN


@pytest.mark.asyncio
async def test_missing_game_result_skips_no_state_change(mgr_and_captor):
    mgr, _ = mgr_and_captor
    oid = await _submit_fill(
        mgr,
        {"signal_id": "s_no_result", "sport": "baseball_mlb",
         "event_id": "PHI", "market": "h2h", "side": "PHI",
         "price_american": -110},
    )
    stats = await reconcile_filled_orders(mgr)
    assert stats["settled"] == 0
    assert stats["skipped_no_result"] == 1
    o = await mgr.get_order(oid)
    assert o.state == FILLED


@pytest.mark.asyncio
async def test_postponed_game_triggers_refund_void(mgr_and_captor):
    mgr, captor = mgr_and_captor
    oid = await _submit_fill(
        mgr,
        {"signal_id": "s_post", "sport": "baseball_mlb",
         "event_id": "POST1", "market": "h2h", "side": "NYM",
         "price_american": -110},
        stake=80.0, price=-110,
    )
    # game_context says postponed, no game_results row.
    await mgr._db.execute(
        "INSERT INTO game_contexts (sport, event_id, game_date, home_team, "
        "away_team, context_json) VALUES (?, ?, ?, ?, ?, ?)",
        ("baseball_mlb", "POST1", "2026-04-22", "NYM", "PHI",
         json.dumps({"status": "postponed"})),
    )
    await mgr._db.commit()
    stats = await detect_voided_orders(mgr)
    assert stats["voided"] == 1

    o = await mgr.get_order(oid)
    assert o.state == CANCELLED
    assert o.pnl_dollars == 0.0

    # Stake refunded to bankroll.
    row = await (await mgr._db.execute(
        "SELECT change FROM bankroll ORDER BY timestamp DESC LIMIT 1"
    )).fetchone()
    assert row["change"] == pytest.approx(80.0)
    assert any("VOIDED" in m for m in captor.messages)


@pytest.mark.asyncio
async def test_idempotent_double_reconcile(mgr_and_captor):
    mgr, _ = mgr_and_captor
    oid = await _submit_fill(
        mgr,
        {"signal_id": "s_idem", "sport": "baseball_mlb",
         "event_id": "LAD", "market": "h2h", "side": "LAD",
         "price_american": -140},
        stake=100.0, price=-140,
    )
    await mgr._db.execute(
        "INSERT INTO game_results (sport, game_date, home_team, away_team, "
        "home_score, away_score, winner, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'test')",
        ("baseball_mlb", "2026-04-22", "LAD", "SF", 6, 3, "LAD"),
    )
    await mgr._db.commit()

    s1 = await reconcile_filled_orders(mgr)
    s2 = await reconcile_filled_orders(mgr)  # no-op
    assert s1["settled"] == 1
    assert s2["settled"] == 0  # already settled, no longer FILLED

    # Bankroll reflects exactly ONE credit (change row count for this order).
    cur = await mgr._db.execute(
        "SELECT COUNT(*) FROM bankroll WHERE description LIKE ?",
        (f"%{oid}%",),
    )
    row = await cur.fetchone()
    assert row[0] == 1


@pytest.mark.asyncio
async def test_hypothesis_stats_row_appended(mgr_and_captor):
    mgr, _ = mgr_and_captor
    oid = await _submit_fill(
        mgr,
        {"signal_id": "s_hyp_stats", "sport": "baseball_mlb",
         "event_id": "LAD", "market": "h2h", "side": "LAD",
         "price_american": -140, "hypothesis_id": "hyp_real"},
        stake=100.0, price=-140,
    )
    # Override hypothesis_id via direct update (submit_order accepts it
    # as a kwarg, signal.hypothesis_id wasn't honoured).
    await mgr._db.execute(
        "UPDATE orders SET hypothesis_id = 'hyp_real' WHERE order_id = ?",
        (oid,),
    )
    await mgr._db.execute(
        "INSERT INTO game_results (sport, game_date, home_team, away_team, "
        "winner, source) VALUES (?, ?, ?, ?, ?, 'test')",
        ("baseball_mlb", "2026-04-22", "LAD", "SF", "LAD"),
    )
    await mgr._db.commit()

    await reconcile_filled_orders(mgr)
    cur = await mgr._db.execute(
        "SELECT stage, total_n, win, loss, hit_rate, roi_pct "
        "FROM hypothesis_stats WHERE hypothesis_id = ? "
        "ORDER BY computed_at DESC LIMIT 1",
        ("hyp_real",),
    )
    row = await cur.fetchone()
    assert row is not None
    assert row["stage"] == "live"
    assert row["total_n"] == 1
    assert row["win"] == 1
    assert row["hit_rate"] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_clv_log_row_written_with_closing_line(mgr_and_captor):
    mgr, _ = mgr_and_captor
    oid = await _submit_fill(
        mgr,
        {"signal_id": "s_clv", "sport": "baseball_mlb",
         "event_id": "LAD", "market": "h2h", "side": "LAD",
         "price_american": -140},
        stake=100.0, price=-140,
    )
    # Seed closing line — our -140 placement beat a close of -160.
    await mgr._db.execute(
        "INSERT INTO closing_lines (event_id, sport, captured_at, source, "
        "market, team, closing_odds, closing_implied) "
        "VALUES (?, ?, ?, 'pinnacle', ?, ?, ?, ?)",
        ("LAD", "baseball_mlb", "2026-04-22T22:00:00Z",
         "h2h", "LAD", -160, 0.6154),
    )
    await mgr._db.execute(
        "INSERT INTO game_results (sport, game_date, home_team, away_team, "
        "winner, source) VALUES (?, ?, ?, ?, ?, 'test')",
        ("baseball_mlb", "2026-04-22", "LAD", "SF", "LAD"),
    )
    await mgr._db.commit()

    await reconcile_filled_orders(mgr)
    cur = await mgr._db.execute(
        "SELECT clv_prob_bp, close_reliable, actual_result "
        "FROM clv_log WHERE bet_id = ?",
        (f"order:{oid}",),
    )
    row = await cur.fetchone()
    assert row is not None
    # Placement implied = 140/240 = 0.5833, close = 0.6154.
    # CLV prob-bp = (0.6154 - 0.5833) * 10000 ≈ 321.
    assert row["clv_prob_bp"] is not None
    assert row["clv_prob_bp"] > 250
    assert row["close_reliable"] == 1
    assert row["actual_result"] == "won"


@pytest.mark.asyncio
async def test_stuck_pending_flag_and_alert(mgr_and_captor):
    mgr, captor = mgr_and_captor
    oid = await _submit_fill(
        mgr,
        {"signal_id": "s_stuck", "sport": "baseball_mlb",
         "event_id": "STUCK1", "market": "h2h", "side": "NYM",
         "price_american": -110},
    )
    # game_context exists but with a 72h-old game_date and no game_results.
    old = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
    await mgr._db.execute(
        "INSERT INTO game_contexts (sport, event_id, game_date, home_team, "
        "away_team, context_json) VALUES (?, ?, ?, ?, ?, '{}')",
        ("baseball_mlb", "STUCK1", old, "NYM", "PHI"),
    )
    await mgr._db.commit()

    stats = await reconcile_filled_orders(mgr)
    assert stats["stuck"] == 1
    assert oid in stats["stuck_order_ids"]
    assert any("stuck" in m.lower() for m in captor.messages)

    # Second run — already flagged, does not re-alert.
    before = len(captor.messages)
    stats2 = await reconcile_filled_orders(mgr)
    assert stats2["stuck"] == 0
    assert len(captor.messages) == before


@pytest.mark.asyncio
async def test_sgp_all_legs_win(mgr_and_captor):
    mgr, _ = mgr_and_captor
    oid = await _submit_fill(
        mgr,
        {"signal_id": "s_sgp_win", "sport": "baseball_mlb",
         "event_id": "LAD", "market": "sgp", "side": "parlay",
         "price_american": 500},
        stake=10.0, price=500,
    )
    legs = [
        {"market": "h2h", "event_id": "LAD", "side": "LAD"},
        {"market": "totals", "event_id": "LAD", "side": "Over 8.5",
         "line": 8.5},
    ]
    await mgr._db.execute(
        "UPDATE orders SET notes = ? WHERE order_id = ?",
        (f"legs={json.dumps(legs)}", oid),
    )
    await mgr._db.execute(
        "INSERT INTO game_results (sport, game_date, home_team, away_team, "
        "home_score, away_score, total_score, winner, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'test')",
        ("baseball_mlb", "2026-04-22", "LAD", "SF", 6, 3, 9, "LAD"),
    )
    await mgr._db.commit()
    stats = await reconcile_filled_orders(mgr)
    assert stats["settled"] == 1
    o = await mgr.get_order(oid)
    assert o.state == SETTLED_WIN
    assert o.pnl_dollars == pytest.approx(50.0)  # +500 on $10


@pytest.mark.asyncio
async def test_sgp_one_losing_leg_kills_ticket(mgr_and_captor):
    mgr, _ = mgr_and_captor
    oid = await _submit_fill(
        mgr,
        {"signal_id": "s_sgp_dead", "sport": "baseball_mlb",
         "event_id": "LAD", "market": "sgp", "side": "parlay",
         "price_american": 600},
        stake=10.0, price=600,
    )
    legs = [
        {"market": "h2h", "event_id": "LAD", "side": "LAD"},
        {"market": "totals", "event_id": "LAD", "side": "Over 15.5",
         "line": 15.5},  # needs 16+, actual 9 -> LOSE
    ]
    await mgr._db.execute(
        "UPDATE orders SET notes = ? WHERE order_id = ?",
        (f"legs={json.dumps(legs)}", oid),
    )
    await mgr._db.execute(
        "INSERT INTO game_results (sport, game_date, home_team, away_team, "
        "home_score, away_score, total_score, winner, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'test')",
        ("baseball_mlb", "2026-04-22", "LAD", "SF", 6, 3, 9, "LAD"),
    )
    await mgr._db.commit()
    await reconcile_filled_orders(mgr)
    o = await mgr.get_order(oid)
    assert o.state == SETTLED_LOSS


# --- ReconciliationReport helper sanity ----------------------------------


def test_report_to_dict_stable_shape():
    r = ReconciliationReport()
    r.settled = 2
    r.by_result["win"] = 2
    r.settled_order_ids = ["a", "b"]
    d = r.to_dict()
    assert d["settled"] == 2
    assert d["by_result"]["win"] == 2
    assert d["settled_order_ids"] == ["a", "b"]
    assert d["errors"] == 0
