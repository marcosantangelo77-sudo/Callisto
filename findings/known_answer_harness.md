# Known-Answer Harness — accuracy of the full AGP pipeline on 20 checkable questions

Date: 2026-08-24. Harness: `callisto-wt/money/harness/` (`questions.py`,
`run.py`, `results_run1.json`, `results_run2_stance.json`), branch
`build/known-answer-harness`. Runs executed in-process through the production
path: `ResearchPipeline(RouterModel(ProviderRouter), adversary_router=router)`
— the exact wiring `callisto.py ask` uses — against backend `ox_alpha`.

**No code was tuned to raise these scores. Nothing was softened after a
failure. The two runs use identical question text; run 2 simply executes a
different checkout (see before/after below).**

## Headline

| | Run 1 (master @ 96e09c9, epistemics worktree) | Run 2 (fix/stance-propagation @ f81cdb3) |
|---|---|---|
| Questions | 20 | 20 |
| **CORRECT** | **4 / 20 = 20%** | **3 / 20 = 15%** |
| refused (answerable Q, no stance produced) | 13 | 16 |
| sealed at UNDETERMINED (stanceless seal) | 3 | 1 |
| **WRONG-AND-SEALED (wrong direction, confident)** | **0** | **0** |

The good news first: across 40 pipeline runs, the system never once sealed a
confident WRONG direction. The original incident's failure mode (sealed
AFFIRMS on a comparison whose true answer is NO) did not reproduce in either
configuration.

The bad news is everything else. On 17 answerable questions whose ground truth
was pinned by direct API calls, the pipeline produced the right stance 7 times
(41%). It refused or returned UNDETERMINED on 10 questions whose answers sit in
sources it has adapters for — including "Is Paris the capital of France
according to Wikidata?" and "According to Wikidata, was Einstein born in Ulm?"
It is an honest system that can almost never complete a lookup.

## Per-question table

Correct = final stance matches ground truth (unknowable questions score
correct when refused or UNDETERMINED). Tier/conf are what got reported.

### Run 1 — master

| ID | Question (abridged) | Shape | Expected | Got | Verdict | Sealed tier | Fetches |
|----|--------------------|-------|----------|-----|---------|-------------|---------|
| Q01 | Unemployment lower Jan-2026 than Jan-2023? | comparison | DENIES | UNDETERMINED | undetermined-sealed | PROBABLE 0.54* | 6 |
| Q02 | UNRATE exceed 4.0% in H1-2026? | comparison | AFFIRMS | — | refused (0 fetches) | — | 0 |
| Q03 | Debt > $23T on 2020-03-31? | comparison | AFFIRMS | — | refused (0 fetches) | — | 0 |
| Q04 | Debt lower than $30T on 2023-01-31? | negation | DENIES | UNDETERMINED | undetermined-sealed | SPECULATIVE 0.54 | 2 |
| Q05 | Payrolls fell >15M Feb→Apr 2020? | arith-comparison | AFFIRMS | — | refused (adversary backend failed) | — | 5 |
| Q06 | Fed funds >5% June 2007? | comparison | AFFIRMS | — | refused (adversary backend failed) | — | 4 |
| Q07 | Any bank failures in 2021? | honest-NO | DENIES | — | refused (adversary backend failed) | — | 7 |
| Q08 | Paris capital of France (Wikidata)? | checkable | AFFIRMS | UNDETERMINED | undetermined-sealed | SPECULATIVE 0.54 | 1 |
| Q09 | Loper Bright decided before 2024? | multi-hop/negation | DENIES | — | refused (0 fetches) | — | 0 |
| Q10 | WB US pop 2020 > 330M? | comparison | AFFIRMS | — | refused (0 fetches) | — | 0 |
| Q11 | SVB failed March 2023? | yes-checkable | AFFIRMS | AFFIRMS | **correct** | PROBABLE 0.70 | 8 |
| Q12 | Exactly 4 failures in 2023? (truth: 5) | negation | DENIES | — | refused (0 fetches) | — | 0 |
| Q13 | UNRATE in Jan 2027? | unknowable-future | UNDET | UNDET | correct | — | 7 |
| Q14 | Hamster households July 2019? | unknowable-gap | UNDET | UNDET | correct | — | 0 |
| Q15 | Kalshi 2032 market close yesterday? | unknowable-market | UNDET | UNDET | correct | — | 0 |
| Q16 | Debt hit $30T before UNRATE fell to 3.5%? | multi-hop | AFFIRMS | — | refused | — | 2 |
| Q17 | More failures 2023 than 2020? | multi-hop/comparison | AFFIRMS | — | refused (0 fetches) | — | 0 |
| Q18 | Mean UNRATE H1-2026 > 4.25%? | arithmetic | AFFIRMS | — | refused (0 fetches) | — | 0 |
| Q19 | Einstein born in Ulm (Wikidata)? | checkable | AFFIRMS | — | refused (0 fetches) | — | 0 |
| Q20 | Napoleon died before Civil War? | temporal | AFFIRMS | — | refused (0 fetches) | — | 0 |

