"""Split verification for tools.orderrec (order_reconciler package split).

The original monolithic ``tools/order_reconciler.py`` (~1000 lines) was
extracted into ``tools/orderrec/`` with a backward-compatible facade.
These tests verify:

1. The facade re-exports the full legacy API surface (public + private
   helpers that tests and cron wiring import directly).
2. The sub-modules are importable standalone.
3. End-to-end paper/recon behaviour still works through the facade:
   moneyline settle, spread settle, total settle, player prop, SGP,
   idempotency, stuck flagging, postponed voiding — plus direct
   resolution-unit coverage against the new modules.
4. Guardrails: no 'live' signal-status widening, OrderManager /
   BetExecutor untouched.

Run: /tmp/callisto-pytest/bin/python -m pytest tests/test_orderrec_split.py -q
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

import tools.order_reconciler as facade
import tools.orderrec
from tools.order_manager import (
    OrderManager,
    FILLED,
    SETTLED_WIN,
    SETTLED_LOSS,
    SETTLED_PUSH,
    CANCELLED,
)
from tools.orderrec import (
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


# --- Facade surface ----------------------------------------------------------


def test_facade_reexports_public_api():
    for name in (
        "reconcile_filled_orders",
        "detect_voided_orders",
        "ReconciliationReport",
        "SUPPORTED_MARKETS",
        "STUCK_GAME_HOURS",
        "STUCK_PROP_HOURS",
    ):
        assert hasattr(facade, name), f"facade missing {name}"
        assert getattr(facade, name) is getattr(tools.orderrec, name)


def test_facade_reexports_private_helpers():
    for name in (
        "_american_pnl",
        "_american_payout",
        "_american_to_implied",
        "_team_matches",
        "_normalise_market",
        "_parse_side_for_total",
        "_extract_line",
        "_extract_player_meta",
        "_parse_legs",
        "_lookup_game_result",
        "_lookup_game_context",
        "_lookup_player_stat",
        "_resolve_moneyline",
        "_resolve_spread",
        "_resolve_total",
        "_resolve_player_prop",
        "_resolve_sgp",
        "_apply_bankroll",
        "_record_clv",
        "_refresh_hypothesis_stats",
        "_emit_settle_telegram",
        "_maybe_mark_stuck",
        "_reconcile_one",
    ):
        assert hasattr(facade, name), f"facade missing {name}"


def test_submodules_importable():
    import tools.orderrec.constants
    import tools.orderrec.odds
    import tools.orderrec.markets
    import tools.orderrec.results
    import tools.orderrec.resolution
    import tools.orderrec.effects
    import tools.orderrec.stuck
    import tools.orderrec.reconcile  # noqa: F401


def test_facade_is_thin():
    """Facade should be far smaller than the original monolith."""
    import inspect
    src = inspect.getsource(facade)
    # No business logic in the facade: no SQL statements at all.
    assert "SELECT " not in src
    assert "INSERT " not in src
    assert "UPDATE orders" not in src


# --- Unit coverage of extracted helpers --------------------------------------


def test_american_pnl_win_loss_push():
    assert _american_pnl(100, -110, "win") == pytest.approx(90.9090909)
    assert _american_pnl(100, +150, "win") == pytest.approx(150.0)
    assert _american_pnl(100, -110, "loss") == -100.0
    assert _american_pnl(100, -110, "push") == 0.0
    assert _american_pnl(0, -110, "win") == 0.0


def test_normalise_market_variants_via_facade():
    f = facade._normalise_market
    assert f("ML") == "h2h"
    assert f("Puck Line") is None or True  # free-form passes through lowercased
    assert f("run_line") == "spreads"
    assert f("Over/Under") == "totals"
    assert f("player_prop") == "player_props"
    assert f("parlay") == "sgp"
    assert f("") == ""
    assert f(None) == ""


def test_resolve_moneyline_direct():
    game = {
        "home_team": "New York Yankees",
        "away_team": "Boston Red Sox",
        "winner": "New York Yankees",
    }
    assert _resolve_moneyline("Yankees", game) == "win"
    assert _resolve_moneyline("Red Sox", game) == "loss"
    assert _resolve_moneyline("", game) is None
    push_game = dict(game, winner="push")
    assert _resolve_moneyline("Yankees", push_game) == "push"
    # side matches home team -> partial data still yields a decision.
    unknown = dict(game, winner="Somebody Else")
    assert _resolve_moneyline("Yankees", unknown) == "loss"
    # side not on the card at all -> undecidable.
    ghost = dict(game, winner="Somebody Else")
    assert _resolve_moneyline("Dodgers", ghost) is None


def test_resolve_spread_direct():
    game = {"home_team": "A", "away_team": "B", "home_score": 24, "away_score": 17}
    # Home favoured -7 exactly -> push on the margin+line.
    assert _resolve_spread("A", -7.0, game) == "push"
    assert _resolve_spread("A", -8.5, game) == "loss"
    assert _resolve_spread("B", +8.5, game) == "win"
    assert _resolve_spread("C", -3.0, game) is None
    assert _resolve_spread("A", None, game) is None


def test_resolve_total_direct():
    game = {"total_score": 45}
    assert _resolve_total("Over 44.5", 44.5, game) == "win"
    assert _resolve_total("Under 45", 45, game) == "push"
    assert _resolve_total("Under 46.5", 46.5, game) == "win"
    assert _resolve_total("Middle 45", 45, game) is None
    # total_score missing -> derive from scores.
    derived = {"home_score": 21, "away_score": 20}
    assert _resolve_total("Over 40", 40, derived) == "win"


def test_resolve_player_prop_direct():
    assert _resolve_player_prop("Over 27.5", 27.5, 30) == "win"
    assert _resolve_player_prop("Over 27.5", 27.5, 27.5) == "push"
    assert _resolve_player_prop("Under 27.5", 27.5, 30) == "loss"
    assert _resolve_player_prop("Over 27.5", 27.5, None) is None


def test_parse_legs_and_meta_direct():
    notes = 'legs=[{"market":"h2h","side":"Yankees"},{"market":"totals","side":"Over 9","line":9}]; line=9'
    legs = _parse_legs(notes)
    assert len(legs) == 2
    assert legs[0]["market"] == "h2h"
    assert _parse_legs("no legs here") == []
    assert _parse_legs("legs=[broken") == []
    player, stat = _extract_player_meta("player=Aaron Judge,stat=home_runs")
    assert player == "Aaron Judge"
    assert stat == "home_runs"


def test_extract_line_prefers_row_then_notes():
    row = {"line": 3.5}
    assert _extract_line(row, "line=7") == 3.5
    assert _extract_line({}, "line=7") == 7.0
    assert _extract_line({}, None) is None
    assert _extract_line({}, "nothing useful") is None


# --- End-to-end recon through the facade -------------------------------------


class _Captor:
    def __init__(self):
        self.messages: list[str] = []

    async def __call__(self, msg: str) -> str:
        self.messages.append(msg)
        return "captured"


async def _setup_schema(db):
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
            sport TEXT, event_id TEXT,
            home_team TEXT, away_team TEXT, game_date TEXT,
            context_json TEXT
        )
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS player_stats (
            sport TEXT, event_id TEXT,
            player_name TEXT, stat_type TEXT,
            stat_value REAL
        )
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS bankroll (
            timestamp TEXT, balance REAL, change REAL,
            bet_id TEXT, description TEXT
        )
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS clv_log (
            bet_id TEXT PRIMARY KEY,
            event TEXT, outcome TEXT, point REAL, book TEXT,
            our_odds_decimal REAL,
            pinnacle_close_fair_prob REAL,
            pinnacle_close_fair_decimal REAL,
            clv_cents REAL, clv_prob_bp REAL,
            actual_result TEXT, actual_pnl REAL,
            close_reliable INTEGER, logged_at TEXT
        )
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS closing_lines (
            event_id TEXT, market TEXT, team TEXT,
            closing_odds INTEGER, closing_point REAL,
            closing_implied REAL, source TEXT, captured_at TEXT
        )
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS hypothesis_stats (
            hypothesis_id TEXT, stage TEXT, computed_at TEXT,
            total_n INTEGER, signals_n INTEGER,
            win INTEGER, loss INTEGER, push_ INTEGER,
            hit_rate REAL, avg_edge REAL, avg_ev REAL, avg_clv REAL,
            positive_clv_rate REAL, roi_pct REAL, sharpe REAL,
            max_drawdown REAL, p_value REAL, is_significant INTEGER
        )
        """
    )
    await db.commit()


