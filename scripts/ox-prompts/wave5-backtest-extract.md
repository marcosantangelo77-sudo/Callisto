# OX TASK: extract paper-signal gate from backtest.py (first slice)

You are an Ox Alpha implementation worker on Callisto. Work ONLY in this worktree.
Branch: `cursor/ox-backtest-extract-2ac0`
Worktree: `/tmp/callisto-ox-backtest-extract`

## Exclusive files (HARD)

You MAY edit:
- `tools/backtest.py`
- `tools/signals/__init__.py` (create)
- `tools/signals/paper.py` (create)
- `tests/test_live_signal_hard_gate.py` (adapt imports if needed)
- `tests/test_backtest_extract_paper.py` (create)

Do NOT add `"live"` to allowed statuses. Do NOT change signal math.
Do NOT edit `tools/autonomous.py` or `api.py`.

## Git rules

No stash / reset --hard / full suite. Push.

## Goal

`tools/backtest.py` is ~4k lines (god module). First slice only:

Move `_PAPER_TRADE_SIGNAL_STATUSES` and `BacktestEngine.generate_paper_trade_signal`
to `tools/signals/paper.py` as functions/constants. `BacktestEngine.generate_paper_trade_signal`
becomes a one-line/thin delegate so existing callers keep working.

If the method uses many `self.` helpers, keep it on the class and **only**
move the frozenset + the gate check into `tools/signals/paper.py`
(`allowed_paper_statuses()`, `reject_non_paper(status) -> bool`). That is
an acceptable smaller slice. Do not copy 400 lines blindly if that
creates a circular import.

Characterization: existing `tests/test_live_signal_hard_gate.py` must still
pass (live status → `[]`). Add a pin that the frozenset lives in
`tools.signals.paper`.

```
/tmp/callisto-pytest/bin/python -m pytest tests/test_live_signal_hard_gate.py tests/test_backtest_extract_paper.py -q
```

Commit: `refactor(backtest): extract paper-signal status gate to tools.signals`

Write `OX_DONE.md`.
