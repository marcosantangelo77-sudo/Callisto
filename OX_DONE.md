# OX_DONE — compute_stake imports canonical Kelly only

- Verified `BetExecutor.compute_stake` already imports `kelly_dynamic`/`kelly_fractional` from `tools.kelly` and only push-aware helpers (`kelly_with_push`, `uncertainty_adjusted_kelly`) from `tools.sizing` — the only canonical implementation. No numeric fixture changed.
- Added one-line comment in `tools/bet_executor.py`: canonical Kelly module is `tools.kelly`.
- Created source-contract test `tests/test_compute_stake_kelly_imports.py` (inspect.getsource asserts no kelly_full/etc. from sizing; mentions tools.kelly).
- Test run: `/tmp/callisto-pytest/bin/python -m pytest tests/test_compute_stake_kelly_imports.py -q` → 3 passed.
