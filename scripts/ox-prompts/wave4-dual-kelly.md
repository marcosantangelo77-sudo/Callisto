# OX TASK: one Kelly — sizing.py delegates, numbers do not change

You are an Ox Alpha implementation worker on Callisto. Work ONLY in this worktree.
Branch: `cursor/ox-dual-kelly-2ac0`
Worktree: `/tmp/callisto-ox-dual-kelly`

## Exclusive files (HARD)

You MAY edit:
- `tools/sizing.py`
- `tools/kelly.py` (thin adapter / shared primitive only; do not rewrite portfolio Kelly)
- `tests/test_kelly_canonical.py` (create)

You MUST NOT edit `tools/bet_executor.py`, `tools/edge.py`, `tools/autonomous.py`,
`api.py`, `tools/telegram_bot.py`, credentials, or `master`.

## Git rules (HARD)

Stay on this branch. No stash, no reset --hard, no `pytest tests/` (full suite).
Commit and `git push -u origin HEAD` when focused tests pass.

## Bug (verified)

Two Kelly implementations:

- `tools/kelly.py:kelly_full(edge, american)` — canonical per `sizing.py` module docstring
- `tools/sizing.py:kelly_binary(fair_prob, decimal)` — same f*=(bp-q)/b, different units

`kelly_full` currently `round(..., 6)`. `kelly_binary` does not round.
Characterization tests first: if the wrapper would change fixtures, STOP
and leave a note — do not loosen tests to hide a numeric change.

## Required change (incremental)

1. Keep `kelly_binary` as a **wrapper**. Convert decimal odds → American
   (or fair_prob → edge vs implied) and call `kelly_full` / a shared
   unrounded primitive so there is one formula.
2. `kelly_with_push` may stay in sizing.py (no push-aware equivalent in
   `tools.kelly`). Do not invent portfolio math.
3. Do not change default stake sizes for the no-push path beyond wrapper
   rounding of ±1e-6. If a fixture would change by more than that, STOP.

```
/tmp/callisto-pytest/bin/python -m pytest tests/test_kelly_canonical.py -q
```

If `tests/test_kelly.py` or similar exists, run it too (focused).

Commit: `refactor(kelly): sizing.kelly_binary delegates to canonical kelly_full`

Write `OX_DONE.md` with SHA, fixture table (before/after), and test output.
