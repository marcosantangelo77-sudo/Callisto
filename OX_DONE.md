# OX DONE — pin generate_paper_trade_signal to paper_trading only

Branch: `cursor/ox-live-signal-hard-gate-2ac0`
Commit: `4ad6cc0d58cf0ef98a9793229e9dcb66c875b11e`
Pushed to: `origin/cursor/ox-live-signal-hard-gate-2ac0`

## Changes

- `tools/backtest.py`
  - Added module constant `_PAPER_TRADE_SIGNAL_STATUSES = frozenset({"paper_trading"})` (with a comment warning never to add `"live"`).
  - Gate in `generate_paper_trade_signal` now checks `h["status"] not in _PAPER_TRADE_SIGNAL_STATUSES -> []`.
  - Docstring: explicit HARD GATE warning that accepting `"live"` is forbidden (would arm untested sizing/caps/kill-switch).
- `tests/test_live_signal_hard_gate.py` (new, executes the method)
  - `test_live_status_returns_empty_even_with_full_odds`: stubs `hypothesis_manager.get_hypothesis` with status=`"live"` and full `live_odds`; asserts the method returns `[]` before any odds/game processing.
  - `test_allowed_status_set_is_exactly_paper_trading`: frozenset must equal exactly `{"paper_trading"}`; fails if anyone adds `"live"` or anything else.
  - `test_source_gate_does_not_accept_live`: source pin asserting membership check via the frozenset and no `("paper_trading", "live")` tuple anywhere in the method.
- `tests/test_tier7_deepresearch.py` (pin kept in sync)
  - Existing source pin updated to accept either the direct `!=` check or the equivalent `_PAPER_TRADE_SIGNAL_STATUSES` membership gate.

## Test output

```
$ /tmp/callisto-pytest/bin/python -m pytest tests/test_live_signal_hard_gate.py tests/test_tier7_deepresearch.py::TestHorizonStatePins::test_paper_trade_signal_requires_paper_trading_status -q
....                                                                     [100%]
4 passed in 0.15s
```

(The tier7 file was run by node id for just the existing pin test, per task instructions.)
