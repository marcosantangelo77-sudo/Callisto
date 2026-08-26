# OX fleet — path to 90 (2026-08-26 05:00Z)

Master: `53f099f`. Honest score **~58**. Optimistic **~68**. Target **90**.
50 was fail-closed Stage A (won't lie / won't spend). 90 is god-module
extracts + one inference plane + remaining dump GETs. See
`findings/scorecard_90.md`.

Hermes CLI latency (n=3): p50 **11.9s**, max **31.4s**. Do **not** point
`MODEL_LADDER` at ProviderRouter on a sub-10s assumption.

## Landed this keep-six (independent tests, OX_DONE stripped)

| Topic | Origin SHA | Landed | Tests |
| --- | --- | --- | --- |
| ask seal required | `8996413` | `69c7f10` | 4 passed |
| prereg keyed verify | `c02f1cd` | `a60bccc` | 29 passed |
| paper-signal extract | `23c3085` | `5bd872f` + pin `c9923fa` | 112 focused (incl. registry/tier7 pins) |
| GET dump batch 3 | `b0a55c1` | `5e5ac8e` | 47 passed; `/health*` stay public |
| Hermes latency measure | `26cc218` | `6d3dd74` | 3 passed; no unify |
| last_cycle_ok | `09005dc` | `796a6fa` + count fix `53f099f` | 27 + 43; count is current cycle only |

## Live (6) — wave 6

| Task | Branch | Owns |
| --- | --- | --- |
| delete LIVE/orders HTML | `cursor/ox-dashboard-delete-live-2ac0` | `web/dashboard/` |
| GET dump batch 4 | `cursor/ox-get-batch4-2ac0` | `api.py` |
| backtest schedule extract | `cursor/ox-backtest-extract2-2ac0` | `tools/backtest.py`, `tools/signals/` |
| do not unify MODEL_LADDER | `cursor/ox-inference-nounify-2ac0` | `inference.py` |
| Stage B registry file | `cursor/ox-registry-w6-2ac0` | `tests/test_fail_closed_wave6.py` |
| extract last_cycle_ok | `cursor/ox-cycle-health-extract-2ac0` | `tools/autonomous.py`, `tools/loop/cycle_health.py` |

## Next refill (audit leftovers)

1. Remaining sync sqlite on API request path if any after eventloop land
2. Further `autonomous.py` / `api.py` / `backtest.py` extracts (<2k/file)
3. Point `MODEL_LADDER` at ProviderRouter **only after** a design that
   budgets p50 ≈ 12s / max ≥ 30s (not this wave)
4. Never `"live"` in paper-signal statuses. Never full `pytest tests/`.
