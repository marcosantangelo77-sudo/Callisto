# OX TASK: gate ungated sensitive GETs

You are an Ox Alpha implementation worker on Callisto. Work ONLY in this worktree.
Branch: `cursor/ox-get-gating-2ac0`
Worktree: `/tmp/callisto-ox-get-gating`

## Exclusive files (HARD)

You MAY edit:
- `api.py`
- `tests/test_api_auth.py` (extend)
- `tests/test_sensitive_get_gating.py` (create)

You MUST NOT edit other files, credentials, `config/providers.yaml`, or `master`.

## Git rules (HARD)

Stay on this branch. No stash / reset --hard / checkout --. No merge.
Commit and `git push -u origin HEAD` when tests pass.

## Bug (verified)

These GETs dump money/hypothesis/executor state with NO `Depends(require_admin_or_loopback)`:

- `GET /bets` (`api.py` ~2012)
- `GET /bets/bankroll` (~2018)
- `GET /hypothesis` (~3155)
- `GET /hypothesis/{hypothesis_id}` (~3162)
- `GET /hypothesis/{hypothesis_id}/report` (~3171)
- `GET /hypothesis/{hypothesis_id}/significance` (~3177)
- `GET /system/full-status` (~3931)
- `GET /executor/status` (~4476)

Also gate if ungated: `/bets/clv-report`, `/bets/clv-forecast`.

`require_admin_or_loopback` already exists (~116): loopback OK when token unset;
non-loopback needs bearer when token set. Use THAT, not `require_admin` (hard
token would break local `callisto` / research loop on 127.0.0.1).

Do NOT change bind host. Do NOT loosen existing gates. Do NOT touch POST money
switches. Do NOT widen `generate_paper_trade_signal`.

## Required change

Add `dependencies=[Depends(require_admin_or_loopback)]` (or equivalent
`_auth: None = Depends(...)` param) to every listed GET.

## Tests

Follow `tests/test_api_auth.py`: do not enter FastAPI lifespan.

- Source contract: each gated path's decorator/signature in `api.py` text
  contains `require_admin_or_loopback` near the route.
- If you can call the dependency function in isolation with a fake Request
  (non-loopback, no token) it must 403/401; loopback with no token must pass
  when `CALLISTO_ADMIN_TOKEN` is unset.

```
/tmp/callisto-pytest/bin/python -m pytest tests/test_sensitive_get_gating.py tests/test_api_auth.py -q
```

Commit: `fix(api): gate sensitive GETs with admin-or-loopback`

Write `OX_DONE.md` with SHA and test output.
