"""FSM transition tests for the order manager."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
import pytest_asyncio

# Avoid the db_writer coordinator in tests — we hit a raw aiosqlite conn.
from tools.order_manager import (
    OrderManager,
    InvalidTransition,
    OrderNotFound,
    PENDING_APPROVAL,
    APPROVED,
    SUBMITTED,
    FILLED,
    REJECTED,
    CANCELLED,
    SETTLED_WIN,
    SETTLED_LOSS,
    SETTLED_PUSH,
    EXPIRED,
    ALLOWED_TRANSITIONS,
)


async def _noop_send(msg: str) -> str:
    return "msg_123"


@pytest_asyncio.fixture
async def mgr(tmp_path):
    db = tmp_path / "orders.db"
    m = OrderManager(db_path=str(db), telegram_sender=_noop_send)
    await m.initialize()
    m.enable()  # default-disabled: arm for tests
    try:
        yield m
    finally:
        await m.close()


@pytest.fixture
def signal():
    return {
        "signal_id": "sig_001",
        "sport": "baseball_mlb",
        "event_id": "evt_LAD_SF_20260422",
        "market": "h2h",
        "side": "LAD",
        "price_american": -140,
        "game_description": "LAD @ SF",
    }


@pytest.mark.asyncio
async def test_happy_path_submit_approve_submit_fill_settle(mgr, signal):
    order_id = await mgr.submit_order(
        hypothesis_id="hyp_home_favs",
        signal=signal,
        stake_units=1.2,
        stake_dollars=120.0,
        edge=0.038,
        fair_prob=0.62,
    )
    assert order_id

    o = await mgr.get_order(order_id)
    assert o.state == PENDING_APPROVAL
    assert len(o.state_history) == 1

    await mgr.approve(order_id)
    o = await mgr.get_order(order_id)
    assert o.state == APPROVED

    await mgr.mark_submitted(order_id)
    o = await mgr.get_order(order_id)
    assert o.state == SUBMITTED
    assert o.placed_at is not None

    await mgr.mark_filled(order_id, actual_price=-138)
    o = await mgr.get_order(order_id)
    assert o.state == FILLED
    assert o.price_american == -138

    await mgr.settle(order_id, result="win", pnl_dollars=86.96)
    o = await mgr.get_order(order_id)
    assert o.state == SETTLED_WIN
    assert o.settled_at is not None
    assert o.pnl_dollars == pytest.approx(86.96)
    assert len(o.state_history) == 5


@pytest.mark.asyncio
async def test_reject_from_pending_terminal(mgr, signal):
    order_id = await mgr.submit_order(
        hypothesis_id="hyp_x", signal=signal,
        stake_units=1.0, stake_dollars=100.0,
    )
    await mgr.reject(order_id, reason="line moved")
    o = await mgr.get_order(order_id)
    assert o.state == REJECTED
    # Terminal: no further transitions allowed.
    with pytest.raises(InvalidTransition):
        await mgr.approve(order_id)
    with pytest.raises(InvalidTransition):
        await mgr.settle(order_id, result="win")


@pytest.mark.asyncio
async def test_invalid_transition_pending_to_filled(mgr, signal):
    order_id = await mgr.submit_order(
        hypothesis_id="hyp_x", signal=signal,
        stake_units=1.0, stake_dollars=100.0,
    )
    with pytest.raises(InvalidTransition):
        await mgr.mark_filled(order_id, actual_price=-110)


@pytest.mark.asyncio
async def test_invalid_transition_approved_to_settle(mgr, signal):
    order_id = await mgr.submit_order(
        hypothesis_id="hyp_x", signal=signal,
        stake_units=1.0, stake_dollars=100.0,
    )
    await mgr.approve(order_id)
    # Can't settle an order that never filled.
    with pytest.raises(InvalidTransition):
        await mgr.settle(order_id, result="win")


@pytest.mark.asyncio
async def test_cancel_from_filled_allowed(mgr, signal):
    order_id = await mgr.submit_order(
        hypothesis_id="hyp_x", signal=signal,
        stake_units=1.0, stake_dollars=100.0,
    )
    await mgr.approve(order_id)
    await mgr.mark_submitted(order_id)
    await mgr.mark_filled(order_id, actual_price=-110)
    await mgr.cancel(order_id, reason="voided by book")
    o = await mgr.get_order(order_id)
    assert o.state == CANCELLED


@pytest.mark.asyncio
async def test_not_found_raises(mgr):
    with pytest.raises(OrderNotFound):
        await mgr.get_order("nonexistent")
    with pytest.raises(OrderNotFound):
        await mgr.approve("nonexistent")


@pytest.mark.asyncio
async def test_transition_map_completeness():
    """Every known state must have an entry in ALLOWED_TRANSITIONS."""
    states = {
        PENDING_APPROVAL, APPROVED, SUBMITTED, FILLED,
        REJECTED, CANCELLED, EXPIRED,
        SETTLED_WIN, SETTLED_LOSS, SETTLED_PUSH,
    }
    assert set(ALLOWED_TRANSITIONS.keys()) == states


@pytest.mark.asyncio
async def test_state_history_appended(mgr, signal):
    order_id = await mgr.submit_order(
        hypothesis_id="hyp_x", signal=signal,
        stake_units=1.0, stake_dollars=100.0,
    )
    await mgr.approve(order_id)
    await mgr.mark_submitted(order_id)
    o = await mgr.get_order(order_id)
    assert [h["state"] for h in o.state_history] == [
        PENDING_APPROVAL, APPROVED, SUBMITTED,
    ]
    for h in o.state_history:
        assert "at" in h
        assert "reason" in h
