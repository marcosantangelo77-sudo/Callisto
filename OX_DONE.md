# OX DONE — characterization pins for fail-closed and extracts

Branch: `cursor/ox-char-tests-2ac0`

## Created
- `tests/test_characterization_wave8.py` (~470 lines) — characterization/source-contract pins:
  paper-signal frozenset extract (`tools/signals/paper.py` = exactly `frozenset({"paper_trading"})`,
  AST-evaluated, no `live` status, single defining site repo-wide),
  `_phase_live_execute` `CALLISTO_ALLOW_LIVE_EXECUTE != "1"` gate ordering,
  admin-or-loopback auth on `/odds/edges`, `/tasks`, `/wiki/stats`,
  ungated `/health` + `/health/livez` + `/health/readyz` (gated `/health/detailed|deep`),
  `callisto.check_seal_key` fail-closed hex validation and `_cmd_ask` wiring,
  `MODEL_LADDER` defined on this worktree in root `inference.py` with reasoning fallback,
  `BetExecutor.enable()` CALLISTO_LOCAL_ONLY check-before-arm,
  dashboard LIVE panels (`panel-hyps`, `panel-orders`, `panel-portfolio`) hidden by default.
- `tests/test_fail_closed_wave8.py` (~300 lines) — fail-closed pins: hard-gate
  docstring, negative membership gate, no env override in the paper module,
  exact gate expression, auth matrix, health handler never raising 401/403,
  seal-key branch order, `_enabled = True` only inside enable(), dashboard containment.

## Constraints honored
- Static analysis only: file text + AST. No `import tools.autonomous`, no servers, no browsers.
- Only the two exclusive test files created; no production files touched.

## Verification
`/tmp/callisto-pytest/bin/python -m pytest tests/test_characterization_wave8.py tests/test_fail_closed_wave8.py tests/test_fail_closed_registry.py -q`
→ **107 passed** (91 new + 16 existing registry).
