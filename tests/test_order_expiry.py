"""Expiry: pending_approval orders past expires_at get swept to expired."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from tools.order_manager import OrderManager, EXPIRED, PENDING_APPROVAL, APPROVED


async def _noop_send(msg: str) -> str:
    return "msg"


@pytest_asyncio.fixture
async def mgr(tmp_path):
    m = OrderManager(
        db_path=str(tmp_path / "orders.db"),
        telegram_sender=_noop_send,
    )
    await m.initialize()
    try:
        yield m
    finally:
        await m.close()


@pytest.mark.asyncio
async def test_expire_stale_moves_old_pending_to_expired(mgr):
    oid = await mgr.submit_order(
        hypothesis_id="hyp_a",
        signal={"signal_id": "s1", "sport": "mlb", "side": "A", "price_american": -110},
        stake_units=1.0, stake_dollars=100.0,
    )
    # Rewrite expires_at to the past.
    past = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
    await mgr._db.execute(
        "UPDATE orders SET expires_at = ? WHERE order_id = ?", (past, oid),
    )
    await mgr._db.commit()

    expired = await mgr.expire_stale()
    assert oid in expired
    o = await mgr.get_order(oid)
    assert o.state == EXPIRED
    # State history must record the expiry.
    assert o.state_history[-1]["state"] == EXPIRED
    assert o.state_history[-1]["reason"] == "ttl_expired"


@pytest.mark.asyncio
async def test_fresh_pending_order_not_expired(mgr):
    oid = await mgr.submit_order(
        hypothesis_id="hyp_a",
        signal={"signal_id": "s2", "sport": "mlb", "side": "A", "price_american": -110},
        stake_units=1.0, stake_dollars=100.0,
    )
    expired = await mgr.expire_stale()
    assert oid not in expired
    o = await mgr.get_order(oid)
    assert o.state == PENDING_APPROVAL


@pytest.mark.asyncio
async def test_approved_order_never_expired(mgr):
    oid = await mgr.submit_order(
        hypothesis_id="hyp_a",
        signal={"signal_id": "s3", "sport": "mlb", "side": "A", "price_american": -110},
        stake_units=1.0, stake_dollars=100.0,
    )
    await mgr.approve(oid)
    # Even if expires_at is ancient, expire_stale only touches pending_approval.
    past = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    await mgr._db.execute(
        "UPDATE orders SET expires_at = ? WHERE order_id = ?", (past, oid),
    )
    await mgr._db.commit()
    expired = await mgr.expire_stale()
    assert oid not in expired
    o = await mgr.get_order(oid)
    assert o.state == APPROVED


@pytest.mark.asyncio
async def test_explicit_now_argument(mgr):
    oid = await mgr.submit_order(
        hypothesis_id="hyp_a",
        signal={"signal_id": "s4", "sport": "mlb", "side": "A", "price_american": -110},
        stake_units=1.0, stake_dollars=100.0,
    )
    # 60 minutes from now — well past default 10-min expiry.
    future = datetime.now(timezone.utc) + timedelta(minutes=60)
    expired = await mgr.expire_stale(now=future)
    assert oid in expired