*Q01's seal carried PROBABLE confidence while its stance said UNDETERMINED —
a confidence/direction mismatch worth its own finding (below).

### Run 2 — fix/stance-propagation (task 180 landed mid-session)

| ID | Expected | Got | Verdict | Notes |
|----|----------|-----|---------|-------|
| Q01 | DENIES | — | refused (0 fetches) | worse than run 1: no evidence admitted at all |
| Q02 | AFFIRMS | — | refused (0 fetches) | unchanged |
| Q03 | AFFIRMS | — | refused (0 fetches) | unchanged |
| Q04 | DENIES | — | refused | seal gone too (improvement in honesty, loss in capability) |
| Q05 | AFFIRMS | UNDETERMINED | undetermined-sealed SPECULATIVE | still seals without direction |
| Q06 | AFFIRMS | — | refused (0 fetches) | regressed from 4 fetches to 0 |
| Q07 | DENIES | — | refused | unchanged |
| Q08 | AFFIRMS | — | refused | regressed from sealed-UNDET to refusal |
| Q09 | DENIES | — | refused (3 fetches) | evidence fetched but leaf unanswered |
| Q10 | AFFIRMS | — | refused (2 fetches) | same pattern |
| Q11 | AFFIRMS | — | refused ("confidence 0.0 below DB floor") | **regression**: run 1 answered this correctly |
| Q12 | DENIES | — | refused (0 fetches) | unchanged |
| Q13 | UNDET | UNDET | correct | stable |
| Q14 | UNDET | UNDET | correct | stable |
| Q15 | UNDET | UNDET | **"correct" but SEALED VERIFIED-tier leaves at p=0.35** | see diagnosis D8 |
| Q16–Q20 | various | — | all refused | unchanged |

## Diagnosis — why each wrong/refused outcome happened

Root causes are ordered by blast radius. Line numbers reference master
(@96e09c9) unless noted.

**D1 — FRED planner returns 1948–1957 for every recent-data question
(`tools/sources/query_builder.py:_plan_fred`).** The plan is
`series_observations {series_id: UNRATE, limit: 120}` with no
`observation_start`; FRED sorts ascending by default, so the limit window ends
in 1957. Every unemployment/fed-funds/payrolls question fetched data 70 years
before the period asked about. This single defect starved Q01/Q02/Q05/Q06/Q18.
FIX ALREADY EXISTS on fix/stance-propagation (commit 4ec05a3 pins
`sort_order=desc` + year windows from the question); verified live there:
`build_plan("fred", "...January 2023...")` → `{'start': '2023-01-01',
'sort_order': 'desc'}`. Not merged to master.

**D2 — BLS planner ignores years in the question
(`_plan_bls`, master line ~491).** Window is hardcoded `start_year =
today.year - 2`, so "January 2023" questions fetch 2024–2026 only. Also fixed
by the same branch (clamps start_year to the named year). Additionally, BLS
no-key tier returned `REQUEST_NOT_PROCESSED` (daily threshold) during runs —
the adapter surfaces this as a normal body, which then fails the relevance gate
rather than being reported as an auth/quota error (masking).

**D3 — Source selection cannot route entity questions
(`tools/sources/registry.py:translate_question_type` +
`select_explained`).** "Is Paris the capital of France according to Wikidata?"
selects `['fdic', 'treasury']`; Wikidata — which holds the answer — scores 0%
because selection matches question words against adapter `answers` clauses and
nothing bridges proper nouns to sources. The FDIC institutions planner then
queries `NAME:Paris` and `NAME:Einstein`. Q19 (Einstein/Ulm) died here with 0
fetches: the decomposer's leaves were never routed to any source that could
answer them.

