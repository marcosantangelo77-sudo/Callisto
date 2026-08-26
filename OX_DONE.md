# OX_DONE — one Kelly: sizing.kelly_binary delegates to canonical kelly_full

Commit: `b70bc85` — `refactor(kelly): sizing.kelly_binary delegates to canonical kelly_full`
Branch: `cursor/ox-dual-kelly-2ac0` (pushed to origin)

## What changed

- `tools/kelly.py`: added `kelly_core(p, b)` — unrounded canonical primitive
  `f* = (b*p - q)/b`, guard `b <= 0 -> 0.0`, clamp `max(0, ...)`.
  `kelly_full(edge, american)` now computes `p` from edge+implied and delegates
  to `kelly_core`, keeping its existing `round(..., 6)` behavior.
- `tools/sizing.py`: `kelly_binary(fair_prob, decimal_odds)` is now a thin
  wrapper: `kelly_core(fair_prob, decimal_odds - 1)`. Unrounded (as before).
  No unit conversion needed — the shared primitive takes `(p, b)` directly,
  so both American-unit (`kelly_full`) and decimal-unit (`kelly_binary`)
  paths use the identical formula.
- `kelly_with_push` left untouched in sizing.py (no push-aware equivalent in
  tools.kelly; no portfolio math invented).
- New characterization tests: `tests/test_kelly_canonical.py` (written BEFORE
  the refactor; pinned pre-refactor values, including exact float
  `0.14090909090909083` and `0.16000000000000003`). No fixture loosening.

## Fixture table (before / after)

| Call | Before | After | Delta |
|---|---|---|---|
| kelly_binary(0.55, 2.10) | 0.14090909090909083 | 0.140909090909091 | +1.9e-16 |
| kelly_binary(0.60, 1.90909…) | 0.16000000000000003 | 0.16000000000000003 | 0 |
| kelly_binary(1/2.10, 2.10) | 0.0 | 0.0 | 0 |
| kelly_binary(0.40, 2.10) | 0.0 | 0.0 | 0 |
| kelly_binary(0.55, 1.0) | 0.0 | 0.0 | 0 |

All deltas ≤ float epsilon (~2e-16), far inside the ±1e-6 wrapper tolerance.
Cross-check vs `kelly_full`: equivalence cases at ±110/+150/−200/+275/−105/+100
all agree within 1e-6.

## Test output

```
$ /tmp/callisto-pytest/bin/python -m pytest tests/test_kelly_canonical.py tests/test_tier0_money_sizing_and_units.py -q
43 passed in 0.12s
```

Focused consumer sweep (backtest/bankroll/kelly-related):

```
$ /tmp/callisto-pytest/bin/python -m pytest tests/test_kelly_canonical.py \
    tests/test_tier0_money_sizing_and_units.py tests/test_backtest_e2e.py \
    tests/test_bankroll_sim.py tests/test_bankroll_race.py ... -q
144 passed, 1 warning in 5.42s   (warning is pre-existing scipy RuntimeWarning in backtest_e2e)
```

No full-suite run per git rules.
