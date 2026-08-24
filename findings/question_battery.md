# QUESTION BATTERY — 41 known-answer questions across every working source (2026-08-24)

Branch `build/question-battery`. Runner: `scripts/run_question_battery.py`,
bank: `findings/battery/questions.json`, raw results: `findings/battery/results.jsonl`
(one JSON line per question, incremental — safe against provider outages).
Scoring: `scripts/score_question_battery.py`.

Every ground truth was obtained by a DIRECT API call made by this agent
(FRED fredgraph.csv, treasurydirect.gov/NP_WS/debt/current, FiscalData,
FDIC BankFind, CourtListener, Kalshi trade-api, OpenAlex, Wayback availability,
Wikidata SPARQL, World Bank API, PubMed eutils) — never through the pipeline.
Timestamp of GT fetch: 2026-08-24.

## HEADLINE NUMBERS

| metric | value |
|---|---|
| questions run | 41 |
| **accuracy on answerable questions (sealed AND correct)** | **0 / 33 = 0%** |
| sealed at all (any tier) | 5 |
| wrong-answers-sealed | 2 (unknowable_04, unknowable_05 — sealed SPECULATIVE on unanswerable questions) |
| wrong-direction-sealed | 1 (wayback_01 — sealed "cannot determine" as an ANSWER to a yes/no question whose answer is YES; the seal asserts ignorance as a finding) |
| correct refusals on unknowable questions | 6 / 8 |
| confident-answers-on-unknowable (FAILURE metric) | 2 |
| refusals on answerable questions | 30 / 33 (91%) |

The system did not produce a single verifiably CORRECT sealed answer in 41 tries.
It also did not produce a single confidently-WRONG factual direction like the
original unemployment AFFIRMS: the failure mode in this build has flipped from
"wrong but sealed" to "refuse almost everything, and when it does seal, seal a
content-free non-answer."

## PER-QUESTION TABLE

verdict S=sealed R=refused. conf = sealed confidence. srcs = admitted sources.
GT provenance per question is in questions.json (`gt_source` field).

