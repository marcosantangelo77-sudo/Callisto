"""Characterization pins for the paper-signal gate extraction.

The frozenset + gate check now live in tools/signals/paper.py.
tools/backtest.py re-exports them for backward compatibility.
"""

import inspect

from tools.backtest import BacktestEngine
from tools.signals.paper import (
    _PAPER_TRADE_SIGNAL_STATUSES,
    allowed_paper_statuses,
    reject_non_paper,
)


def test_frozenset_lives_in_tools_signals_paper():
    """Pin: the canonical definition lives in tools.signals.paper."""
    src = inspect.getsource(
        __import__("tools.signals.paper", fromlist=["x"])
    )
    assert '_PAPER_TRADE_SIGNAL_STATUSES = frozenset({"paper_trading"})' in src
    assert isinstance(_PAPER_TRADE_SIGNAL_STATUSES, frozenset)
    assert allowed_paper_statuses() == frozenset({"paper_trading"})


def test_backtest_reexports_are_the_same_object():
    """backtest.py must alias the paper module's set, not define its own."""
    from tools.backtest import _PAPER_TRADE_SIGNAL_STATUSES as bt_set
    import tools.signals.paper as pmod

    assert bt_set is pmod._PAPER_TRADE_SIGNAL_STATUSES


def test_reject_non_paper_gate():
    """Gate semantics: only 'paper_trading' passes; 'live' (and everything
    else) is rejected."""
    assert reject_non_paper("paper_trading") is False
    assert reject_non_paper("live") is True
    assert reject_non_paper(None) is True
    assert reject_non_paper("archived") is True
    assert "live" not in allowed_paper_statuses()


def test_engine_gate_uses_extracted_check():
    """Source-level pin: generate_paper_trade_signal routes through
    tools.signals.paper (reject_non_paper), not an inline status literal."""
    src = inspect.getsource(BacktestEngine.generate_paper_trade_signal)
    assert "reject_non_paper(" in src
    assert '"paper_trading"' not in src.split("reject_non_paper(")[0].split('"""')[-1]
