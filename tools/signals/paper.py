"""Paper-trade signal hard gate.

Extracted from tools/backtest.py (first slice of the god-module diet).
This module owns the ONLY definition of which hypothesis statuses may run
``BacktestEngine.generate_paper_trade_signal``.
"""

# HARD GATE: generate_paper_trade_signal runs ONLY for paper_trading
# hypotheses. "live" (or any other status) must NEVER be added here — the live
# path needs separately tested sizing/caps/kill-switch, not this method.
_PAPER_TRADE_SIGNAL_STATUSES = frozenset({"paper_trading"})


def allowed_paper_statuses() -> frozenset:
    """Return the set of statuses allowed to generate paper-trade signals."""
    return _PAPER_TRADE_SIGNAL_STATUSES


def reject_non_paper(status: object) -> bool:
    """True when ``status`` is NOT permitted through the paper-signal gate."""
    return status not in _PAPER_TRADE_SIGNAL_STATUSES
