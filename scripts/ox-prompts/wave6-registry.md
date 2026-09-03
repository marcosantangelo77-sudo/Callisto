# OX TASK: pin Stage B invariants in a new registry file

You are an Ox Alpha implementation worker on Callisto. Work ONLY in this worktree.
Branch: `cursor/ox-registry-w6-2ac0`
Worktree: `/tmp/callisto-ox-registry-w6`

## Exclusive files (HARD)

You MAY edit:
- `tests/test_fail_closed_wave6.py` (create)

Do NOT edit `api.py`, `callisto.py`, `tools/autonomous.py`, dashboard HTML,
or `tests/test_fail_closed_registry.py`. Source/AST pins only. Do NOT
`import tools.autonomous` (it can hang). Do NOT start servers.

## Git rules

No stash / reset --hard / full `pytest tests/`. Push. Base origin/master.

## Required

New file `tests/test_fail_closed_wave6.py` pinning (read files as text):

1. `callisto.py`: `def check_seal_key` exists; `_cmd_ask` calls it before
   `_load_router` / research.
2. `tools/signals/paper.py`:
   `_PAPER_TRADE_SIGNAL_STATUSES = frozenset({"paper_trading"})` and `"live"`
   is not in that literal.
3. `api.py`: `@app.get("/tasks"` and `@app.get("/wiki/stats"` chunks include
   `require_admin_or_loopback`. `@app.get("/health")`, `/health/livez`,
   `/health/readyz` decorator lines do **not**.
4. `agp/preregistration.py` `verify_seal`: on exception returns False
   (fail-closed, no raise to caller).
5. `tools/autonomous.py` `get_status` source contains `"last_cycle_ok"` and
   `"last_cycle_phase_failures"`.
6. `findings/hermes_latency_2026-08-26.md` exists and mentions p50.

```
/tmp/callisto-pytest/bin/python -m pytest tests/test_fail_closed_wave6.py tests/test_fail_closed_registry.py -q
```

Commit: `test: pin Stage B fail-closed invariants (ask/paper/GET/prereg/cycle)`

Write `OX_DONE.md`.
