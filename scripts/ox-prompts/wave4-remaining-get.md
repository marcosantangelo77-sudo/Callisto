# OX TASK: gate remaining research/odds dump GETs

You are an Ox Alpha implementation worker on Callisto. Work ONLY in this worktree.
Branch: `cursor/ox-remaining-get-2ac0`
Worktree: `/tmp/callisto-ox-remaining-get`

## Exclusive files (HARD)

You MAY edit:
- `api.py`
- `tests/test_sensitive_get_gating.py` (extend the existing list)

You MUST NOT edit bind host, POST money switches, `generate_paper_trade_signal`,
event-loop `to_thread` offloads, credentials, or `master`.
Do NOT switch existing `require_admin` routes down to loopback-or-admin.

## Git rules (HARD)

No stash / reset --hard / full `pytest tests/`. Push this branch.
This worktree is based on current origin/master which already gates
`/bets`, `/hypothesis*`, `/system/full-status`, `/executor/status`.

## Bug (verified on master @ 645c8a8)

Still ungated (no `require_admin` in the `@app.get` decorator or signature):

- `/odds/edges`
- `/edges/live`
- `/session/{session_id}`  (if not already gated via `_auth` param — verify;
  if `_auth: Depends(require_admin_or_loopback)` is already on the function,
  skip it)
- `/world/{domain}` (same check)
- `/debug/memory`
- `/debug/memory/top-traces`

Do **not** try to gate every `/odds/*` scan in this PR. Those six (or fewer
if already param-gated) are the leak/debug set. You MAY also gate
`/odds/opportunities` if it is a one-line match to `/odds/edges`.

## Required change

Add `dependencies=[Depends(require_admin_or_loopback)]` or
`_auth: None = Depends(require_admin_or_loopback)` consistently with
neighboring routes. Loopback callers without a token must still work.

Extend `SENSITIVE_GETS` in `tests/test_sensitive_get_gating.py` for every
path you newly gate via the decorator. If you use a signature `_auth`
param instead, add a source pin that the function def contains
`require_admin_or_loopback`.

```
/tmp/callisto-pytest/bin/python -m pytest tests/test_sensitive_get_gating.py -q
```

Commit: `fix(api): gate remaining edges/session/world/debug GETs`

Write `OX_DONE.md` with SHA, the exact paths gated, and test output.
