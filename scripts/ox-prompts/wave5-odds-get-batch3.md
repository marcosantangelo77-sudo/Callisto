# OX TASK: gate remaining odds/analysis dump GETs (batch 3)

You are an Ox Alpha implementation worker on Callisto. Work ONLY in this worktree.
Branch: `cursor/ox-odds-get-batch3-2ac0`
Worktree: `/tmp/callisto-ox-odds-get-batch3`

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

`dependencies=[Depends(require_admin_or_loopback)]` on any of these still
ungated (skip if already gated via decorator or `_auth` param):

- `/odds/sgp-analysis/{sport}`
- `/odds/props/{sport}/{event_id}`
- `/odds/dk-props/{sport}`
- `/odds/learned-correlations`
- `/odds/market-analysis/{sport}`
- `/odds/stale-lines/{sport}`
- `/odds/psychology/{sport}`
- `/odds/psychology`
- `/odds/dead-numbers/{sport}`
- `/odds/line-analysis/{sport}`
- `/odds/line-gaps/{sport}`
- `/odds/prop-gaps/{sport}`
- `/analysis/futures-efficiency`
- `/analysis/half-market/{sport}`
- `/analysis/cross-tabulate/{sport}`
- `/wiki/stats` `/wiki/articles` `/wiki/article/{topic}` `/wiki/search` `/wiki/contradictions`
- `/health/detailed` `/health/deep` `/health/integrity/history`
- `/tasks` (list GET only)

Extend `SENSITIVE_GETS` for newly gated decorator paths.

```
/tmp/callisto-pytest/bin/python -m pytest tests/test_sensitive_get_gating.py -q
```

Commit: `fix(api): gate remaining odds/analysis dump GETs (batch 3)`

Write `OX_DONE.md` with the exact paths changed.