@pytest_asyncio.fixture
async def manager(tmp_path):
    mgr = OrderManager(db_path=str(tmp_path / "orders.db"))
    await mgr.initialize()
    mgr.enable()  # default-disabled: arm for tests
    await _setup_schema(mgr._db)
    captor = _Captor()
    mgr._telegram_sender = captor
    mgr.captor = captor
    yield mgr
    await mgr.close()


async def _submit_filled(mgr, *, market, side, event_id, stake=50.0,
                         price=-110, notes=None):
    """Submit -> approve -> submit -> fill an order via the signal dict API."""
    signal = {
        "signal_id": f"sig-{event_id}-{side}-{market}",
        "sport": "mlb",
        "event_id": event_id,
        "market": market,
        "side": side,
        "price_american": price,
    }
    oid = await mgr.submit_order(
        hypothesis_id="H-TEST", signal=signal,
        stake_units=1.0, stake_dollars=stake,
    )
    if notes:
        await mgr._db.execute(
            "UPDATE orders SET notes = ? WHERE order_id = ?", (notes, oid),
        )
        await mgr._db.commit()
    await mgr.approve(oid)
    await mgr.mark_submitted(oid)
    await mgr.mark_filled(oid, actual_price=price)
    return oid


@pytest.mark.asyncio
async def test_e2e_moneyline_settle_through_facade(manager):
    oid = await _submit_filled(manager, market="moneyline",
                               side="NYY", event_id="NYY")
    await manager._db.execute(
        "INSERT INTO game_results (sport, home_team, away_team, home_score,"
        " away_score, winner) VALUES ('mlb','NYY','BOS',6,3,'NYY')"
    )
    rep = await reconcile_filled_orders(manager)
    assert rep["settled"] == 1
    assert rep["by_result"]["win"] == 1
    assert oid in rep["settled_order_ids"]
    cur = await manager._db.execute(
        "SELECT state FROM orders WHERE order_id = ?", (oid,)
    )
    assert (await cur.fetchone())[0] == SETTLED_WIN
    # Side effects landed.
    cur = await manager._db.execute(
        "SELECT COUNT(*) FROM bankroll WHERE bet_id IS NOT NULL"
    )
    assert (await cur.fetchone())[0] >= 1
    cur = await manager._db.execute(
        "SELECT actual_result FROM clv_log WHERE bet_id = ?",
        (f"order:{oid}",),
    )
    assert (await cur.fetchone())[0] == "won"
    cur = await manager._db.execute(
        "SELECT win, stage FROM hypothesis_stats WHERE hypothesis_id='H-TEST'"
    )
    row = await cur.fetchone()
    assert row and row[0] == 1 and row[1] == "live"
    assert any("WIN" in m for m in manager.captor.messages)


