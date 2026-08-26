# OX DONE — extract paper-signal gate from backtest.py (first slice)

Branch: `cursor/ox-backtest-extract-2ac0`

## What was done

Smaller-slice extraction (the method uses many `self.` helpers like
`_parse_hypothesis_filters` / `_build_schedule_context`, so moving the whole
method would have created coupling/circular-import risk):

- Created `tools/signals/__init__.py` and `tools/signals/paper.py`.
- Moved the canonical hard-gate frozenset `_PAPER_TRADE_SIGNAL_STATUSES`
  into `tools/signals/paper.py`, plus:
  - `allowed_paper_statuses() -> frozenset`
  - `reject_non_paper(status) -> bool` (True = blocked by the gate)
- `tools/backtest.py` now imports/re-exports these (existing callers keep
  working; `from tools.backtest import _PAPER_TRADE_SIGNAL_STATUSES` intact)
  and `BacktestEngine.generate_paper_trade_signal` gates via
  `reject_non_paper(h["status"])`.
- Signal math untouched. `"live"` NOT added anywhere.

## Tests

- `tests/test_live_signal_hard_gate.py`: source pin adapted to require
  `reject_non_paper` in the method source + frozenset wiring check.
- New `tests/test_backtest_extract_paper.py`: pins that the frozenset lives
  in `tools.signals.paper` (source-level), backtest aliases the same object,
  gate semantics, and method routes through `reject_non_paper`.

Result: `/tmp/callisto-pytest/bin/python -m pytest tests/test_live_signal_hard_gate.py tests/test_backtest_extract_paper.py -q`
→ **7 passed in 0.30s**

Commit: `refactor(backtest): extract paper-signal status gate to tools.signals`
