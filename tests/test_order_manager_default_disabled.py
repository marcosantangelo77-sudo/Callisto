"""OrderManager must default to DISABLED (fail-closed)."""

from __future__ import annotations

import pytest

from tools.order_manager import OrderManager


class MockSender:
    async def __call__(self, msg: str):
        pass


@pytest.mark.asyncio
async def test_order_manager_defaults_disabled(tmp_path):
    m = OrderManager(db_path=str(tmp_path / "o.db"), telegram_sender=MockSender())
    assert not m.is_enabled


@pytest.mark.asyncio
async def test_submit_refused_until_enabled(tmp_path, sig=None):
    sig = {
        "signal_id": "sig_001",
        "sport": "baseball_mlb",
        "event_id": "evt_1",
        "side": "LAD",
        "price_american": -140,
        "game_description": "LAD @ SF",
    }
    m = OrderManager(db_path=str(tmp_path / "o.db"), telegram_sender=MockSender())
    await m.initialize()
    try:
        with pytest.raises(RuntimeError, match="disabled"):
            await m.submit_order(
                hypothesis_id="hyp_x", signal=sig,
                stake_units=1.0, stake_dollars=100.0,
            )
        m.enable()
        oid = await m.submit_order(
            hypothesis_id="hyp_x", signal=sig,
            stake_units=1.0, stake_dollars=100.0,
        )
        assert oid
    finally:
        await m.close()
