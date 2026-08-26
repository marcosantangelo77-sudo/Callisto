# OX TASK: fail-closed regression registry (tests only)

You are an Ox Alpha implementation worker on Callisto. Work ONLY in this worktree.
Branch: `cursor/ox-fail-closed-registry-2ac0`
Worktree: `/tmp/callisto-ox-fail-closed-registry`

## Exclusive files (HARD)

You MAY edit:
- `tests/test_fail_closed_registry.py` (create)

You MUST NOT edit any production source. No `api.py`, no `tools/*`, no
`callisto.py`, no credentials, no `master`.

## Git rules (HARD)

No stash / reset --hard / full `pytest tests/`. Push this branch.
This worktree is origin/master @ the fail-closed series (590d10b or later).

## Goal

One test module that will FAIL if someone reverts the Stage A invariants
landed on master. Read source as text (and AST where easy). Do not import
`tools.autonomous` (it hangs). Do not start browsers.

## Pins (all of these)

1. `start.bat` and `scripts/overnight_setup.py` contain no uvicorn `0.0.0.0`.
   `scripts/start-callisto.ps1` and `scripts/watchdog.ps1` likewise.
2. `tools/backtest.py`: `_PAPER_TRADE_SIGNAL_STATUSES == frozenset({"paper_trading"})`
   or the assignment literal is exactly that — no `"live"` in the set.
3. `tools/autonomous.py` text: `_phase_live_execute` contains
   `CALLISTO_ALLOW_LIVE_EXECUTE` and `!= "1"` before `list_hypotheses`.
4. `tools/bet_executor.py` `enable` contains `CALLISTO_LOCAL_ONLY` and does
   not set `_enabled = True` before that check.
5. `tools/order_manager.py` `__init__` assigns `_enabled = False` (the
   assignment before `def enable`).
6. `agp/__init__.py` `verify_seal`: unkeyed SHA-256 candidate is only added
   when no key is configured (look for `_seal_keys()` / `_seal_key_configured`
   guard — do not require a specific helper name, but a public SHA-256
   hexdigest must not be an unconditional candidate).
7. `api.py`: `@app.get("/bets"` and `@app.get("/system/full-status"`
   decorator text includes `require_admin_or_loopback`.

```
/tmp/callisto-pytest/bin/python -m pytest tests/test_fail_closed_registry.py -q
```

Commit: `test: pin fail-closed Stage A invariants against regression`

Write `OX_DONE.md` with SHA and test output.
