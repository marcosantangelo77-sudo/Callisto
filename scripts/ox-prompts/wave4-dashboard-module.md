# OX TASK: tools.dashboard module docstring is research, not ops

You are an Ox Alpha implementation worker on Callisto. Work ONLY in this worktree.
Branch: `cursor/ox-dashboard-module-2ac0`
Worktree: `/tmp/callisto-ox-dashboard-module`

## Exclusive files (HARD)

You MAY edit:
- `tools/dashboard.py` (docstring / comments / HTML-facing strings only)
- `tests/test_dashboard_module_face.py` (create)

Do NOT add executor enable. Do NOT change SQL. Do NOT widen live trading.
`web/dashboard/` is a different worker — do not edit it.

## Required

Replace "ops dashboard" language in the module docstring and any user-visible
string in this file with "research appliance" / "loop health". Keep the
`/api/hypotheses/live` route (the UI hides it unless `?trading=1`).

```
/tmp/callisto-pytest/bin/python -m pytest tests/test_dashboard_module_face.py tests/test_dashboard_research_face.py -q
```

Skip missing. Source pin: `ops dashboard` not in `tools/dashboard.py` (case-insensitive).

Commit: `fix(ui): dashboard backend copy is research appliance, not ops`

Write `OX_DONE.md`.
