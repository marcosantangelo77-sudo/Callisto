# OX TASK: extract BacktestEngine I/O and filters (LONG)

You are an Ox Alpha implementation worker on Callisto. Work ONLY in this worktree.
Branch: `cursor/ox-backtest-split-2ac0`
Worktree: `/tmp/callisto-ox-backtest-split`

LONG extract. `tools/backtest.py` is ~4.2k. Slice 1 moved the paper-status
gate to `tools/signals/paper.py`. Move more: DB/load helpers, filter parsers,
and/or schedule helpers into `tools/signals/` or `tools/backtest_io.py`.
Do not move `generate_paper_trade_signal` whole. Do not add `"live"` to
the paper frozenset.

## Exclusive files (HARD)

You MAY edit:
- `tools/backtest.py`
- `tools/signals/` (additional modules)
- `tools/backtest_io.py` (create if cleaner)
- `tests/test_backtest_extract_paper.py` / `tests/test_live_signal_hard_gate.py`
  / `tests/test_fail_closed_registry.py` (adapt)
- `tests/test_backtest_split.py` (create)

## Git rules

No stash / reset --hard / full `pytest tests/`. Push. No merge to master.

## Required

- `_PAPER_TRADE_SIGNAL_STATUSES = frozenset({"paper_trading"})` stays in
  `tools/signals/paper.py`; backtest re-exports; no `"live"`.
- `generate_paper_trade_signal` still `reject_non_paper`.
- Drop hundreds of lines from backtest.py.

```
/tmp/callisto-pytest/bin/python -m pytest tests/test_backtest_split.py tests/test_backtest_extract_paper.py tests/test_live_signal_hard_gate.py tests/test_fail_closed_registry.py -q
```

Commit: `refactor(backtest): extract engine I/O and filters out of god module`

Write `OX_DONE.md` with line counts.
