# OX TASK: tools.edge inline Kelly delegates to tools.kelly

You are an Ox Alpha implementation worker on Callisto. Work ONLY in this worktree.
Branch: `cursor/ox-edge-kelly-2ac0`
Worktree: `/tmp/callisto-ox-edge-kelly`

## Exclusive files (HARD)

You MAY edit:
- `tools/edge.py`
- `tests/test_edge_kelly_canonical.py` (create)

You MUST NOT edit `tools/sizing.py`, `tools/kelly.py`, `tools/bet_executor.py`,
`tools/autonomous.py`, `api.py`, credentials, or `master`.

## Git rules (HARD)

No stash / reset --hard / full `pytest tests/`. Push this branch.

## Bug (verified)

`tools/edge.py` already imports `kelly_full` from `tools.kelly` (~line 41)
then **ignores it** and recomputes:

```
kelly_full_frac = max(0.0, (b * calibrated_prob - q) / b)
```

Third copy of the same formula. Dual-Kelly cleanup is a different worker
on `sizing.py`; you only touch `edge.py`.

## Required change

1. Characterization tests FIRST: freeze `assess_edge` (or the inner Kelly
   numbers) on 4–5 fixtures (american prices + calibrated_prob). Commit
   the test file with the refactor in the same commit only if numbers match
   ±1e-6.
2. Replace the inline f* with `kelly_full(edge, american_odds)` using the
   same edge the assessment already computed (`calibrated_prob - market_fair`)
   and the quote's American price. Keep `MAX_FRACTION_FULL_KELLY` cap.
3. If `kelly_full`'s `round(..., 6)` would change a fixture, STOP and
   report in OX_DONE — do not loosen the cap or the tests.

Do not change `actionable` rules except via the delegated number.

```
/tmp/callisto-pytest/bin/python -m pytest tests/test_edge_kelly_canonical.py -q
```

If `tests/test_edge.py` exists, run it too (focused).

Commit: `refactor(kelly): edge.assess_edge uses canonical kelly_full`

Write `OX_DONE.md` with SHA, fixture table, test output.