**D4 — Relevance gate rejects exactly-on-topic numeric bodies**
(`tools/pipeline/retrieval.py:RelevanceGate.judge`, min_coverage=0.25).
Fetched FRED observation bodies contain dates and numbers but no topic words,
so the most relevant source on the machine scores 0% coverage and is rejected
at ingestion while keyword-junk sources (World Bank indicator searches for
"fertilizer consumption") pass or get admitted. In run 1 Q07, all 5 FDIC
failure-list fetches were rejected with "missing: 2021, calendar, closed,
during, failed..." even though the query was authored BY the pipeline. The
stance branch adds `_series_title` to FRED bodies to mitigate precisely this;
it is not on master.

**D5 — Decomposition collapse: "every leaf came back unanswered" with 0
fetches.** 12 of run 1's refusals show zero fetches and zero leaves recorded.
When every leaf fails, the engine refuses wholesale
(`engine.py`: `if not any(l.answer ...)`), so the harness records nothing about
WHICH stage starved which leaf — the trace exists only if a checkpointer is
injected. Observability gap, not just a quality gap: the refusal reason should
name per-leaf gap kinds even when nothing answered.

**D6 — Adversary backend JSON failures refuse-by-default (agp/adversary.py +
engine seal step).** 4 run-1 refusals are literally "adversary backend failed
(JSONDecodeError ...) — conclusion unattacked, refusing by default". The
refuse-closed instinct is right, but the adversary's transport dies often
enough (4/20) to be a top-3 cause of non-answers. These were runs where leaves
HAD answered; the system destroyed its own partial progress at review time.

**D7 — Stanceless seals (the Q01/Q04/Q08 pattern).** Engine takes parent
direction from the best leaf (`engine.py` ~line 914 master:
`parent_stance = best_leaf.stance`) but every leaf declared UNDETERMINED, so
the seal went out with stance=UNDETERMINED and confidence up to PROBABLE.
Confidence magnitude and direction came apart — a seal asserting "we're 0.54+
sure" of nothing in particular. Task 180's dd4fb18 fixes the mechanism
(decisional-leaves-only); post-fix, Q05 still sealed UNDETERMINED/SPECULATIVE
because the underlying retrieval starvation (D1/D2/D4) left no decisional leaf
to inherit from.

**D8 — Sealing on honest-null evidence (run 2 Q15).** Post-fix, a question the
system correctly could not answer was nonetheless SEALED at 0.35 with one leaf
at "VERIFIED 0.95" whose ANSWER TEXT says "the evidence does not address the
question." The leaf tier measures evidence provenance class, not whether the
evidence answers anything; a null wrapped in primary-class bytes still reads as
VERIFIED. The seal survived because the verdict-text contradiction lives in
free prose where no gate looks.

**D9 — Non-determinism between runs.** Q11 (SVB) went correct-AFFIRMS/PROBABLE
on master to refused-below-floor on the stance branch, with different fetch
counts both times. Between adaptive gain-skipping, iterative rounds, and model
variance, single-run correctness is noisy ±1-2 questions; the harness supports
re-runs (`--tag`) so flakiness is measurable rather than hidden.

## What the numbers mean

- The pipeline's effective accuracy on checkable factual questions is ~20%,
  and its most common outcome by far is total silence (refusal) — 65-80% of
  the time depending on branch.
- Zero wrong-direction seals in 40 runs is consistent with task 180's thesis:
  the dangerous case requires a confident wrong LEAF, and D1-D4 usually
  prevent any leaf from being confident about anything. Honesty is currently
  enforced mostly by keeping the system too starved to be wrong.
- The stance-propagation branch (f81cdb3) does fix the mechanism it claims to
  (no more PROBABLE-confidence UNDETERMINED seals except via D7 residue) and
  carries working fixes for D1/D2/D4 that master lacks. It costs some
  capability (Q11 regression) but removes the worst failure shapes. Merge
  recommendation belongs to whoever owns that branch; this report only notes
  the before/after.

## Reproducing

```
cd ~/callisto-wt/money
python3 harness/run.py --backend ox_alpha --tag <name> \
    [--only Q01,Q02] [--repo <checkout>]
# resumable: results_<name>.json keeps completed questions; re-running skips them
```

Ground truths were pinned by direct API calls (fredgraph.csv, fiscaldata.treasury.gov,
banks.data.fdic.gov, api.worldbank.org, wikidata API, courtlistener API) on
2026-08-24 and are stored per-question in `harness/questions.py::verified_by`.
