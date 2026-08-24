# Market-Implied Benchmark — CME Fed Funds Futures (cmefedfut)

Branch `build/market-implied-benchmark`, commit `105b57c` (+ autosave commits).
Deliverable: a FREE, read-only source supplying market-implied probabilities for
macro/rate questions, wired onto `RetrodictionQuestion.market_implied` so the
beat-market rate (`edge = model_prob − market_implied` in tools/simulation.py,
`directional_edge` in batch.magnitude_score) becomes computable.

## Candidate triage

1. **CME FedWatch API — rejected as-is**: it is PAID ($25/mo+ per CME's own
   pricing page). But the underlying inputs are free: official daily
   **settlement prices** for 30-Day Fed Funds futures (ZQ) via CME's public
   `CmeWS/Settlements/TradeDate/305` feed. The probability derivation from
   settlements is published methodology ("Understanding the CME Group FedWatch
   Tool Methodology"). So: consume the free settlements, derive locally —
   which also makes every number reproducible from the ledgered raw payload.
2. **Kalshi** — already live and healthy (source-health probe OK). Its gap
   (no free-text search in list_markets) remains real but covers CPI/event
   markets; not needed for rate questions and left for a separate task.
3. **FRED** — supplies realised levels only (DGS10/T10Y2Y), NOT implied
   probabilities. Explicitly NOT used as a benchmark; the spec's
   `cannot_answer` states the distinction.

## Provenance classes — kept distinct by construction

- **PRIMARY (tier 3: market prices)** — the exchange settlement rows exactly
  as fetched; URL + sha256 recorded into the ledger by RestSource before any
  derivation runs.
- **INFERRED** — expected EFFR per contract month, expected change at an FOMC
  meeting, and probability of a move. Every derived dict carries
  `provenance_class="INFERRED"` and `derived_from={class: PRIMARY,
  trade_date, fetch:{url,sha256}}`. The distinction survives into whatever
  consumes the value.

Derivation (published FedWatch method): ZQ settles at 100 − expected average
EFFR for the month; average-EFFR identity gives post-meeting level
`post = (E·N − c·D)/d` (N days in month, D before meeting-day-effective
change, d after); probability = |post − c| / 0.25bp, direction from sign.
Pinned by a hand-computed test (95:80 settle → prob ≈ 0.443 cut).

## Publication-timestamp guard (W5)

A benchmark mis-dated after the event it "predicted" is worse than none:
- settlements carry CME's own trade date;
- `attach_from_derived()` REFUSES any attachment whose provenance trade date
  is not strictly before the question's claim_date (same-day refused too),
  and refuses unprovenanced values unless explicitly allowed;
- meetings outside available contract months → None (honest absence).

## Read-only / opt-in

Public market-data GETs only; no keys, wallet, order, or account path.
Live client constructed only with `CALLISTO_ENABLE_NETWORK=1`
(`tools/sources/cmefedfut.py::make_adapter`). tests/helpers/no_socket.py
untouched; all 11 new tests run on injected transports.

## Wiring

- Registered in adapters.py as `("cmefedfut", "CmeFedFutAdapter")`.
- `scripts/attach_market_implied.py` (live-only): loads a question file,
  derives probabilities for Fed/rate questions whose resolution date falls in
  a covered contract month, attaches through the W5 guard, prints a JSON
  summary carrying the full provenance (URL/sha256/trade date) to archive
  beside the question file. Questions without coverage keep None.
- Usage:
  `CALLISTO_ENABLE_NETWORK=1 python3 scripts/attach_market_implied.py data/retro_questions.json --trade-date 20241101 --current-rate 4.25`

## Honest limits

- End-of-day settlements only — no intraday/pre-settlement probabilities.
- Single-meeting binary decomposition of a continuous curve; multi-meeting
  months need the expanding-tree variant (documented, not implemented here).
- Requires knowing the current target-range upper bound (caller-supplied).
- No benchmark fabricated where no contract month covers the meeting.

## Verification

- 11 new tests pass (tests/test_build_r7_cmefedfut.py): spec honesty, PRIMARY
  recording, curve filtering, hand-checked derivation, honest absence, W5
  refusal cases, opt-in gating.
- Full suite: 34 failed / 11,215 passed — identical failure set to clean HEAD
  baseline (backtest_e2e ×11 pre-existing verified earlier on dcd27a0,
  redteam artifacts/laundering ×20, lifecycle ×2, prop_scanner ×1). Zero new
  failures; no confidence score touched.
