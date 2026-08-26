# Scorecard — path from teens to 50/100

**Date:** 2026-08-26 (updated 04:40Z)
**Rater:** orchestrator. Not a vibe. Each row is an invariant we can
falsify with a test or a grep. **Current column is origin/master @
`db77c68`.**

The 29/100 audit score was generous. Master this morning was **~18–21**.
50 is “overnight the box will not lie and will not spend money, and the
product is one appliance.” It is not 100. Dual inference planes and an
8k-line `autonomous.py` can still exist at 50.

| # | Dimension (10 pts) | Morning | **Master now** | Evidence on `db77c68` |
| --- | --- | --- | --- | --- |
| 1 | Evidence integrity | 1 | **7** | `auto_promote` diagnose-only (`dca6b91`); `_phase_refresh_signals` gated (`ecf22fb`) |
| 2 | Money switches | 2 | **8** | OM default off; `/resume_all` does not `bet_executor.enable`; `CALLISTO_LOCAL_ONLY` refuses both enables; `_phase_live_execute` requires `CALLISTO_ALLOW_LIVE_EXECUTE=1` |
| 3 | Bind + GET auth | 3 | **8** | Launchers loopback; `/bets` `/hypothesis*` `/system/full-status` `/executor/status` `/odds/edges` `/odds/opportunities` `/edges/live` gated. Other `/odds/*` still open (live batch-2 worker). |
| 4 | Seals | 2 | **8** | keyed `verify_seal` rejects unkeyed SHA-256; invalid hex fails closed (no unkeyed fallback); doctor fails if key missing |
| 5 | Event loop / health | 2 | **6** | `to_thread` on sim/regime/health-file; phase-failure ledger extracted |
| 6 | Product identity | 3 | **6** | Direction written; dashboard title/research face; LIVE panels hidden unless `?trading=1` |
| 7 | One Kelly / one router | 2 | **6** | `kelly_binary` and `edge.assess_edge` → `kelly_core`/`kelly_full`; MODEL_LADDER still duplicate (pinned in tests) |
| 8 | God modules | 1 | **3** | `tools/loop/phase_ledger.py` extracted; `autonomous.py` still ~8k |
| 9 | Loaded-gun tests | 2 | **8** | paper-only signal executed; live-execute AST pin; LOCAL_ONLY pins; dashboard source contract; `tests/test_fail_closed_registry.py` |
| 10 | CLI honesty | 3 | **7** | doctor: seal + bind + money switches; `callisto status` prints bind/LOCAL_ONLY/live-execute |
| | **Total /100** | **~21** | **~67** | Conservative floor **~55**. **50 is cleared on master.** |

Haircut notes (why not 80): many `/odds/*` dump GETs still loopback-only by
bind, not by auth; kernel `MODEL_LADDER` ≠ ProviderRouter; `ResearchLoop`
still lives in one file; `callisto ask` is unchanged; no overnight soak of
the new gates on Marco's box.

## Landed on origin/master this run (orchestrator cherry-pick after independent tests)

Fail-closed: autopromote, bind, ps-bind, seal, seal-invalid-key, cli-seal,
doctor-money, telegram arming, local-only executor, OM local-only,
live-signal hard gate, live-execute env gate, GET gating (bets/hyps/status),
event-loop offload, signal-refresh gate.

Structural: `kelly_core` + `kelly_binary` wrapper, `edge.assess_edge` →
`kelly_full`, dashboard research face, `tools.loop.PhaseFailureLedger`,
inference-plane pin, fail-closed registry tests.

## Still live OX (do not collide)

See `findings/ox_wave4_fleet.md`. Never arm live betting. Never add `"live"`
to paper-signal statuses.
