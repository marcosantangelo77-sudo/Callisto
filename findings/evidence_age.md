# Evidence Age: Measurement Before Model

**Branch:** `build/evidence-age` · **Commit:** 3cab908 · 2026-08-24
**Scope:** measurement only. No staleness penalty, no decay function, no
confidence adjustment of any kind — in either direction.

## What was built

1. **`FetchResult.fetched_at`** (ISO-8601 UTC), stamped at the moment the
   bytes land (`retrieval._mk_fetch`). This is fetch time, not source-data
   time — the two are different things and only the first is ours to know.
2. **Carried to the seal.** `PipelineResult.evidence_age = {n,
   n_timestamped, oldest_s, newest_s, median_s}` is computed at seal time
   and added to `summary_dict`. The sealed **conclusion text itself**
   states the spread, so a reader never opens a debug field to learn how
   stale the basis was.
3. **Checkpoint honesty.** Resume restores the ORIGINAL `fetched_at`, not
   resume time. Without this, every resumed run would launder a stale
   fetch into a fresh-looking one — the exact failure mode this task
   exists to expose, one level up.
4. **Unknown ≠ zero.** Legacy payloads without timestamps report
   `oldest_s: null`, never `0s`. An unknown age must not masquerade as a
   fresh one.
5. `tests/test_evidence_age.py` — 10 tests, including the core one: a run
   whose evidence spans a 43-minute window reports `oldest ≈ 43 min`,
   and the conclusion text says so, rather than presenting the fetches as
   simultaneous.

## What the spread actually looks like

There is no production telemetry yet (this change IS the telemetry), so
the honest answer is: **unknown until runs carry the field.** What we can
say from the architecture:

- Leaves fetch concurrently but answer sequentially-ish after; within a
  leaf the fetch→answer gap is small (seconds).
- The dominant age driver is the **run wall clock (~43 min)** times where
  each leaf's retrieval sits inside it. A resumed run can legitimately
  report hours-old evidence — and now does so honestly instead of
  silently.
- Expected shape for a live run: median age ≈ half the retrieval phase;
  oldest ≈ full elapsed time; spread (oldest−newest) ≈ the retrieval
  phase duration. For a 43-minute question that is tens of minutes of
  spread, presented today as if simultaneous.

## Recommendation (in order; nothing below is implemented)

1. **Collect first.** Let sealed results accumulate with `evidence_age`
   before deciding anything. One week of real distributions beats any
   prior about "stale."
2. **Report, don't adjust.** The inline conclusion line may be all that
   is ever needed: a human (or downstream scorer) who sees
   `oldest 2580s` can discount it themselves, with context no fixed
   decay constant has.
3. **If a penalty is ever justified**, it must be per-source-class
   (a CFTC position date and a news article age differently) and fitted
   against observed retrodiction score-vs-age data — never a hand-tuned
   constant. Preregistration/cutoff freezing (W5) stays untouched: those
   modes deliberately freeze evidence, and this field records the freeze
   rather than fighting it.
4. **Surface age in the gate later.** The relevance gate currently judges
   content only; whether fetch-time belongs there is exactly the kind of
   decision that should wait for (1).

## Verification

- Full suite on this branch's true baseline: 29 pre-existing failures
  (21 stated baseline + 8 environment drift accumulated since it was
  taken). Post-change failure set is byte-identical — zero new failures.
- `tests/test_evidence_age.py`: 10/10 pass.
- Speed goldens regenerated: the only fingerprint delta is the new age
  line in the conclusion text; every other golden field identical.
