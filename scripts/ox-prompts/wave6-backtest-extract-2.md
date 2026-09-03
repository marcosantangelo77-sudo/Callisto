# OX TASK: extract schedule/date helpers from generate_paper_trade_signal

You are an Ox Alpha implementation worker on Callisto. Work ONLY in this worktree.
Branch: `cursor/ox-backtest-extract2-2ac0`
Worktree: `/tmp/callisto-ox-backtest-extract2`

## Exclusive files (HARD)

You MAY edit:
- `tools/backtest.py` (only `generate_paper_trade_signal` helpers + imports)
- `tools/signals/schedule.py` (create)
- `tools/signals/__init__.py` (export if needed)
- `tests/test_backtest_extract_schedule.py` (create)
- `tests/test_live_signal_hard_gate.py` / `tests/test_backtest_extract_paper.py` (adapt only if import paths break)

Do NOT add `"live"` to paper-signal statuses. Do NOT move the whole
`generate_paper_trade_signal` method (it uses many `self.` helpers).
Do NOT change signal math, thresholds, or odds processing.

## Git rules

No stash / reset --hard / full `pytest tests/`. Push. Base origin/master.

## Why

`tools/backtest.py` is still ~4.2k. Slice 1 moved the paper-status
frozenset to `tools/signals/paper.py`. Slice 2: pull the nested
`_game_date_from_commence` helper (and only that, plus a thin wrapper if
needed) into `tools/signals/schedule.py` so the god file shrinks without
rewriting the method.

## Required

- Canonical helper lives in `tools/signals/schedule.py`.
- `generate_paper_trade_signal` calls it; keep fail-closed fallback
  (venue-local date, then UTC `[:10]` if helper returns None).
- Gate remains `reject_non_paper`. Frozenset stays
  `frozenset({"paper_trading"})` in `tools/signals/paper.py`.
- Tests: source pin that the nested def is gone from backtest.py and the
  new module owns the helper; paper gate tests still pass.

```
/tmp/callisto-pytest/bin/python -m pytest tests/test_backtest_extract_schedule.py tests/test_backtest_extract_paper.py tests/test_live_signal_hard_gate.py tests/test_fail_closed_registry.py -q
```

Commit: `refactor(backtest): extract paper-signal game-date helper to tools.signals`

Write `OX_DONE.md`.
