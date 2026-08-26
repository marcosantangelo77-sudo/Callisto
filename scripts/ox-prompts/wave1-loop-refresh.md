# OX TASK: loop refresh kill-switch + phase-failure recording

You are an Ox Alpha implementation worker on Callisto. Work ONLY in this worktree.
Branch: `cursor/ox-loop-refresh-2ac0`
Worktree: `/tmp/callisto-ox-loop-refresh`

## Exclusive file ownership (HARD)

You MAY edit:
- `tools/autonomous.py`
- `tests/test_loop_phase_errors.py` (create)
- `tests/test_loop_signal_refresh_gate.py` (create)

You MUST NOT edit any other file. Especially not `tools/hypothesis.py`, `api.py`,
`tools/backtest.py`, `config/providers.yaml`, credentials, other worktrees, or `master`.

## Git rules (HARD)

- Stay on `cursor/ox-loop-refresh-2ac0`. Never checkout another branch.
- Do not `git stash`, `git reset --hard`, or `git checkout --`.
- Do not merge. Do not touch `master`.
- After tests pass, commit and `git push -u origin HEAD`.
- Never put secrets in commits.

## Forbidden product changes (HARD)

- Do NOT change `generate_paper_trade_signal` to accept `status == "live"`.
- Do NOT split `tools/autonomous.py` (8148 lines). No god-file refactor this turn.
- Do NOT enable live betting, Telegram arming, or bind-host changes.

## Goal 1 — stop silent evidence rewriting

`ResearchLoop._phase_refresh_signals` (`tools/autonomous.py` ~3193–3249) runs every
cycle and `UPDATE`s `backtest_events.signal_generated = 1` when `edge >= edge_threshold`.
That launders history: a later threshold drop retroactively creates signals.

Change:

1. Default: `_phase_refresh_signals` MUST NOT write `signal_generated` or
   `backtest_runs.signals_generated`. Diagnose-only: log how many rows *would*
   have been upgraded (SELECT COUNT, no UPDATE). Return without writing.
2. Escape hatch only if `os.getenv("CALLISTO_ALLOW_SIGNAL_REFRESH") == "1"`.
   When that env is set, keep the existing UPDATE path (operator-explicit).
3. Update the docstring to say the write path is gated and off by default.

Do not delete the function. Quarantine the write, do not invent a rewrite.

## Goal 2 — phase failures must be recorded

`ResearchLoop._loop` (~2445+) wraps every `_phase_*` in `except Exception` and
continues. Logging a warning is not enough; the loop then looks healthy.

Add:

1. `self._phase_failures: list[dict]` in `__init__`, cap at 50 (drop oldest).
2. Helper `_record_phase_failure(self, phase: str, kind: str, exc: BaseException | None = None) -> None`
   recording `{"cycle", "phase", "kind" ("exception"|"timeout"), "error", "ts"}`.
   `error` is `repr(exc)[:300]` or `"timeout"`.
3. Call it from EVERY `_loop` handler that currently does
   `except Exception` / `except asyncio.TimeoutError` around a `_phase_*`.
   Keep the existing log lines. Keep `asyncio.CancelledError` as break/reraise
   where it already is. Do not change timeouts.
4. Include `"phase_failures": list(self._phase_failures)[-10:]` and
   `"phase_failure_count": len(self._phase_failures)` in `ResearchLoop.get_status`
   (~8088).

Do not make a phase failure stop the whole loop. Record, then continue.

## Tests (required)

Create focused tests. Follow `tests/test_tier1_loop_autonomous_gate_policy.py`:
in-memory sqlite, stub polars if needed, no network, no live API.

`tests/test_loop_signal_refresh_gate.py`:
- Build a tiny ResearchLoop-like driver OR call `_phase_refresh_signals` on a
  stub instance with `backtest_engine.db_path` pointing at a temp sqlite that
  has `hypotheses` + `backtest_events` (+ `backtest_runs` if the UPDATE path
  needs it).
- Seed one event with `signal_generated=0`, `edge` above `edge_threshold`.
- Default env: after the phase, `signal_generated` is still 0.
- With `CALLISTO_ALLOW_SIGNAL_REFRESH=1`: the existing upgrade may run (assert 1).
- Use `monkeypatch` / `os.environ` and always restore env.

`tests/test_loop_phase_errors.py`:
- Construct a `ResearchLoop` with the cheapest stubs you can (`hypothesis_manager`,
  `backtest_engine`, etc. as SimpleNamespace / MagicMock) OR extract/call
  `_record_phase_failure` on a partially constructed instance.
- `_record_phase_failure` appends; 51st entry drops the oldest.
- `get_status()` includes the last failures if you can construct enough of
  ResearchLoop; if `get_status` pulls heavy deps, test the list on the instance
  directly and still assert the key names exist in the `get_status` source.

Do NOT run `pytest tests/` (full suite). Run only:

```
/tmp/callisto-pytest/bin/python -m pytest tests/test_loop_signal_refresh_gate.py tests/test_loop_phase_errors.py tests/test_tier1_loop_autonomous_gate_policy.py -q
```

If that venv is missing, `uv venv /tmp/ox-pytest && /tmp/ox-pytest/bin/pip install pytest pytest-asyncio aiosqlite`.

If `test_tier1_loop_autonomous_gate_policy.py` fails for reasons you did not cause, do not "fix" it by rewriting interpret-backtests. Stop and commit your gated refresh + recording work.

## Done

Commit message:
`fix(loop): gate signal refresh writes and record phase failures`

Push: `git push -u origin HEAD`

Write `OX_DONE.md` in the worktree root with: files changed, test command + result, commit SHA.
