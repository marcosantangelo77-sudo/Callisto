"""Telegram bot command dispatch tests — mock send, assert state changes."""

from __future__ import annotations

import pytest
import pytest_asyncio

from tools.order_manager import (
    OrderManager, PENDING_APPROVAL, APPROVED, REJECTED, SUBMITTED, FILLED,
)
from tools.telegram_bot import handle_order_command


class MockSender:
    """Records every message the bot tries to send."""

    def __init__(self):
        self.sent: list[str] = []

    async def __call__(self, msg: str):
        self.sent.append(msg)


@pytest_asyncio.fixture
async def mgr(tmp_path):
    sender = MockSender()
    m = OrderManager(db_path=str(tmp_path / "orders.db"), telegram_sender=sender)
    await m.initialize()
    try:
        yield m, sender
    finally:
        await m.close()


@pytest.fixture
def sig():
    return {
        "signal_id": "sig_001",
        "sport": "baseball_mlb",
        "event_id": "evt_LAD_SF_20260422",
        "side": "LAD",
        "price_american": -140,
        "game_description": "LAD @ SF",
    }


@pytest.mark.asyncio
async def test_approve_command_transitions_state(mgr, sig):
    m, _submit_sender = mgr
    oid = await m.submit_order(
        hypothesis_id="hyp_x", signal=sig,
        stake_units=1.0, stake_dollars=100.0,
    )
    replies = MockSender()
    ret = await handle_order_command(f"/approve {oid}", m, replies)
    assert ret == f"approved {oid}"
    o = await m.get_order(oid)
    assert o.state == APPROVED
    assert any("APPROVED" in s for s in replies.sent)


@pytest.mark.asyncio
async def test_reject_command_with_reason(mgr, sig):
    m, _ = mgr
    oid = await m.submit_order(
        hypothesis_id="hyp_x", signal=sig,
        stake_units=1.0, stake_dollars=100.0,
    )
    replies = MockSender()
    await handle_order_command(f"/reject {oid} line moved", m, replies)
    o = await m.get_order(oid)
    assert o.state == REJECTED
    # reason recorded in state history.
    assert o.state_history[-1]["reason"] == "line moved"


@pytest.mark.asyncio
async def test_fill_command_parses_price_and_transitions(mgr, sig):
    m, _ = mgr
    oid = await m.submit_order(
        hypothesis_id="hyp_x", signal=sig,
        stake_units=1.0, stake_dollars=100.0,
    )
    await m.approve(oid)
    await m.mark_submitted(oid)
    replies = MockSender()
    await handle_order_command(f"/fill {oid} -138", m, replies)
    o = await m.get_order(oid)
    assert o.state == FILLED
    assert o.price_american == -138
    assert any("FILLED" in s for s in replies.sent)


@pytest.mark.asyncio
async def test_fill_bad_price_rejected(mgr, sig):
    m, _ = mgr
    oid = await m.submit_order(
        hypothesis_id="hyp_x", signal=sig,
        stake_units=1.0, stake_dollars=100.0,
    )
    await m.approve(oid)
    await m.mark_submitted(oid)
    replies = MockSender()
    ret = await handle_order_command(f"/fill {oid} not_a_number", m, replies)
    assert ret == "error: bad price"
    o = await m.get_order(oid)
    assert o.state == SUBMITTED


@pytest.mark.asyncio
async def test_pause_and_resume(mgr):
    m, _ = mgr
    replies = MockSender()
    await handle_order_command("/pause_all", m, replies)
    assert not m.is_enabled
    await handle_order_command("/resume_all", m, replies)
    assert m.is_enabled


@pytest.mark.asyncio
async def test_non_command_returns_none(mgr):
    m, _ = mgr
    replies = MockSender()
    ret = await handle_order_command("hello there", m, replies)
    assert ret is None


@pytest.mark.asyncio
async def test_order_status_shows_pending(mgr, sig):
    m, _ = mgr
    await m.submit_order(
        hypothesis_id="hyp_x", signal=sig,
        stake_units=1.5, stake_dollars=150.0,
    )
    replies = MockSender()
    await handle_order_command("/order_status", m, replies)
    combined = "\n".join(replies.sent)
    assert "Pending approval" in combined


@pytest.mark.asyncio
async def test_submit_order_telegram_prompt_format(tmp_path, sig):
    """The outbound approval prompt must contain order_id, price, stake."""
    sender = MockSender()
    m = OrderManager(db_path=str(tmp_path / "o.db"), telegram_sender=sender)
    await m.initialize()
    try:
        oid = await m.submit_order(
            hypothesis_id="mlb_home_favs_vs_lefty",
            signal=sig,
            stake_units=1.2,
            stake_dollars=120.0,
            edge=0.038,
            clv_prior=0.021,
        )
        assert len(sender.sent) == 1
        msg = sender.sent[0]
        assert oid[-6:] in msg
        assert "1.2u" in msg or "1.20u" in msg
        assert "/approve" in msg and "/reject" in msg
        assert "-140" in msg
        assert "mlb_home_favs_vs_lefty" in msg
    finally:
        await m.close()