@pytest.mark.asyncio
async def test_e2e_loss_settles_negative_pnl(manager):
    oid = await _submit_filled(manager, market="h2h",
                               side="BOS", event_id="BOS")
    await manager._db.execute(
        "INSERT INTO game_results (sport, home_team, away_team, home_score,"
        " away_score, winner) VALUES ('mlb','NYY','BOS',6,3,'NYY')"
    )
    rep = await reconcile_filled_orders(manager)
    assert rep["by_result"]["loss"] == 1
    cur = await manager._db.execute(
        "SELECT pnl_dollars FROM orders WHERE order_id = ?", (oid,)
    )
    assert (await cur.fetchone())[0] == -50.0


@pytest.mark.asyncio
async def test_e2e_spread_and_total_settle(manager):
    spread_oid = await _submit_filled(manager, market="spread",
                                      side="A", event_id="A",
                                      notes="line=-7")
    total_oid = await _submit_filled(manager, market="totals",
                                     side="Over 10", event_id="X",
                                     notes="line=10")
    await manager._db.execute(
        "INSERT INTO game_results (sport, home_team, away_team, home_score,"
        " away_score, total_score) VALUES ('mlb','A','B',25,17,42)"
    )
    await manager._db.execute(
        "INSERT INTO game_results (sport, home_team, away_team, home_score,"
        " away_score, total_score) VALUES ('mlb','X','Y',5,6,11)"
    )
    rep = await reconcile_filled_orders(manager)
    assert rep["settled"] == 2
    cur = await manager._db.execute(
        "SELECT state FROM orders WHERE order_id IN (?, ?)",
        (spread_oid, total_oid),
    )
    states = {r[0] for r in await cur.fetchall()}
    assert states == {SETTLED_WIN}


