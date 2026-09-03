# OX TASK: split api.py wiki+analysis handlers into tools.api (LONG)

You are an Ox Alpha implementation worker on Callisto. Work ONLY in this worktree.
Branch: `cursor/ox-api-split-2ac0`
Worktree: `/tmp/callisto-ox-api-split`

This is a LONG extract. Do not stop after moving one function. Keep going until
the wiki endpoints AND the /analysis/* endpoints live in `tools/api/` with
thin wrappers left in `api.py`. If that is done and tests pass, also move
`/odds/psychology*` + `/odds/dead-numbers*` + `/odds/line-analysis*` bodies
the same way. Use the whole session.

## Exclusive files (HARD)

You MAY edit:
- `api.py`
- `tools/api/` (create package)
- `tests/test_sensitive_get_gating.py` (adapt imports/decorators only)
- `tests/test_api_split_wiki.py` (create — source pins)

Do NOT change auth semantics. Do NOT drop `require_admin_or_loopback`.
Do NOT gate `/health`, `/health/livez`, `/health/readyz`.
Do NOT arm live betting. Do NOT touch `tools/autonomous.py`.

## Git rules

No stash / reset --hard / full `pytest tests/`. Push this branch. Do not merge master.

## Required

- `api.py` keeps the FastAPI `@app.get` decorators and `Depends(...)`.
- Handler bodies move to `tools/api/wiki.py` and `tools/api/analysis.py`
  (and `tools/api/odds_extra.py` if you get that far).
- Public function names can stay; api.py calls them.
- Tests: decorator still has `require_admin_or_loopback` next to those routes;
  new module files contain the moved logic (grep a unique docstring/string
  that used to live in api.py).

```
/tmp/callisto-pytest/bin/python -m pytest tests/test_sensitive_get_gating.py tests/test_api_split_wiki.py -q
```

Commit: `refactor(api): extract wiki/analysis handlers to tools.api`

Write `OX_DONE.md` listing every route moved.
