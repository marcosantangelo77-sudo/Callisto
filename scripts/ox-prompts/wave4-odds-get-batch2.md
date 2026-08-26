# OX TASK: gate remaining /odds dump GETs (batch 2)

You are an Ox Alpha implementation worker on Callisto. Work ONLY in this worktree.
Branch: `cursor/ox-odds-get-batch2-2ac0`
Worktree: `/tmp/callisto-ox-odds-get-batch2`

## Exclusive files (HARD)

You MAY edit:
- `api.py`
- `tests/test_sensitive_get_gating.py`

Do not touch event-loop `to_thread`, POST money switches, bind host, or
`generate_paper_trade_signal`. Do not downgrade `require_admin` routes.

## Git rules

No stash / reset --hard / full pytest. Push. Base is current origin/master
(`70199b5`+), which already gates `/odds/edges`, `/odds/opportunities`,
`/edges/live`.

## Required

Add `dependencies=[Depends(require_admin_or_loopback)]` to these GETs if
they are still ungated:

- `/odds/movements`
- `/odds/snapshots/{sport}`
- `/odds/status`
- `/odds/narrative-edges`
- `/odds/kl-metrics`

Skip any that already have require_admin(_or_loopback) on decorator or
`_auth` param. Extend `SENSITIVE_GETS` (or add a second list) for newly
gated decorator paths.

```
/tmp/callisto-pytest/bin/python -m pytest tests/test_sensitive_get_gating.py -q
```

Commit: `fix(api): gate remaining odds dump GETs (batch 2)`

Write `OX_DONE.md` listing exact paths changed.