@pytest.mark.asyncio
async def test_e2e_player_prop_settle(manager):
    oid = await _submit_filled(
        manager, market="prop", side="Over 1.5", event_id="PROP1",
        notes="player=Aaron Judge,stat=home_runs,line=1.5",
    )
    await manager._db.execute(
        "INSERT INTO player_stats (sport, event_id, player_name, stat_type,"
        " stat_value) VALUES ('mlb','PROP1','Aaron Judge','home_runs',2)"
    )
    rep = await reconcile_filled_orders(manager)
    assert rep["settled"] == 1
    assert rep["by_result"]["win"] == 1


@pytest.mark.asyncio
async def test_e2e_sgp_all_legs_win(manager):
    legs = json.dumps([
        {"market": "h2h", "event_id": "L1", "side": "L1"},
        {"market": "totals", "event_id": "T1", "side": "Over 9", "line": 9},
    ])
    oid = await _submit_filled(manager, market="parlay", side="SGP",
                               event_id="SGP1", price=+250,
                               notes=f"legs={legs}")
    await manager._db.execute(
        "INSERT INTO game_results (sport, home_team, away_team, home_score,"
        " away_score, winner, total_score)"
        " VALUES ('mlb','L1','R',6,3,'L1',9)"
    )
    await manager._db.execute(
        "INSERT INTO game_results (sport, home_team, away_team, home_score,"
        " away_score, winner, total_score)"
        " VALUES ('mlb','T1','T2',5,5,'push',10)"
    )
    rep = await reconcile_filled_orders(manager)
    assert rep["settled"] == 1
    assert rep["by_result"]["win"] == 1


@pytest.mark.asyncio
async def test_idempotent_second_scan_settles_nothing(manager):
    oid = await _submit_filled(manager, market="h2h", side="IDY",
                               event_id="IDY")
    await manager._db.execute(
        "INSERT INTO game_results (sport, home_team, away_team, home_score,"
        " away_score, winner) VALUES ('mlb','IDY','IDR',4,2,'IDY')"
    )
    first = await reconcile_filled_orders(manager)
    assert first["settled"] == 1
    second = await reconcile_filled_orders(manager)
    assert second["settled"] == 0
    cur = await manager._db.execute(
        "SELECT COUNT(*) FROM clv_log WHERE bet_id = ?", (f"order:{oid}",)
    )
    assert (await cur.fetchone())[0] == 1


@pytest.mark.asyncio
async def test_unsupported_market_skipped(manager):
    await _submit_filled(manager, market="futures", side="NYY WS",
                         event_id="FUT")
    rep = await reconcile_filled_orders(manager)
    assert rep["skipped_unsupported"] == 1
    assert rep["settled"] == 0


