# OX fleet — path to 90 (2026-08-26 04:49Z)

Master: `4a9a63b`. Honest score **~55**. Optimistic **~66**. Target **90**.
50 was fail-closed Stage A (won't lie / won't spend). 90 is god-module
extracts + one inference plane + remaining dump GETs. See
`findings/scorecard_90.md`.

## Live (6)

| Task | Branch | Owns |
| --- | --- | --- |
| odds/analysis/wiki/health GET batch 3 | `cursor/ox-odds-get-batch3-2ac0` | `api.py` |
| backtest paper-signal extract | `cursor/ox-backtest-extract-2ac0` | `tools/backtest.py`, `tools/signals/` |
| preregistration seal | `cursor/ox-prereg-seal-2ac0` | `agp/preregistration.py` |
| Hermes latency measure | `cursor/ox-hermes-latency-2ac0` | `scripts/measure_hermes_latency.py` |
| ask requires seal key | `cursor/ox-ask-seal-required-2ac0` | `callisto.py` |
| last_cycle_ok | `cursor/ox-loop-cycle-health-2ac0` | `tools/autonomous.py` |

## Next refill (audit leftovers)

1. Remaining sync sqlite on API request path (`asyncio.to_thread`)
2. Dashboard: delete LIVE/orders HTML, not only `hidden`
3. Point `MODEL_LADDER` at ProviderRouter **only after** latency doc exists
4. Further `autonomous.py` / `api.py` extracts (<2k/file)
5. Never `"live"` in paper-signal statuses. Never full `pytest tests/`.
