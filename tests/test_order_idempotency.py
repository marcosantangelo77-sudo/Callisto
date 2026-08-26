"""Idempotency: submitting twice with same (hypothesis_id, signal_id)
returns the same order_id."""

from __future__ import annotations

import pytest
import pytest_asyncio

from tools.order_manager import OrderManager, APPROVED, REJECTED


async def _noop_send(msg: str) -> str:
    return "msg"


@pytest_asyncio.fixture
async def mgr(tmp_path):
    m = OrderManager(
        db_path=str(tmp_path / "orders.db"),
        telegram_sender=_noop_send,
    )
    await m.initialize()
    m.enable()  # default-disabled: arm for tests
    try:
        yield m
    finally:
        await m.close()


@pytest.fixture
def sig():
    return {
        "signal_id": "sig_42",
        "sport": "baseball_mlb",
        "event_id": "evt_LAD_SF",
        "market": "h2h",
        "side": "LAD",
        "price_american": -140,
    }


@pytest.mark.asyncio
async def test_double_submit_same_signal_same_id(mgr, sig):
    oid1 = await mgr.submit_order(
        hypothesis_id="hyp_a", signal=sig,
        stake_units=1.0, stake_dollars=100.0,
    )
    oid2 = await mgr.submit_order(
        hypothesis_id="hyp_a", signal=sig,
        stake_units=2.5, stake_dollars=250.0,  # even with different stake
    )
    assert oid1 == oid2
    # Stake from the first submit wins — we don't overwrite.
    o = await mgr.get_order(oid1)
    assert o.stake_units == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_different_signal_ids_yield_different_orders(mgr, sig):
    oid1 = await mgr.submit_order(
        hypothesis_id="hyp_a", signal=sig,
        stake_units=1.0, stake_dollars=100.0,
    )
    sig2 = dict(sig, signal_id="sig_43")
    oid2 = await mgr.submit_order(
        hypothesis_id="hyp_a", signal=sig2,
        stake_units=1.0, stake_dollars=100.0,
    )
    assert oid1 != oid2


@pytest.mark.asyncio
async def test_different_hypotheses_yield_different_orders(mgr, sig):
    oid1 = await mgr.submit_order(
        hypothesis_id="hyp_a", signal=sig,
        stake_units=1.0, stake_dollars=100.0,
    )
    oid2 = await mgr.submit_order(
        hypothesis_id="hyp_b", signal=sig,
        stake_units=1.0, stake_dollars=100.0,
    )
    assert oid1 != oid2


@pytest.mark.asyncio
async def test_rejected_order_does_not_block_resubmit(mgr, sig):
    oid1 = await mgr.submit_order(
        hypothesis_id="hyp_a", signal=sig,
        stake_units=1.0, stake_dollars=100.0,
    )
    await mgr.reject(oid1)
    # Same signal_id, different order_id now — terminal states don't block.
    oid2 = await mgr.submit_order(
        hypothesis_id="hyp_a", signal=sig,
        stake_units=1.0, stake_dollars=100.0,
    )
    assert oid2 != oid1


@pytest.mark.asyncio
async def test_null_signal_id_creates_distinct_orders(mgr):
    # Without a signal_id we fall back to "no idempotency" — each submit
    # is a new order. This matches the migration's partial-unique-index
    # definition.
    sig_no_id = {"sport": "mlb", "side": "LAD", "price_american": -140}
    oid1 = await mgr.submit_order(
        hypothesis_id="hyp_a", signal=sig_no_id,
        stake_units=1.0, stake_dollars=100.0,
    )
    oid2 = await mgr.submit_order(
        hypothesis_id="hyp_a", signal=sig_no_id,
        stake_units=1.0, stake_dollars=100.0,
    )
    assert oid1 != oid2
