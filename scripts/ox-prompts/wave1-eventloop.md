# OX TASK: offload blocking work off the FastAPI event loop

You are an Ox Alpha implementation worker on Callisto. Work ONLY in this worktree.
Branch: `cursor/ox-eventloop-2ac0`
Worktree: `/tmp/callisto-ox-eventloop`

## Exclusive file ownership (HARD)

You MAY edit:
- `api.py`
- `tests/test_api_eventloop_offload.py` (create)

You MUST NOT edit any other file. Especially not `tools/autonomous.py`,
`tools/hypothesis.py`, `tools/bankroll_sim.py`, `tools/market_regime.py`,
`config/providers.yaml`, credentials, other worktrees, or `master`.

## Git rules (HARD)

- Stay on `cursor/ox-eventloop-2ac0`. Never checkout another branch.
- Do not `git stash`, `git reset --hard`, or `git checkout --`.
- Do not merge. Do not touch `master`.
- After tests pass, commit and `git push -u origin HEAD`.
- Never put secrets in commits.

## Forbidden product changes (HARD)

- Do NOT change bind host (that is a different worker).
- Do NOT loosen auth.
- Do NOT change `generate_paper_trade_signal` to accept live status.
- Do NOT add `asyncio.to_thread` around unrelated endpoints "while you are here".

## Bugs (verified)

1. `simulate_portfolio_endpoint` (`api.py` ~2480–2542) calls
   `tools.bankroll_sim.simulate_portfolio` synchronously on the event loop.
   Caps are 5000 paths × 365 days. Also opens sqlite with `sqlite3.connect`
   when `all_live=1`. There is **no** `asyncio.to_thread` anywhere in `api.py`.

2. `/health` (`api.py` ~3712) calls `system_health.write_health_file()` on
   every poll (sync disk JSON). Watchdogs poll this.

3. `/health/detailed` and `/regime/sizer-multipliers` call `detect_regime(sp)`
   in a loop (sync sqlite) on the event loop (`api.py` ~3794–3852).

4. `_PORTFOLIO_SIM_CACHE` is unbounded except for TTL (`api.py` ~2476).

## Required changes (api.py only)

1. Offload `simulate_portfolio(...)` via `await asyncio.to_thread(...)`.
   Offload the `all_live` sqlite read the same way (small helper function
   defined next to the endpoint is fine). Keep caps (10–5000 sims, 1–365 days).
   Keep the cache key/TTL behavior.

2. Bound the cache. Use an OrderedDict or explicit eviction: max 32 entries,
   evict oldest (or least-recently-used) when inserting the 33rd. TTL still
   applies. Do not grow without limit.

3. Debounce `write_health_file` on `/health`: skip if last successful write
   was < 10 seconds ago. Offload the write with `asyncio.to_thread` when it
   does run. Use a module-level timestamp. Never fail `/health` if the write
   fails (already true — keep that).

4. In `/health/detailed` and `/regime/sizer-multipliers`, call `detect_regime`
   (and any other sync sqlite helpers in those loops) via `asyncio.to_thread`.
   Keep the per-sport try/except so one sport failing does not fail the endpoint.

Do not change response JSON shape except:
- cache bound does not add required fields
- you MAY add `"health_file_written": bool` only if you must; prefer no shape change

## Tests (required)

Create `tests/test_api_eventloop_offload.py`.

Importing `api.py` executes module-level FastAPI setup. Follow
`tests/test_api_auth.py`: set `CALLISTO_BIND_HOST=127.0.0.1` first.

Preferred tests (do not start the full lifespan if TestClient triggers it
and hangs — inspect + unit-test helpers instead):

A. Source/contract tests:
- `api.py` text contains `asyncio.to_thread` near `simulate_portfolio` and
  `detect_regime` and `write_health_file`.
- `_PORTFOLIO_SIM_CACHE` eviction: if you extract `_store_portfolio_sim_cache(key, payload)`
  / `_get_portfolio_sim_cache(key)`, unit-test: insert 33 items, len <= 32,
  oldest gone. If you keep logic inline, still add a tiny helper so this is
  testable without TestClient.

B. If TestClient is safe (lifespan not entered by calling the endpoint
functions directly):
- Monkeypatch `simulate_portfolio` with a function that records
  `threading.get_ident()` and sleeps 0.05s; patch `asyncio.to_thread` to wrap
  the real thing; assert the endpoint awaits and returns.

Keep tests fast (<5s). No 5000-path sims.

Run only:

```
/tmp/callisto-pytest/bin/python -m pytest tests/test_api_eventloop_offload.py tests/test_api_auth.py -q
```

If `test_api_auth.py` fails for reasons you did not cause, do not rewrite auth
to make the suite green. Commit the offload + your new tests.

## Done

Commit message:
`fix(api): offload portfolio sim, regime, and health-file IO off the event loop`

Push: `git push -u origin HEAD`

Write `OX_DONE.md` in the worktree root with: files changed, test command + result, commit SHA.
