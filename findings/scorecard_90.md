# Scorecard — 50 was fail-closed. 90 is the actual codebase.

**Date:** 2026-08-26
**Master:** `700245c`
**Rater:** orchestrator

The jump teens → 50 was **Stage A only**: overnight the box will not
rewrite evidence and will not spend money. It was not a rewrite. A
skeptical 50 is correct. A 67 was too generous on product identity
(hiding LIVE panels is not deleting a sportsbook UI).

**90** means the audit leftovers are gone: god modules carved, one
inference plane, dump GETs gated, phase failures not swallowed into
success, dashboard is a seal/run viewer, every verify path fail-closed.
It is still not 100 (no overnight soak on Marco's box; sports gym
remains).

| # | Dimension (10) | Now | 90 requires |
| --- | --- | --- | --- |
| 1 | Evidence | 7 | No remaining silent `signal_generated` / threshold writes in loop, hypgen, wiki |
| 2 | Money | 8 | Live path attic'd or `callisto arm`; Telegram cannot enable OM without LOCAL_ONLY already blocking |
| 3 | Bind + GET | 7 | Every `/odds/*` `/analysis/*` `/wiki/*` `/health/{detailed,deep}` `/tasks` gated; health/livez stay public |
| 4 | Seals | 8 | `agp/preregistration.verify_seal` same keyed regime; no unkeyed fallback when key invalid |
| 5 | Event loop | 6 | Remaining sync sqlite on request path offloaded or gone |
| 6 | Product | 6 | Dashboard default has no LIVE/orders HTML; panels are runs/seals/loop. `callisto ask` is the ritual |
| 7 | One Kelly / router | 6 | `MODEL_LADDER` delegates to ProviderRouter **after** measured Hermes latency, or ladder deleted with proof |
| 8 | God modules | 3 | `autonomous.py` / `api.py` / `backtest.py` each < ~2k by extract, class names stable |
| 9 | Loaded-gun tests | 8 | Registry covers every Stage A+B invariant; live-execute import tests that do not hang |
| 10 | CLI | 7 | `ask` refuses to pretend keyed when key missing (visible fail, no generated key in-band unless doctor) |
| | **/100** | **~66 optimistic / ~55 honest** | **90 = Stages B+C+D+E landed, not promised** |

## Audit leftovers still open (fleet queue)

From `findings/production_ready_2026-08-26.md` + brutal audit:

1. God modules: `tools/autonomous.py` ~8k, `api.py` ~4.7k, `tools/backtest.py` ~4.2k
2. Dual inference: `MODEL_LADDER` vs `ProviderRouter` (comment-pinned only)
3. Remaining dump GETs: `/odds/sgp|props|psychology|…`, `/analysis/*`, `/wiki/*`, `/health/detailed`
4. `ResearchLoop._loop` still `except Exception: continue` (ledger records; cycle still "succeeds")
5. `agp/preregistration.py` verify is HMAC-via-`_seal_digest` but invalid-key raise vs False
6. Dashboard still *contains* LIVE/orders markup (hidden)
7. Event-loop: other sync sqlite in `api.py` besides sim/regime/health-file
8. Frozen unreviewed Codex: `dbcc751`, `1ec9778` — do not merge on testimony

Never arm live betting. Never add `"live"` to paper-signal statuses.