| id | source | shape | verdict | conf | contributing srcs | n_fetch | elapsed_s | outcome vs GT |
|---|---|---|---|---|---|---|---|---|
| fred_01 | fred | comparison-NO | R | – | worldbank | 3 | 287 | refused on answerable |
| fred_02 | fred | simple-fact | R | – | none | 0 | 156 | refused on answerable (YES, all 2023 <4.0%) |
| fred_03 | fred | comparison-reverse | R | – | none | 0 | 75 | refused on answerable (answer YES) |
| fred_04 | fred | stale-evidence | R | – | worldbank | 2 | 243 | refused on answerable (14.8% Apr 2020) |
| fred_05 | fred | multi-hop | R | – | none | 0 | 77 | refused on answerable (Dec 2021 first <4.0) |
| treasury_01 | treasury | recent-changed | R | – | none | 0 | 336 | refused on answerable ($40.03T > $39T) |
| treasury_02 | treasury | value-fact | R | – | worldbank | 2 | 354 | refused on answerable (T-bills 0.347%) |
| treasury_03 | treasury | comparison | R | – | worldbank | 1 | 151 | refused on answerable (notes 3.043% < 4%) |
| fdic_01 | fdic | count-NO | R | – | worldbank | 4 | 432 | refused on answerable (4,245 active, NOT >5k) |
| fdic_02 | fdic | trend-comparison | R | – | none | 0 | 69 | refused on answerable |
| cl_01 | courtlistener | date-fact | R | – | worldbank | 1 | 271 | refused on answerable (Jun 26, 2015) |
| cl_02 | courtlistener | multi-hop | R | – | none | 0 | 79 | refused on answerable |
| gdelt_01 | gdelt | coverage | R | – | none | 0 | 70 | refused on answerable |
| gdelt_02 | gdelt | unknowable-negligible | R | – | worldbank | 1 | 278 | refusal defensible (phrase nonexistent) |
| kalshi_01 | kalshi | market-state | R | – | none | 0 | 67 | refused on answerable (open markets live now) |
| kalshi_02 | kalshi | series-existence | R | – | worldbank | 3 | 429 | refused on answerable (1,627 series) |
| openalex_01 | openalex | count-comparison | R | – | worldbank | 3 | 411 | refused on answerable (115,513 works) |
| openalex_02 | openalex | multi-hop | R | – | none | 0 | 66 | refused on answerable (1882 < Nov 1915) |
| wayback_01 | wayback | snapshot | S | 0.34 | worldbank | 3 | 313 | SEALED WRONG-FRAME: conclusion is "cannot determine"; truth is YES (snapshot 20260824) |
| wikidata_01 | wikidata | entity-comparison | R | – | worldbank | 1 | 327 | refused on answerable (1.40B vs 340M) |
| worldbank_01 | worldbank | value-fact | R | – | worldbank | 1 | 183 | refused on answerable (332.1M in 2021) |
| worldbank_02 | worldbank | trend-monotonic | S | 0.34 | worldbank | 2 | 236 | SEALED with zero evidence-bearing leaves; fetched Africa E&S region rows, not US; GT is YES but no leaf supports it |
| worldbank_03 | worldbank | data-vintage | R | – | worldbank | 1 | 302 | refused; GT says YES ($30.77T 2025 exists) — refusal is wrong but honest |
| fed_01 | federalreserve/FRED | policy-path | R | – | worldbank | 3 | 390 | refused on answerable (FEDFUNDS 2025 min 3.72%) |
| pubmed_01 | pubmed | literature-count | R | – | worldbank | 2 | 272 | refused on answerable (80,460 articles) |
| unknowable_01 | none | unknowable | R | – | none | 0 | 64 | CORRECT REFUSAL |
| unknowable_02 | none | unknowable | R | – | worldbank | 5 | 430 | CORRECT REFUSAL (future event) |
| unknowable_03 | none | unknowable | R | – | none | 0 | 57 | CORRECT REFUSAL |
| unknowable_04 | none | unknowable-private | S | 0.34 | worldbank | 4 | 314 | **SEALED on UNKNOWABLE — FAILURE** (conclusion restates "no evidence", still minted a seal) |
| unknowable_05 | none | unknowable-count | S | 0.34 | worldbank | 4 | 407 | **SEALED on UNKNOWABLE — FAILURE** |
| changed_01 | fred | recent-changed-trap | R | – | worldbank | 3 | 509 | refused on answerable (truth NO: rate rose) |
| changed_02 | treasury | threshold | R | – | none | 0 | 64 | refused on answerable (debt $40T ≥ $38T → NO) |
| changed_03 | kalshi | live-state | S | 0.34 | worldbank | 3 | 323 | SEALED without any supporting fetch; GT YES — seal carries no evidentiary basis |
| changed_04 | wayback | recent-capture | R | – | none | 0 | 88 | refused on answerable (snapshot today) |
| changed_05 | openalex | growing-corpus | R | – | wikidata+worldbank | 4 | 284 | refused on answerable (>250M works) |
| census_01 | census | estimate-fact | R | – | none | 0 | 262 | refused on answerable (~334.9M ACS 2023) |
| cl_03 | courtlistener | comparison-reverse | R | – | none | 0 | 64 | refused on answerable (Dobbs AFTER Obergefell) |
| fred_06 | fred | unknowable-future | R | – | fdic+worldbank | 4 | 366 | CORRECT REFUSAL |
| worldbank_04 | worldbank | cross-country | R | – | none | 0 | 84 | refused on answerable |
| openalex_03 | openalex | author-attribution | R | – | none | 0 | 69 | refused on answerable |
| treasury_04 | treasury | unknowable-intraday | R | – | none | 0 | 261 | CORRECT REFUSAL |

Totals: 60 admitted fetches + ~50 rejected-at-ingestion fetches; 2h38m wall clock.
Sources that ever contributed admitted content: worldbank (23 runs), wikidata (1),
fdic (1). No FRED, BLS, Treasury, Census, GDELT, Kalshi, OpenAlex, CourtListener,
PubMed or Wayback content was EVER admitted into evidence in 41 runs.

## ROOT-CAUSE GROUPING (five apparent defects collapse into THREE)

