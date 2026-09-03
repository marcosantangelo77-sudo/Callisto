# OX TASK: wiki + detailed-health GET gating (no livez)

You are an Ox Alpha implementation worker on Callisto. Work ONLY in this worktree.
Branch: `cursor/ox-wiki-health-get-2ac0`
Worktree: `/tmp/callisto-ox-wiki-health-get`

WAIT: `api.py` is owned by `ox-odds-get-batch3` if that worker is live.
If you cannot start because this prompt is launched together with batch3,
the orchestrator will only launch ONE of these. **This task is the
fallback if batch3 is not launched.** If you are running, you own api.py.

## Exclusive files (HARD)

- `api.py`
- `tests/test_sensitive_get_gating.py`

Do not gate `/health`, `/health/livez`, `/health/readyz`.
Do not downgrade `require_admin`. No money POST changes.

## Required

Gate with `require_admin_or_loopback`:

- `/wiki/stats` `/wiki/articles` `/wiki/article/{topic}` `/wiki/search` `/wiki/contradictions`
- `/health/detailed` `/health/deep` `/health/integrity/history`
- `/tasks` (list — not to be confused with POST /task)

Skip already-gated. Extend tests.

```
/tmp/callisto-pytest/bin/python -m pytest tests/test_sensitive_get_gating.py -q
```

Commit: `fix(api): gate wiki, detailed health, and /tasks GETs`

Write `OX_DONE.md`.
