# API SERVING SURFACE — improvement pass (build/cli-front-door)

**Area chosen: the API serving surface** (`api.py` — 4,685 lines, 108
endpoints, the HTTP half of "how a human actually uses this thing").

Why this one: every improve area in the list was taken except this one.
CLI got two passes (improve_cli, improve_cli_run_persistence); AGP core,
retrodiction/calibration, edge sizing, artifacts/sandbox, hypothesis
lifecycle, autonomous loop, provider routing, memory/wiki, schema seam,
source registry and synthesis are all owned by prior or concurrent runs —
several of those files carry peers' uncommitted work in this tree right now
(engine.py, retrieval.py, sources/*, knowledge_wiki.py, hermes_memory.py,
schema/core.py), so they were off-limits under exclusive file ownership.
`api.py` is the single largest serving module (fan-out 58 per
ARCHITECTURE_MAP), it is what CLAUDE.md's session-start protocol and the
Telegram/MCP/autonomous-loop integrations all talk to, and no pass had ever
read its endpoint surface end to end. This one did: all 108 endpoints
enumerated and classified by auth posture and parameter hygiene.

## What was wrong — measured

The write side is in good shape (default-secure middleware gates every
POST/PATCH/PUT/DELETE behind loopback-or-bearer; `/admin/sql` has an AST
validator with a PRAGMA allowlist — 29 tests in test_api_auth.py). The READ
side never got the same treatment:

1. **Unbounded `limit` on 12 read endpoints.** The middleware only gates
   writes, so reads lean entirely on their (optional) dependencies — and a
   dozen GETs took a user-supplied `limit` straight into `SQL LIMIT ?` with
   no server-side cap. SQLite treats `LIMIT -1` as unlimited (verified:
   `select * from t limit -1` returns everything), so `?limit=-1` on any of
   them materialises the full table per request. Affected: /odds/movements,
   /odds/opportunities, /odds/snapshots/{sport}, /edges/live,
   /odds/kl-metrics, /bets, /bets/bankroll, /health/integrity/history,
   /debug/memory/top-traces, /orders, /wiki/articles, /wiki/search.
   `/world/{domain}` got exactly this clamp in the 2026-04-21 audit ("limit
   beyond 500 materialises gigabytes"); these twelve never did. Measured
   before: 12 of 14 limit-taking GETs uncapped.

2. **15 money/gate/internal reads with no auth dependency at all.** Because
   the middleware ignores GET, these served real position and gate data to
   ANY caller the moment CALLISTO_BIND_HOST is set non-loopback (the config
   exists for that; the code even anticipates it in comments): bet history
   including stakes and PnL (/bets, /bets/bankroll, /bets/clv-report,
   /bets/clv-forecast), the hypothesis pool with thresholds
   (/hypothesis*×4), backtest results (/backtest/run/{run_id}),
   research-loop status (/research/status, /research/sports),
   order-book status (/executor/status), plus /claude/status,
   /embeddings/stats, /historical/cache. GET /task/{id} and /tasks were
   already gated for leaking less. Measured after: 0 ungated sensitive
   reads remain; the class is pinned shut structurally (below).

## What landed

One commit against api.py only (+44/−15):

- `_cap_limit(raw, default=50, cap=500)` helper next to `public_endpoint`,
  same defence `/world/{domain}` uses, applied at all 12 sites (negative →
  floor of 1, huge → 500, non-numeric → default).
- `dependencies=[Depends(require_admin_or_loopback)]` added to the 15
  ungated sensitive GETs listed above — identical posture to GET
  /task/{id}; loopback callers (MCP server, research loop, dashboard) are
  unaffected by design (`require_admin_or_loopback` allows loopback when no
  token is set, and always allows loopback when one is).
- `tests/test_improve_api_surface.py` (9 tests): unit tests for the clamp
  semantics (the LIMIT -1 case is asserted explicitly) AND structural pins
  that parse api.py's AST so a FUTURE endpoint reintroducing either shape
  fails CI: every limit-taking GET must clamp, and any GET whose path
  touches money/gate/internal vocabulary must carry require_admin.

## Before/after

| measure | before | after |
|---|---|---|
| GETs taking user `limit` with no server cap | 12 | 0 |
| `?limit=-1` behaviour on affected endpoints | full table | 1 row |
| money/gate/internal GETs reachable unauthenticated off-loopback | 15 | 0 |
| area tests | test_api_auth (29, writes only) | +9 structural |
| api.py suite + neighbours (auth, cli, health, odds_api, serving, dashboard) | — | 115 passed |

Full-suite run with my diff: 2,175 passed / 33 failed (+2 collection errors:
xgboost/libomp missing on this Mac). Baseline check with my two files
reverted reproduces the SAME 33 failures byte-for-name (backtest_e2e ×16 —
pre-existing documented set, build_w1_retrieval ×7 and w6_synthesis_adoption
×3 from a peer's in-flight engine work, redteam canaries ×3 belonging to
other branches, wiki_loop/prop_scanner/p1_findings/i1 ×1 each). My diff
changes none of them; the api-touching suites go 115/115 green.

## Honest caveats

- Loopback-only deployments (the default) saw NO behaviour change from the
  auth additions: require_admin_or_loopback admits localhost with no token.
  Anyone scraping these paths from another machine was relying on a
  misconfiguration; that is exactly what the gating closes.
- I did NOT touch lifespan startup (345 lines, god-function territory), the
  25-copy-paste phase runner debt (loop pass declined it too), or the
  remaining ~40 ungated-but-benign GETs (odds/model/data/health surfaces) —
  they expose market-derived aggregates, not positions or gate state, and
  the dashboard reads them without credentials. A stricter posture would be
  a product decision, not an improvement pass.
- `/system/full-status` remains deliberately ungated (CLAUDE.md's session
  protocol depends on it); it reports aggregate status, though its output
  is broad enough that gating it is worth an owner decision.

## What I deliberately did not do

- No new endpoints, no capability, no framework swap (FastAPI stays).
- Did not refactor injury_impact_model (141 lines of inline logic) — real
  debt, but a rewrite of a working sports surface mid-season violates rule 5
  and risks the regression-test domain for zero behavioural gain.