### D1 — Retrieval returns only World Bank indicator-search junk (dominant; ~all 41)
The planner stuffs natural-language sub-questions into the World Bank
`search` parameter (`...&search=official+unemployment+rate+January+2023+reported+BLS`),
which the WB API ignores, returning the same default indicator list every time —
byte-identical sha256 `bd377215…` across differently-worded queries. Meanwhile
the RIGHT adapters (fred, bls, fdic, clinicaltrials…) DO get called and their
responses are REJECTED AT INGESTION by the topical-word gate ("content covers
8% … need 25%"). So the one source whose output survives ingestion is the one
that cannot answer anything. This is PATTERNS family #9 (internally consistent,
externally wrong) riding on #3 (absence treated as success would be next):
the adversary SEE the junk and objects loudly every run, yet nothing upstream
of sealing changes course. The adversary's own objections diagnose it:
"three differently-worded queries were routed to the same irrelevant endpoint…
the retrieval process was effectively [a no-op]".

Evidence: identical-sha256 fetches in every run record; rejection notes listing
fred/bls/fdic/clinicaltrials rejections; worldbank URL pattern above.

### D2 — Sealing a NON-answer (wayback_01, worldbank_02, changed_03, unknowable_04/05)
When every leaf comes back "[GAP: unprovable]", the parent can still SEAL at
0.34/SPECULATIVE with a conclusion that literally says "cannot be determined
from this evidence". Two distinct defects:
 - For genuinely unknowable questions (unknowable_04/05), sealing a
   "we found nothing" conclusion is scored as a SUCCESS-shaped artifact — it
   looks like an answer while asserting nothing. PATTERNS #9 exactly: it looks
   exactly like success.
 - For answerable questions (worldbank_02: US population monotonic — GT YES;
   changed_03: Kalshi open markets — GT YES), the seal contains no directional
   claim at all, so accuracy cannot even be evaluated; the system spent 4–7
   minutes and produced a shrug stamped SPECULATIVE.
Root cause is stance propagation taking its stance from leaves that bear no
relation to the parent claim (PATTERNS #9's original root cause, unfixed in
this branch — dd4fb18's DECISIONAL-leaf fix is NOT present here).

### D3 — Adversary fail-closed path converts model-output flakiness into refusals (7 refusals)
"adversary veto: adversary backend failed (JSONDecodeError…)" — the hermes_cli
backend returns prose-wrapped JSON sometimes; `_parse_json_response` fails; the
adversary fails closed (correct!) and the run refuses. Separately, `--backend
ox_alpha_proxy` CANNOT run the adversary at all: candidates_for() filters out
openai_compat endpoints with structured_output=false when a schema is present,
so schema-bearing adversarial_review calls have zero candidates ("All endpoints
failed … no candidates") — reproducible in isolation. The config declares
best-effort JSON for hermes_cli specifically so it PASSES that filter, but
ox_alpha_proxy (same model over HTTP) doesn't get the same treatment. PATTERNS
#2 (fix landed for one copy of the same idea — hermes_cli backend — not the
other) and #1 (a capability check that filters rather than degrades).

### Refusal taxonomy (36 refusals)
- 22 × "every leaf came back unanswered" → downstream of D1
- 7 × "confidence 0.0 below DB floor 0.3 after adversary penalties" → D1+D2
- 7 × "adversary veto: backend failed (JSONDecodeError)" → D3

## WHAT WOULD MOVE THE NUMBERS
1. Fix the query_builder's worldbank fallback so free-text questions do NOT
   route to `search_indicators` when no curated indicator resolves (D1).
2. Loosen the ingestion topical gate for exact-match primary sources (a FRED
   observations CSV answering "unemployment rate January 2023" covers few
   question words BY DESIGN — it's a table of numbers) (D1).
3. A parent whose leaves are all GAP:unprovable must REFUSE, never seal (D2).
4. Give ox_alpha_proxy the same best-effort-JSON treatment as hermes_cli in
   candidates_for(), or catch parse failure and retry once (D3).

## HONESTY NOTES
- gt_source for census_01 required an API key we don't hold; value taken from
  published ACS tables and flagged as such in the bank.
- gdelt_01/02 ground truths were blocked by GDELT's rate limiter during GT
  collection; marked VERIFY-at-runtime in the bank. Both pipeline outcomes
  were refusals either way, so classification is unaffected.
- The battery ran against ONE provider tier (ox_alpha via hermes CLI). gpu1
  was down all session; frontier unset. Results describe this configuration.
