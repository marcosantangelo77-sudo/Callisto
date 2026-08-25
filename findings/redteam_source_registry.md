# RED TEAM — source registry, query builders, and the fetch→provenance seam

Date: 2026-08-25 · Instance: rotating red team (ox-alpha) · Worktree `money/`,
branch `redteam/source-registry-0825`. READ-ONLY on production code — the
deliverable is failing repro tests: `tests/test_redteam_source_registry.py`
(committed first at e5c3688 so failures are verifiable). No live API was
touched; no execution path armed; no confidence raised.

## Surface, method, and why

**Surface: the source registry + query builders + fetch seam.** Explicitly
named unattacked ground ("what happens when a source lies, or returns 200
with zero results"). Of the twelve-plus prior passes, none attacked it:
calibration, resume×4, concurrency, loop, pipeline_wiring, provenance,
retrieval-relevance, seal, synthesis, money-path, CLI persistence,
artifacts, domain-plugins, mutation are all covered.

**Method: property-style sweep** (PATTERNS.md's #1 ranked method, not yet
used on this surface): every registered source swept against its OWN answer
clauses (self-selection must be 1.0) and against every other source's
clauses (over-breadth), plus a planner×signature cross-check over all 19
sources × 13 question shapes, plus adversarial-input probes at each seam of
the fetch→gate→ledger path.

**Families hunted:** #1 inert verification, #2 same-rule-two-copies,
#3 absence-as-success, #7 tests passing for wrong reason, #9 internally
consistent / externally wrong.

---

## Sweep results that did NOT break (honest negatives)

- **Self-selection: 0 misses across all 19 sources × all answer clauses.**
  The MORNING_REPORT selection defects are genuinely fixed for the
  registry's own vocabulary.
- **Planner kwarg/signature cross-check:** zero mismatches — every planned
  call binds against the real adapter signature.
- **Independence collapse works end-to-end** through `independence_key`
  (normalised): openalex+semanticscholar correctly count as ONE voice.
- **`numeric_window_matches` is stricter than I expected**: bodies whose
  date years merely INTERSECT the asked years are rejected; only full
  containment passes. Good direction.
- **The gate refuses empty inputs** (family-3 probe failed to break it).
- **Diagnostic floor cannot bypass caller min_score** (the comment in
  registry.py is true — verified with min_score=0.99).
- **Gate-rejection survives refetch of the identical body** via exact-hash
  supersession… but see S4 below for the hole one representation away.

---

## CONFIRMED BREAKS (all with failing tests)

### S1 — A non-200 error page is minted PRIMARY in the ledger (HIGH)
`RestSource._record` runs before any status check (`base.py:226`, called at
line 299 with whatever status came back), and `_record` always writes
`primary=True`. Verified: a 503 HTML gateway page lands in
ProvenanceLedger as primary bytes for that URL. The get_json() call then
raises, but the ledger write is NOT rolled back. Consequence: any later
text citing this URL can verify as SECONDARY off an error page — the exact
shape of C1 ("absent digest skipped verification") one layer up.
Family 3 (absence treated as success) × Family 9.

### S2 — Injected POST transport silently drops the payload (MEDIUM)
`post()` routes through `self._transport(url, headers)` when a transport is
injected (`base.py:270`) — the JSON payload never reaches the test double,
and nothing checks that it did. Every fixture-tested POST adapter (BLS)
has exercised POST semantics that do not exist in the real path. Family 7:
tests that pass for the wrong reason, structural edition.

### S3 — Ledger failure = silent zero-provenance fetch (HIGH)
The `except Exception: logger.exception(...)` around `record_tool_result`
(`base.py:315`) means a broken/full/rotated ledger produces fetched bytes
that NO seal or citation can ever verify, while the pipeline keeps running
green. Provenance must be fail-closed here or the failure must surface at
ERROR level with an explicit degraded-provenance record; today it is a
swallowed log line at best. Family 1: a verification layer that never
actually runs.

### S4 — Gate-rejection binding is exact-hash: re-serialisation escapes it (HIGH)
R4/R4b bind the REJECT verdict to bytes by exact sha256 and to the URL.
But the retriever carries forward `json.dumps(parsed, sort_keys=True)` — a
different string from the raw body whenever key order differs — and any
reordered-key echo re-mints PRIMARY from rejected bytes. Verified directly
against ProvenanceLedger: `record_gate_rejection(raw)` does not supersede
`json.dumps(json.loads(raw), sort_keys=True)`. The binding should be over
a canonical form (sorted-keys JSON of the parsed value), not the wire
bytes. Family 2 (two representations of one rule disagreeing).

### S6 — Wikidata planner crash kills the ENTIRE leaf's retrieval (CRITICAL)
`_plan_wikidata_concept` sorts `matched.sort(key=lambda p: -p[1])` where
`p[1]` is a hint STRING ("company", "country", "person"), not a score.
Any question containing a hint word raises TypeError. Worse: the round-1
plannability pre-filter in `IterativeRetriever.retrieve` calls
`build_plan` UNGUARDED (retrieval.py:589), so the exception propagates out
of `retrieve()` itself. Verified end-to-end with the real registry:
`retrieve()` crashes for "Which company has the most patents in battery
technology?" — one planner bug takes down all 19 sources for that leaf.
(The per-source try/except in `_fetch_one` never gets a chance to contain
it because the pre-filter runs outside it.) Families 3×7: a crash that
looks like a hard failure but which no test reached because fixtures use
planner vocabulary, never ordinary nouns like "company".

### S7 — Health-probe names drift from registry names; cftc probe can never run (MEDIUM)
health.py registers probes under module filenames ('cftc', 'sec_fts',
'semantic_scholar'); the registry uses spec names ('cftc_cot',
'sec_fulltext', 'semanticscholar'). `_build`'s underscore-normalising alias
loop rescues semantic_scholar→semanticscholar but NOT cftc→cftc_cot
('cftccot' ≠ 'cftc'), so the cftc health probe reports BROKEN regardless
of the real API — the health layer lies in both directions. Family 2, the
third naming-drift incident in exactly this family of modules (I2 planner
keying was the second).

### S8 — Error-envelope classification covers exactly ONE of 21 sources (HIGH)
D2 landed `classify_fetch_failure` for BLS only. Every other source's
200-with-error-body flows into the relevance gate as text. Adversarial
construction verified end-to-end: a fake openalex returning
{"error": "...your query 'unemployment rate among teenagers in Spain' was
malformed..."} is ADMITTED by the gate at 83% coverage (an API echoing the
query words scores high by construction), minted PRIMARY three rounds
running, and adds 'scholarly-aggregator' to independent_keys. That is:
one lying endpoint MANUFACTURES an independent voice. This is the
"source lies / 200-with-error-body" defect named in the brief, confirmed
in the general case. Family 9 at full strength: internally consistent
(provenance intact, gate satisfied, independence counted), externally
wrong.

Related asymmetry worth noting (no test pinned): `classify_fetch_failure`
is also only consulted in `_fetch_one`; the engine's parallel-leaf path
and anything resuming from checkpoints re-ingest stored bodies without it.

---

## Pattern note for PATTERNS.md

No NEW family — every finding above maps onto families 1/2/3/7/9. But two
RECURRING SUB-SHAPES are now confirmed often enough to name:

1. **"Naming drift" (sub-shape of family 2).** Third instance in this
   module cluster alone: registry vs planner keys (I2), why.py's
   unnormalised membership, now probes vs specs. Wherever two modules
   refer to sources by string, normalise once, in one place, and derive
   everything else.
2. **"The guard is narrower than the class."** D2 guarded BLS; R4/R4b
   bound exact bytes; W5 signed only when keyed. Each fix is correct for
   the ONE instance seen. Hunt rule: after any fix, ask "what OTHER input
   sits outside this guard but inside the failure class?" — then go find
   it. It took under an hour to find four.

## What I would fix first

S8 + S4 together close the "one lying endpoint manufactures independent
primary corroboration" chain, which is the highest-severity composite.
S6 is the cheapest fix (one lambda) and the biggest blast radius.

---

# SECOND PASS — same surface, later rotation (2026-08-25)

Method rotated per the standing brief: corrupt-one-field replay against the
LIVE retriever (planner mode, real registry, fake transport), plus empty-input
probes at every gate — instead of another selection sweep. New tests S9–S12
in `tests/test_redteam_source_registry.py`; all four FAIL on current code.

## S9 — Three registered sources unknown to the query builder (MEDIUM)
`kalshi`, `cmefedfut`, `sec_fulltext` are registered AND selected for real
questions ("prediction market contracts" → kalshi+cmefedfut top-3; "futures
market prices"), but `_KEYWORD_PLANNERS` and `_HONEST_GAPS` hold none of them:
`build_plan` answers "unknown source". The docstring's claim "every registered
source now has a planner" is false in both directions — not planned, and not
honestly gapped. Each is fanned out to, skipped as unknown, then EXCLUDED from
later rounds: a silent dead end wearing an honest gap's clothes. Family 2
(third naming/keying drift in this cluster) × family 3.

## S10 — Byte-identical bodies mint N independent voices (HIGH)
`independence_key` collapses openalex+semanticscholar by NAME, but nothing
ever compares CONTENT. Four hosts returning one identical payload → 4
admitted fetches → 3 distinct independent keys → "sufficient: 3 >= 2".
A mirror host, a proxy, or one liar duplicated under a second domain
manufactures corroboration. Verified end-to-end through planner mode.
Family 5: structural property (distinct keys) standing in for agreement
(distinct evidence). Fix direction: collapse independent_keys whose admitted
bodies share a content_sha256.

## S11 — Zero-hit envelopes count toward sufficiency (CRITICAL)
A source returning 200 with `{"results": [], "meta": {"query": "<the query>"}}`
— an honest NULL — scores ~75% token coverage at the gate (the echo IS the
topic) and is ADMITTED. In production-default planner mode with all-zero-hit
bodies: 4 admitted, 3 independent keys, stop_reason "sufficient: 3 independent
sources >= required 2". The pipeline then answers a question NO SOURCE
ANSWERED and seals it. Family 9 at maximum strength: every internal signal
green, externally empty. Related observation: legacy mode re-admitted the
same zero-hit bodies SIX times over three rounds — exclusion only fires on
skip/fail, never on admit-with-zero-hits.

## S12 — D4 structural route admits off-topic data (HIGH; corrects pass 1)
Pass 1 listed `numeric_window_matches` under honest negatives ("stricter than
I expected"). At the GATE level it is LOOSER than its own docstring:
`extract_text` keeps strings only, so `"value": "6.48"` arrives as text and
ANY series whose ISO dates fall inside the question's years passes — topic
never checked. A 30-year-mortgage table answers an unemployment question at
min_coverage=1.0 via the structural route. Dates-in-window is a property of
time-series data in general, not of the asked-about quantity. The gate needs
at least one topical token match (series label/title) alongside the window,
or the route admits whatever the calendar permits.

## Honest negatives (pass 2)
- FDIC filter guard holds: `NAME:*`, quotes, OR-injection all rejected at
  planning (`_VALUE_OK`); CFTC passthrough requires the strict code shape.
- FRED id passthrough behaves: bare ALL-CAPS words do NOT resolve (the
  uppercase+digit/known-id rule works); 'GDP' resolving to nominal-GDP
  series for "What is GDP" is defensible ambiguity handling, not a defect.
- Gate refuses truly empty inputs ({} / {"results": []} without echoes).

## Composite severity note
S8 + S11 chain: any endpoint that echoes query words inside an error or
zero-result envelope is ADMITTED, MINTED PRIMARY, and COUNTS INDEPENDENT.
One lying endpoint can satisfy min_independent_sources alone. This is the
highest-value fix on this surface: require non-empty result sets AND
canonical-hash-deduped independence AND generalised envelope classification.