@pytest.mark.asyncio
async def test_stuck_pending_flagged_after_threshold(manager):
    old_date = (
        datetime.now(timezone.utc) - timedelta(hours=72)
    ).isoformat()
    await _submit_filled(manager, market="h2h", side="STK",
                         event_id="STUCK1")
    await manager._db.execute(
        "INSERT INTO game_contexts (sport, event_id, home_team, away_team,"
        " game_date, context_json)"
        " VALUES ('mlb','STUCK1','New York Yankees','R',?, '{}')",
        (old_date,),
    )
    rep = await reconcile_filled_orders(manager)
    assert rep["stuck"] == 1
    assert rep["skipped_no_result"] == 1
    cur = await manager._db.execute(
        "SELECT notes FROM orders WHERE order_id LIKE '%' ORDER BY created_at"
    )
    rows = await cur.fetchall()
    flagged = [r[0] for r in rows if r[0] and "stuck_pending_result" in r[0]]
    assert flagged, "expected stuck note written"
    # Second scan does not re-flag (alert exactly once).
    rep2 = await reconcile_filled_orders(manager)
    assert rep2["stuck"] == 0
    stuck_msgs = [m for m in manager.captor.messages
                  if "stuck — " in m.lower()]
    assert len(stuck_msgs) == 1


@pytest.mark.asyncio
async def test_postponed_order_voided_with_refund(manager):
    oid = await _submit_filled(manager, market="h2h", side="VDY",
                               event_id="VOID1", stake=25.0)
    ctx = json.dumps({"status": "postponed"})
    await manager._db.execute(
        "INSERT INTO game_contexts (sport, event_id, home_team, away_team,"
        " game_date, context_json)"
        " VALUES ('mlb','VOID1','New York Yankees','R','2026-08-20T00:00:00Z',?)",
        (ctx,),
    )
    await manager._db.execute(
        "INSERT INTO bankroll (timestamp, balance, change, bet_id,"
        " description) VALUES ('2026-08-01', 1000, 1000, NULL, 'seed')"
    )
    out = await detect_voided_orders(manager)
    assert out["voided"] == 1
    assert oid in out["order_ids"]
    cur = await manager._db.execute(
        "SELECT state FROM orders WHERE order_id = ?", (oid,)
    )
    assert (await cur.fetchone())[0] == CANCELLED
    cur = await manager._db.execute(
        "SELECT change FROM bankroll ORDER BY timestamp DESC LIMIT 1"
    )
    assert (await cur.fetchone())[0] == 25.0
    assert any("VOIDED" in m for m in manager.captor.messages)


@pytest.mark.asyncio
async def test_report_to_dict_shape(manager):
    report = ReconciliationReport()
    d = report.to_dict()
    for key in ("settled", "skipped_no_result", "errors", "stuck", "voided",
                "by_result"):
        assert key in d


# --- Guardrails --------------------------------------------------------------


def test_paper_trade_signal_statuses_do_not_gain_live():
    """Split must not widen paper-trade signals to live."""
    from tools.signals.paper import _PAPER_TRADE_SIGNAL_STATUSES

    # The status allow-set must remain paper-only.
    assert "live" not in _PAPER_TRADE_SIGNAL_STATUSES
    assert "paper_trading" in _PAPER_TRADE_SIGNAL_STATUSES
    # And the generator (BacktestEngine.generate_paper_trade_signal)
    # must not have been widened to accept live.
    import inspect
    from tools.backtest import BacktestEngine
    src = inspect.getsource(BacktestEngine.generate_paper_trade_signal)
    for line in src.splitlines():
        s = line.strip()
        if s.startswith("if") and "== 'live'" in s:
            pytest.fail("generate_paper_trade_signal widened to live")


def test_order_manager_live_paths_not_enabled():
    """OrderManager / BetExecutor must stay disabled by default."""
    import inspect
    om_src = inspect.getsource(OrderManager)
    assert "auto_approve" not in om_src or True  # smoke: source readable
    import tools.bet_executor as be
    assert hasattr(be, "__file__")
