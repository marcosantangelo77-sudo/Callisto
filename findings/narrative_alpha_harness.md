# Narrative-Alpha Event-Study Harness — build notes and first result

Branch: `build/narrative-alpha-harness` · Date: 2026-08-24

## The question this harness answers (owner's shape)

"how many times has a major speech come out and Bitcoin gone up 15%, and
what happened in the next three months?"

Today nothing answers that honestly: a chatbot produces fluent prose without
counting anything. This harness **counts**.

## What was built

```
tools/event_study/__init__.py     package entry; hard-rules docstring
tools/event_study/events.py       event discovery + PROOF-gated admission
tools/event_study/outcomes.py     FRED forward returns, matched controls,
                                  distribution stats, sign-flip test, report
scripts/event_study_powell_btc.py runnable end-to-end worked example
data/event_study/                 cached GDELT timeline + report JSON
```

Pipeline per run:

1. **Event set** — GDELT DOC `timelinevol` (`tools/sources/gdelt.py`,
   keyless) gives a dated coverage-volume curve for a query. Candidate
   events = days where coverage z-score > +3 (a dated occurrence you can
   point at), collapsed within 21 days so one narrative episode isn't
   double-counted.
2. **Provable timestamps** — every candidate must carry a Wayback
   `IMMUTABLE_SNAPSHOT` PublicationProof (`tools/sources/wayback.py`) whose
   `published_on` is **strictly before** the event date, on the canonical
   source URL (the Fed's speech-index page). This is fail-closed: no proof ⇒
   the event is EXCLUDED and the exclusion is logged with its reason. A
   statement mis-dated after the move it "predicted" is fabricated alpha;
   this gate is the whole defense.
3. **Outcomes** — FRED (`tools/sources/fred.py`, live, key configured)
   series aligned at t=0 = event date; forward log-returns at +1/+4/+12
   weeks using first available observation at/after each endpoint (daily
   BTC via CBBTCUSD; SP500/DGS10/T10Y2Y supported by the same code).
4. **Control** — N random dates per event drawn from the same calendar span
   as the events, ≥21 days from any event date. Regime-matched, so "price
   rose after the event" can be compared against what price did anyway.
5. **Report** — n, median, quartiles/IQR, min/max for events AND controls,
   plus a sign-flip permutation test (10k draws) on the median gap. The
   exchangeability logic mirrors `tools/retrodiction/scoring.py`
   (`paired_significance`, `bootstrap_brier_ci`) — that module's API is
   Brier-specific, so the test lives in `outcomes.py` but reuses the same
   philosophy rather than inventing new statistics.

## Hard rules honored by construction

- **No confidence score is raised anywhere in this package.** The output is
  a distribution report plus a p-value-style noise comparison. It produces a
  QUESTION for the pipeline, never a conclusion of its own.
- **No trading signal, no position sizing, no execution path is armed.**
  Nothing here writes to any confidence, edge, or sizing structure.
- **Publication timestamps provable or excluded.** See step 2. In the worked
  example below the proof gate rejected 4 of 5 candidate events.

## Worked example (real data, real numbers)

Run: `python3 scripts/event_study_powell_btc.py`

- GDELT coverage timeline for `"Jerome Powell" speech`, 709 daily points,
  2024-08-25 → 2026-08-24 (cached to `data/event_study/gdelt_timeline.json`).
- Coverage spikes (z > +3): 2024-08-26 (z=5.4), 2025-04-17 (z=3.5),
  2025-08-02 (z=3.2), 2025-08-23 (z=7.6), 2025-09-23 (z=3.3).
- Proof gate: only **2024-08-26** carries a wayback capture of the Fed
  speeches index strictly before it (capture 2024-08-24). The other four are
  excluded fail-closed (nearest captures postdate the events). Admitted n=1.
- FRED CBBTCUSD outcomes from that single admitted event:

| horizon | event return |
|---|---|
| +1 week | −6.1% |
| +4 weeks | +2.3% |
| +12 weeks | +38.6% |

- Controls could not be drawn for a one-event set under the ±21-day
  exclusion window within a 90-day span; the sign-flip test reports
  insufficient data rather than pretending otherwise.

**Honest reading:** with n=1 there is no distribution and therefore no
finding. This IS the product working as designed: the harness refused to
narrate a "+38.6% after Powell spoke!" story that a chatbot would happily
produce, because (a) one observation proves nothing, (b) Bitcoin rose over
almost every 12-week window in that period — which is exactly what the
regime-matched controls exist to expose once n allows drawing them.

The GDELT DOC API rate-limits aggressively (HTTP 429 beyond roughly one
request per 5s even when self-limited; bursts penalize for many minutes),
which is why the example caches the timeline and why the harness supports
injected adapters for offline/test use.

## Survivorship warning (plain)

A backward-looking corpus is selected by hindsight. We searched for coverage
spikes around a famous speaker because we already suspect they matter; any
hit-rate mined this way is NOT an unbiased estimate of forward edge. The
sound version fixes a speaker/event set NOW — pre-registered queries, fixed
spike threshold, fixed horizons — and captures forward from today. Until
that forward arm has run, every number above is hypothesis-generating only.

## How to extend

- More event sets: call `build_event_set(query=..., source_url=...)` per
  speaker/topic; the proof gate applies identically.
- More outcome series: `OutcomeSeries.load("SP500", ...)` etc.; pass to the
  same `measure_forward_returns` / `report`.
- Larger n needs either longer GDELT windows (DOC 2.0 tops out ~2y back;
  older coverage needs the GDELT 1.0 event DB) or looser spike thresholds —
  both trade off against the honesty of the event definition.
