# OX TASK: extend fail-closed registry with newly landed pins

You are an Ox Alpha implementation worker on Callisto. Work ONLY in this worktree.
Branch: `cursor/ox-registry-v2-2ac0`
Worktree: `/tmp/callisto-ox-registry-v2`

## Exclusive files (HARD)

You MAY edit only:
- `tests/test_fail_closed_registry.py`

No production source. Base: origin/master `70199b5`+ (file already exists).

## Add pins

1. `tools/kelly.py` defines `def kelly_core`
2. `tools/sizing.py` `kelly_binary` source contains `kelly_core`
3. `web/dashboard/index.html` `panel-hyps` section has `hidden`
4. `api.py` `@app.get("/odds/edges"` decorator includes `require_admin_or_loopback`
5. `tools/loop/phase_ledger.py` exists and mentions cap 50 or `maxlen`/50

Keep existing pins working. Do not import `tools.autonomous`.

```
/tmp/callisto-pytest/bin/python -m pytest tests/test_fail_closed_registry.py -q
```

Commit: `test: extend fail-closed registry (kelly_core, dashboard, edges GET)`

Write `OX_DONE.md`.
