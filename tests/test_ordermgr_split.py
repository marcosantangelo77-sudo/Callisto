"""Tests for the tools.ordermgr split from tools/order_manager.py.

Verifies that:
  * the extracted package re-exports everything the manager needs
  * FSM validation, ULID, settle aliasing, and transition history work
  * OrderManager behavior is unchanged (idempotency, expiry, bets sync)
  * fail-closed defaults and CALLISTO_LOCAL_ONLY kill switch survive
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

import tools.order_manager as om
from tools.ordermgr import (
    ALLOWED_TRANSITIONS,
    APPROVED,
    EXPIRED,
    FILLED,
    InvalidTransition,
    OPEN_STATES,
    ORDER_EXPIRY_MIN,
    Order,
    OrderNotFound,
    PENDING_APPROVAL,
    SETTLED_WIN,
    SUBMITTED,
    TERMINAL_STATES,
    apply_transition,
    assert_transition,
    canonical_settle_result,
    format_approval_message,
    new_ulid,
)
from tools.ordermgr.transitions import WRITABLE_COLUMNS


# --- Package integrity -------------------------------------------------------


def test_package_reexports_match_manager():
    for name in (
        "PENDING_APPROVAL", "APPROVED", "SUBMITTED", "FILLED",
        "REJECTED", "CANCELLED", "SETTLED_WIN", "SETTLED_LOSS",
        "SETTLED_PUSH", "EXPIRED", "ALLOWED_TRANSITIONS",
        "InvalidTransition", "OrderNotFound", "new_ulid",
    ):
        assert getattr(om, name) is not None, f"order_manager missing {name}"


def test_states_are_strings_and_disjoint():
    assert isinstance(OPEN_STATES, frozenset)
    assert isinstance(TERMINAL_STATES, frozenset)
    assert not (OPEN_STATES & TERMINAL_STATES)


def test_fsm_edges_point_at_known_states():
    known = OPEN_STATES | TERMINAL_STATES
    for src, dsts in ALLOWED_TRANSITIONS.items():
        assert src in known
        for d in dsts:
            assert d in known
            assert d in ALLOWED_TRANSITIONS  # every state has an entry


def test_terminal_states_have_no_outgoing_edges():
    for s in TERMINAL_STATES:
        assert len(ALLOWED_TRANSITIONS[s]) == 0


def test_happy_path_edges_exist():
    assert APPROVED in ALLOWED_TRANSITIONS[PENDING_APPROVAL]
    assert SUBMITTED in ALLOWED_TRANSITIONS[APPROVED]
    assert FILLED in ALLOWED_TRANSITIONS[SUBMITTED]
    assert SETTLED_WIN in ALLOWED_TRANSITIONS[FILLED]


# --- ULID --------------------------------------------------------------------


def test_new_ulid_shape_and_uniqueness():
    ids = {new_ulid() for _ in range(500)}
    assert len(ids) == 500
    for u in ids:
        assert len(u) == 26
        assert all(c in "0123456789ABCDEFGHJKMNPQRSTVWXYZ" for c in u)


def test_ulids_sort_roughly_by_time():
    a, b = new_ulid(), None
    b = new_ulid()
    assert a <= b


# --- Settle aliases ----------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("win", SETTLED_WIN),
        ("won", SETTLED_WIN),
        ("settled_win", SETTLED_WIN),
        ("loss", "settled_loss"),
        ("lost", "settled_loss"),
        ("push", "settled_push"),
        ("SETTLED_LOSS", "settled_loss"),
        ("Push", "settled_push"),
    ],
)
def test_canonical_settle_result(raw, expected):
    assert canonical_settle_result(raw) == expected


@pytest.mark.parametrize("bad", ["", "banana", "forfeit", "WINN"])
def test_canonical_settle_result_rejects_unknown(bad):
    with pytest.raises(ValueError, match="Unknown settle result"):
        canonical_settle_result(bad)


def test_assert_transition_accepts_and_rejects():
    assert_transition(PENDING_APPROVAL, APPROVED, "o1")
    with pytest.raises(InvalidTransition):
        assert_transition(EXPIRED, APPROVED, "o1")


# --- Approval message --------------------------------------------------------


def test_format_approval_message_contains_commands():
    msg = format_approval_message(
        order_id="01ABCDEFGHJKMNPQRSTVWXYZ1",
        signal={
            "sport": "baseball_mlb",
            "game_description": "Yankees @ Red Sox",
            "side": "Yankees ML",
            "price_american": -120,
        },
        book="draftkings",
        stake_units=1.5,
        stake_dollars=150.0,
        hypothesis_id="hyp123",
        edge=0.04,
        clv_prior=0.02,
        expiry_min=ORDER_EXPIRY_MIN,
    )
    assert "/approve 01ABCDEFGHJKMNPQRSTVWXYZ1" in msg
    assert "/reject 01ABCDEFGHJKMNPQRSTVWXYZ1" in msg
    assert "-120" in msg
    assert "draftkings" in msg
    assert "edge=4.0%" in msg
    assert "CLV_prior=+2.0%" in msg
    assert "Expires in" in msg


def test_format_approval_message_positive_price_gets_plus():
    msg = format_approval_message(
        order_id="X",
        signal={"sport": "nfl", "side": "Underdog ML", "price_american": 210},
        book="fanatics",
        stake_units=0.5,
        stake_dollars=50,
        hypothesis_id="h",
        edge=None,
        clv_prior=None,
        expiry_min=10,
    )
    assert "+210" in msg


# --- Transition engine against a real sqlite DB ------------------------------


class FakeCursor:
    def __init__(self, db, sql, params):
        self._db = db
        self._sql = sql
        self._params = params

    async def fetchone(self):
        self._db.execute(self._sql, self._params)
        return self._db.execute("SELECT * FROM orders WHERE order_id = ?",
                                (self._params[-1] if self._params else "",)).fetchone()

    async def fetchall(self):
        return []


@pytest.mark.asyncio
async def test_apply_transition_appends_history(tmp_path):
    import sqlite3

    from tools.ordermgr.constants import CREATE_ORDERS_TABLE_SQL

    conn = sqlite3.connect(str(tmp_path / "t.db"))
    conn.row_factory = sqlite3.Row
    conn.execute(CREATE_ORDERS_TABLE_SQL)
    now = datetime.now(timezone.utc).isoformat()
    # Seed with the same initial history submit_order() would have written.
    initial = json.dumps([{"state": PENDING_APPROVAL, "at": now,
                           "reason": "submitted"}])
    conn.execute(
        "INSERT INTO orders (order_id, hypothesis_id, state, "
        "state_history_json, created_at) VALUES (?, ?, ?, ?, ?)",
        ("ordX", "hypX", PENDING_APPROVAL, initial, now),
    )
    conn.commit()
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, lambda: None)

    order = await apply_transition(conn, "ordX", APPROVED, reason="test_ok")
    assert order.state == APPROVED
    hist = json.loads(conn.execute(
        "SELECT state_history_json FROM orders WHERE order_id='ordX'"
    ).fetchone()[0])
    assert [h["state"] for h in hist] == [PENDING_APPROVAL, APPROVED]
    assert hist[-1]["reason"] == "test_ok"
    conn.close()


@pytest.mark.asyncio
async def test_apply_transition_rejects_illegal_edge(tmp_path):
    import sqlite3

    from tools.ordermgr.constants import CREATE_ORDERS_TABLE_SQL

    conn = sqlite3.connect(str(tmp_path / "t.db"))
    conn.row_factory = sqlite3.Row
    conn.execute(CREATE_ORDERS_TABLE_SQL)
    conn.execute(
        "INSERT INTO orders (order_id, hypothesis_id, state, "
        "state_history_json, created_at) VALUES (?, ?, ?, '[]', ?)",
        ("ordY", "hypY", EXPIRED, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    with pytest.raises(InvalidTransition, match="Cannot transition"):
        await apply_transition(conn, "ordY", FILLED, reason="nope")
    conn.close()


@pytest.mark.asyncio
async def test_load_order_row_raises_not_found(tmp_path):
    import sqlite3

    from tools.ordermgr.constants import CREATE_ORDERS_TABLE_SQL
    from tools.ordermgr.transitions import load_order_row

    conn = sqlite3.connect(str(tmp_path / "t.db"))
    conn.row_factory = sqlite3.Row
    conn.execute(CREATE_ORDERS_TABLE_SQL)
    conn.commit()
    with pytest.raises(OrderNotFound):
        await load_order_row(conn, "missing")
    conn.close()


def test_writable_columns_whitelist_is_safe():
    assert "state" not in WRITABLE_COLUMNS
    assert "state_history_json" not in WRITABLE_COLUMNS
    assert "pnl_dollars" in WRITABLE_COLUMNS
    assert "placed_at" in WRITABLE_COLUMNS


# --- Manager end-to-end (mirrors legacy behavior) ----------------------------


class RecordingSender:
    def __init__(self):
        self.messages: list[str] = []

    async def __call__(self, msg: str):
        self.messages.append(msg)
        return f"tg-{len(self.messages)}"


@pytest.fixture()
def sender():
    return RecordingSender()


SIG = {
    "signal_id": "sig-1",
    "sport": "basketball_nba",
    "event_id": "evt-9",
    "market": "moneyline",
    "side": "Lakers ML",
    "price_american": -110,
    "game_description": "LAL @ BOS",
}


@pytest.mark.asyncio
async def test_disabled_manager_refuses_submit(tmp_path, sender):
    m = om.OrderManager(db_path=str(tmp_path / "d.db"), telegram_sender=sender)
    await m.initialize()
    try:
        with pytest.raises(RuntimeError, match="disabled"):
            await m.submit_order(
                hypothesis_id="h", signal=SIG,
                stake_units=1.0, stake_dollars=100.0,
            )
    finally:
        await m.close()


@pytest.mark.asyncio
async def test_submit_then_full_lifecycle(tmp_path, sender):
    m = om.OrderManager(db_path=str(tmp_path / "l.db"), telegram_sender=sender)
    m.enable()
    await m.initialize()
    try:
        oid = await m.submit_order(
            hypothesis_id="hyp-lc",
            signal=SIG,
            stake_units=1.25,
            stake_dollars=125.0,
            edge=0.05,
            fair_prob=0.57,
        )
        assert isinstance(oid, str) and len(oid) == 26

        order = await m.get_order(oid)
        assert order.state == PENDING_APPROVAL
        assert order.stake_units == 1.25
        assert len(order.state_history) == 1
        assert len(sender.messages) == 1
        assert f"/approve {oid}" in sender.messages[0]
        assert order.telegram_msg_id == "tg-1"

        await m.approve(oid)
        await m.mark_submitted(oid)
        filled = await m.mark_filled(oid, actual_price=-105)
        assert filled.state == FILLED
        assert filled.price_american == -105
        assert filled.placed_at is not None

        settled = await m.settle(oid, result="win", pnl_dollars=113.64)
        assert settled.state == SETTLED_WIN
        assert settled.pnl_dollars == 113.64
        assert settled.settled_at is not None

        hist = settled.state_history
        assert [h["state"] for h in hist] == [
            PENDING_APPROVAL, APPROVED, SUBMITTED, FILLED, SETTLED_WIN,
        ]
    finally:
        await m.close()


@pytest.mark.asyncio
async def test_submit_is_idempotent_on_signal(tmp_path, sender):
    m = om.OrderManager(db_path=str(tmp_path / "i.db"), telegram_sender=sender)
    m.enable()
    await m.initialize()
    try:
        first = await m.submit_order(
            hypothesis_id="hyp-idem", signal=SIG,
            stake_units=1.0, stake_dollars=100.0,
        )
        second = await m.submit_order(
            hypothesis_id="hyp-idem", signal=dict(SIG),
            stake_units=2.0, stake_dollars=200.0,
        )
        assert first == second
        orders = await m.list_orders(state=PENDING_APPROVAL)
        assert len([o for o in orders if o.order_id == first]) == 1
    finally:
        await m.close()


@pytest.mark.asyncio
async def test_expire_stale_moves_only_past_ttl(tmp_path, sender):
    m = om.OrderManager(db_path=str(tmp_path / "e.db"), telegram_sender=sender)
    m.enable()
    await m.initialize()
    try:
        fresh = await m.submit_order(
            hypothesis_id="hyp-fresh", signal=SIG,
            stake_units=1.0, stake_dollars=100.0,
        )
        stale_sig = dict(SIG, signal_id="sig-stale")
        stale = await m.submit_order(
            hypothesis_id="hyp-stale", signal=stale_sig,
            stake_units=1.0, stake_dollars=100.0,
        )
        # Backdate the stale order past its TTL.
        past = (datetime.now(timezone.utc) - timedelta(minutes=60)).isoformat()
        await m._db.execute(
            "UPDATE orders SET expires_at = ? WHERE order_id = ?",
            (past, stale),
        )
        await m._db.commit()

        expired = await m.expire_stale()
        assert expired == [stale]
        assert (await m.get_order(stale)).state == EXPIRED
        assert (await m.get_order(fresh)).state == PENDING_APPROVAL
    finally:
        await m.close()


@pytest.mark.asyncio
async def test_get_missing_order_raises(tmp_path, sender):
    m = om.OrderManager(db_path=str(tmp_path / "n.db"), telegram_sender=sender)
    await m.initialize()
    try:
        with pytest.raises(OrderNotFound):
            await m.get_order("does-not-exist")
    finally:
        await m.close()


@pytest.mark.asyncio
async def test_bets_sync_on_fill_best_effort(tmp_path, sender):
    """No ``bets`` table in this stripped DB -> fill must not raise."""
    m = om.OrderManager(db_path=str(tmp_path / "b.db"), telegram_sender=sender)
    m.enable()
    await m.initialize()
    try:
        oid = await m.submit_order(
            hypothesis_id="hyp-bets", signal=SIG,
            stake_units=1.0, stake_dollars=100.0,
            edge=0.05, fair_prob=0.55,
        )
        await m.approve(oid)
        await m.mark_submitted(oid)
        order = await m.mark_filled(oid)
        assert order.state == FILLED
        assert order.bet_id is None  # sync was skipped, not fatal
    finally:
        await m.close()


@pytest.mark.asyncio
async def test_local_only_blocks_enable(tmp_path, sender):
    with patch.dict(os.environ, {"CALLISTO_LOCAL_ONLY": "yes"}):
        m = om.OrderManager(db_path=str(tmp_path / "lo.db"), telegram_sender=sender)
        assert m.enable() is False
        assert m.is_enabled is False


def test_order_from_row_tolerates_bad_history_json():
    class Row(dict):
        pass

    row = Row({
        "order_id": "o", "hypothesis_id": "h", "state": PENDING_APPROVAL,
        "state_history_json": "{not json",
    })
    o = Order.from_row(row)
    assert o.state_history == []
