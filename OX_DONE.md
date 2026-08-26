# OX_DONE — Stage B fail-closed invariant registry (wave 6)

- Created `tests/test_fail_closed_wave6.py` (8 source/AST pins, no imports
  of tools.autonomous, no servers):
  1. `callisto.py` has `check_seal_key`; `_cmd_ask` gates on it before
     `_load_router`/research.
  2. `tools/signals/paper.py` pins `_PAPER_TRADE_SIGNAL_STATUSES =
     frozenset({"paper_trading"})` with no `"live"` in the literal.
  3. `api.py` `/tasks` and `/wiki/stats` chunks include
     `require_admin_or_loopback`; `/health`, `/health/livez`,
     `/health/readyz` do not.
  4. `agp/preregistration.py` `verify_seal` returns False on exception
     (fail-closed, never raises).
  5. `tools/autonomous.py` last `get_status` contains `"last_cycle_ok"`
     and `"last_cycle_phase_failures"`.
  6. `findings/hermes_latency_2026-08-26.md` exists and mentions p50.

Verification: `/tmp/callisto-pytest/bin/python -m pytest tests/test_fail_closed_wave6.py tests/test_fail_closed_registry.py -q`
→ 24 passed.

One fix during development: two `get_status` defs exist in autonomous.py;
pin uses the last one (`rindex`) which carries the cycle-health fields.
