"""Hard-gate characterization tests for generate_paper_trade_signal.

These tests EXECUTE the method (not just scan source) and must FAIL if anyone:
  - adds "live" (or anything else) to _PAPER_TRADE_SIGNAL_STATUSES, or
  - rewrites the gate to accept a live-status hypothesis.
A live hypothesis must return [] BEFORE any odds/game processing — even when
live_odds is full of games.
"""

import asyncio
from unittest.mock import MagicMock

import pytest

from tools.backtest import BacktestEngine, _PAPER_TRADE_SIGNAL_STATUSES


FULL_LIVE_ODDS = {
    "games": [
        {
            "id": "g1",
            "sport_key": "basketball_nba",
            "home_team": "Lakers",
            "away_team": "Celtics",
            "commence_time": "2026-08-26T02:30:00Z",
            "bookmakers": [],
        }
        for _ in range(5)
    ]
}


def _engine_with_hypothesis(status):
    engine = BacktestEngine.__new__(BacktestEngine)
    hm = MagicMock()

    async def _get(hid):
        return {"status": status}

    hm.get_hypothesis = _get
    engine.hypothesis_manager = hm
    return engine


@pytest.mark.asyncio
async def test_live_status_returns_empty_even_with_full_odds():
    """A 'live' hypothesis must be rejected before odds processing."""
    engine = _engine_with_hypothesis("live")
    signals = await engine.generate_paper_trade_signal("hyp-1", FULL_LIVE_ODDS)
    assert signals == []


def test_allowed_status_set_is_exactly_paper_trading():
    """The frozenset gate must contain ONLY 'paper_trading'. Adding 'live'
    (or any other status) fails this pin."""
    assert isinstance(_PAPER_TRADE_SIGNAL_STATUSES, frozenset)
    assert _PAPER_TRADE_SIGNAL_STATUSES == frozenset({"paper_trading"})
    assert "live" not in _PAPER_TRADE_SIGNAL_STATUSES


def test_source_gate_does_not_accept_live():
    """Source-level pin stronger than the tier7 one: the gate comparison must
    use the frozenset membership check, and no tuple/set in the method may
    include "live"."""
    import inspect

    src = inspect.getsource(BacktestEngine.generate_paper_trade_signal)
    # Gate moved to tools.signals.paper.reject_non_paper (extraction slice 1)
    assert "reject_non_paper" in src
    # Belt-and-braces: the canonical frozenset must still be wired through
    from tools.backtest import _PAPER_TRADE_SIGNAL_STATUSES
    assert _PAPER_TRADE_SIGNAL_STATUSES == frozenset({"paper_trading"})
    assert 'h["status"] != "paper_trading"' not in src
    assert '"paper_trading", "live"' not in src
    assert '"live", "paper_trading"' not in src
