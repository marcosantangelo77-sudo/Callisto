# OX TASK: pin generate_paper_trade_signal to paper_trading only

You are an Ox Alpha implementation worker on Callisto. Work ONLY in this worktree.
Branch: `cursor/ox-live-signal-hard-gate-2ac0`
Worktree: `/tmp/callisto-ox-live-signal-hard-gate`

## Exclusive files (HARD)

You MAY edit:
- `tools/backtest.py`
- `tests/test_live_signal_hard_gate.py` (create)
- `tests/test_tier7_deepresearch.py` (only if you must keep an existing pin in sync)

You MUST NOT edit `tools/autonomous.py`, `api.py`, credentials, or `master`.

## Git rules (HARD)

Stay on this branch. No stash / reset --hard / checkout --. No merge.
Commit and `git push -u origin HEAD` when tests pass.

## Loaded gun (ROADMAP / audit)

`BacktestEngine.generate_paper_trade_signal` (`tools/backtest.py` ~3799)
returns `[]` unless `h["status"] == "paper_trading"`. That is the ONLY
producer feeding live `submit_order`. **Do not "fix" it to accept `live`.**
That would arm untested sizing/caps/kill-switch at once.

## Required change

1. Keep the `status != "paper_trading"` → `[]` check. Make it impossible
   to miss: compare with a frozenset / constant
   `_PAPER_TRADE_SIGNAL_STATUSES = frozenset({"paper_trading"})`
   and return [] if status not in that set. Do NOT add `"live"`.
2. Docstring: explicit warning that accepting `live` is forbidden.
3. Characterization test that FAILS if anyone adds `"live"` to the allowed
   set OR changes the check to `status in ("paper_trading", "live")`.

Read `tests/test_tier7_deepresearch.py` (~89) — there is already a source
pin. Your new test must be stronger: execute the method (stub
hypothesis_manager.get_hypothesis to return status=`live` and
status=`paper_trading`) and assert live → `[]` even when live_odds is full
of games. Paper path may return [] too if odds don't match; the live path
must return [] BEFORE odds processing (spy/mock so a live hyp never
reaches game loop).

```
/tmp/callisto-pytest/bin/python -m pytest tests/test_live_signal_hard_gate.py tests/test_tier7_deepresearch.py -q
```

If the full tier7 file is slow/hangs, run only `tests/test_live_signal_hard_gate.py`
plus the single existing pin test by node id.

Commit: `fix(backtest): pin paper-only generate_paper_trade_signal hard gate`

Write `OX_DONE.md` with SHA and test output.
