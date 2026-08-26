"""CALLISTO_LOCAL_ONLY must make OrderManager.enable() a no-op (nuclear kill switch)."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from tools.order_manager import OrderManager


class MockSender:
    async def __call__(self, msg: str):
        pass


def _make(tmp_path):
    return OrderManager(db_path=str(tmp_path / "o.db"), telegram_sender=MockSender())


def test_enable_arms_by_default(tmp_path):
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("CALLISTO_LOCAL_ONLY", None)
        m = _make(tmp_path)
        assert m.is_enabled is False
        result = m.enable()
        assert m.is_enabled is True
        if result is not None:
            assert result is True


@pytest.mark.parametrize("val", ["1", "true", "yes", "TRUE", "Yes"])
def test_local_only_blocks_enable(tmp_path, val):
    with patch.dict(os.environ, {"CALLISTO_LOCAL_ONLY": val}):
        m = _make(tmp_path)
        result = m.enable()
        assert m.is_enabled is False
        if result is not None:
            assert result is False


@pytest.mark.asyncio
async def test_local_only_submit_order_refused(tmp_path):
    sig = {"signal_id": "sig_lo", "sport": "baseball_mlb"}
    with patch.dict(os.environ, {"CALLISTO_LOCAL_ONLY": "1"}):
        m = _make(tmp_path)
        m.enable()
        assert not m.is_enabled
        await m.initialize()
        try:
            with pytest.raises(RuntimeError, match="disabled"):
                await m.submit_order(
                    hypothesis_id="hyp_lo", signal=sig,
                    stake_units=1.0, stake_dollars=100.0,
                )
        finally:
            await m.close()


def test_disable_still_works(tmp_path):
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("CALLISTO_LOCAL_ONLY", None)
        m = _make(tmp_path)
        m.enable()
        assert m.is_enabled is True
        m.disable()
        assert m.is_enabled is False
