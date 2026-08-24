# RED TEAM — source registry & query builders (+ the fetch→ledger→checkpoint seam)

**Surface:** `tools/sources/registry.py`, `tools/sources/query_builder.py`, and the
provenance seam they feed (`RestSource._record` → relevance gate → ledger → fetch
checkpoint → resume).

**Why this surface:** explicitly unattacked ground — the rotation list names it
("what happens when a source lies, or returns 200 with zero results") and none of the
twelve attacked surfaces covers it (four of twelve are resume/checkpoint variants;
none touches selection or query authoring). Also the surface the MORNING REPORT's own
live-fire test flagged as brittle ("clinical trials" → no sources) before the
diagnostic-floor patch landed; that patch has never been adversarially reviewed.

**Method:** F. CROSS-MODULE — the same rule implemented twice, checked for agreement
(independence membership has already landed three separate times in this codebase,
which is why I expected copies here too) — combined with **corrupt-one-field replay**
of a real checkpoint payload. Neither method had been used on this surface.

**PATTERNS.md families hunted:** #1 (a check that cannot fail), #3 (absence treated
as success), #2 (fix in one copy, not the other). All three hit; see below.

Deliverable: `tests/test_redteam_registry_queries.py` — 5 confirmed-defect xfails +
6 passing pins/controls. Run: `python3 -m pytest tests/test_redteam_registry_queries.py -q`

---

## THE HEADLINE — RQ5 (CRITICAL): the gate's REJECT verdict does not survive a resume

Live run: `RestSource._record` mints fetched bytes PRIMARY *before* the relevance gate
judges them (by design), then `record_gate_rejection()` supersedes them when the gate
says irrelevant. That supersede state lives only in the running process's ledger.

Resume run: the engine replays the same bytes from the checkpoint payload via
`replay_ledger()` — which mints them `primary=True` again — and restores the payload's
`rejections` list onto the TRACE only. **Nothing ever calls `record_gate_rejection()`
on the resumed ledger for restored rejections** (engine.py calls it at :791 for fresh
retrievals only; `_trace_from_payload` :1081 rebuilds the trace's rejected list but no
ledger call follows). Evidence the live run judged irrelevant enters the resumed run as
PRIMARY-class material, `seal_guard` verifies "provenance intact" and returns SEAL.

This is R4/R4b reopened through the resume boundary — the exact laundering shape those
fixes closed, reintroduced by the seam between two fixes that each work alone. Family
#2 exactly: both halves are individually correct; the composition is not.

Repro: `test_rq5_gate_rejection_lost_across_resume_lauanders_bytes` — live-path
control shows INFERRED (correct); resumed path yields PRIMARY + SEAL.

## CONFIRMED BREAKS

### RQ1 (HIGH) — search-metadata masquerades as the data itself
When no curated concept matches, `_plan_fred` falls back to `series_search`, whose
reason string honestly says results "carry series ids for a follow-up observations
fetch". The follow-up never happens: `_fetch_one` executes `plan.queries[0]` and feeds
the search-results page to the gate as if it were evidence. Because series TITLES
contain the question's words ("Unemployment Rate labor market slack"), the gate admits
it, RestSource minted it PRIMARY, and `engine._answer_leaf` assigns SourceClass.PRIMARY
— ceiling 1.0 — to an answer built on zero actual observations. The quant gate fares no
better: `produced_quant` is `bool(re.search(r"\d", ...))`, and series ids/dates are all
digits. A leaf can seal PROBABLE on metadata about where numbers live.
Repro: `test_rq1_fred_series_search_metadata_is_not_observations`.

### RQ2 (HIGH) — HTTP 200 with zero results can be ADMITTED (family #3)
A real empty page `{"results": [], "meta": {"count": 0}, "next": "...cursor=unemployment&filter=rate"}`
scores **67% coverage** against question "unemployment rate" — the URL field values and
schema keys count toward coverage because `extract_text` keeps every string including
dict KEYS' values and URL text. Nothing downstream distinguishes count=0 from count=20.
The FDIC/ClinicalTrials family (11 live defects) is structurally still open wherever an
adapter's schema echoes its topic. Repro: `test_rq2_zero_result_page...`.

### RQ3 (MEDIUM) — World Bank planner answers half a comparison silently
"Compare GDP growth USA CHN" resolves `country='USA'`; CHN is dropped on the floor.
`_wb_resolve_country`'s own docstring says multiple countries must return candidates
for disambiguation — but 'CHN' isn't in the name table and the ISO3 regex is anchored
(`^...$`) so it matches nothing mid-string. The plan is plannable, resolved, confident —
and wrong by half. Family #9 shape: a confident half-answer indistinguishable from
success. Repro: `test_rq3_worldbank_planner_silently_answers_half_a_comparison`.

