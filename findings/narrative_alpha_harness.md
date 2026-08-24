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
- Coverage spikes (z > +2, the loosest threshold with a usable n; z > +3
  gives only 5 candidates): 2024-08-25 (z=2.3), 2024-09-18 (z=2.3),
  2025-04-04 (z=2.8), 2025-07-17 (z=2.1), 2025-08-18 (z=3.1),
  2025-09-23 (z=3.3), 2026-01-13 (z=3.0) — after 21-day collapsing.
- Proof gate: **4 of 7 admitted**, each via a wayback capture of the Fed
  speeches index strictly before the event date:
  - 2024-08-25 ← capture 2024-08-24 · 2024-09-18 ← capture 2024-09-17
  - 2025-07-17 ← capture 2025-07-13 · 2026-01-13 ← capture 2026-01-12
  Excluded fail-closed (nearest capture postdates event): 2025-04-04,
  2025-08-18, 2025-09-23.
- FRED CBBTCUSD forward log-returns vs 32 regime-matched random controls:

| horizon | events median | events IQR | controls median | controls IQR | p (sign-flip) |
|---|---|---|---|---|---|
| +1 wk  | −3.6% | [−8.0%, +0.6%]   | +0.8% | [−1.8%, +3.6%]  | 0.064 |
| +4 wks | −1.0% | [−9.1%, +1.3%]   | +2.4% | [−3.0%, +13.3%] | 0.621 |
| +12 wks| +14.9%| [−10.3%, +37.2%] | +6.2% | [−16.0%, +23.0%]| 0.585 |

**Honest reading:** this happened 4 times; at +12 weeks the median was
+14.9% — but random dates in the same period median +6.2%, and the spread is
indistinguishable from noise (p = 0.59). At no horizon is the post-event
distribution distinguishable from random-date returns. That null IS the
product: a chatbot would have narrated "+15% after Powell spoke!" from the
same four data points.

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
