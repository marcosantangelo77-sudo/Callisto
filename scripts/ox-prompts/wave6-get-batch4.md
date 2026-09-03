# OX TASK: gate remaining dump GETs (batch 4)

You are an Ox Alpha implementation worker on Callisto. Work ONLY in this worktree.
Branch: `cursor/ox-get-batch4-2ac0`
Worktree: `/tmp/callisto-ox-get-batch4`

## Exclusive files (HARD)

You MAY edit:
- `api.py`
- `tests/test_sensitive_get_gating.py`

Do not touch event-loop `to_thread`, POST money switches, bind host,
`generate_paper_trade_signal`, or `require_admin` (do not downgrade).
Do not gate `/health`, `/health/livez`, `/health/readyz`.

## Git rules

No stash / reset --hard / full `pytest tests/`. Push. Base origin/master.

## Required

Add `dependencies=[Depends(require_admin_or_loopback)]` on any of these
still ungated (skip if already gated via decorator **or** `_auth` param):

- `/model/total/{sport}`
- `/model/environment`
- `/model/injury-impact/{sport}`
- `/data/injuries/{sport}`
- `/data/scoreboard/{sport}`
- `/data/weather`
- `/data/referee`
- `/data/stats`
- `/backtest/run/{run_id}`
- `/historical/cache`
- `/research/status`
- `/research/sports`
- `/embeddings/stats`
- `/claude/status`
- `/debug/memory`
- `/debug/memory/top-traces`

Optional consistency: `/edges/live` already has `_auth`; you may also add
the decorator. Do not remove the param.

Extend `SENSITIVE_GETS` for newly gated decorator paths. Keep `/health*`
liveness public.

```
/tmp/callisto-pytest/bin/python -m pytest tests/test_sensitive_get_gating.py tests/test_api_auth.py -q
```

Skip missing. Commit: `fix(api): gate remaining model/data/debug dump GETs (batch 4)`

Write `OX_DONE.md` with the exact paths changed.