### RQ4 (HIGH, known-accepted-risk made concrete) — unkeyed tamper self-verifies
In the documented default deployment (`CALLISTO_SEAL_KEY` unset — confirmed unset in
this environment), `content_sha256` travels inside the same editable JSON file as the
body it authenticates. Rewrite the body, recompute the digest: replay_ledger passes,
bytes mint PRIMARY, `admissible_checkpoints` admits, `seal_guard` returns SEAL.
`fix_d3.md` owns this (D1: making keys mandatory); this repro makes the tautology
concrete so the day keys become mandatory, `test_rq4` flips to enforcing rejection.
Keyed control passes (HMAC catches full forgery) — `test_hn2`.

### RQ6 (MEDIUM) — translation widens selection beyond the question
`translate_question_type` builds its select() input from the winning sources' OWN
answer-clause vocabulary UNION the question. Those adopted tokens then match other
sources' clauses: "economic time series unemployment" directly selects {fred}; after
translation the selected set includes bea/bls/treasury/uspto/worldbank/courtlistener/
wayback/kalshi/gdelt — sources whose clauses share few of the question's words.
Selection becomes a function of registry vocabulary, not the question. Not a
confidence inflation per se, but every widened source gets fan-out budget and any
admission adds an independence voice. Repro: `test_rq6_...`.

### RQ7 (LOW/MEDIUM) — diagnostic floor defeats explicit caller strictness
The comment at registry.py:186-191 says a caller asking for min_score=0.99 "still gets
0.99". False for partial matches: `_DIAGNOSTIC_FLOOR`(0.5) REPLACES the score whenever
any matched word has tf ≤ n_sources//3 — with 21 sources, tf≤7, which covers nearly
every topical word. `select("unemployment", min_score=0.99)` returns [bls, fred] at
effective coverage 1.0-of-1-word… i.e., the floor bypasses the include-test the way the
comment itself warns bypassing would. The floor-vs-bypass distinction was fixed; the
floor-vs-caller-threshold interaction was not. Repro: `test_rq7_...`.

## PINNED LATENT DEFECT

### RQ8 — `plan.queries[0]`: retriever runs ONE query, `execute()` documents ALL
Two contracts for one plan object. Today every planner emits exactly one query so it
is latent; pinned so a future multi-query planner cannot silently lose half its
fetches while reporting plannable=True.

## HONEST NEGATIVES / CONTROLS (passing)

- `hn1` digest mismatch without recompute IS caught (C1 fix holds).
- `hn2` keyed regime catches body+digest forgery (D1/D2 HMAC layer works).
- `hn3` quiet-schema empty pages ARE rejected.
- `rq2b` documents the gate's byte-blindness differentially rather than claiming a break.
- `rq8` latent-divergence pin.

Independence-membership cross-check (the rule that landed 3×): retrieval.in_family,
why.py's import of it, base.INDEPENDENCE_FAMILIES and synthesis' indep_key all now
agree — `independence_key("openalex","https://mirror.example.org") == "scholarly-aggregator"`
regardless of spelling or host. No fourth copy found. That part of the surface held.

## FAMILY VERDICT

Every defect above is a recurrence of a documented family:
- RQ5 → family #2 (two fixes, broken composition) AND #1 (a supersede check whose
  enforcement input — the resumed ledger's rejection set — is missing).
- RQ1/RQ2 → family #3 (absence treated as success) and #9 (internally consistent,
  externally wrong: PRIMARY class on metadata).
- RQ3 → family #9.
- RQ6/RQ7 → family #8 inverted (an automated widening/flooring that defeats an
  explicit human-set parameter).
No NEW family proposed; but note the recurring meta-shape: **a verdict computed in
one process must survive into another, and only the DATA survives, not the VERDICT
about the data** (RQ5, RQ4, C1/D1 lineage). Worth adding as family #10: "verdicts are
not durable; state is."

## What I could NOT break

- The relevance gate's prefix hole is real but already pinned (R1/R2 prior pass);
  my independent sweep reproduced it, not re-reported as new.
- `independence_key` normalization survived every mutation I threw at it.
- seal_guard's D3 shared-predicate contract holds between engine replay path and guard
  (checked by reading + hn controls); the laundering in RQ5 happens BEFORE the guard,
  through the ledger-replay seam, not through divergent admissibility.
