# Retrieval starvation — D2/D3/D4 from the known-answer harness

Date: 2026-08-24. Branch: `fix/retrieval-starvation` (loop worktree).
Harness: `callisto-wt/money/harness/` (task 181's 20 questions, ground truth
pinned by direct API calls), run in-process through the production path
`ResearchPipeline(RouterModel(ProviderRouter), adversary_router=router)`
against backend `ox_alpha`, `--repo ~/callisto-wt/loop`.

## Headline

| | Before (baseline @ c504363) | After (@ 930fe02 + D1 windowing 2a59e9f) |
|---|---|---|
| ACCURACY | **15%** (3/20, run `starvation_after`) | **20%** (4/20, run `starvation_after3`) |
| refused | 17 | 14 (+1 error, +1 undetermined-sealed) |
| wrong-and-sealed | 0 | 0 |

The absolute number moved less than the mechanism did. The important result
is per-seam: each defect now fails loudly at its own layer instead of
starving every leaf downstream of it. The residual refusals are dominated by
D5/D6-class causes (decomposition/adversary), which are out of scope here.

## What was fixed

### D4 — relevance gate rejected its own correct answers
`tools/pipeline/retrieval.py` (`RelevanceGate.judge`).

FRED observation bodies carry dates and numbers but no topic words; pure
token coverage scored them 10–22% against a 25% floor while the pipeline's
own authored queries came back. Fix is a SECOND admission route that is
stricter, not looser: `numeric_window_matches()` admits a body only when
(a) the question names years, (b) the body carries date-stamped observations,
(c) every observation year lies INSIDE the question's year set, and (d) the
body carries at least one numeric value. Verified rejections preserved by
test: wrong-window numeric bodies, empty results, catalogue rows, topic-word
prose without data.

### D3 — source selection could not route entity questions
`tools/sources/wikidata.py` (spec clause) + `translate_question_type`
(retrieval.py).

"Is Paris the capital of France according to Wikidata?" selected
fdic+treasury and wikidata scored 0%. A deliberately narrow entity-shape
detector (`_looks_like_entity_question`: who/where/born/capital of/founded/
located/wikidata/...) pins the entity graph into the selection candidates;
the spec gained an explicit capital-city/birthplace clause so ranking puts
wikidata FIRST for entity lookups. Macro routing verified unchanged by test.
False positives can only ADD a candidate source — they cannot remove the
keyword-selected ones.

### D2 — BLS planner ignored named years; quota errors masked as bodies
`tools/sources/query_builder.py` (`_plan_bls`, `classify_fetch_failure`) +
retrieval `_fetch_one`.

A year named in the question now anchors the fetch window (clamped to the
no-key 3-year history by anchoring AT the named year, not at today —
"January 2023" must fetch 2023 even if 2026 falls outside the call).
Separately, BLS returns HTTP 200 with `status:
["REQUEST_NOT_PROCESSED"]` on quota exhaustion; `classify_fetch_failure()`
now detects error envelopes BEFORE the gate and records them as quota/auth
fetch failures instead of letting them fail relevance as "irrelevant data".

### D1 (found en route) — FRED planner returned 1948–1957 for recent-data questions
`_plan_fred` + `tools/sources/fred.py`.

`limit=120` with ascending default order ended in 1957 — the most relevant
body could not contain the asked-about months. The plan now windows
observations on the question's named years and sorts descending; the adapter
passes `sort_order` through. This was live in the stance-propagation branch's
history but absent from this checkout; reimplemented with the same seam.

### En-route fix cherry-picked: adversary parse flakiness (battery D3)
Commit 9240a98 from `fix/adversary-parse-flakiness`: hardened JSON
extraction, transport-vs-veto distinction, bounded parse-only retry. In the
after-run this converted several hard refusals into answered leaves and
removed the dominant "adversary backend failed (JSONDecodeError)" refusal
class.

## The trap check (gate must be more correct, not looser)

Explicit adversarial tests pin what must STAY rejected:
wrong-window numeric bodies (1957 dates vs a 2026 question), FDIC rows for a
bank question asked of BLS, zero-overlap documents, keyword-search catalogue
rows, and prose that shares topic words but carries no structured data (the
structural route requires dates+numbers+window match). Full suite: 78
failures after vs 79 baseline failures — every delta accounted for below,
zero new failures.

## Reconciliation notes (peers' concurrent landings)

Two baseline-failure deltas are bookkeeping, not regressions:

1. Peer commits 75e6617/8f518d2 landed mid-session on this branch (worldbank
   planner honestly refuses NL search fallback). That changed which sources
   fill the fan-out slots in the `rejected_fetches_noted` speed golden
   (semanticscholar replaces worldbank). Golden regenerated FROM THE SERIAL
   ENGINE with serial==parallel verified first (verdict (a)), per the
   documented regeneration rule; scripts/discriminate_goldens.py logic reused.
2. The C3 planner pin (`test_same_series_two_leaves_get_identical_query_parameters`)
   asserted byte-identical calls across leaves naming DIFFERENT years — the
   exact hole the year-window fix deliberately closes. Updated to pin what
   must not vary (series id, method, ordering) plus the new visible window
   behavior; the strict-xfail C3 companion now PASSES (window is explicit),
   promoted to a real test.
3. Baseline failure `test_engine_fetches_from_multiple_sources` passes after
   the fixes (it starved on the old gate).

## Test coverage added

`tests/test_redteam_retrieval_starvation.py` — 18 tests, all written failing
first: 8 D4 gate tests (4 admit, 4 reject-the-junk), 4 D3 routing tests,
6 D2 planner/masking tests.

## Reproducing

```
cd ~/callisto-wt/money
CALLISTO_REPO=$HOME/callisto-wt/loop python3 harness/run.py \
    --backend ox_alpha --tag <name> --repo $HOME/callisto-wt/loop
# runs: results_starvation_after.json  (before fixes landed: 3/20)
#       results_starvation_after3.json (after all fixes:     4/20)
python3 -m pytest tests/test_redteam_retrieval_starvation.py -q
```

## What still starves (honest accounting)

Q01–Q06, Q16–Q18 mostly refuse with "every leaf came back unanswered": the
model-authored decompositions route leaves whose phrasing misses both the
concept tables and the diagnostic-term floors, and the adversary veto still
kills sealed answers on transport noise (partially mitigated by the
cherry-pick). Q07 sealed UNDETERMINED with 2 fetches — evidence arrived but
the leaf would not commit to DENIES on a yes/no question. Those are D5/D6/D7
shapes, owned by other findings; retrieval no longer starves before the
pipeline gets the chance to fail at THEM.
