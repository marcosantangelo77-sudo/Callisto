# OX TASK: live-execute phase must be explicitly armed

You are an Ox Alpha implementation worker on Callisto. Work ONLY in this worktree.
Branch: `cursor/ox-live-execute-gate-2ac0`
Worktree: `/tmp/callisto-ox-live-execute-gate`

## Exclusive files (HARD)

You MAY edit:
- `tools/autonomous.py`
- `tests/test_live_execute_gate.py` (create)

You MUST NOT edit `tools/backtest.py`, `api.py`, `tools/bet_executor.py`,
`tools/telegram_bot.py`, credentials, or `master`.

## Git rules (HARD)

Stay on this branch. No stash / reset --hard / checkout --. No merge.
Commit and `git push -u origin HEAD` when tests pass.

## Forbidden (HARD)

Do NOT change `generate_paper_trade_signal` to accept `status=="live"`.
Do NOT split the 8k-line file. Do NOT delete `_phase_live_execute`.

## Bug / loaded gun

`ResearchLoop._phase_live_execute` (`tools/autonomous.py` ~5821) still
collects signals for `status="live"` hypotheses and can submit via
order_manager / executor. Today it often no-ops because
`generate_paper_trade_signal` rejects live — fail-safe by accident.
The phase should refuse to run unless the operator explicitly arms it.

## Required change

At the top of `_phase_live_execute`, before drawdown/odds/order_manager:

```
if os.getenv("CALLISTO_ALLOW_LIVE_EXECUTE") != "1":
    logger.info("live_execute skipped (CALLISTO_ALLOW_LIVE_EXECUTE!=1)")
    return
```

Keep the rest of the function intact (including the accidental paper-only
signal producer). Docstring: default off; env `1` is the only arming switch
for this phase.

## Tests

`tests/test_live_execute_gate.py`:
- Stub a cheap ResearchLoop-like instance OR call the coroutine on a
  MagicMock/SimpleNamespace with `_phase_live_execute` unbound/partial.
  Easiest path: instantiate ResearchLoop with MagicMocks if import works
  (stub polars like `tests/test_tier1_loop_autonomous_gate_policy.py`).
- Default env: `_phase_live_execute` returns without calling
  `list_hypotheses` / `generate_paper_trade_signal` / `enable`.
- `CALLISTO_ALLOW_LIVE_EXECUTE=1`: it proceeds far enough to call existing
  executor.is_enabled (which can be False and return). Restore env.

Do NOT run the full suite.

```
/tmp/callisto-pytest/bin/python -m pytest tests/test_live_execute_gate.py -q
```

Commit: `fix(loop): live_execute phase requires CALLISTO_ALLOW_LIVE_EXECUTE=1`

Write `OX_DONE.md` with SHA and test output.
