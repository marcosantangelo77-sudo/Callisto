# World Bank planner defect (question-battery D1) — fix and audit

Branch `fix/retrieval-starvation`. Commits 75e6617, 8f518d2.
Tests: `tests/test_redteam_worldbank_planner.py` (13 passing, fully offline).

## The defect

`_plan_worldbank` stuffed the natural-language core of every sub-question
into the World Bank API's free-text `search` parameter
(`/v2/indicator?search=...&source=2`). The API **ignores** that parameter and
returns the same default indicator-catalogue page regardless of query —
byte-identical sha256 across differently-worded questions (verified across
all 41 battery runs). So:

- court-case, bank-count, literature-count, market-state questions ALL got
  "fertilizer consumption (% of production)" style catalogue rows;
- the relevance gate kept them (they share topical words with nothing, but
  pre-D4 the gate's junk path admitted them; post-D4 they ride other passes);
- the correct primary sources were gated OUT at ingestion by the numeric-body
  defect (191/D4), so World Bank junk was literally the ONLY thing retrieval
  admitted: worldbank contributed on 23 of 41 battery runs; fred, bls,
  treasury, census, gdelt, kalshi, openalex, courtlistener, pubmed and
  wayback contributed on ZERO.

Result: 22 × "every leaf came back unanswered" refusals + 7 below-floor
refusals = 29 of 41 bad outcomes. A source that always returns irrelevant
results is worse than a source that says "I cannot answer this."

## Fix 1 — honest gap replaces the free-text fallback

When no curated WDI concept resolves AND no explicit indicator code is in
the question, `_plan_worldbank` now returns `PlanResult(plannable=False)`
naming the defect and the fix (add to `_WORLDBANK_INDICATORS`, or supply a
code like `SP.POP.TOTL`). Precedent: kalshi's honest gap in this same file.
The retriever already treats unplannable sources correctly — it records the
gap once and spends the fan-out budget on sources it CAN serve.

## Fix 2 — CORRECT retrieval, not silent retrieval

The trap was making WB return nothing. `_WORLDBANK_INDICATORS` gains real
WDI codes for unemployment (SL.UEM.TOTL.ZS), inflation (FP.CPI.TOTL.ZG),
life expectancy (SP.DYN.LE00.IN), GDP per capita (NY.GDP.PCAP.CD), exports,
imports, CO2 per capita — data edits with real ids, not guesses. Also added
"us" to `_WB_COUNTRIES`: battery questions say "US population", which
resolved to iso3=all (fetching every country) instead of USA.

On the battery bank, worldbank now plans REAL indicator fetches for 11/41
questions (fred_* unemployment ×6 via SL.UEM.TOTL.ZS, worldbank_01/02
population, worldbank_03 GDP, census_01 population) and declares an honest
gap for the other 30.

## Task 2 — audit of all other planners for the same shape

Audited every planner in `tools/sources/query_builder.py` plus its adapter's
parameter semantics. Verdicts:

| planner | parameter fed | verdict |
|---|---|---|
| **worldbank** | `search=` (API-ignored free text) | **DEFECT — fixed above** |
| fred | `/series/search?search_text=` | OK — genuine server-side full-text search returning rankable series ids |
| openalex | `/works?search=` | OK — real full-text search |
| semanticscholar | `/paper/search?query=` | OK — real search |
| clinicaltrials | `query.term` (+structured status filter) | OK — real search; status word correctly moved out of the term |
| federalregister | `conditions[term]` | OK — real FR conditions search |
| courtlistener | `q=` | OK — real search (token-gated) |
| gdelt | quoted phrase in `query` | OK — phrase-quoted so multi-word cores match as phrases |
| fdic | NAME:x field predicate / failures endpoint | OK — structured filters, never NL free text; honest gap otherwise (the FDIC `filters=` vs `search=` fix held) |
| treasury | catalog dataset resolution only | OK — honest gap when no dataset resolves (~1000-entry catalog, refuses rather than guessing) |
| bls | series id required | OK — honest gap ("no free-text search endpoint") |
| cftc_cot | explicit/curated market code only | OK — honest gap |
| uspto_odp | simplified query syntax over documented fields | OK — real search endpoint (key-gated) |
| wikidata | SPARQL EntitySearch service | OK — real server-side entity search inside SPARQL |
| census / eia / bea / wayback | year+dataset+variables / facet routes / URL extraction | OK — honest gaps when unmappable |

No second instance of the exact D1 shape (NL into an API-ignored or
match-anything parameter) found. The nearest cousins are the *keyword* search
planners (fred/openalex/etc.), but their parameters are backed by genuine
server-side search endpoints — the FDIC distinction (`filters=` vs `search=`,
0 hits vs 11) — so a wrong-topic result there is a ranking/gate problem, not
a planner fabrication problem.

## Task 3 — proof on the task-185 battery (41 questions)

Reconstructed `findings/battery/questions.json`, `results.jsonl`,
runner and scorer from branch history onto `fix/retrieval-starvation`.

Classification of the 22 "every leaf unanswered" refusals (offline, against
the fixed planner):

- **22/22 had their World Bank junk path eliminated** — worldbank now either
  plans a correct WDI fetch (worldbank_01 → SP.POP.TOTL/USA,
  worldbank_04 → disambiguated IND-vs-USA candidates instead of fertilizer
  rows) or declares an honest gap.
- **15/22 become answerable**: an actually-plannable correct source exists
  and can now receive fan-out budget the WB junk used to burn (fred_02,
  fred_03, fred_05, cl_01, cl_02, cl_03, gdelt_01, gdelt_02, openalex_01,
  openalex_02, openalex_03, pubmed_01, changed_02, changed_04, census_01).
- 4/22 are genuinely unknowable questions whose CORRECT outcome is refusal;
  the honest-gap behavior is right for them.
- 3/22 remain blocked upstream of the planner: treasury_02/fdic_02 need
  catalog-mapping additions (treasury interest rates, FDIC institution
  counts); kalshi_01 has NO planner at all ("unknown source 'kalshi'").
  These are separate data-edit tasks, not D1.

The 7 below-floor refusals are downstream of the same starvation (junk
admitted → adversary penalizes → confidence floor) and inherit the fix.
Full re-run of the live battery is the remaining verification step; it needs
~2h40m of provider time and was not executed in this session.

## What would falsify this

If a live re-run still shows byte-identical WB sha256s or worldbank in the
contributing-srcs column of non-WB questions, the fix is incomplete.
