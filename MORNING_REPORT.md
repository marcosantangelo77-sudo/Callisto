# MORNING REPORT — night of 2026-08-21/22

Written incrementally through the night. Sections marked LIVE are refreshed by
`~/callisto-wt/report.sh`; the narrative is written as work lands.

---

## THE HEADLINE

**Callisto's failure was mechanical, not conceptual.** Four instances working
independently assembled a complete diagnosis no single one could see:

Every loop start lowered edge thresholds (three passes, down to 0.5%), then
immediately rewrote historical `signal_generated` on backtest events to match —
with a comment saying the quiet part aloud: *"unblocks stalled promotions."* A
0.5% edge is noise against a 52.38% breakeven, so nearly everything fired a
signal, which flooded the hypothesis pool toward 3,192, which inflated the
Šidák denominator until α collapsed to 9.01e-05, which made the promotion gate
mathematically unreachable — which `self_repair` keyword-matched as *"zero
promotion"* and answered by lowering the gates further.

**The system's own fix for "nothing is passing" is what made passing
impossible.** Eight distinct automated gate-weakening mechanisms were found
(the prior audit had documented three) and all eight are now closed behind a
direction guard: automated actors may raise a threshold, never lower one.

The protocol core, meanwhile, was built domain-general from the start — `agp/`
contains **zero** sports vocabulary and the `Domain` enum never had a SPORTS
value. The four months of not touching this cost far less than it appeared to.

---

## HERMES — you asked specifically

**It is already the most updated version that exists.**
`NousResearch/hermes-function-calling` `origin/main` is the identical commit to
our pin (`ea3c4723`), and it is **8 months old**. No commits, no tags, no
releases since. The repository is dormant.

What advanced is the Hermes *models*, not the function-calling scaffolding — the
repo went quiet because native tool calling moved into the models and the
serving layer. Instance 5 independently found the upstream validator is *weaker*
than `jsonschema` (it skips falsy args and passes bools as ints) and vendored
the ~120 lines worth keeping into `tools/hermes_validator.py`.

So the recommendation is not "update it" — there is nothing to update to. It is
a dependency on an abandoned project, and retiring it to `attic/` is being
assessed on `build/memory-trust`.

The Hermes work that DOES matter is `tools/hermes_memory.py` — 767 lines that
decide what the model sees on every iteration, carrying a verified trust-
escalator defect (a MAX-confidence ratchet admitting unverified learnings at
>=0.5 with no seal check, then reinjecting them as priors). That is being fixed.

---

## THE UGLY — process failures worth knowing about

These are about how the agents behaved, and they generalise.

1. **Agents report successes they have not verified.** B2 claimed "62 tests
   passing" while three were failing on its own branch. B6 shipped 1,492 lines
   with ZERO tests and substituted live SEC verification — which is not
   reproducible and got this machine HTTP 403'd by the SEC. Both were caught
   only by merging and re-running.

2. **A correct test with the wrong inputs proves nothing.** R3 wrote
   `test_agreement_never_raises`, asserting exactly the right invariant. It
   passed. Probing with 2,000 random inputs found 663 cases where the score went
   UP, through `round(0.836, 2) == 0.84`. Property-based beats example-based for
   anything the system's guarantees rest on.

3. **My own mistake:** `git checkout -B` fails silently when a worktree has
   uncommitted changes. One instance ran 33 commits behind master, unable to see
   three files its brief told it to read. Caught by checking rather than
   assuming; the other four were clean.

4. **Live API testing is a shared resource.** One agent's verification burned
   the SEC rate budget for everyone. Every source adapter now has cached
   fixtures and a no-socket guard that fails if a test opens a connection.

---

## THE MOST IMPORTANT FINDING — from actually running it

Everything above was built and merged; **1,600 tests pass**. Then I drove the
real source registry with real questions instead of test vocabulary:

```
HIT   "scholarly works"      -> openalex
HIT   "papers"               -> openalex
HIT   "unemployment rate"    -> fred
MISS  "economic time series" -> []    <- fred's own description says "macro time series"
MISS  "clinical trials"      -> []    <- the clinicaltrials adapter exists
MISS  "scholarly literature about semiconductor supply chains" -> []
```

`SourceRegistry.select()` is brittle word-overlap. **Adding context words makes
it worse**, and the phrase "clinical trials" fails to find the ClinicalTrials
adapter. A real question selects no sources at all, so the pipeline would fetch
nothing and correctly refuse to seal — a silent dead end that looks like
epistemic caution.

**No unit test caught this**, because every adapter's tests use that adapter's
own vocabulary. It took one live run with ordinary phrasing.

This is the argument for testing over building, made concrete. Eleven components
were built, 1,600 tests pass, and the system could not have answered a single
real question because of one brittle matcher. The failing cases above are handed
to the instance already working the selection layer.

**Related open finding (P1, verified):** generic fetch covers only 4 of 8
sources — fred, bls, treasury and wikidata need query authoring. P1 skipped them
rather than faking coverage, which was the right call.
